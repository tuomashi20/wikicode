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



def load_business_terms(path: Path | str | None) -> list[str]:
    """加载核心业务词列表。"""
    if not path:
        return []
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        if isinstance(data, dict):
            return [str(x).strip() for x in data.get("core_keywords", []) if str(x).strip()]
        return []
    except Exception:
        return []



def _tokenize(query: str, core_keywords: list[str] | None = None) -> list[str]:
    q = query.strip().lower()
    if not q:
        return []

    tokens_with_score: list[tuple[str, int]] = []
    seen: set[str] = set()
    cores = set(x.lower() for x in (core_keywords or []))

    # 1. 提取基础词块 (英文/数字)
    for match in re.findall(r"[a-z0-9_]{2,}", q):
        if match not in seen:
            score = 100 if match.isdigit() else 50
            tokens_with_score.append((match, score))
            seen.add(match)

    # 2. 提取中文序列并生成 N-grams
    cn_seqs = re.findall(r"[\u4e00-\u9fff]{2,}", q)
    for seq in cn_seqs:
        # 保留原词块（取消 8 位限制）
        if seq not in seen:
            score = 80
            if seq in cores:
                score = 200  # 核心词最高分
            tokens_with_score.append((seq, score))
            seen.add(seq)

        # 生成 N-grams 滑窗
        for n in (4, 3, 2):
            if len(seq) >= n:
                for i in range(0, len(seq) - n + 1):
                    gram = seq[i : i + n]
                    if gram not in _STOPWORDS and gram not in seen:
                        score = 40
                        if gram in cores:
                            score = 150 # 核心词片段也给高分
                        tokens_with_score.append((gram, score))
                        seen.add(gram)

    # 3. 按优先级排序（分数高者优先，同分者按原始位置顺序）
    tokens_with_score.sort(key=lambda x: x[1], reverse=True)
    
    # 扩大配额至 40 个 Token
    out: list[str] = []
    for t, _ in tokens_with_score:
        if t in _STOPWORDS:
            continue
        out.append(t)
        if len(out) >= 40:
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



def rewrite_query(
    query: str, 
    synonyms: dict[str, list[str]] | None = None,
    core_keywords: list[str] | None = None,
    llm: Any | None = None,
    priority: str = "append"
) -> QueryRewrite:
    """
    [重写版] 混合动力查询重写：
    1. 基础分词与静态同义词扩展。
    2. (可选) LLM 语义意图重构。
    """
    keywords = _tokenize(query, core_keywords=core_keywords)
    expanded: list[str] = []
    seen: set[str] = set()
    syn_map = synonyms or _DEFAULT_SYNONYMS

    # 基础层：静态同义词
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

    # 增强层：LLM 语义重写（如果传入了 LLM 客户端）
    if llm is not None and len(query) >= 4:
        try:
            # 极速 Prompt：要求返回 3-5 个专业关键词
            prompt = (
                f"你是一个知识库检索专家。请将以下用户口语转化为 3 个可能出现在专业 Wiki 文档中的核心关键词。\n"
                f"用户问题：{query}\n"
                f"要求：只返回关键词，用逗号隔开。"
            )
            llm_text = llm.generate(system_prompt="Query Rewriter", user_prompt=prompt)
            if llm_text:
                llm_terms = [t.strip().lower() for t in re.split(r"[,，、\s]+", llm_text) if t.strip()]
                for lt in llm_terms:
                    if lt not in seen:
                        if priority == "prepend":
                            expanded.insert(0, lt)
                        else:
                            expanded.append(lt)
                        seen.add(lt)
        except Exception:
            pass

    expanded = expanded[:25] 
    fts_query = " OR ".join([f'"{t}"' for t in expanded[:18]]) if expanded else ""
    suggest_terms = expanded[:10]

    return QueryRewrite(
        original=query,
        keywords=keywords,
        expanded_terms=expanded,
        fts_query=fts_query,
        suggest_terms=suggest_terms,
    )
