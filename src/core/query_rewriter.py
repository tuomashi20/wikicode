from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml


_STOPWORDS = {
    "什么",
    "如何",
    "怎么",
    "怎样",
    "呢",
    "吗",
    "请问",
    "一下",
    "标准",
    "判断",
    "定义",
    "是",
    "的",
}

_DEFAULT_SYNONYMS: dict[str, list[str]] = {
    "废旧": ["报废", "淘汰"],
    "终端": ["设备", "cpe", "onu", "光猫"],
    "翻新": ["维修", "整修", "再利用"],
    "回收": ["返还", "收回"],
    "利旧": ["再利用", "复用"],
    "标准": ["规则", "判定", "口径"],
}


@dataclass
class QueryRewrite:
    original: str
    keywords: list[str]
    expanded_terms: list[str]
    fts_query: str
    suggest_terms: list[str]



def _tokenize(query: str) -> list[str]:
    q = query.strip().lower()
    if not q:
        return []

    tokens: list[str] = []
    tokens.extend(re.findall(r"[a-z0-9_]{2,}", q))
    cn_seqs = re.findall(r"[\u4e00-\u9fff]{2,}", q)
    for seq in cn_seqs:
        if len(seq) <= 8:
            tokens.append(seq)
        for n in (4, 3, 2):
            if len(seq) >= n:
                for i in range(0, len(seq) - n + 1):
                    gram = seq[i : i + n]
                    if gram not in _STOPWORDS:
                        tokens.append(gram)

    out: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        if t in _STOPWORDS:
            continue
        if t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= 20:
            break
    return out



def load_synonyms(path: Path | str | None) -> dict[str, list[str]]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, list[str]] = {}
        for k, v in data.items():
            if isinstance(k, str) and isinstance(v, list):
                vals = [str(x).strip() for x in v if str(x).strip()]
                if vals:
                    out[k.strip()] = vals
        return out
    except Exception:
        return {}



def rewrite_query(query: str, synonyms: dict[str, list[str]] | None = None) -> QueryRewrite:
    keywords = _tokenize(query)
    expanded: list[str] = []
    seen: set[str] = set()
    syn_map = synonyms or _DEFAULT_SYNONYMS

    for kw in keywords:
        if kw not in seen:
            expanded.append(kw)
            seen.add(kw)
        for k, syns in syn_map.items():
            if k in kw or kw in k:
                for s in syns:
                    if s not in seen:
                        expanded.append(s)
                        seen.add(s)

    expanded = expanded[:10]
    fts_query = " OR ".join([f'"{t}"' for t in expanded[:8]]) if expanded else ""
    suggest_terms = expanded[:6]

    return QueryRewrite(
        original=query,
        keywords=keywords,
        expanded_terms=expanded,
        fts_query=fts_query,
        suggest_terms=suggest_terms,
    )
