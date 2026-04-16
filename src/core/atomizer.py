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
            "related",
            "about",
            "with",
            "from",
            "that",
            "this",
            "and",
            "the",
            "for",
            "requirement",
            "rule",
            "rules",
            "policy",
            "process",
            "standard",
            "management",
        }

        source = f"{title}\n{content[:1200]}"
        parts = re.split(r"[\s,.;:!?()\[\]{}<>\"'`/_\\\-]+", source)

        candidates: list[str] = []
        for p in parts:
            t = p.strip()
            if not t:
                continue

            if re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{2,24}", t):
                tl = t.lower()
                if tl not in stopwords:
                    candidates.append(tl)
                continue

            if re.fullmatch(r"[\u4e00-\u9fff]{2,12}", t):
                if not t.endswith(("的", "了")):
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
