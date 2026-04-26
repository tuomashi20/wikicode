from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.wiki_compiler import WikiCompiler
from src.utils.config import AppConfig, PROJECT_ROOT
from src.utils.db_manager import delete_chunks_by_parent, init_db, upsert_chunk
from src.utils.logger import get_file_logger


@dataclass
class Chunk:
    chunk_id: str
    title: str
    content: str
    parent_file: str
    raw_file_path: str


class Atomizer:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.logger = get_file_logger("sync", "sync.log")
        self.processed_root = config.wiki_strategy.processed_path
        self.chunks_dir = self.processed_root / "chunks"
        self.state_path = self.processed_root / "sync_state.json"
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        init_db()

    def sync(self) -> dict[str, int]:
        raw_root = self.config.wiki_strategy.raw_path
        md_files = sorted(raw_root.rglob("*.md")) if raw_root.exists() else []

        prev_state = self._load_state()
        prev_files: dict[str, dict[str, Any]] = prev_state.get("files", {})

        processed_files = 0
        skipped_files = 0
        deleted_files = 0
        total_chunks = 0

        new_files_state: dict[str, dict[str, Any]] = {}
        current_rel_paths: set[str] = set()

        for md_file in md_files:
            rel_raw = str(md_file.relative_to(raw_root)).replace("\\", "/")
            current_rel_paths.add(rel_raw)
            text = md_file.read_text(encoding="utf-8", errors="ignore")
            file_hash = hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()

            prev_meta = prev_files.get(rel_raw, {})
            if str(prev_meta.get("hash", "")) == file_hash:
                skipped_files += 1
                new_files_state[rel_raw] = prev_meta
                continue

            old_chunk_ids = [str(x) for x in (prev_meta.get("chunk_ids") or [])]
            self._remove_chunk_files(old_chunk_ids)

            chunks = self._process_file(md_file, text=text)
            processed_files += 1
            total_chunks += len(chunks)
            new_files_state[rel_raw] = {
                "hash": file_hash,
                "chunk_ids": [c.chunk_id for c in chunks],
                "updated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
            }

        removed = set(prev_files.keys()) - current_rel_paths
        for rel_raw in sorted(removed):
            prev_meta = prev_files.get(rel_raw, {})
            self._remove_chunk_files([str(x) for x in (prev_meta.get("chunk_ids") or [])])
            delete_chunks_by_parent(rel_raw)
            deleted_files += 1
            self.logger.info("sync_delete file=%s", rel_raw)

        self._save_state({"version": 1, "files": new_files_state})

        compiled = {"pages": 0, "files": 0, "tags": 0}
        if self.config.wiki_strategy.wiki_compile_on_sync:
            compiled = WikiCompiler(self.config).compile()
            self.logger.info(
                "wiki_compile_done pages=%s files=%s tags=%s",
                compiled["pages"],
                compiled["files"],
                compiled["tags"],
            )

        self.logger.info(
            "sync_done changed=%s skipped=%s deleted=%s chunks=%s",
            processed_files,
            skipped_files,
            deleted_files,
            total_chunks,
        )
        return {
            "files": processed_files,
            "skipped": skipped_files,
            "deleted": deleted_files,
            "chunks": total_chunks,
            "wiki_pages": compiled["pages"],
        }

    def _process_file(self, md_file: Path, *, text: str | None = None) -> list[Chunk]:
        if text is None:
            text = md_file.read_text(encoding="utf-8", errors="ignore")
        rel_raw = str(md_file.relative_to(self.config.wiki_strategy.raw_path)).replace("\\", "/")

        delete_chunks_by_parent(rel_raw)

        chunks = self._split_by_heading(text, rel_raw, level=self.config.wiki_strategy.heading_level)
        for c in chunks:
            out_path = self.chunks_dir / f"{c.chunk_id}.md"
            out_path.write_text(c.content, encoding="utf-8")
            tags = self._extract_tags(c.title, c.content)
            try:
                content_path = str(out_path.relative_to(PROJECT_ROOT)).replace("\\", "/")
            except ValueError:
                content_path = str(out_path.resolve())
            upsert_chunk(
                chunk_id=c.chunk_id,
                title=c.title,
                parent_file=c.parent_file,
                raw_file_path=c.raw_file_path,
                tags=tags,
                content_path=content_path,
                content_text=c.content,
                last_modified=datetime.utcnow().isoformat(timespec="seconds") + "Z",
            )

        self.logger.info("sync_file file=%s chunks=%s", rel_raw, len(chunks))
        return chunks

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"version": 1, "files": {}}
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                return {"version": 1, "files": {}}
            files = data.get("files")
            if not isinstance(files, dict):
                data["files"] = {}
            return data
        except Exception:
            return {"version": 1, "files": {}}

    def _save_state(self, data: dict[str, Any]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def _remove_chunk_files(self, chunk_ids: list[str]) -> None:
        for cid in chunk_ids:
            p = self.chunks_dir / f"{cid}.md"
            if p.exists():
                try:
                    p.unlink()
                except Exception:
                    pass

    @staticmethod
    def _split_by_heading(text: str, rel_raw_path: str, level: int = 2) -> list[Chunk]:
        """
        [进化版] 层级感知切片：
        1. 识别所有层级的标题 (H1-H6)。
        2. 为每个切片构建面包屑路径 (如: 首页 > 运维 > 数据库)。
        3. 确保子章节能够继承父章节的语义背景。
        """
        lines = text.splitlines()
        chunks: list[Chunk] = []
        
        current_headers = [None] * 7 # 存储 H1-H6 的当前标题内容
        current_body: list[str] = []
        chunk_idx = 1

        def _create_chunk(headers, body, idx):
            # 过滤掉 None，构造面包屑路径
            breadcrumb = " > ".join([h for h in headers if h])
            # 注入面包屑到内容头部，增强检索上下文
            full_content = f"【上下文路径: {breadcrumb}】\n\n" + "\n".join(body).strip()
            # 取当前最细颗粒度的标题作为 Chunk 标题
            title = next((h for h in reversed(headers) if h), Path(rel_raw_path).stem)
            cid = Atomizer._build_chunk_id(rel_raw_path, idx, title)
            return Chunk(
                chunk_id=cid,
                title=title,
                content=full_content,
                parent_file=rel_raw_path,
                raw_file_path=rel_raw_path
            )

        for line in lines:
            header_match = re.match(r"^(#+)\s+(.+)$", line)
            if header_match:
                # 发现新标题，如果之前有内容，先封装成块
                if current_body and any(current_headers):
                    chunks.append(_create_chunk(current_headers, current_body, chunk_idx))
                    chunk_idx += 1
                    current_body = []
                
                h_level = len(header_match.group(1))
                h_title = header_match.group(2).strip()
                if h_level <= 6:
                    current_headers[h_level] = h_title
                    # 清空更低等级（更细分）的旧标题
                    for i in range(h_level + 1, 7):
                        current_headers[i] = None
            else:
                current_body.append(line)
        
        # 处理文件结尾的最后一段内容
        if current_body:
            chunks.append(_create_chunk(current_headers, current_body, chunk_idx))
            
        # 极端兜底：如果全文没有标题，退化为单块模式
        if not chunks:
            default_title = Path(rel_raw_path).stem
            chunks.append(Chunk(
                chunk_id=Atomizer._build_chunk_id(rel_raw_path, 1, default_title),
                title=default_title,
                content=text.strip(),
                parent_file=rel_raw_path,
                raw_file_path=rel_raw_path
            ))
            
        return chunks

    def _extract_tags(self, title: str, content: str) -> str:
        ws = self.config.wiki_strategy
        stopwords = set(str(x).strip().lower() for x in ws.tag_stopwords if str(x).strip())
        block_patterns = [str(x) for x in ws.tag_block_patterns if str(x).strip()]
        block_prefixes = [str(x).strip().lower() for x in ws.tag_block_prefixes if str(x).strip()]

        def is_noise_term(term: str) -> bool:
            t = term.strip()
            if not t:
                return True
            tl = t.lower()
            for p in block_prefixes:
                if tl.startswith(p):
                    return True
            for pat in block_patterns:
                try:
                    if re.fullmatch(pat, t, flags=re.IGNORECASE):
                        return True
                except re.error:
                    continue
            if tl in stopwords:
                return True
            return False

        source = f"{title}\n{content[:1200]}"
        parts = re.split(r"[\s,.;:!?()\[\]{}<>\"'`/_\\\-]+", source)

        candidates: list[str] = []
        for p in parts:
            t = p.strip()
            if not t:
                continue

            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,24}", t):
                tl = t.lower()
                if not is_noise_term(tl):
                    candidates.append(tl)
                continue

            if re.fullmatch(r"[\u4e00-\u9fff]{2,12}", t):
                if not t.endswith(("的", "了")) and not is_noise_term(t):
                    candidates.append(t)

        title_terms = [t for t in candidates if t in title]
        others = [t for t in candidates if t not in title_terms]

        seen: set[str] = set()
        out: list[str] = []
        for t in title_terms + others:
            if t in seen:
                continue
            seen.add(t)
            out.append(t)
            if len(out) >= 8:
                break

        return ",".join(out)


    @staticmethod
    def _build_chunk_id(rel_path: str, index: int, title: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", title.lower()).strip("_") or "section"
        path_slug = re.sub(r"[^a-zA-Z0-9]+", "_", rel_path.lower()).strip("_")
        digest = hashlib.md5(f"{rel_path}:{index}:{title}".encode("utf-8")).hexdigest()[:8]
        return f"wc_{path_slug}_{slug}_{index:02d}_{digest}"
