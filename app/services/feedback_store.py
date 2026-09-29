"""User feedback store — thumbs up/down on chat answers.

Stores feedback in SQLite so we can:
- Identify questions where the LLM answer was wrong/unhelpful
- Track accuracy per report/metric over time
- Build a training dataset for prompt tuning / fine-tuning

Schema:
    feedback(id, session_id, question, answer, report_key, metric,
             rating, comment, company_code, user_id, created_at)
    rating: "up" (correct/helpful) or "down" (wrong/unhelpful)
"""

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOG_DIR = _PROJECT_ROOT / "logs"
_table_ready = False
_FAILURE_REASONS = {
    "wrong_intent", "wrong_filter", "wrong_date", "wrong_data", "incomplete_result",
    "wrong_business_definition", "slow_response", "irrelevant_answer",
}


def _db_path() -> str:
    os.makedirs(_LOG_DIR, exist_ok=True)
    return str(_LOG_DIR / "feedback.db")


@contextmanager
def _get_conn():
    """Open a SQLite connection with WAL mode and close it after each operation."""
    conn = sqlite3.connect(_db_path(), timeout=30)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _ensure_table() -> None:
    global _table_ready
    if _table_ready:
        return
    with _get_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '',
                report_key TEXT NOT NULL DEFAULT '',
                metric TEXT NOT NULL DEFAULT '',
                rating TEXT NOT NULL,
                comment TEXT NOT NULL DEFAULT '',
                company_code TEXT NOT NULL DEFAULT '',
                user_id TEXT NOT NULL DEFAULT '',
                question_hash TEXT NOT NULL DEFAULT '',
                corrected_metric TEXT NOT NULL DEFAULT '',
                failure_reason TEXT NOT NULL DEFAULT '',
                query_plan TEXT NOT NULL DEFAULT '{}',
                confidence REAL,
                latency_ms REAL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_rating ON feedback (rating)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_report ON feedback (report_key)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_feedback_session ON feedback (session_id)"
        )
        # Add corrected_metric column if upgrading from older schema
        migrations = (
            "ALTER TABLE feedback ADD COLUMN corrected_metric TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE feedback ADD COLUMN failure_reason TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE feedback ADD COLUMN query_plan TEXT NOT NULL DEFAULT '{}'",
            "ALTER TABLE feedback ADD COLUMN confidence REAL",
            "ALTER TABLE feedback ADD COLUMN latency_ms REAL",
        )
        for statement in migrations:
            try:
                conn.execute(statement)
            except sqlite3.OperationalError:
                pass
    _table_ready = True


def _hash_question(question: str) -> str:
    """Normalize and hash a question for dedup detection."""
    import hashlib
    normalized = " ".join(question.lower().strip().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:16]


def add_feedback(
    session_id: str,
    question: str,
    answer: str = "",
    report_key: str = "",
    metric: str = "",
    rating: str = "",
    comment: str = "",
    company_code: str = "",
    user_id: str = "",
    corrected_metric: str = "",
    failure_reason: str = "",
    query_plan: Optional[Dict[str, Any]] = None,
    confidence: Optional[float] = None,
    latency_ms: Optional[float] = None,
) -> int:
    """Store a feedback entry. Returns the row id.

    rating must be "up" or "down".
    corrected_metric: when a user down-votes because the wrong metric was used,
    they can suggest the correct metric name from the catalog.
    """
    if rating not in ("up", "down"):
        raise ValueError(f"rating must be 'up' or 'down', got: {rating!r}")
    if failure_reason and failure_reason not in _FAILURE_REASONS:
        raise ValueError(f"unsupported failure_reason: {failure_reason!r}")

    _ensure_table()
    qhash = _hash_question(question)
    now = datetime.now(timezone.utc).isoformat()

    with _get_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO feedback
                (session_id, question, answer, report_key, metric, rating,
                 comment, company_code, user_id, question_hash, corrected_metric,
                 failure_reason, query_plan, confidence, latency_ms, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (session_id, question, answer, report_key, metric, rating,
             comment, company_code, user_id, qhash, corrected_metric,
             failure_reason, json.dumps(query_plan or {}, default=str), confidence, latency_ms, now),
        )
        feedback_id = cur.lastrowid or 0
    if rating == "up" and query_plan:
        from app.services.verified_queries import upsert_candidate
        upsert_candidate(question, report_key, query_plan)
    return feedback_id


