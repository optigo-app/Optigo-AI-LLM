import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOG_DIR = PROJECT_ROOT / "logs"
_table_ready = False


def _db_path() -> str:
    os.makedirs(_LOG_DIR, exist_ok=True)
    return str(_LOG_DIR / "conversations.db")


def _get_conn() -> sqlite3.Connection:
    """Open a SQLite connection with WAL mode and a busy timeout for concurrency."""
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_conversations_session
            ON conversations (session_id, created_at)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS session_context (
                session_id TEXT PRIMARY KEY,
                report_key TEXT,
                updated_at TEXT NOT NULL
            )
            """
        )
        try:
            conn.execute("ALTER TABLE session_context ADD COLUMN filters_json TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE session_context ADD COLUMN metric TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE session_context ADD COLUMN dimension TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE session_context ADD COLUMN resolved_question TEXT")
        except sqlite3.OperationalError:
            pass
    _table_ready = True


def new_session_id() -> str:
    return uuid.uuid4().hex[:16]


def get_messages(session_id: Optional[str], limit: int = 10) -> List[Dict[str, str]]:
    if not session_id:
        return []
    limit = max(1, min(int(limit), 100))
    _ensure_table()
    with _get_conn() as conn:
        rows = conn.execute(
            """
            SELECT role, content FROM conversations
            WHERE session_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (session_id, limit),
        ).fetchall()
    return [{"role": r[0], "content": r[1]} for r in reversed(rows)]


def add_message(session_id: Optional[str], role: str, content: str) -> Optional[str]:
    if not session_id:
        return None
    if not role or not content:
        return None
    _ensure_table()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO conversations (session_id, role, content, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                session_id,
                role,
                content,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    return session_id


def get_last_report(session_id: Optional[str]) -> Optional[str]:
    if not session_id:
        return None
    _ensure_table()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT report_key FROM session_context WHERE session_id = ?", (session_id,)
        ).fetchone()
    return row[0] if row and row[0] else None


def set_last_report(session_id: Optional[str], report_key: str) -> None:
    if not session_id:
        return
    _ensure_table()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO session_context (session_id, report_key, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                report_key = excluded.report_key,
                updated_at = excluded.updated_at
            """,
            (session_id, report_key, datetime.now(timezone.utc).isoformat()),
        )


def get_last_filters(session_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not session_id:
        return None
    _ensure_table()
    with _get_conn() as conn:
        row = conn.execute(
            "SELECT filters_json FROM session_context WHERE session_id = ?", (session_id,)
        ).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return None


def set_last_filters(session_id: Optional[str], filters: Optional[Dict[str, Any]]) -> None:
    if not session_id:
        return
    _ensure_table()
    filters_json = json.dumps(filters or {}, default=str)
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO session_context (session_id, report_key, filters_json, updated_at)
            VALUES (?, '', ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                filters_json = excluded.filters_json,
                updated_at = excluded.updated_at
            """,
            (session_id, filters_json, now),
        )


def set_last_intent(
    session_id: Optional[str],
    report_key: str = "",
    metric: str = "",
    dimension: str = "",
    resolved_question: str = "",
) -> None:
    """Store the last parsed intent for context resolution.

    Called after a successful parse so the next follow-up question can
    resolve against the last report/metric/dimension even if the LLM
    rewrite fails.
    """
    if not session_id:
        return
    _ensure_table()
    now = datetime.now(timezone.utc).isoformat()
    with _get_conn() as conn:
        conn.execute(
            """
            INSERT INTO session_context
                (session_id, report_key, metric, dimension, resolved_question, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(session_id) DO UPDATE SET
                report_key = excluded.report_key,
                metric = excluded.metric,
                dimension = excluded.dimension,
                resolved_question = excluded.resolved_question,
                updated_at = excluded.updated_at
            """,
            (session_id, report_key, metric, dimension, resolved_question, now),
        )


def get_last_intent(session_id: Optional[str]) -> Dict[str, str]:
    """Retrieve last intent for fallback context resolution.

    Returns dict with keys: report_key, metric, dimension, resolved_question.
    Values are empty strings if not set.
    """
    if not session_id:
        return {"report_key": "", "metric": "", "dimension": "", "resolved_question": ""}
    _ensure_table()
    with _get_conn() as conn:
        row = conn.execute(
            """
            SELECT report_key, metric, dimension, resolved_question
            FROM session_context WHERE session_id = ?
            """,
            (session_id,),
        ).fetchone()
    if not row:
        return {"report_key": "", "metric": "", "dimension": "", "resolved_question": ""}
    return {
        "report_key": row[0] or "",
        "metric": row[1] or "",
        "dimension": row[2] or "",
        "resolved_question": row[3] or "",
    }
