import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from app.services import feedback_store


class FeedbackV2Tests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.patch = patch.object(feedback_store, "_LOG_DIR", Path(self.tempdir.name))
        self.patch.start()
        feedback_store._table_ready = False

    def tearDown(self):
        feedback_store._table_ready = False
        self.patch.stop()
        self.tempdir.cleanup()

    def test_enriched_feedback_is_stored(self):
        feedback_store.add_feedback(
            "session", "wrong total", report_key="sales_report", metric="Amount",
            rating="down", failure_reason="wrong_filter", query_plan={"metric": "Amount"},
            confidence=0.62, latency_ms=120.5,
        )
        with closing(sqlite3.connect(feedback_store._db_path())) as conn:
            row = conn.execute("SELECT failure_reason, query_plan, confidence, latency_ms FROM feedback").fetchone()
        self.assertEqual(row[0], "wrong_filter")
        self.assertEqual(json.loads(row[1]), {"metric": "Amount"})
        self.assertEqual(row[2], 0.62)
        self.assertEqual(row[3], 120.5)

    def test_unknown_failure_reason_is_rejected(self):
        with self.assertRaises(ValueError):
            feedback_store.add_feedback("session", "q", rating="down", failure_reason="unknown")


if __name__ == "__main__":
    unittest.main()
