from __future__ import annotations

import os
import json
import re
import shutil
import sqlite3
from pathlib import Path

from .config import PROJECT_ROOT


def _get_configured_db_path() -> Path:
    """[WikiCoder 路径中枢] 从 config 获取数据库存放位置"""
    try:
        from src.utils.config import load_config
        config = load_config()
        
        # 兼容处理：支持对象属性或字典访问
        strategy = getattr(config, "wiki_strategy", None)
        if strategy is None:
            try: strategy = config.get("wiki_strategy", {})
            except: strategy = {}
        
        v_path = getattr(strategy, "vault_path", "wiki") if hasattr(strategy, "vault_path") else strategy.get("vault_path", "wiki")
        
        # 数据库统一存放在知识库的隐藏元数据目录下
        db_path = Path(v_path) / ".wikicoder" / "wiki.db"
        return db_path
    except:
        return PROJECT_ROOT / ".wikicoder" / "wiki.db"

DB_PATH = _get_configured_db_path()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  parent_file TEXT NOT NULL,
  raw_file_path TEXT NOT NULL,
  breadcrumb TEXT DEFAULT '',
  tags TEXT DEFAULT '',
  content_path TEXT NOT NULL,
  content_text TEXT DEFAULT '',
  last_modified TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_title ON chunks(title);
CREATE INDEX IF NOT EXISTS idx_chunks_parent_file ON chunks(parent_file);
CREATE INDEX IF NOT EXISTS idx_chunks_raw_file_path ON chunks(raw_file_path);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  chunk_id UNINDEXED,
  title,
  tags,
  parent_file,
  breadcrumb,
  content_text,
  tokenize='unicode61'
);
"""


_resolved_db_path: Path | None = None



def configure_db_path(path: Path | str) -> None:
    global DB_PATH, _resolved_db_path
    DB_PATH = Path(path)
    _resolved_db_path = None



def _try_open(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    # 增加超时时间并允许跨线程访问，解决插件与 TUI 并发导致的 database locked 崩溃
    conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("SELECT 1")
    return conn



def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()}
    if "content_text" not in cols:
        conn.execute("ALTER TABLE chunks ADD COLUMN content_text TEXT DEFAULT ''")
    if "breadcrumb" not in cols:
        conn.execute("ALTER TABLE chunks ADD COLUMN breadcrumb TEXT DEFAULT ''")

    # backfill fts from chunks
    conn.execute("DELETE FROM chunks_fts")
    conn.execute(
        """
        INSERT INTO chunks_fts (chunk_id, title, tags, parent_file, breadcrumb, content_text)
        SELECT chunk_id, title, tags, parent_file, breadcrumb, content_text
        FROM chunks
        """
    )
    conn.commit()



def _init_schema(path: Path) -> None:
    with _try_open(path) as conn:
        _ensure_schema(conn)



def resolve_db_path() -> Path:
    global _resolved_db_path
    if _resolved_db_path is not None:
        return _resolved_db_path

    # 路径绝对服从：只认配置好的 DB_PATH
    try:
        _init_schema(DB_PATH)
        _resolved_db_path = DB_PATH
        return DB_PATH
    except Exception as e:
        raise RuntimeError(f"无法在配置路径 {DB_PATH} 初始化数据库。请检查 vault_path 是否正确。") from e



def get_conn(db_path: Path | None = None) -> sqlite3.Connection:
    target = db_path or resolve_db_path()
    return _try_open(target)



def init_db(db_path: Path | None = None) -> None:
    target = db_path or resolve_db_path()
    _init_schema(target)



def _fts_upsert(
    conn: sqlite3.Connection,
    *,
    chunk_id: str,
    title: str,
    tags: str,
    parent_file: str,
    breadcrumb: str,
    content_text: str,
) -> None:
    # [核心修复]：针对 unicode61 分词器在 CJK/Latin 混排时无法切分的问题，
    # 在存入 FTS 虚拟表前，强制在中文与英数边界插入空格。
    def _pad_cjk(text: str) -> str:
        if not text: return ""
        # 在 [中文][英数] 间插空格
        text = re.sub(r"([\u4e00-\u9fff])([a-zA-Z0-9])", r"\1 \2", text)
        # 在 [英数][中文] 间插空格
        text = re.sub(r"([a-zA-Z0-9])([\u4e00-\u9fff])", r"\1 \2", text)
        return text

    p_title = _pad_cjk(title)
    p_content = _pad_cjk(content_text)

    conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,))
    conn.execute(
        """
        INSERT INTO chunks_fts (chunk_id, title, tags, parent_file, breadcrumb, content_text)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (chunk_id, p_title, tags, parent_file, breadcrumb, p_content),
    )



