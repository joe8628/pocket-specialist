"""SQLite-backed checkpoint and DAG observation store."""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

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

TERMINAL_STATUSES = {"done", "failed", "skipped"}


@dataclass(frozen=True, slots=True)
class DAGNodeState:
    document: str
    page: int
    node: str
    status: str
    attempts: int
    path: str | None
    started_at: float | None
    finished_at: float | None
    metadata: dict[str, object]
    error: str | None


@dataclass(frozen=True, slots=True)
class DAGResumeState:
    document: str
    node: str
    last_done_page: int | None
    next_page: int | None
    done_pages: list[int]
    failed_pages: list[int]
    running_pages: list[int]


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _encode_metadata(metadata: dict[str, object] | None) -> str:
    return json.dumps(metadata or {}, sort_keys=True, default=str)


def _decode_metadata(value: str | None) -> dict[str, object]:
    if not value:
        return {}
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return {"raw": value}
    return decoded if isinstance(decoded, dict) else {"value": decoded}


def _validate_stage(stage: str) -> None:
    if stage not in _PATH_COL:
        raise ValueError(f"Unknown checkpoint stage: {stage}")


def _default_node_metadata(document: str, page: int, node: str) -> dict[str, object]:
    unit_id = f"{document}:page:{page}"
    return {
        "doc_id": document,
        "unit_id": unit_id,
        "task_id": f"{node}:{unit_id}",
        "task_type": node,
        "source_stage": node,
    }


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


