import time
import unittest

from app.services.pipeline_trace import StageTracer


class StageTracerTests(unittest.TestCase):

    def test_start_end_records_stage(self):
        t = StageTracer("req-1")
        t.start("semantic_parse")
        time.sleep(0.01)
        ms = t.end("semantic_parse", detail="sales_report/Amount conf=0.9")
        self.assertGreater(ms, 0)
        self.assertEqual(len(t.stages), 1)
        s = t.stages[0]
        self.assertEqual(s["name"], "semantic_parse")
        self.assertEqual(s["outcome"], "ok")
        self.assertIn("sales_report", s["detail"])

    def test_context_manager_error_outcome(self):
        t = StageTracer()
        with self.assertRaises(ValueError):
            with t.stage("execute"):
                raise ValueError("boom")
        self.assertEqual(t.stages[0]["outcome"], "error")
        self.assertEqual(t.stages[0]["detail"], "boom")
        self.assertEqual(t.failure_stage, "execute")

    def test_failure_stage_empty_when_all_ok(self):
        t = StageTracer()
        t.record("context", outcome="ok")
        t.record("execute", outcome="ok")
        self.assertEqual(t.failure_stage, "")

    def test_latencies_dict(self):
        t = StageTracer()
        t.record("a", elapsed_ms=10.0)
        t.record("b", elapsed_ms=20.0)
        self.assertEqual(t.latencies, {"a": 10.0, "b": 20.0})

    def test_end_without_start_returns_zero(self):
        t = StageTracer()
        ms = t.end("ghost")
        self.assertEqual(ms, 0.0)

    def test_detail_truncated(self):
        t = StageTracer()
        t.record("x", detail="a" * 500)
        self.assertTrue(t.stages[0]["detail"].endswith("..."))
        self.assertLessEqual(len(t.stages[0]["detail"]), 203)

    def test_outcome_of(self):
        t = StageTracer()
        t.record("execute", outcome="ok")
        t.record("execute", outcome="error")
        self.assertEqual(t.outcome_of("execute"), "error")

    def test_emit_writes_event(self):
        from unittest.mock import patch
        t = StageTracer("req-42")
        t.record("execute", elapsed_ms=5.0)
        with patch("app.services.pipeline_trace._write_entry") as mock_write:
            t.emit(question="sales today", status="success", report_key="sales_report")
        entry = mock_write.call_args[0][0]
        self.assertEqual(entry["event"], "pipeline_trace")
        self.assertEqual(entry["request_id"], "req-42")
        self.assertEqual(entry["report_key"], "sales_report")
        self.assertEqual(entry["total_ms"], 5.0)
        self.assertEqual(len(entry["stages"]), 1)


if __name__ == "__main__":
    unittest.main()
