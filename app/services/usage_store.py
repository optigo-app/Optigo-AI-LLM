import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

PROJECT_ROOT = Path(__file__).resolve().parents[2]
_LOG_DIR = PROJECT_ROOT / "logs"


def _db_path() -> str:
    os.makedirs(_LOG_DIR, exist_ok=True)
    return str(_LOG_DIR / "token_usage.db")


def _ensure_table() -> None:
    with sqlite3.connect(_db_path()) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS token_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                company_code TEXT NOT NULL,
                user_id TEXT NOT NULL,
                question TEXT,
                report_key TEXT,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                details TEXT
            )
            """
        )


def _summarize(token_usage: Dict[str, Any]) -> Dict[str, int]:
    calls = token_usage.get("calls", [])
    prompt = sum(u.get("prompt_tokens", 0) for u in calls)
    completion = sum(u.get("completion_tokens", 0) for u in calls)
    total = prompt + completion
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def build_token_usage(calls: Optional[list] = None) -> Dict[str, Any]:
    calls = calls or []
    totals = _summarize({"calls": calls})
    return {
        **totals,
        "calls": calls,
    }


def log_token_usage(
    endpoint: str,
    company_code: str,
    user_id: str,
    question: Optional[str],
    report_key: Optional[str],
    token_usage: Dict[str, Any],
) -> None:
    _ensure_table()
    totals = _summarize(token_usage)
    details = str(token_usage.get("calls", []))

    with sqlite3.connect(_db_path()) as conn:
        conn.execute(
            """
            INSERT INTO token_usage
            (created_at, endpoint, company_code, user_id, question, report_key,
             prompt_tokens, completion_tokens, total_tokens, details)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                endpoint,
                company_code,
                user_id,
                question,
                report_key,
                totals["prompt_tokens"],
                totals["completion_tokens"],
                totals["total_tokens"],
                details,
            ),
        )