def _create_dag_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dag_node_state (
            document    TEXT NOT NULL,
            page        INTEGER NOT NULL,
            node        TEXT NOT NULL,
            status      TEXT NOT NULL,
            path        TEXT,
            attempts    INTEGER DEFAULT 0,
            started_at  REAL,
            finished_at REAL,
            metadata    TEXT,
            error       TEXT,
            PRIMARY KEY (document, page, node)
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS dag_observations (
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            document  TEXT NOT NULL,
            page      INTEGER NOT NULL,
            node      TEXT NOT NULL,
            event     TEXT NOT NULL,
            status    TEXT NOT NULL,
            path      TEXT,
            metadata  TEXT,
            error     TEXT,
            ts        REAL NOT NULL
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dag_state_document_node ON dag_node_state(document, node)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dag_observations_document_node ON dag_observations(document, node, ts)")


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


def _record_node_state(
    conn: sqlite3.Connection,
    document: str,
    page: int,
    node: str,
    status: str,
    path: str | None = None,
    metadata: dict[str, object] | None = None,
    error: str | None = None,
    event: str | None = None,
    increment_attempts: bool = False,
) -> None:
    now = time.time()
    existing = conn.execute(
        """
        SELECT attempts, started_at, metadata FROM dag_node_state
        WHERE document = ? AND page = ? AND node = ?
        """,
        (document, page, node),
    ).fetchone()
    attempts = int(existing["attempts"]) if existing else 0
    if increment_attempts:
        attempts += 1
    started_at = now if status == "running" or existing is None else existing["started_at"]
    finished_at = now if status in TERMINAL_STATUSES else None
    previous_metadata = _decode_metadata(existing["metadata"]) if existing else {}
    merged_metadata = {**_default_node_metadata(document, page, node), **previous_metadata, **(metadata or {})}
    encoded_metadata = _encode_metadata(merged_metadata)

    conn.execute(
        """
        INSERT INTO dag_node_state (
            document, page, node, status, path, attempts, started_at, finished_at, metadata, error
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(document, page, node) DO UPDATE SET
            status = excluded.status,
            path = COALESCE(excluded.path, dag_node_state.path),
            attempts = excluded.attempts,
            started_at = COALESCE(dag_node_state.started_at, excluded.started_at),
            finished_at = excluded.finished_at,
            metadata = excluded.metadata,
            error = excluded.error
        """,
        (document, page, node, status, path, attempts, started_at, finished_at, encoded_metadata, error),
    )
    conn.execute(
        """
        INSERT INTO dag_observations (document, page, node, event, status, path, metadata, error, ts)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (document, page, node, event or status, status, path, encoded_metadata, error, now),
    )


def init_db() -> None:
    """Create checkpoint and DAG observation tables if they don't exist. Idempotent."""
    with _connect() as conn:
        for stage in _STAGES:
            _migrate_stage_table(conn, stage)
        _create_dag_tables(conn)
        conn.commit()


def get_status(stage: str, document: str, page: int) -> tuple[str | None, int]:
    """Return (status, attempts). status is None if the page has no record."""
    _validate_stage(stage)
    with _connect() as conn:
        row = conn.execute(
            f"SELECT status, attempts FROM {stage} WHERE document = ? AND page = ?",
            (document, page),
        ).fetchone()
    return (row[0], row[1]) if row else (None, 0)


def set_status(stage: str, document: str, page: int, status: str, path: str | None = None) -> None:
    """Upsert a stage checkpoint and mirror it into the DAG observation store."""
    _validate_stage(stage)
    path_col = _PATH_COL[stage]
    with _connect() as conn:
        _create_dag_tables(conn)
        existing = conn.execute(
            f"SELECT attempts FROM {stage} WHERE document = ? AND page = ?",
            (document, page),
        ).fetchone()
        prev_attempts = existing[0] if existing else 0
        attempts = prev_attempts + (1 if status in ("done", "failed") else 0)
        ts = time.time()
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
            (document, page, path, status, attempts, ts),
        )
        _record_node_state(
            conn,
            document=document,
            page=page,
            node=stage,
            status=status,
            path=path,
            metadata={"artifact_uri": path} if path else None,
            event="stage_status",
            increment_attempts=status in ("done", "failed"),
        )
        conn.commit()


def record_node_start(document: str, page: int, node: str, metadata: dict[str, object] | None = None) -> None:
    """Record that a DAG node began processing a page/document unit."""
    with _connect() as conn:
        _create_dag_tables(conn)
        _record_node_state(conn, document, page, node, "running", metadata=metadata, event="node_started")
        conn.commit()


def record_node_done(document: str, page: int, node: str, path: str | None = None, metadata: dict[str, object] | None = None) -> None:
    """Record successful completion of a DAG node for a page/document unit."""
    artifact_metadata = {"artifact_uri": path} if path else {}
    merged_metadata = {**artifact_metadata, **(metadata or {})}
    with _connect() as conn:
        _create_dag_tables(conn)
        _record_node_state(
            conn,
            document,
            page,
            node,
            "done",
            path=path,
            metadata=merged_metadata,
            event="node_done",
            increment_attempts=True,
        )
        conn.commit()


def record_node_failed(document: str, page: int, node: str, error: str, metadata: dict[str, object] | None = None) -> None:
    """Record a failed DAG node attempt for resume/retry decisions."""
    with _connect() as conn:
        _create_dag_tables(conn)
        _record_node_state(conn, document, page, node, "failed", metadata=metadata, error=error, event="node_failed", increment_attempts=True)
        conn.commit()


def get_node_state(document: str, page: int, node: str) -> DAGNodeState | None:
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT document, page, node, status, attempts, path, started_at, finished_at, metadata, error
            FROM dag_node_state
            WHERE document = ? AND page = ? AND node = ?
            """,
            (document, page, node),
        ).fetchone()
    if row is None:
        return None
    return DAGNodeState(
        document=row["document"],
        page=row["page"],
        node=row["node"],
        status=row["status"],
        attempts=row["attempts"],
        path=row["path"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        metadata=_decode_metadata(row["metadata"]),
        error=row["error"],
    )


def should_process_node(document: str, page: int, node: str, max_retries: int | None = None) -> bool:
    """True if a DAG node should run for the page/document unit."""
    retry_limit = MAX_RETRIES if max_retries is None else max_retries
    state = get_node_state(document, page, node)
    if state is None:
        return True
    if state.status == "done":
        return False
    if state.status == "failed" and state.attempts >= retry_limit:
        return False
    return True


def should_process(stage: str, document: str, page: int) -> bool:
    """True if the page should be processed (not done and within retry limit)."""
    _validate_stage(stage)
    status, attempts = get_status(stage, document, page)
    if status == "done":
        return False
    if status == "failed" and attempts >= MAX_RETRIES:
        return False
    return True


def get_summary(stage: str, document: str | None = None) -> dict[str, int]:
    """Return {status: count} for a pipeline step, optionally scoped to one document."""
    _validate_stage(stage)
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


def get_node_summary(document: str | None = None) -> dict[str, dict[str, int]]:
    """Return status counts keyed by DAG node."""
    with _connect() as conn:
        if document is None:
            rows = conn.execute(
                "SELECT node, status, COUNT(*) AS count FROM dag_node_state GROUP BY node, status"
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT node, status, COUNT(*) AS count FROM dag_node_state
                WHERE document = ? GROUP BY node, status
                """,
                (document,),
            ).fetchall()
    summary: dict[str, dict[str, int]] = {}
    for row in rows:
        summary.setdefault(row["node"], {})[row["status"]] = row["count"]
    return summary


def get_failed_pages(stage: str, document: str) -> list[int]:
    _validate_stage(stage)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT page FROM {stage} WHERE document = ? AND status = 'failed' ORDER BY page",
            (document,),
        ).fetchall()
    return [row[0] for row in rows]


