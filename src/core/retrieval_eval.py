from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.query_rewriter import load_synonyms, rewrite_query
from src.skills.wiki_tools import wiki_search_v2


@dataclass
class EvalCase:
    query: str
    expect_any: list[str]
    expect_in: str = "any"  # any|title|content|parent|tags


@dataclass
class EvalRow:
    query: str
    hit: bool
    top_hit: str
    matched_field: str
    rank: int


def load_eval_cases(path: Path) -> list[EvalCase]:
    if not path.exists():
        raise FileNotFoundError(f"Eval file not found: {path}")

    rows: list[EvalCase] = []
    text = path.read_text(encoding="utf-8-sig")
    for idx, line in enumerate(text.splitlines(), start=1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        obj = json.loads(line)
        query = str(obj.get("query", "")).strip()
        expect_any = [str(x).strip().lower() for x in (obj.get("expect_any") or []) if str(x).strip()]
        expect_in = str(obj.get("expect_in", "any")).strip().lower() or "any"
        if not query:
            raise ValueError(f"Invalid case line {idx}: missing query")
        if not expect_any:
            raise ValueError(f"Invalid case line {idx}: missing expect_any")
        rows.append(EvalCase(query=query, expect_any=expect_any, expect_in=expect_in))
    return rows


def _field_text(r: dict[str, Any], field: str) -> str:
    if field == "title":
        return str(r.get("title", "")).lower()
    if field == "content":
        return str(r.get("content_text", "")).lower()
    if field == "parent":
        return str(r.get("parent_file", "")).lower()
    if field == "tags":
        return str(r.get("tags", "")).lower()
    return " ".join(
        [
            str(r.get("title", "")),
            str(r.get("content_text", "")),
            str(r.get("parent_file", "")),
            str(r.get("tags", "")),
        ]
    ).lower()


def evaluate_retrieval(
    *,
    cases: list[EvalCase],
    topk: int = 8,
    synonyms_path: Path | str | None = None,
) -> tuple[dict[str, Any], list[EvalRow]]:
    details: list[EvalRow] = []
    hit_count = 0
    top1_hit = 0
    mrr_total = 0.0

    # pre-load for consistency
    syns = load_synonyms(synonyms_path)

    for c in cases:
        rw = rewrite_query(c.query, synonyms=syns)
        rows, _ = wiki_search_v2(query=c.query, limit=topk, synonyms_path=synonyms_path)

        hit = False
        matched_field = ""
        rank = 0
        for r in rows:
            rank += 1
            fields = [c.expect_in] if c.expect_in in {"title", "content", "parent", "tags"} else ["title", "content", "parent", "tags"]
            for f in fields:
                txt = _field_text(r, f)
                if any(term in txt for term in c.expect_any):
                    hit = True
                    matched_field = f
                    break
            if hit:
                break

        if hit:
            hit_count += 1
            mrr_total += 1.0 / float(rank)
            if rank == 1:
                top1_hit += 1

        top = str(rows[0].get("title", "")) if rows else ""
        # keep rw used to avoid dead-code warnings in future and for debugging extension
        _ = rw
        details.append(EvalRow(query=c.query, hit=hit, top_hit=top, matched_field=matched_field, rank=rank if hit else 0))

    total = len(cases)
    recall = (hit_count / total) if total else 0.0
    summary = {
        "total": total,
        "hit": hit_count,
        "miss": total - hit_count,
        "recall_at_k": round(recall, 4),
        "top1_accuracy": round((top1_hit / total) if total else 0.0, 4),
        "mrr": round((mrr_total / total) if total else 0.0, 4),
        "topk": topk,
    }
    return summary, details


def save_eval_report(summary: dict[str, Any], details: list[EvalRow], out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "summary": summary,
        "details": [
            {
                "query": d.query,
                "hit": d.hit,
                "top_hit": d.top_hit,
                "matched_field": d.matched_field,
                "rank": d.rank,
            }
            for d in details
        ],
        "miss_queries": [d.query for d in details if not d.hit],
    }
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return out_path


def load_eval_report(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Eval report not found: {path}")
    data = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("Invalid report format (root should be object)")
    return data


def compare_eval_reports(base: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    bsum = dict(base.get("summary") or {})
    csum = dict(current.get("summary") or {})
    metrics = ["recall_at_k", "top1_accuracy", "mrr", "hit", "miss", "total", "topk"]
    delta: dict[str, Any] = {}
    for m in metrics:
        b = bsum.get(m)
        c = csum.get(m)
        if isinstance(b, (int, float)) and isinstance(c, (int, float)):
            delta[m] = round(float(c) - float(b), 4)
        else:
            delta[m] = None

    b_miss = set(str(x) for x in (base.get("miss_queries") or []))
    c_miss = set(str(x) for x in (current.get("miss_queries") or []))
    fixed = sorted(b_miss - c_miss)
    regressed = sorted(c_miss - b_miss)
    still_miss = sorted(b_miss & c_miss)

    return {
        "base_summary": bsum,
        "current_summary": csum,
        "delta": delta,
        "fixed_queries": fixed,
        "regressed_queries": regressed,
        "still_miss_queries": still_miss,
    }
