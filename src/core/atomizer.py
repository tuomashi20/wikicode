from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.utils.config import AppConfig, PROJECT_ROOT
from src.utils.db_manager import delete_chunks_by_parent, init_db, upsert_chunk
from src.utils.logger import get_file_logger
from src.core.wiki_compiler import WikiCompiler


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
        self.chunks_dir = config.wiki_strategy.processed_path / "chunks"
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        init_db()

    def sync(self) -> dict[str, int]:
        raw_root = self.config.wiki_strategy.raw_path
        md_files = sorted(raw_root.rglob("*.md")) if raw_root.exists() else []

        processed_files = 0
        total_chunks = 0

        for md_file in md_files:
            chunks = self._process_file(md_file)
            processed_files += 1
            total_chunks += len(chunks)

        compiled = {"pages": 0, "files": 0, "tags": 0}
        if self.config.wiki_strategy.wiki_compile_on_sync:
            compiled = WikiCompiler(self.config).compile()
            self.logger.info(
                "wiki_compile_done pages=%s files=%s tags=%s",
                compiled["pages"],
                compiled["files"],
                compiled["tags"],
            )

        self.logger.info("sync_done files=%s chunks=%s", processed_files, total_chunks)
        return {"files": processed_files, "chunks": total_chunks, "wiki_pages": compiled["pages"]}

    def _process_file(self, md_file: Path) -> list[Chunk]:
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
                # when vault is outside project root, persist absolute path
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

    @staticmethod
    def _split_by_heading(text: str, rel_raw_path: str, level: int = 2) -> list[Chunk]:
        heading_pattern = re.compile(rf"^{'#' * level}\s+(.+?)\s*$", re.MULTILINE)
        matches = list(heading_pattern.finditer(text))

        if not matches:
            default_title = Path(rel_raw_path).stem
            chunk_id = Atomizer._build_chunk_id(rel_raw_path, 1, default_title)
            return [
                Chunk(
                    chunk_id=chunk_id,
                    title=default_title,
                    content=text.strip() or default_title,
                    parent_file=rel_raw_path,
                    raw_file_path=rel_raw_path,
                )
            ]

        chunks: list[Chunk] = []
        for idx, m in enumerate(matches, start=1):
            title = m.group(1).strip()
            start = m.end()
            end = matches[idx].start() if idx < len(matches) else len(text)
            body = text[start:end].strip()
            content = f"## {title}\n\n{body}" if body else f"## {title}"
            chunk_id = Atomizer._build_chunk_id(rel_raw_path, idx, title)
            chunks.append(
                Chunk(
                    chunk_id=chunk_id,
                    title=title,
                    content=content,
                    parent_file=rel_raw_path,
                    raw_file_path=rel_raw_path,
                )
            )
        return chunks

    @staticmethod
    def _extract_tags(title: str, content: str) -> str:
        stopwords = {
            "相关",
            "以及",
            "如何",
            "进行",
            "根据",
            "要求",
            "规则",
            "规范",
            "管理",
            "流程",
            "标准",
            "说明",
            "内容",
            "附件",
            "其中",
            "包括",
            "使用",
            "需要",
            "可以",
            "本次",
            "本条",
            "该项",
            "如下",
            "and",
            "the",
            "for",
            "with",
            "from",
            "that",
            "this",
        }

        source = f"{title}\n{content[:1200]}"
        parts = re.split(r"[，。；：、\s\-_/()（）\[\]【】<>《》\"'“”]+", source)

        candidates: list[str] = []
        for p in parts:
            t = p.strip()
            if not t:
                continue

            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,24}", t):
                candidates.append(t.lower())
                continue

            if re.fullmatch(r"[\u4e00-\u9fff]{2,12}", t):
                if t not in stopwords and not t.endswith(("的", "了")):
                    candidates.append(t)

        strong_terms = re.findall(
            r"[\u4e00-\u9fff]{2,14}(?:管理|流程|规范|制度|标准|策略|机制|规则|办法|要求|定义|术语|系统|平台|终端|设备|申请|审批|回收|处置)",
            source,
        )

        for term in strong_terms:
            if term not in stopwords and len(term) <= 20:
                candidates.append(term)

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
