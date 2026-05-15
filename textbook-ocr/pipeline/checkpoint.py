"""SQLite-backed checkpoint store shared by all pipeline stages."""
import sqlite3
import time
from pathlib import Path

from config import DB_PATH, MAX_RETRIES

STAGES  = ("render", "ocr", "equations", "correction")
_STAGES = STAGES  # backward-compat alias

_PATH_COL: dict[str, str] = {
    "render":     "png_path",
    "ocr":        "json_path",
    "equations":  "json_path",
    "correction": "md_path",
}


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    return sqlite3.connect(str(DB_PATH))


def init_db() -> None:
    """Create checkpoint tables if they don't exist. Idempotent."""
    with _connect() as conn:
        for stage in _STAGES:
            path_col = _PATH_COL[stage]
            conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {stage} (
                    page     INTEGER PRIMARY KEY,
                    {path_col} TEXT,
                    status   TEXT NOT NULL,
                    attempts INTEGER DEFAULT 0,
                    ts       REAL
                )
            """)
        conn.commit()


def get_status(stage: str, page: int) -> tuple[str | None, int]:
    """Return (status, attempts). status is None if the page has no record."""
    with _connect() as conn:
        row = conn.execute(
            f"SELECT status, attempts FROM {stage} WHERE page = ?", (page,)
        ).fetchone()
    return (row[0], row[1]) if row else (None, 0)


def set_status(stage: str, page: int, status: str, path: str | None = None) -> None:
    """Upsert a page record. Increments attempts on terminal states."""
    path_col = _PATH_COL[stage]
    with _connect() as conn:
        existing = conn.execute(
            f"SELECT attempts FROM {stage} WHERE page = ?", (page,)
        ).fetchone()
        prev_attempts = existing[0] if existing else 0
        attempts = prev_attempts + (1 if status in ("done", "failed") else 0)
        conn.execute(
            f"""INSERT INTO {stage} (page, {path_col}, status, attempts, ts)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(page) DO UPDATE SET
                    {path_col} = excluded.{path_col},
                    status     = excluded.status,
                    attempts   = excluded.attempts,
                    ts         = excluded.ts""",
            (page, path, status, attempts, time.time()),
        )
        conn.commit()


def should_process(stage: str, page: int) -> bool:
    """True if the page should be processed (not done and within retry limit)."""
    status, attempts = get_status(stage, page)
    if status == "done":
        return False
    if status == "failed" and attempts >= MAX_RETRIES:
        return False
    return True


def get_summary(stage: str) -> dict[str, int]:
    """Return {status: count} for a stage."""
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT status, COUNT(*) FROM {stage} GROUP BY status"
        ).fetchall()
    return {row[0]: row[1] for row in rows}


def get_failed_pages(stage: str) -> list[int]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT page FROM {stage} WHERE status = 'failed' ORDER BY page"
        ).fetchall()
    return [row[0] for row in rows]


def get_done_pages(stage: str) -> list[int]:
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT page FROM {stage} WHERE status = 'done' ORDER BY page"
        ).fetchall()
    return [row[0] for row in rows]


def reset_stage(stage: str) -> None:
    with _connect() as conn:
        conn.execute(f"DELETE FROM {stage}")
        conn.commit()
