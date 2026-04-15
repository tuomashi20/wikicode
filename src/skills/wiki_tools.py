from __future__ import annotations

from pathlib import Path
from typing import Any

from src.utils.config import PROJECT_ROOT
from src.utils.db_manager import get_chunk_by_id, list_structure, search_chunks



def wiki_search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = search_chunks(query=query, limit=limit)
    return [dict(r) for r in rows]



def wiki_read_chunk(chunk_id: str) -> str:
    row = get_chunk_by_id(chunk_id)
    if not row:
        return ""
    p = PROJECT_ROOT / row["content_path"]
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="ignore")



def wiki_list_structure() -> list[dict[str, Any]]:
    items = list_structure()
    return [{"parent_file": p, "chunk_count": c} for p, c in items]
