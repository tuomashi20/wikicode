from __future__ import annotations
import re
import json
import yaml
from pathlib import Path
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Union, Optional, List, Dict

@dataclass
class QueryRewrite:
    original: str
    keywords: List[str]
    expanded_terms: List[str]
    fts_query: str
    suggest_terms: List[str]

@lru_cache(maxsize=16)
def load_business_terms(path: Optional[Union[Path, str]]) -> List[str]:
    if not path: return []
    p = Path(path)
    if not p.exists(): return []
    try:
        with open(p, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            if isinstance(data, list): return [str(x).strip() for x in data if x]
            if isinstance(data, dict): return [str(x).strip() for x in data.get("terms", []) if x]
    except: pass
    return []

@lru_cache(maxsize=16)
def load_synonyms(path: Optional[Union[Path, str]]) -> Dict[str, List[str]]:
    if not path: return {}
    p = Path(path)
    if not p.exists(): return {}
    try:
        with open(p, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)
            if not isinstance(data, dict): return {}
            out: Dict[str, List[str]] = {}
            for k, v in data.items():
                if isinstance(v, list): out[str(k).strip().lower()] = [str(x).strip().lower() for x in v]
            return out
    except: pass
    return []

def _tokenize(query: str, core_keywords: Optional[List[str]] = None, synonyms: Optional[Dict[str, List[str]]] = None, stopwords: Optional[List[str]] = None) -> List[str]:
    """[工业级分词]：基于业务字典的正向最大匹配（FMM） + 噪音词过滤"""
    q = query.strip().lower()
    if not q: return []
    
    # 构造分词字典
    dict_terms = set()
    if core_keywords: dict_terms.update([x.lower() for x in core_keywords])
    if synonyms: dict_terms.update([k.lower() for k in synonyms.keys()])
    
    # 增加通用业务高频词
    common_biz = ["结算", "标准", "规范", "规则", "分工", "界面", "负责", "维护", "流程", "金额", "资费", "报账"]
    dict_terms.update(common_biz)
    
    sorted_dict = sorted(list(dict_terms), key=len, reverse=True)
    
    tokens = []
    temp_q = q
    
    # 1. 字典匹配 (正向最大匹配)
    idx = 0
    while idx < len(temp_q):
        match = None
        for term in sorted_dict:
            if temp_q[idx:].startswith(term):
                match = term
                break
        if match:
            tokens.append(match)
            idx += len(match)
        else:
            # 尝试抓取英文/数字
            eng_match = re.match(r"[a-z0-9_]{2,}", temp_q[idx:])
            if eng_match:
                tokens.append(eng_match.group())
                idx += len(eng_match.group())
            else:
                idx += 1 
                
    # 2. 补漏：增加基础 2-4 字中文块提取
    blocks = re.findall(r"[\u4e00-\u9fff]{2,4}", q)
    tokens.extend(blocks)
        
    # 去重并过滤噪音
    stops = set(x.lower() for x in (stopwords or []))
    stops.update({"是多少", "是什么", "怎么", "哪个", "进行", "关于", "对于", "多少", "有没有", "有没有相关的", "标准是多少"})
    
    res = []
    seen = set()
    for t in tokens:
        if t in stops or len(t) < 2: continue
        if t not in seen:
            seen.add(t)
            res.append(t)
    return res

@lru_cache(maxsize=128)
def _get_llm_terms(llm: Any, query: str) -> List[str]:
    """缓存 LLM 重写结果"""
    prompt = f"你是一个业务分析专家。请将以下用户问题提炼为 3 个用于检索的核心业务术语（如：结算标准、代维费、分工界面），仅输出词，用空格分隔：\n{query}"
    try:
        resp = llm.generate("业务术语专家", prompt)
        return re.findall(r"[\u4e00-\u9fffA-Za-z0-9]{2,}", resp)
    except: return []

_REWRITE_CACHE: Dict[str, QueryRewrite] = {}

def rewrite_query(
    query: str, 
    synonyms: Optional[Dict[str, List[str]]] = None,
    core_keywords: Optional[List[str]] = None,
    stopwords: Optional[List[str]] = None,
    llm: Any = None,
    skip_llm: bool = False
) -> QueryRewrite:
    q_norm = query.strip().lower()
    cache_key = f"{q_norm}:{llm is not None}:{skip_llm}"
    if cache_key in _REWRITE_CACHE: return _REWRITE_CACHE[cache_key]

    # 1. 字典分词
    tokens = _tokenize(query, core_keywords=core_keywords, synonyms=synonyms, stopwords=stopwords)
    
    # 2. 同义词扩展
    expanded = list(tokens)
    syn_map = synonyms or {}
    for t in tokens:
        if t in syn_map:
            expanded.extend(syn_map[t])
            
    # 3. LLM 语义提纯
    if llm and not skip_llm:
        lt = _get_llm_terms(llm, query)
        expanded.extend(lt)
    
    # 4. 精化与去重 (保持 FTS 语法纯净)
    expanded = list(dict.fromkeys([t for t in expanded if t and len(t) > 1]))[:12]
    
    # 5. 构建最稳健的 FTS 查询 (撤销权重语法，保证版本兼容)
    fts_query = " OR ".join([f'"{t}"' for t in expanded])
    
    res = QueryRewrite(
        original=query,
        keywords=tokens,
        expanded_terms=expanded,
        fts_query=fts_query,
        suggest_terms=expanded[:10]
    )
    if len(_REWRITE_CACHE) < 500: _REWRITE_CACHE[cache_key] = res
    return res