def upsert_chunk(
    chunk_id: str,
    title: str,
    parent_file: str,
    raw_file_path: str,
    breadcrumb: str,
    tags: str,
    content_path: str,
    content_text: str,
    last_modified: str,
    db_path: Path | None = None,
) -> None:
    sql = """
    INSERT INTO chunks (chunk_id, title, parent_file, raw_file_path, breadcrumb, tags, content_path, content_text, last_modified)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(chunk_id) DO UPDATE SET
      title=excluded.title,
      parent_file=excluded.parent_file,
      raw_file_path=excluded.raw_file_path,
      breadcrumb=excluded.breadcrumb,
      tags=excluded.tags,
      content_path=excluded.content_path,
      content_text=excluded.content_text,
      last_modified=excluded.last_modified
    """
    with get_conn(db_path) as conn:
        conn.execute(sql, (chunk_id, title, parent_file, raw_file_path, breadcrumb, tags, content_path, content_text, last_modified))
        _fts_upsert(
            conn,
            chunk_id=chunk_id,
            title=title,
            tags=tags,
            parent_file=parent_file,
            breadcrumb=breadcrumb,
            content_text=content_text,
        )
        conn.commit()



def delete_chunks_by_parent(raw_file_path: str, db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        ids = conn.execute("SELECT chunk_id FROM chunks WHERE raw_file_path = ?", (raw_file_path,)).fetchall()
        for r in ids:
            conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (r["chunk_id"],))
        conn.execute("DELETE FROM chunks WHERE raw_file_path = ?", (raw_file_path,))
        conn.commit()



def _tokenize_query(query: str) -> list[str]:
    q = query.strip().lower()
    if not q:
        return []

    tokens: list[str] = []

    # English / digits words
    tokens.extend(re.findall(r"[a-z0-9_]{2,}", q))

    # Chinese sequences + ngrams
    cn_seqs = re.findall(r"[\u4e00-\u9fff]{2,}", q)
    for seq in cn_seqs:
        tokens.append(seq)
        for n in (4, 3, 2):
            if len(seq) >= n:
                for i in range(0, len(seq) - n + 1):
                    tokens.append(seq[i : i + n])

    # de-dup with stable order
    out: list[str] = []
    seen: set[str] = set()
    for t in tokens:
        if t and t not in seen:
            seen.add(t)
            out.append(t)
        if len(out) >= 18:
            break
    return out



