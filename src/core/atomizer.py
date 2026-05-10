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
    breadcrumb: str


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
            # 确保在 Windows 下路径字符串的 UTF-8 一致性
            rel_raw = str(md_file.relative_to(raw_root)).replace("\\", "/")
            current_rel_paths.add(rel_raw)
            
            # [核心修复]：自适应编码读取，防止内容静默丢失
            try:
                text = md_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = md_file.read_text(encoding="gbk", errors="replace")
                
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
            
            # --- [V2.0] gbrain 同步删除 ---
            try:
                from src.core.mcp_client import GBrainMCPClient
                gb_client = GBrainMCPClient()
                gb_slug = f"wiki/raw/{rel_raw.replace('.md', '')}"
                gb_client.call_tool("delete_page", {"slug": gb_slug})
            except: pass
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
            try:
                text = md_file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = md_file.read_text(encoding="gbk", errors="replace")
        rel_raw = str(md_file.relative_to(self.config.wiki_strategy.raw_path)).replace("\\", "/")

        delete_chunks_by_parent(rel_raw)

        # --- [V2.0] gbrain 镜像同步 ---
        try:
            from src.core.mcp_client import GBrainMCPClient
            gb_client = GBrainMCPClient()
            # 简化 slug: 运维/规范.md -> wiki/raw/运维/规范
            gb_slug = f"wiki/raw/{rel_raw.replace('.md', '')}"
            gb_client.call_tool("put_page", {"slug": gb_slug, "content": text})
            self.logger.info("gbrain_mirror_sync success slug=%s", gb_slug)
        except Exception as e:
            self.logger.warn("gbrain_mirror_sync failed err=%s", str(e))

        ws = self.config.wiki_strategy
        patterns = getattr(ws, "chapter_title_patterns", [])

        chunks = self._split_by_heading(text, rel_raw, level=ws.heading_level, patterns=patterns)
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
                breadcrumb=c.breadcrumb,
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
    def _split_by_heading(text: str, rel_raw_path: str, level: int = 2, patterns: list[str] = []) -> list[Chunk]:
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

        def _create_chunks(headers, body_lines, base_idx):
            """
            [V2.1 增强版] 支持长文本二次分段并保留重叠
            """
            full_text = "\n".join(body_lines).strip()
            max_len = 1800 # 单个片段最大字数
            overlap = 300  # 重叠字数
            
            breadcrumb = " > ".join([h for h in headers if h])
            title_base = next((h for h in reversed(headers) if h), Path(rel_raw_path).stem)
            
            if len(full_text) <= max_len:
                # 内容短，直接生成单块
                cid = Atomizer._build_chunk_id(rel_raw_path, base_idx, title_base)
                content = f"【上下文路径: {breadcrumb}】\n\n" + full_text
                return [Chunk(cid, title_base, content, rel_raw_path, rel_raw_path, breadcrumb)], base_idx + 1
            
            # 内容长，递归切分
            sub_chunks = []
            start = 0
            sub_idx = 0
            while start < len(full_text):
                end = start + max_len
                chunk_body = full_text[start:end]
                # 构建带有子索引的 ID
                cid = Atomizer._build_chunk_id(rel_raw_path, base_idx, f"{title_base}_part{sub_idx}")
                content = f"【上下文路径: {breadcrumb} (第{sub_idx+1}部分)】\n\n" + chunk_body
                sub_chunks.append(Chunk(cid, title_base, content, rel_raw_path, rel_raw_path, breadcrumb))
                
                start += (max_len - overlap)
                sub_idx += 1
                base_idx += 1
            return sub_chunks, base_idx

        for line in lines:
            line_s = line.strip()
            header_match = re.match(r"^(#+)\s+(.+)$", line)
            is_custom_header = False
            custom_title = ""
            for pat in patterns:
                if re.match(f"^{pat}", line_s):
                    is_custom_header = True
                    custom_title = line_s
                    break

            if header_match or is_custom_header:
                if current_body:
                    new_chunks, chunk_idx = _create_chunks(current_headers, current_body, chunk_idx)
                    chunks.extend(new_chunks)
                    current_body = []
                
                if header_match:
                    h_level = len(header_match.group(1))
                    h_title = header_match.group(2).strip()
                else:
                    h_level = 2
                    h_title = custom_title

                if h_level <= 6:
                    current_headers[h_level] = h_title
                    for i in range(h_level + 1, 7):
                        current_headers[i] = None
            else:
                current_body.append(line)
        
        if current_body:
            new_chunks, chunk_idx = _create_chunks(current_headers, current_body, chunk_idx)
            chunks.extend(new_chunks)
            
        # 极端兜底：如果全文没有标题，退化为单块模式
        if not chunks:
            default_title = Path(rel_raw_path).stem
            chunks.append(Chunk(
                chunk_id=Atomizer._build_chunk_id(rel_raw_path, 1, default_title),
                title=default_title,
                content=text.strip(),
                parent_file=rel_raw_path,
                raw_file_path=rel_raw_path,
                breadcrumb=default_title
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
        # 移除潜在的乱码干扰，优先保证 ID 的唯一性与可读性
        slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "_", title.lower()).strip("_") or "section"
        path_slug = re.sub(r"[^a-zA-Z0-9\u4e00-\u9fff]+", "_", rel_path.lower()).strip("_")
        # 增加盐值哈希，防止路径冲突
        digest = hashlib.md5(f"{rel_path}:{index}:{title}".encode("utf-8", errors="replace")).hexdigest()[:8]
        return f"wc_{path_slug[:30]}_{slug[:20]}_{index:02d}_{digest}"
