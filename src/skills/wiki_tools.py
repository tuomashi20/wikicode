from __future__ import annotations

from pathlib import Path
from typing import Any

from src.core.query_rewriter import QueryRewrite, load_synonyms, rewrite_query, load_business_terms
from src.utils.config import PROJECT_ROOT
from src.utils.db_manager import get_chunk_by_id, list_structure, search_chunks



def _build_hit_reason(row: dict[str, Any], terms: list[str]) -> str:
    title = str(row.get("title", "")).lower()
    tags = str(row.get("tags", "")).lower()
    content = str(row.get("content_text", "")).lower()
    reasons: list[str] = []
    for t in terms[:8]:
        if t and t in title:
            reasons.append(f"title:{t}")
            continue
        if t and t in tags:
            reasons.append(f"tags:{t}")
            continue
        if t and t in content:
            reasons.append(f"content:{t}")
    if not reasons:
        return "fallback"
    return ", ".join(reasons[:3])



def wiki_search_v2(
    query: str,
    limit: int = 20,
    synonyms_path: Path | str | None = None,
    business_terms_path: Path | str | None = None,
    llm: Any | None = None,
    fanout_limit: int = 12,
    rewrite_priority: str = "append",
) -> tuple[list[dict[str, Any]], QueryRewrite]:
    syns = load_synonyms(synonyms_path)
    cores = load_business_terms(business_terms_path)
    # 接入配置化的语义重写逻辑
    rw = rewrite_query(query, synonyms=syns, core_keywords=cores, llm=llm, priority=rewrite_priority)
    if not query.strip():
        return [], rw

    fanout = [query.strip()]
    for t in rw.expanded_terms[:fanout_limit]:  # 受配置管控的名额
        if t not in fanout:
            fanout.append(t)

    scored: dict[str, dict[str, Any]] = {}
    for i, q in enumerate(fanout):
        rows = search_chunks(query=q, limit=max(limit, 8))
        for rank, r in enumerate(rows):
            d = dict(r)
            cid = str(d["chunk_id"])
            score = (100 - rank) + (12 if i == 0 else max(0, 8 - i))
            prev = scored.get(cid)
            if prev is None:
                d["_score"] = score
                d["_hit_reason"] = _build_hit_reason(d, rw.expanded_terms or rw.keywords)
                scored[cid] = d
            else:
                prev["_score"] = max(float(prev.get("_score", 0)), score)

    ordered = sorted(scored.values(), key=lambda x: float(x.get("_score", 0)), reverse=True)
    return ordered[:limit], rw



def wiki_search(query: str, limit: int = 20) -> list[dict[str, Any]]:
    rows, _ = wiki_search_v2(query=query, limit=limit)
    return rows



def wiki_read_chunk(chunk_id: str) -> str:
    row = get_chunk_by_id(chunk_id)
    if not row:
        return ""
    cp = str(row["content_path"])
    p = Path(cp) if Path(cp).is_absolute() else (PROJECT_ROOT / cp)
    if not p.exists():
        return ""
    return p.read_text(encoding="utf-8", errors="ignore")



def wiki_list_structure() -> list[dict[str, Any]]:
    items = list_structure()
    return [{"parent_file": p, "chunk_count": c} for p, c in items]
