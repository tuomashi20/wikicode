from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from src.utils.config import AppConfig, PROJECT_ROOT
from src.utils.db_manager import delete_chunks_by_parent, init_db, upsert_chunk
from src.utils.logger import get_file_logger


CHUNKS_DIR = PROJECT_ROOT / "data" / "wiki_processed" / "chunks"


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
        CHUNKS_DIR.mkdir(parents=True, exist_ok=True)
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

        self.logger.info("sync_done files=%s chunks=%s", processed_files, total_chunks)
        return {"files": processed_files, "chunks": total_chunks}

    def _process_file(self, md_file: Path) -> list[Chunk]:
        text = md_file.read_text(encoding="utf-8", errors="ignore")
        rel_raw = str(md_file.relative_to(self.config.wiki_strategy.raw_path)).replace("\\", "/")

        delete_chunks_by_parent(rel_raw)

        chunks = self._split_by_heading(text, rel_raw, level=self.config.wiki_strategy.heading_level)
        for c in chunks:
            out_path = CHUNKS_DIR / f"{c.chunk_id}.md"
            out_path.write_text(c.content, encoding="utf-8")
            tags = self._extract_tags(c.title, c.content)
            upsert_chunk(
                chunk_id=c.chunk_id,
                title=c.title,
                parent_file=c.parent_file,
                raw_file_path=c.raw_file_path,
                tags=tags,
                content_path=str(out_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
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
        text = f"{title}\n{content}".lower()
        words = re.findall(r"[a-z0-9_]{3,}", text)
        cn = re.findall(r"[\u4e00-\u9fff]{2,}", text)

        tokens: list[str] = []
        tokens.extend(words)
        for seq in cn:
            tokens.append(seq)
            for n in (4, 3, 2):
                if len(seq) >= n:
                    tokens.append(seq[:n])

        seen: set[str] = set()
        out: list[str] = []
        for t in tokens:
            if t not in seen:
                seen.add(t)
                out.append(t)
            if len(out) >= 12:
                break
        return ",".join(out)

    @staticmethod
    def _build_chunk_id(rel_path: str, index: int, title: str) -> str:
        slug = re.sub(r"[^a-zA-Z0-9]+", "_", title.lower()).strip("_") or "section"
        path_slug = re.sub(r"[^a-zA-Z0-9]+", "_", rel_path.lower()).strip("_")
        digest = hashlib.md5(f"{rel_path}:{index}:{title}".encode("utf-8")).hexdigest()[:8]
        return f"wc_{path_slug}_{slug}_{index:02d}_{digest}"
