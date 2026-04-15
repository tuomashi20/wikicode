from __future__ import annotations

import os
import re
import shutil
import sqlite3
from pathlib import Path

from .config import PROJECT_ROOT


PREFERRED_DB_PATH = PROJECT_ROOT / "data" / "wiki_processed" / "db.sqlite"
FALLBACK_DB_PATH = Path.home() / ".codex" / "memories" / "wikicoder" / "db.sqlite"
DB_PATH = Path(os.getenv("WIKICODER_DB_PATH", str(PREFERRED_DB_PATH)))


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS chunks (
  chunk_id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  parent_file TEXT NOT NULL,
  raw_file_path TEXT NOT NULL,
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
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("SELECT 1")
    return conn



def _ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_SQL)

    cols = {r["name"] for r in conn.execute("PRAGMA table_info(chunks)").fetchall()}
    if "content_text" not in cols:
        conn.execute("ALTER TABLE chunks ADD COLUMN content_text TEXT DEFAULT ''")

    # backfill fts from chunks
    conn.execute("DELETE FROM chunks_fts")
    conn.execute(
        """
        INSERT INTO chunks_fts (chunk_id, title, tags, parent_file, content_text)
        SELECT chunk_id, title, tags, parent_file, content_text
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

    candidates = [DB_PATH]
    if DB_PATH != FALLBACK_DB_PATH:
        candidates.append(FALLBACK_DB_PATH)

    last_error: Exception | None = None
    for p in candidates:
        try:
            _init_schema(p)
            _resolved_db_path = p
            return p
        except Exception as e:  # noqa: BLE001
            last_error = e

    raise RuntimeError(f"Unable to open sqlite database at candidates={candidates}") from last_error



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
    content_text: str,
) -> None:
    conn.execute("DELETE FROM chunks_fts WHERE chunk_id = ?", (chunk_id,))
    conn.execute(
        """
        INSERT INTO chunks_fts (chunk_id, title, tags, parent_file, content_text)
        VALUES (?, ?, ?, ?, ?)
        """,
        (chunk_id, title, tags, parent_file, content_text),
    )



def upsert_chunk(
    chunk_id: str,
    title: str,
    parent_file: str,
    raw_file_path: str,
    tags: str,
    content_path: str,
    content_text: str,
    last_modified: str,
    db_path: Path | None = None,
) -> None:
    sql = """
    INSERT INTO chunks (chunk_id, title, parent_file, raw_file_path, tags, content_path, content_text, last_modified)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(chunk_id) DO UPDATE SET
      title=excluded.title,
      parent_file=excluded.parent_file,
      raw_file_path=excluded.raw_file_path,
      tags=excluded.tags,
      content_path=excluded.content_path,
      content_text=excluded.content_text,
      last_modified=excluded.last_modified
    """
    with get_conn(db_path) as conn:
        conn.execute(sql, (chunk_id, title, parent_file, raw_file_path, tags, content_path, content_text, last_modified))
        _fts_upsert(
            conn,
            chunk_id=chunk_id,
            title=title,
            tags=tags,
            parent_file=parent_file,
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
    scored: dict[str, tuple[sqlite3.Row, float]] = {}
    for row, score in rows_with_score:
        cid = str(row["chunk_id"])
        old = scored.get(cid)
        if old is None or score > old[1]:
            scored[cid] = (row, score)

    ordered = sorted(scored.values(), key=lambda x: x[1], reverse=True)
    return [r for r, _ in ordered[:limit]]



def search_chunks(query: str, limit: int = 20, db_path: Path | None = None) -> list[sqlite3.Row]:
    q = query.strip()
    if not q:
        return []

    tokens = _tokenize_query(q)
    if not tokens:
        tokens = [q]

    with get_conn(db_path) as conn:
        scored_rows: list[tuple[sqlite3.Row, float]] = []

        # 1) FTS retrieval
        try:
            fts_query = " OR ".join([f'"{t.replace("\"", "")}"' for t in tokens[:8]])
            fts_rows = conn.execute(
                """
                SELECT c.chunk_id, c.title, c.parent_file, c.raw_file_path, c.tags, c.content_path, c.content_text, c.last_modified,
                       bm25(chunks_fts) AS rank
                FROM chunks_fts
                JOIN chunks c ON c.chunk_id = chunks_fts.chunk_id
                WHERE chunks_fts MATCH ?
                ORDER BY rank ASC
                LIMIT ?
                """,
                (fts_query, max(limit * 3, 30)),
            ).fetchall()
            for i, r in enumerate(fts_rows):
                # rank smaller is better
                score = 1000.0 - float(i)
                scored_rows.append((r, score))
        except Exception:
            pass

        # 2) LIKE retrieval over title/tags/parent/content_text
        like_sql = """
        SELECT chunk_id, title, parent_file, raw_file_path, tags, content_path, content_text, last_modified
        FROM chunks
        WHERE title LIKE ? OR tags LIKE ? OR parent_file LIKE ? OR content_text LIKE ?
        LIMIT ?
        """
        for idx, t in enumerate(tokens[:12]):
            like = f"%{t}%"
            rows = conn.execute(like_sql, (like, like, like, like, max(limit * 2, 20))).fetchall()
            for r in rows:
                score = 200.0 - (idx * 2)
                if str(r["title"]).lower().find(t) >= 0:
                    score += 30
                if str(r["tags"]).lower().find(t) >= 0:
                    score += 20
                if str(r["content_text"]).lower().find(t) >= 0:
                    score += 8
                scored_rows.append((r, score))

        merged = _merge_rows_by_score(scored_rows, limit=limit)
        if merged:
            return merged

        # 3) Robust fallback: scan chunk markdown files directly.
        # This keeps recall when old data lacks content_text/tags or schema migration wasn't fully rebuilt yet.
        rows_all = conn.execute(
            """
            SELECT chunk_id, title, parent_file, raw_file_path, tags, content_path, content_text, last_modified
            FROM chunks
            """
        ).fetchall()

    scored_fallback: list[tuple[sqlite3.Row, float]] = []
    for r in rows_all:
        try:
            content_text = str(r["content_text"] or "")
            if not content_text and r["content_path"]:
                cp = str(r["content_path"])
                p = Path(cp) if Path(cp).is_absolute() else (PROJECT_ROOT / cp)
                if p.exists():
                    content_text = p.read_text(encoding="utf-8", errors="ignore")
            hay = f"{r['title']} {r['tags']} {r['parent_file']} {content_text}".lower()
            score = 0.0
            for t in tokens[:16]:
                c = hay.count(t)
                if c > 0:
                    score += c * 3.0
                    if str(r["title"]).lower().find(t) >= 0:
                        score += 10.0
                    if str(r["tags"]).lower().find(t) >= 0:
                        score += 6.0
            if score > 0:
                scored_fallback.append((r, score))
        except Exception:
            continue

    return _merge_rows_by_score(scored_fallback, limit=limit)



def get_chunk_by_id(chunk_id: str, db_path: Path | None = None) -> sqlite3.Row | None:
    with get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT chunk_id, title, parent_file, raw_file_path, tags, content_path, content_text, last_modified
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

    # 2) clear known sqlite files (prefer configured processed_path db first)
    candidates = {PREFERRED_DB_PATH, FALLBACK_DB_PATH}
    if processed_path is not None:
        candidates.add(Path(processed_path) / "db.sqlite")
    try:
        candidates.add(resolve_db_path())
    except Exception:
        pass

    for base in candidates:
        removed_any = False
        for p in base.parent.glob(f"{base.stem}.sqlite*"):
            try:
                p.unlink()
                messages.append(f"Removed: {p}")
                removed_any = True
            except Exception as e:  # noqa: BLE001
                messages.append(f"Failed removing {p}: {e}")
        # fallback when db file is locked: clear rows in-place
        if (not removed_any) and base.exists():
            try:
                with get_conn(base) as conn:
                    conn.execute("DELETE FROM chunks_fts")
                    conn.execute("DELETE FROM chunks")
                    conn.commit()
                messages.append(f"Cleared rows in locked db: {base}")
            except Exception as e:  # noqa: BLE001
                messages.append(f"Failed clearing rows in {base}: {e}")

    # reset resolved path so next use can re-detect
    global _resolved_db_path
    _resolved_db_path = None

    return messages
