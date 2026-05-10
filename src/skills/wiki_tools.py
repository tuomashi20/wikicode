from __future__ import annotations
import os
import re
import json
import sqlite3
from pathlib import Path
from typing import Any, List, Dict, Union, Optional

def _get_wiki_root() -> Path:
    """[WikiCoder 路径中枢] 严格按照 vault_path + wiki_dir 拼接命"""
    try:
        from src.utils.config import load_config
        config = load_config()
        # 提取策略配置
        strategy = getattr(config, "wiki_strategy", None)
        if strategy is None:
            if hasattr(config, "get"):
                strategy = config.get("wiki_strategy", {})
            else:
                strategy = {}
            
        v_path = getattr(strategy, "vault_path", "wiki") if hasattr(strategy, "vault_path") else (strategy.get("vault_path", "wiki") if isinstance(strategy, dict) else "wiki")
        w_dir = getattr(strategy, "wiki_dir", "") if hasattr(strategy, "wiki_dir") else (strategy.get("wiki_dir", "") if isinstance(strategy, dict) else "")
        
        # 绝对对齐：拼接根目录与 Wiki 子目录
        full_path = Path(v_path) / w_dir
        return full_path
    except Exception as e:
        # 最后的保底
        return Path("D:/lihq_obsi/lihq_obsi/LLM_wiki/wiki")


def wiki_search_v2(
    query: str,
    limit: int = 10,
    llm: Optional[Any] = None,
    skip_llm: bool = True
) -> tuple[List[Dict[str, Any]], Any]:
    """[WikiCoder 极效版] 优先检索编译后的 Wiki 库，次选原始切片"""
    from src.utils.db_manager import search_chunks
    
    # 1. 动态获取 Wiki 根目录
    wiki_root = _get_wiki_root()
    wiki_hits = []
    
    # 简单的关键词提取（处理 OLT, 结算等核心词）
    keywords = re.findall(r'[\u4e00-\u9fa5a-zA-Z0-9]{2,}', query)
    
    if wiki_root.exists():
        for kw in keywords:
            # 在全库搜索匹配的文件
            for md_file in wiki_root.rglob(f"*{kw}*.md"):
                if ".wikicoder" in str(md_file): continue
                # 构造伪 chunk 格式，兼容下游
                wiki_hits.append({
                    "chunk_id": f"WIKI:{md_file.name}",
                    "title": f"【Wiki 聚合页】{md_file.stem}",
                    "parent_file": str(md_file.relative_to(wiki_root)),
                    "is_wiki": True,
                    "content_preview": md_file.read_text(encoding="utf-8")[:1000]
                })
                if len(wiki_hits) >= 3: break # Wiki 命中不在多而在精
            if wiki_hits: break

    # 2. 调用原始数据库检索
    results = search_chunks(query, limit=limit)
    
    # 3. 合并结果：Wiki 命中具有压倒性的权重，置于首位
    final_results = wiki_hits + [r for r in results if not any(w["title"] == r["title"] for w in wiki_hits)]
    
    return final_results[:limit], None

from src.utils.db_manager import get_conn

def wiki_read_chunk(chunk_id: Union[str, int]) -> str:
    """[全景视野版] 读取片段，支持 Wiki 聚合页直读"""
    try:
        # 1. [WikiCoder 优化] 如果是 Wiki 聚合页，直接读取文件
        if isinstance(chunk_id, str) and chunk_id.startswith("WIKI:"):
            filename = chunk_id.replace("WIKI:", "")
            wiki_root = _get_wiki_root()
            # 递归寻找匹配的文件
            for p in wiki_root.rglob(filename):
                return p.read_text(encoding="utf-8")
            return f"错误: 未找到 Wiki 页面 {filename}"

        # 2. 原始数据库检索逻辑
        with get_conn() as conn:
            target = conn.execute(
                "SELECT rowid, parent_file, content_text FROM chunks WHERE chunk_id = ?", 
                (chunk_id,)
            ).fetchone()
            if not target: return "未找到该片段。"
            
            rid = target['rowid']
            p_file = target['parent_file']
            
            # 扩大范围获取上下文
            neighbors = conn.execute(
                "SELECT content_text FROM chunks WHERE parent_file = ? AND rowid >= ? AND rowid <= ? ORDER BY rowid ASC",
                (p_file, rid - 5, rid + 5)
            ).fetchall()
            
            contents = [n['content_text'] for n in neighbors if n['content_text']]
            if not contents: return target['content_text'] or ""
            
            return "\n--- [自动加载上下文关联] ---\n" + "\n\n".join(contents)
    except Exception as e:
        return f"读取失败: {e}"

def wiki_list_structure() -> List[Dict[str, Any]]:
    from src.utils.db_manager import list_structure
    items = list_structure()
    return [{"parent_file": p, "chunk_count": c} for p, c in items]