def get_feedback_stats(
    report_key: str = "",
    days: int = 30,
) -> Dict[str, Any]:
    """Aggregate feedback stats for analytics.

    Returns:
        {
            "total": int,
            "up": int,
            "down": int,
            "up_pct": float,
            "down_pct": float,
            "by_report": {report_key: {up, down}},
            "recent_down": [{question, answer, report_key, metric, comment, created_at}],
        }
    """
    # Validate inputs
    days = max(1, min(int(days), 365))
    _ensure_table()
    cutoff = datetime.now(timezone.utc)
    from datetime import timedelta
    cutoff_str = (cutoff - timedelta(days=days)).isoformat()

    with _get_conn() as conn:
        base_where = "WHERE created_at >= ?"
        params: list = [cutoff_str]

        if report_key:
            base_where += " AND report_key = ?"
            params.append(report_key)

        row = conn.execute(
            f"SELECT rating, COUNT(*) FROM feedback {base_where} GROUP BY rating",
            params,
        ).fetchall()

        up_count = 0
        down_count = 0
        for r in row:
            if r[0] == "up":
                up_count = r[1]
            elif r[0] == "down":
                down_count = r[1]

        total = up_count + down_count

        # Per-report breakdown
        report_rows = conn.execute(
            f"""
            SELECT report_key, rating, COUNT(*)
            FROM feedback {base_where}
            GROUP BY report_key, rating
            """,
            params,
        ).fetchall()

        by_report: Dict[str, Dict[str, int]] = {}
        for rk, rating, cnt in report_rows:
            key = rk or "unknown"
            if key not in by_report:
                by_report[key] = {"up": 0, "down": 0}
            by_report[key][rating] = cnt

        # Recent down-votes (for prompt tuning review)
        down_rows = conn.execute(
            f"""
            SELECT question, answer, report_key, metric, comment, created_at
            FROM feedback {base_where} AND rating = 'down'
            ORDER BY created_at DESC
            LIMIT 50
            """,
            params,
        ).fetchall()

        recent_down = [
            {
                "question": r[0],
                "answer": r[1][:200] if r[1] else "",
                "report_key": r[2],
                "metric": r[3],
                "comment": r[4],
                "created_at": r[5],
            }
            for r in down_rows
        ]

    return {
        "total": total,
        "up": up_count,
        "down": down_count,
        "up_pct": round(up_count / total * 100, 1) if total else 0.0,
        "down_pct": round(down_count / total * 100, 1) if total else 0.0,
        "by_report": by_report,
        "recent_down": recent_down,
    }


def get_down_feedback(
    report_key: str = "",
    limit: int = 100,
) -> List[Dict[str, Any]]:
    """Get all down-voted feedback for prompt tuning / retraining analysis."""
    limit = max(1, min(int(limit), 1000))
    _ensure_table()
    with _get_conn() as conn:
        if report_key:
            rows = conn.execute(
                """
                SELECT question, answer, report_key, metric, comment,
                       company_code, user_id, session_id, corrected_metric, created_at
                FROM feedback WHERE rating = 'down' AND report_key = ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (report_key, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT question, answer, report_key, metric, comment,
                       company_code, user_id, session_id, corrected_metric, created_at
                FROM feedback WHERE rating = 'down'
                ORDER BY created_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()

    return [
        {
            "question": r[0],
            "answer": r[1],
            "report_key": r[2],
            "metric": r[3],
            "comment": r[4],
            "company_code": r[5],
            "user_id": r[6],
            "session_id": r[7],
            "corrected_metric": r[8],
            "created_at": r[9],
        }
        for r in rows
    ]

