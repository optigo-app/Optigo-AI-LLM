import unittest
from unittest.mock import AsyncMock, patch

from app.services.parse_result import ParseResult
from app.services.query_plan import QueryComplexity
from app.services.query_planner import _apply_deterministic_override, finalize_query, plan_query


class QueryPlanTests(unittest.IsolatedAsyncioTestCase):
    def test_aggregate_limit_is_one(self):
        parsed = ParseResult({"report_key": "sales_report", "metric": "Amount", "limit": 500})
        plan = parsed.to_query_plan("total sales").validate_against_registry()
        self.assertEqual(plan.limit, 1)
        self.assertEqual(plan.complexity, QueryComplexity.simple)

    def test_breakdown_limit_is_clamped(self):
        parsed = ParseResult({"report_key": "sales_report", "metric": "Amount", "dimension": "categoryname", "limit": 500})
        plan = parsed.to_query_plan("sales by category").validate_against_registry()
        self.assertLessEqual(plan.limit, 100)
        self.assertTrue(plan.results_limited)
        self.assertEqual(plan.complexity, QueryComplexity.moderate)

    def test_unknown_metric_rejected(self):
        parsed = ParseResult({"report_key": "sales_report", "metric": "InventedMetric"})
        with self.assertRaises(ValueError):
            parsed.to_query_plan().validate_against_registry()

    def test_explicit_intent_confidence(self):
        parsed = ParseResult({"report_key": "sales_report", "metric": "Amount"})
        _apply_deterministic_override(parsed, "how many bills")
        self.assertGreaterEqual(parsed.confidence, 0.95)

    def test_comparison_is_complex(self):
        parsed = ParseResult({"report_key": "sales_report", "metric": "Amount"})
        result = finalize_query(parsed, "sales growth compared with the previous period", routing_source="intent")
        self.assertEqual(result.plan.complexity, QueryComplexity.complex)
        self.assertEqual(result.plan.date_range.preset, "this_month")

    def test_backend_routing_confidence_replaces_model_claim(self):
        parsed = ParseResult({"report_key": "sales_report", "metric": "Amount", "confidence": 0.99})
        result = finalize_query(parsed, "sales value", routing_source="keyword")
        self.assertEqual(result.plan.confidence, 0.75)

    def test_shared_planner_inherits_date_filters(self):
        parsed = ParseResult({"report_key": "sales_report", "metric": "Amount"})
        result = finalize_query(
            parsed, "same sales", previous_filters={"start_date": "2026-09-01", "end_date": "2026-09-30"},
            routing_source="context",
        )
        self.assertEqual(result.validated_filters["start_date"], "2026-09-01")
        self.assertEqual(result.plan.confidence, 0.8)

    @patch("app.services.query_planner.parse_query", new_callable=AsyncMock)
    async def test_frontend_report_cannot_be_overridden_by_model(self, parse):
        parse.return_value = ParseResult({"report_key": "wip_report", "metric": "Amount"})
        result = await plan_query("show value", report_name="sales_report")
        self.assertEqual(result.plan.report_key, "sales_report")
        self.assertEqual(result.plan.confidence, 1.0)
        self.assertEqual(result.plan.alternatives[0].intent, "wip_report")


if __name__ == "__main__":
    unittest.main()
