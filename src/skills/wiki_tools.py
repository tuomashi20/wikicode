from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from src.core.query_rewriter import QueryRewrite, load_synonyms, rewrite_query, load_business_terms
from src.utils.config import PROJECT_ROOT
from src.utils.db_manager import get_chunk_by_id, list_structure, search_chunks


def wiki_search_v2(
    query: str,
    limit: int = 20,
    synonyms_path: Path | str | None = None,
    business_terms_path: Path | str | None = None,
    llm: Any | None = None,
    fanout_limit: int = 12,
    rewrite_priority: str = "append",
    skip_llm: bool = False
) -> tuple[list[dict[str, Any]], QueryRewrite]:
    """[标准数据版] 检索知识库，返回原始行数据，用于 UI 和测评"""
    rw = rewrite_query(query, llm=llm, skip_llm=skip_llm)
    if not query.strip():
        return [], rw

    rows = search_chunks(query=rw.fts_query, limit=limit)
    results = []
    for r in rows:
        d = dict(r)
        # 补全 breadcrumb 逻辑
        if not d.get("breadcrumb"):
            d["breadcrumb"] = f"{d.get('parent_file')} > {d.get('title')}"
        results.append(d)
    return results, rw


def wiki_search(
    query: str,
    limit: int = 5,
    llm: Any | None = None,
    skip_llm: bool = False
) -> str:
    """[Agent 专用版] 返回带路径背书的格式化字符串"""
    results, _ = wiki_search_v2(query, limit=limit, llm=llm, skip_llm=skip_llm)
    if not results:
        return f"Wiki: 未找到关于 '{query}' 的匹配。建议使用 wiki_list 查看相关目录。"

    output = []
    for r in results:
        path = r.get("breadcrumb")
        content = r.get("content_text") or ""
        output.append(f"### [路径背书]: {path}\n{content[:2000]}")
    
    return "\n\n---\n\n".join(output)


def wiki_list(sub_dir: str = "") -> str:
    """[Agent 专用版] 列出知识库目录结构"""
    from src.utils.config import load_config
    config = load_config()
    base_path = Path(config.wiki_strategy.raw_path)
    
    target = base_path
    if sub_dir:
        target = base_path / sub_dir
    
    if not target.exists():
        return f"Error: 路径 '{sub_dir}' 不存在。"
    
    files = []
    for p in target.glob("**/*"):
        if p.is_file() and p.suffix in [".md", ".txt", ".docx", ".pdf"]:
            files.append(str(p.relative_to(base_path)))
    
    if not files:
        return f"Wiki: 在 '{sub_dir}' 下未发现规范文件。"
        
    return "知识库文件列表:\n" + "\n".join([f"- {f}" for f in sorted(files)])


def wiki_read(rel_path: str) -> str:
    """[Agent 专用版] 通读规范文件"""
    from src.utils.config import load_config
    config = load_config()
    p = Path(config.wiki_strategy.raw_path) / rel_path
    if not p.exists():
        return f"Error: 未找到文件 '{rel_path}'。"
        
    try:
        content = p.read_text(encoding="utf-8", errors="ignore")
        return f"--- 文件内容预览: {rel_path} ---\n{content[:10000]}"
    except Exception as e:
        return f"Error reading file: {str(e)}"


def wiki_read_chunk(chunk_id: str) -> str:
    """按 ID 读取知识片段内容 (向下兼容)"""
    row = get_chunk_by_id(chunk_id)
    if not row:
        return ""
    cp = str(row["content_path"])
    p = Path(cp) if Path(cp).is_absolute() else (PROJECT_ROOT / cp)
    if not p.exists():
        return ""
    try:
        return p.read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return ""


def wiki_list_structure() -> list[dict[str, Any]]:
    """供 UI 使用的结构化列表"""
    items = list_structure()
    return [{"parent_file": p, "chunk_count": c} for p, c in items]


# --- 别名兼容 ---
wiki_list_files = wiki_list
wiki_read_file = wiki_read