def get_done_pages(stage: str, document: str) -> list[int]:
    _validate_stage(stage)
    with _connect() as conn:
        rows = conn.execute(
            f"SELECT page FROM {stage} WHERE document = ? AND status = 'done' ORDER BY page",
            (document,),
        ).fetchall()
    return [row[0] for row in rows]


def get_node_pages(document: str, node: str, status: str) -> list[int]:
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT page FROM dag_node_state
            WHERE document = ? AND node = ? AND status = ?
            ORDER BY page
            """,
            (document, node, status),
        ).fetchall()
    return [row["page"] for row in rows]


def get_resume_state(document: str, node: str, total_pages: int | None = None) -> DAGResumeState:
    """Return resume information for a document/node pair.

    If total_pages is provided, next_page is the first page that is not done and not
    retry-exhausted. Without total_pages, next_page is the first failed/running page
    when present, otherwise one page after the last contiguous done page.
    """
    done_pages = get_node_pages(document, node, "done")
    failed_pages = get_node_pages(document, node, "failed")
    running_pages = get_node_pages(document, node, "running")
    last_done = max(done_pages) if done_pages else None

    next_page: int | None = None
    if total_pages is not None:
        for page in range(1, total_pages + 1):
            if should_process_node(document, page, node):
                next_page = page
                break
    elif failed_pages or running_pages:
        next_page = min(failed_pages + running_pages)
    elif last_done is not None:
        next_page = last_done + 1

    return DAGResumeState(
        document=document,
        node=node,
        last_done_page=last_done,
        next_page=next_page,
        done_pages=done_pages,
        failed_pages=failed_pages,
        running_pages=running_pages,
    )


def get_documents() -> list[str]:
    docs: set[str] = set()
    with _connect() as conn:
        for stage in _STAGES:
            rows = conn.execute(f"SELECT DISTINCT document FROM {stage}").fetchall()
            docs.update(row[0] for row in rows if row[0] and row[0] != "__legacy__")
        info = conn.execute("PRAGMA table_info(dag_node_state)").fetchall()
        if info:
            rows = conn.execute("SELECT DISTINCT document FROM dag_node_state").fetchall()
            docs.update(row[0] for row in rows if row[0] and row[0] != "__legacy__")
    return sorted(docs)


def reset_stage(stage: str, document: str | None = None) -> None:
    _validate_stage(stage)
    with _connect() as conn:
        if document is None:
            conn.execute(f"DELETE FROM {stage}")
            conn.execute("DELETE FROM dag_node_state WHERE node = ?", (stage,))
            conn.execute("DELETE FROM dag_observations WHERE node = ?", (stage,))
        else:
            conn.execute(f"DELETE FROM {stage} WHERE document = ?", (document,))
            conn.execute("DELETE FROM dag_node_state WHERE document = ? AND node = ?", (document, stage))
            conn.execute("DELETE FROM dag_observations WHERE document = ? AND node = ?", (document, stage))
        conn.commit()


def reset_document_observations(document: str) -> None:
    with _connect() as conn:
        conn.execute("DELETE FROM dag_node_state WHERE document = ?", (document,))
        conn.execute("DELETE FROM dag_observations WHERE document = ?", (document,))
        conn.commit()
