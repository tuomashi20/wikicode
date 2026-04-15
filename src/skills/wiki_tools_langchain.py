from __future__ import annotations

from typing import Any, Callable

from src.skills.wiki_tools import wiki_list_structure, wiki_read_chunk, wiki_search



def build_langchain_tools() -> list[Any]:
    """Optional helper: build LangChain Tool objects for wiki access.

    Requires `langchain-core` to be installed.
    """
    try:
        from langchain_core.tools import Tool
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(
            "LangChain is not installed. Install with: pip install langchain-core"
        ) from e

    def _search(query: str) -> str:
        rows = wiki_search(query, limit=8)
        if not rows:
            return "[]"
        lines = [f"{r['chunk_id']} | {r['title']} | {r['parent_file']}" for r in rows]
        return "\n".join(lines)

    def _read(chunk_id: str) -> str:
        return wiki_read_chunk(chunk_id)

    def _structure(_: str = "") -> str:
        items = wiki_list_structure()
        return "\n".join([f"{i['parent_file']} ({i['chunk_count']})" for i in items])

    return [
        Tool.from_function(
            name="wiki_search",
            description="Search wiki chunks by keyword and return chunk ids and titles.",
            func=_search,
        ),
        Tool.from_function(
            name="wiki_read_chunk",
            description="Read full markdown content of a wiki chunk by chunk_id.",
            func=_read,
        ),
        Tool.from_function(
            name="wiki_list_structure",
            description="List wiki source files and chunk counts.",
            func=_structure,
        ),
    ]
