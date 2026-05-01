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



from functools import lru_cache

@lru_cache(maxsize=128)
def _get_llm_terms(llm: Any, query: str) -> list[str]:
    """缓存 LLM 重写结果，减少重复开销"""
    try:
        prompt = (
            f"你是一个知识库检索专家。请将以下用户口语转化为 3 个可能出现在专业 Wiki 文档中的核心关键词。\n"
            f"用户问题：{query}\n"
            f"要求：只返回关键词，用逗号隔开。"
        )
        llm_text = llm.generate(system_prompt="Query Rewriter", user_prompt=prompt)
        if llm_text:
            return [t.strip().lower() for t in re.split(r"[,，、\s]+", llm_text) if t.strip()]
    except Exception:
        pass
    return []

def rewrite_query(
    query: str, 
    synonyms: dict[str, list[str]] | None = None,
    core_keywords: list[str] | None = None,
    llm: Any | None = None,
    priority: str = "append",
    skip_llm: bool = False
) -> QueryRewrite:
    """
    [极致性能与精度平衡版] 混合动力查询重写：
    1. 基础分词与精准同义词扩展。
    2. (可选) LLM 语义意图重构（带缓存）。
    """
    keywords = _tokenize(query, core_keywords=core_keywords)
    expanded: list[str] = []
    seen: set[str] = set()
    syn_map = synonyms or _DEFAULT_SYNONYMS

    # 基础层：精准同义词扩展
    for kw in keywords:
        if kw not in seen:
            expanded.append(kw)
            seen.add(kw)
        
        # 优化点：只有长度大于 1 的词才进行同义词扩展，防止单字污染
        if len(kw) < 2: continue

        for k, syns in syn_map.items():
            group_members = [k] + syns
            # 优化点：必须是包含关系且字符匹配度较高
            if any(m == kw or (len(kw) >= 2 and (m in kw or kw in m)) for m in group_members):
                added_in_group = 0
                for m in group_members:
                    if m not in seen:
                        expanded.append(m)
                        seen.add(m)
                        added_in_group += 1
                        if added_in_group >= 3: break # 每组最多扩展 3 个，防止噪声

    # 增强层：只有在非跳过模式且静态结果较少时才调用 LLM
    if llm is not None and len(query) >= 4 and not skip_llm:
        if len(expanded) < 6: # 阈值微调
            llm_terms = _get_llm_terms(llm, query)
            for lt in llm_terms:
                if lt not in seen:
                    if priority == "prepend": expanded.insert(0, lt)
                    else: expanded.append(lt)
                    seen.add(lt)

    expanded = expanded[:20] # 进一步精简到 20 个词
    # 构造 FTS 查询：原始词排在前，增加权重感
    fts_query = " OR ".join([f'"{t}"' for t in expanded[:15]]) if expanded else ""
    suggest_terms = expanded[:10]

    return QueryRewrite(
        original=query,
        keywords=keywords,
        expanded_terms=expanded,
        fts_query=fts_query,
        suggest_terms=suggest_terms,
    )