def _merge_rows_by_score(rows_with_score: list[tuple[sqlite3.Row, float]], limit: int) -> list[sqlite3.Row]:
    # [高级优化]：基于多样性（Diversity）的重排逻辑
    # 动态计算每个文档的配额：防止大文件垄断上下文
    max_per_doc = max(3, limit // 4)
    
    scored: dict[str, tuple[sqlite3.Row, float]] = {}
    for row, score in rows_with_score:
        cid = str(row["chunk_id"])
        old = scored.get(cid)
        if old is None or score > old[1]:
            scored[cid] = (row, score)

    # 按照分数从高到低进行二次筛选
    ranked_chunks = sorted(scored.values(), key=lambda x: x[1], reverse=True)
    
    selected: list[sqlite3.Row] = []
    doc_counts: dict[str, int] = {}
    breadcrumb_counts: dict[str, int] = {} # 章节级去重
    
    for row, score in ranked_chunks:
        row_dict = dict(row)
        doc_id = str(row_dict.get("parent_file", ""))
        bc = str(row_dict.get("breadcrumb") or "")
        
        # 1. 文档级配额限制
        if doc_id and doc_counts.get(doc_id, 0) >= max_per_doc:
            # 即使分数再高，如果该文件已经占了太多坑位，也给其他文件让路
            continue
            
        # 2. 章节级去重（防止同一个小节的内容复读）
        if bc and breadcrumb_counts.get(doc_id + bc, 0) >= 2:
            continue
            
        selected.append(row)
        doc_counts[doc_id] = doc_counts.get(doc_id, 0) + 1
        breadcrumb_counts[doc_id + bc] = breadcrumb_counts.get(doc_id + bc, 0) + 1
        
        if len(selected) >= limit:
            break
            
    return selected



def search_chunks(query: str, limit: int = 20, db_path: Path | None = None) -> list[sqlite3.Row]:
    if not query or not str(query).strip():
        return []
    q = str(query).strip()

    # 1. 提取核心关键词与加权词
    core_phrases = re.findall(r"[\u4e00-\u9fff]{2,}", q)
    boost_words = set()
    try:
        terms_path = os.getenv("WIKICODER_BUSINESS_TERMS", "./data/dictionaries/business_terms.yaml")
        from src.core.query_rewriter import load_business_terms
        dynamic_terms = load_business_terms(terms_path)
        if dynamic_terms:
            boost_words.update(dynamic_terms)
    except Exception:
        pass
    
    if not boost_words:
        boost_words = {"结算", "标准", "规则", "费用", "纪要", "2024", "采购", "结算单", "明细", "报账", "规范", "分册"}

    # 2. 缓存同义词加载 (单例模式)
    if not hasattr(search_chunks, "_syno_cache"):
        syno_cache = {}
        try:
            from src.utils.config import load_config
            config = load_config()
            strategy = getattr(config, "wiki_strategy", None)
            if strategy is None:
                try: strategy = config.get("wiki_strategy", {})
                except: strategy = {}
            
            syno_path_str = getattr(strategy, "synonyms_path", "./data/dictionaries/synonyms_zh.yaml") if hasattr(strategy, "synonyms_path") else strategy.get("synonyms_path", "./data/dictionaries/synonyms_zh.yaml")
            syno_path = Path(syno_path_str)
            
            if syno_path.exists():
                import yaml
                with syno_path.open("r", encoding="utf-8") as f:
                    raw_syno = yaml.safe_load(f)
                    if raw_syno and "synonyms" in raw_syno:
                        for entry in raw_syno["synonyms"]:
                            words = entry.get("terms", [])
                            for w in words:
                                syno_cache[w.lower()] = words
        except Exception: 
            pass
        search_chunks._syno_cache = syno_cache

    # 3. 构造检索令牌 (包含同义词扩展)
    tokens = _tokenize_query(q)
    expanded_tokens = list(tokens)
    for t in tokens:
        syns = search_chunks._syno_cache.get(t.lower())
        if syns:
            expanded_tokens.extend(syns)
    tokens = list(set(expanded_tokens))
    if not tokens: tokens = [q]

    # 4. 执行 FTS 检索与分值计算
    scored_rows: list[tuple[sqlite3.Row, float]] = []
    with get_conn(db_path) as conn:
        try:
            fts_query = " OR ".join([f'"{t.replace("\"", "")}"' for t in tokens[:15]])
            fts_rows = conn.execute(
                """
                SELECT c.chunk_id, c.title, c.parent_file, c.raw_file_path, c.breadcrumb, c.tags, c.content_path, c.content_text, c.last_modified,
                       bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
                WHERE chunks_fts MATCH ?
                ORDER BY rank ASC
                LIMIT ?
                """,
                (fts_query, max(limit * 8, 80)),
            ).fetchall()

            for i, r in enumerate(fts_rows):
                score = 1000.0 - float(i)
                text_low = f"{r['title']} {r['content_text']} {r['parent_file']} {r['breadcrumb']}".lower()
                for ph in core_phrases:
                    if ph.lower() in text_low: 
                        score += 700.0
                
                # 业务词路径奖励
                query_low = q.lower()
                is_resp = any(k in query_low for k in ["负责", "维护", "谁干", "专业", "归属"])
                for bw in boost_words:
                    if bw.lower() in query_low or is_resp:
                        if bw.lower() in str(r['parent_file']).lower():
                            score += 10000.0
                scored_rows.append((r, score))
        except Exception: 
            pass

    return _merge_rows_by_score(scored_rows, limit=limit)



def get_chunk_by_id(chunk_id: str, db_path: Path | None = None) -> sqlite3.Row | None:
    with get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT chunk_id, title, parent_file, raw_file_path, breadcrumb, tags, content_path, content_text, last_modified
            FROM chunks
            WHERE chunk_id = ?
            LIMIT 1
            """,
            (chunk_id,),
        ).fetchone()
    return row



def list_structure(db_path: Path | None = None) -> list[tuple[str, int]]:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            """
            SELECT parent_file, COUNT(*) AS chunk_count
            FROM chunks
            GROUP BY parent_file
            ORDER BY parent_file ASC
            """
        ).fetchall()
    return [(r["parent_file"], r["chunk_count"]) for r in rows]


def clear_index_store(processed_path: Path | None = None) -> list[str]:
    """Clear local wiki index artifacts (db/chunks)."""
    messages: list[str] = []

    # 1) clear chunks directory (prefer configured processed_path)
    if processed_path is None:
        chunks_dir = PROJECT_ROOT / "data" / "wiki_processed" / "chunks"
    else:
        chunks_dir = Path(processed_path) / "chunks"
    if chunks_dir.exists():
        for child in chunks_dir.iterdir():
            try:
                if child.is_file():
                    child.unlink()
                elif child.is_dir():
                    shutil.rmtree(child, ignore_errors=True)
            except Exception as e:  # noqa: BLE001
                # fallback for locked file: truncate content
                if child.is_file():
                    try:
                        child.write_text("", encoding="utf-8")
                        messages.append(f"Truncated locked chunk: {child}")
                        continue
                    except Exception:
                        pass
                messages.append(f"Failed removing {child}: {e}")
        messages.append(f"Cleared chunks: {chunks_dir}")
    else:
        messages.append(f"Chunks dir not found: {chunks_dir}")

    # 1.5) clear incremental sync state
    if processed_path is None:
        state_file = PROJECT_ROOT / "data" / "wiki_processed" / "sync_state.json"
    else:
        state_file = Path(processed_path) / "sync_state.json"

    if state_file.exists():
        try:
            state_file.unlink()
        except Exception:
            try:
                state_file.write_text(json.dumps({"version": 1, "files": {}}, ensure_ascii=False), encoding="utf-8")
            except Exception: pass

    # 2) clear known sqlite files
    candidates = {DB_PATH}
    if processed_path is not None:
        candidates.add(Path(processed_path) / "wiki.db")
    
    for base in candidates:
        if base.exists():
            try:
                with get_conn(base) as conn:
                    conn.execute("DELETE FROM chunks_fts")
                    conn.execute("DELETE FROM chunks")
                    conn.commit()
                messages.append(f"Cleared rows in database: {base.name}")
            except Exception: pass
        
        for p in base.parent.glob(f"{base.stem}.sqlite*"):
            try:
                p.unlink()
            except Exception: pass

    messages.append("✅ 知识库索引已完成全量静默清理。")

    # reset resolved path so next use can re-detect
    global _resolved_db_path
    _resolved_db_path = None

    return messages
