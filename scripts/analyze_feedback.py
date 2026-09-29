import json
import sqlite3
from collections import Counter
from pathlib import Path

DB = Path(__file__).resolve().parents[1] / "logs" / "feedback.db"


def analyze() -> dict:
    if not DB.exists():
        return {"total_down": 0, "failure_reasons": {}, "report_metrics": {}}
    with sqlite3.connect(DB) as conn:
        rows = conn.execute("SELECT report_key, metric, failure_reason FROM feedback WHERE rating='down'").fetchall()
    return {
        "total_down": len(rows),
        "failure_reasons": dict(Counter(reason or "unspecified" for _, _, reason in rows)),
        "report_metrics": dict(Counter(f"{report}:{metric}" for report, metric, _ in rows)),
    }


if __name__ == "__main__":
    print(json.dumps(analyze(), indent=2))
