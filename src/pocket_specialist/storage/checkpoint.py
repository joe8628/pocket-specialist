"""SQLite-backed checkpoint store shared by pipeline execution steps."""
from __future__ import annotations

import sqlite3
import time

from pocket_specialist.core.config import DB_PATH, MAX_RETRIES

STAGES = ("render", "ocr", "layout", "equations", "correction", "structured")
_STAGES = STAGES  # backward-compat alias

_PATH_COL: dict[str, str] = {
    "render": "png_path",
    "ocr": "json_path",
    "layout": "json_path",
    "equations": "json_path",
    "correction": "md_path",
    "structured": "json_path",
}


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(DB_PATH))


def _create_stage_table(conn: sqlite3.Connection, stage: str, table_name: str | None = None) -> None:
    path_col = _PATH_COL[stage]
    target = table_name or stage
    conn.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {target} (
            document TEXT NOT NULL,
            page     INTEGER NOT NULL,
            {path_col} TEXT,
            status   TEXT NOT NULL,
            attempts INTEGER DEFAULT 0,
            ts       REAL,
            PRIMARY KEY (document, page)
        )
        """
    )


def _migrate_stage_table(conn: sqlite3.Connection, stage: str) -> None:
    info = conn.execute(f"PRAGMA table_info({stage})").fetchall()
    if not info:
        _create_stage_table(conn, stage)
        return

    columns = {row[1] for row in info}
    if "document" in columns:
        return

    legacy_table = f"{stage}_legacy"
    conn.execute(f"ALTER TABLE {stage} RENAME TO {legacy_table}")
    _create_stage_table(conn, stage)

    path_col = _PATH_COL[stage]
    legacy_columns = {row[1] for row in conn.execute(f"PRAGMA table_info({legacy_table})").fetchall()}
    if path_col in legacy_columns:
        conn.execute(
            f"""
            INSERT INTO {stage} (document, page, {path_col}, status, attempts, ts)
            SELECT '__legacy__', page, {path_col}, status, attempts, ts
            FROM {legacy_table}
            """
        )
    conn.execute(f"DROP TABLE {legacy_table}")


def init_db() -> None:
    """Create checkpoint tables if they don't exist. Idempotent."""
    with _connect() as conn:
        for stage in _STAGES:
            _migrate_stage_table(conn, stage)
        conn.commit()


def get_status(stage: str, document: str, page: int) -> tuple[str | None, int]:
    """Return (status, attempts). status is None if the page has no record."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT status, attempts FROM {stage} WHERE document = ? AND page = ?",
            (document, page),
        ).fetchone()
    return (row[0], row[1]) if row else (None, 0)


def set_status(stage: str, document: str, page: int, status: str, path: str | None = None) -> None:
    """Upsert a page record. Increments attempts on terminal states."""
    path_col = _PATH_COL[stage]
    with _connect() as conn:
        existing = conn.execute(
            f"SELECT attempts FROM {stage} WHERE document = ? AND page = ?",
            (document, page),
        ).fetchone()
        prev_attempts = existing[0] if existing else 0
        attempts = prev_attempts + (1 if status in ("done", "failed") else 0)
        conn.execute(
            f"""
            INSERT INTO {stage} (document, page, {path_col}, status, attempts, ts)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(document, page) DO UPDATE SET
                {path_col} = excluded.{path_col},
                status = excluded.status,
                attempts = excluded.attempts,
                ts = excluded.ts
            """,
            (document, page, path, status, attempts, time.time()),
        )
        conn.commit()


def should_process(stage: str, document: str, page: int) -> bool:
    """True if the page should be processed (not done and within retry limit)."""
    status, attempts = get_status(stage, document, page)
    if status == "done":
        return False
    if status == "failed" and attempts >= MAX_RETRIES:
        return False
    return True


def get_summary(stage: str, document: str | None = None) -> dict[str, int]:
    """Return {status: count} for a pipeline step, optionally scoped to one document."""
    with _connect() as conn:
        if document is None:
            rows = conn.execute(
                f"SELECT status, COUNT(*) FROM {stage} GROUP BY status"
            ).fetchall()
        else:
            rows = conn.execute(
                f"SELECT status, COUNT(*) FROM {stage} WHERE document = ? GROUP BY status",
                (document,),
            ).fetchall()
    return {row[0]: row[1] for row in rows}


def get_failed_pages(stage: str, document: str) -> list[int]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT page FROM {stage} WHERE document = ? AND status = 'failed' ORDER BY page",
            (document,),
        ).fetchall()
    return [row[0] for row in rows]


def get_done_pages(stage: str, document: str) -> list[int]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT page FROM {stage} WHERE document = ? AND status = 'done' ORDER BY page",
            (document,),
        ).fetchall()
    return [row[0] for row in rows]


def get_documents() -> list[str]:
    docs: set[str] = set()
    with _connect() as conn:
        for stage in _STAGES:
            rows = conn.execute(f"SELECT DISTINCT document FROM {stage}").fetchall()
            docs.update(row[0] for row in rows if row[0] and row[0] != "__legacy__")
    return sorted(docs)


def reset_stage(stage: str, document: str | None = None) -> None:
    with _connect() as conn:
        if document is None:
            conn.execute(f"DELETE FROM {stage}")
        else:
            conn.execute(f"DELETE FROM {stage} WHERE document = ?", (document,))
        conn.commit()
