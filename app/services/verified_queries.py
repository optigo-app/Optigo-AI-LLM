import json
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Dict, List

_ROOT = Path(__file__).resolve().parents[2]
_PATH = _ROOT / "app" / "verified_queries.json"
_CANDIDATE_DB = _ROOT / "logs" / "verified_query_candidates.db"


def load_examples(verified_only: bool = True) -> List[Dict[str, Any]]:
    try:
        data = json.loads(_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    examples = data.get("examples", [])
    return [item for item in examples if item.get("verified")] if verified_only else examples


def upsert_candidate(question: str, report_key: str, query_plan: Dict[str, Any]) -> None:
    normalized = " ".join(question.lower().split())
    result_type = "comparison" if query_plan.get("steps") else "breakdown" if query_plan.get("dimension") else "aggregate"
    _CANDIDATE_DB.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(_CANDIDATE_DB, timeout=30)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS candidates (
                normalized_question TEXT NOT NULL,
                question TEXT NOT NULL,
                report_key TEXT NOT NULL,
                query_plan TEXT NOT NULL,
                expected_result_type TEXT NOT NULL,
                feedback_count INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (normalized_question, report_key)
            )
        """)
        conn.execute("""
            INSERT INTO candidates
                (normalized_question, question, report_key, query_plan, expected_result_type)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(normalized_question, report_key) DO UPDATE SET
                question=excluded.question,
                query_plan=excluded.query_plan,
                expected_result_type=excluded.expected_result_type,
                feedback_count=candidates.feedback_count + 1
        """, (normalized, question, report_key, json.dumps(query_plan, default=str), result_type))
        conn.commit()
