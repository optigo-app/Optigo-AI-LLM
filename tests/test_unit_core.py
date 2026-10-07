"""Unit tests for core services: intent, formatters, classifier scope guard, answer generator.

Run with: python -m pytest tests/test_unit_core.py -v
Or: python -m tests.test_unit_core
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class TestIntentDetection(unittest.TestCase):
    """Tests for deterministic intent detection from intent.py."""

    def setUp(self):
        from app.services.intent import detect_intent, classify_question_by_intent
        self.detect_intent = detect_intent
        self.classify_question_by_intent = classify_question_by_intent

    def test_top_spending_customer(self):
        spec = self.detect_intent("Who is our top spending customer?", "sales_summary")
        self.assertEqual(spec.intent, "customer_sales_ranking")
        self.assertEqual(spec.metric_key, "Amount")
        self.assertEqual(spec.dimension, "CustomerFullName")
        self.assertEqual(spec.aggregation, "sum")

    def test_bill_count(self):
        spec = self.detect_intent("How many bills did we generate this month?", "sales_summary")
        self.assertEqual(spec.intent, "bill_count")
        self.assertEqual(spec.metric_key, "total_count")
        self.assertEqual(spec.aggregation, "count")

    def test_average_bill(self):
        spec = self.detect_intent("What's the average bill value?", "sales_summary")
        self.assertEqual(spec.intent, "average_bill")
        self.assertEqual(spec.metric_key, "Amount")
        self.assertEqual(spec.aggregation, "avg")

    def test_diamond_amount(self):
        spec = self.detect_intent("What's the total diamond amount?", "sales_summary")
        self.assertEqual(spec.intent, "diamond_amount")
        self.assertEqual(spec.metric_key, "DiamondAmount")
        self.assertTrue(spec.is_field_metric)
        self.assertIn("category", spec.clear_filters)

    def test_best_branch(self):
        spec = self.detect_intent("Which branch is performing the best?", "sales_summary")
        self.assertEqual(spec.intent, "branch_sales_ranking")
        self.assertEqual(spec.dimension, "branch")

    def test_total_sales_fallback(self):
        spec = self.detect_intent("Show me total sales", "sales_summary")
        self.assertEqual(spec.intent, "total_sales")
        self.assertEqual(spec.metric_key, "Amount")

    def test_salesperson_generates_most_revenue(self):
        spec = self.detect_intent("Which salesperson generates the most revenue?", "sales_summary")
        self.assertEqual(spec.intent, "sales_rep_ranking")
        self.assertEqual(spec.metric_key, "Amount")
        self.assertEqual(spec.dimension, "SalesRep")
        self.assertEqual(spec.aggregation, "sum")
        self.assertEqual(spec.sort, "desc")
        self.assertEqual(spec.limit, 1)

    def test_who_generated_most_revenue(self):
        spec = self.detect_intent("Who generated the most revenue?", "sales_summary")
        self.assertEqual(spec.intent, "sales_rep_ranking")
        self.assertEqual(spec.dimension, "SalesRep")

    def test_salesperson_highest_sales(self):
        spec = self.detect_intent("Salesperson generating highest sales", "sales_summary")
        self.assertEqual(spec.intent, "sales_rep_ranking")
        self.assertEqual(spec.dimension, "SalesRep")

    def test_employee_sold_most(self):
        spec = self.detect_intent("Which employee sold the most?", "sales_summary")
        self.assertEqual(spec.intent, "sales_rep_ranking")
        self.assertEqual(spec.dimension, "SalesRep")

    def test_category_generates_most_revenue(self):
        spec = self.detect_intent("Which category generates the most revenue?", "sales_summary")
        self.assertEqual(spec.intent, "category_sales_ranking")
        self.assertEqual(spec.dimension, "categoryname")

    def test_customer_spent_most(self):
        spec = self.detect_intent("Which customer spent the most?", "sales_summary")
        self.assertEqual(spec.intent, "customer_sales_ranking")
        self.assertEqual(spec.dimension, "CustomerFullName")

    def test_branch_highest_sales(self):
        spec = self.detect_intent("Which branch had highest sales?", "sales_summary")
        self.assertEqual(spec.intent, "branch_sales_ranking")
        self.assertEqual(spec.dimension, "branch")

    def test_salesrep_dimension_uses_view_column(self):
        # The SalesRep logic lives inside vw_LLMSalesReport, so _build_p only
        # emits a plain column reference.
        import json
        from app.services.intent import IntentSpec
        from app.services.real_api_client import _build_p
        spec = IntentSpec(report_key="sales_report")
        spec.metric_key = "Amount"
        spec.aggregation = "sum"
        spec.dimension = "SalesRep"
        spec.sort = "desc"
        spec.limit = 5
        p = json.loads(_build_p("sales_report", spec, {}, ""))
        self.assertEqual(p["Dimension"], "SalesRep")
        self.assertEqual(p["DimensionExpr"], "ISNULL(DI.SalesRep,'')")

    def test_classify_routes_to_sales(self):
        # Classifier registry uses registry keys (sales_report), but legacy
        # intent config uses sales_summary. The intent layer must normalize.
        registry = {"sales_report": {}}
        result = self.classify_question_by_intent("top spending customer", registry)
        self.assertEqual(result, "sales_report")

    def test_classify_returns_none_for_unknown(self):
        registry = {"sales_report": {}}
        result = self.classify_question_by_intent("xyz random question", registry)
        self.assertIsNone(result)

    def test_classify_quote_question_routes_to_order_report(self):
        # 'quote' is distinctive order_report vocabulary; 'jobs' is a generic
        # entity noun — the specific term must win the intent-order tie.
        registry = {"wip_report": {}, "order_report": {}}
        result = self.classify_question_by_intent(
            "how many jobs has been created from quote", registry)
        self.assertEqual(result, "order_report")

    def test_classify_generic_jobs_question_stays_wip(self):
        # No distinctive vocabulary -> generic 'jobs' routes to wip_report.
        registry = {"wip_report": {}, "order_report": {}}
        result = self.classify_question_by_intent(
            "how many jobs in casting", registry)
        self.assertEqual(result, "wip_report")


class TestFormatters(unittest.TestCase):
    """Tests for Indian currency/number formatting from formatters.py."""

    def setUp(self):
        from app.services.formatters import format_currency, format_number
        self.format_currency = format_currency
        self.format_number = format_number

    def test_format_number_indian_grouping(self):
        self.assertEqual(self.format_number(100000), "1,00,000")
        self.assertEqual(self.format_number(1000000), "10,00,000")
        self.assertEqual(self.format_number(10000000), "1,00,00,000")
        self.assertEqual(self.format_number(1234), "1,234")
        self.assertEqual(self.format_number(123), "123")

    def test_format_number_decimals(self):
        self.assertEqual(self.format_number(1234.56, decimals=2), "1,234.56")
        self.assertEqual(self.format_number(100000.50, decimals=2), "1,00,000.50")
        # Default (decimals=0) rounds to integer
        self.assertEqual(self.format_number(1234.56), "1,235")

    def test_format_currency_basic(self):
        result = self.format_currency(354.00)
        self.assertIn("354", result)
        self.assertIn("₹", result)


class TestScopeGuard(unittest.TestCase):
    """Tests for out-of-domain question detection from classifier.py."""

    def setUp(self):
        from app.services.classifier import is_out_of_scope
        self.is_out_of_scope = is_out_of_scope

    def test_greeting_is_out_of_scope(self):
        self.assertTrue(self.is_out_of_scope("hi"))
        self.assertTrue(self.is_out_of_scope("hello"))
        self.assertTrue(self.is_out_of_scope("hey there"))

    def test_thanks_is_out_of_scope(self):
        self.assertTrue(self.is_out_of_scope("thank you"))
        self.assertTrue(self.is_out_of_scope("thanks"))

    def test_general_knowledge_is_out_of_scope(self):
        self.assertTrue(self.is_out_of_scope("What is the weather today?"))
        self.assertTrue(self.is_out_of_scope("Tell me a joke"))
        self.assertTrue(self.is_out_of_scope("Who is the president?"))

    def test_business_question_is_in_scope(self):
        self.assertFalse(self.is_out_of_scope("What's our total sales?"))
        self.assertFalse(self.is_out_of_scope("How many bills did we generate?"))
        self.assertFalse(self.is_out_of_scope("Show me Mumbai branch sales"))

    def test_empty_question_is_in_scope(self):
        # Empty questions should not be flagged by scope guard
        # (they're handled by empty-question validation)
        self.assertFalse(self.is_out_of_scope(""))


class TestTruncatedSampleMath(unittest.TestCase):
    """Tests for the avg calculation fix in answer_generator.py."""

    def test_avg_uses_actual_sample_size(self):
        from app.services.answer_generator import _compute_from_values
        # Simulate a truncated sample: 3 rows with values, but total_count=100
        values = [1000.0, 2000.0, 3000.0]
        total_count = 100
        result = _compute_from_values(values, total_count, "avg")
        # Should divide by len(values)=3, not total_count=100
        expected = sum(values) / len(values)  # 2000.0
        self.assertEqual(result, expected)
        # Make sure it's NOT dividing by total_count
        self.assertNotEqual(result, sum(values) / total_count)

    def test_sum_uses_all_values(self):
        from app.services.answer_generator import _compute_from_values
        values = [100.0, 200.0, 300.0]
        result = _compute_from_values(values, 1000, "sum")
        self.assertEqual(result, 600.0)

    def test_count_uses_total_count(self):
        from app.services.answer_generator import _compute_from_values
        values = [1, 2, 3]
        result = _compute_from_values(values, 1000, "count")
        self.assertEqual(result, 1000)

    def test_max_uses_sample(self):
        from app.services.answer_generator import _compute_from_values
        values = [100.0, 500.0, 200.0]
        result = _compute_from_values(values, 1000, "max")
        self.assertEqual(result, 500.0)


class TestRetryLogic(unittest.TestCase):
    """Tests for LLM retry logic — 4xx should not retry."""

    def test_4xx_error_not_retried(self):
        from app.services.llm_gateway import _with_retry
        import asyncio

        call_count = [0]

        async def failing_func():
            call_count[0] += 1
            raise Exception("Error 401: Invalid API key")

        loop = asyncio.new_event_loop()
        try:
            with self.assertRaises(Exception):
                loop.run_until_complete(_with_retry(failing_func))
        finally:
            loop.close()

        # Should have been called exactly once (no retries)
        self.assertEqual(call_count[0], 1)

    def test_5xx_error_retried(self):
        from app.services.llm_gateway import _with_retry
        import asyncio

        call_count = [0]

        async def failing_func():
            call_count[0] += 1
            raise Exception("Error 500: Internal server error")

        loop = asyncio.new_event_loop()
        try:
            with self.assertRaises(Exception):
                # Set max_retries to 2 for faster testing
                import app.config
                original = app.config.settings.llm_max_retries
                app.config.settings.llm_max_retries = 2
                app.config.settings.llm_retry_base_delay = 0.01
                loop.run_until_complete(_with_retry(failing_func))
                app.config.settings.llm_max_retries = original
        finally:
            loop.close()

        # Should have been called 3 times (1 initial + 2 retries)
        self.assertEqual(call_count[0], 3)


class TestIntentConfigLoading(unittest.TestCase):
    """Tests for JSON config-driven intent loading."""

    def test_config_file_exists(self):
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "app", "intent_config.json"
        )
        self.assertTrue(os.path.exists(config_path), f"Config file not found: {config_path}")

    def test_config_has_all_reports(self):
        from app.services.column_registry import _REGISTRY
        # Reports are now defined in report_columns/*.json, not intent_config.json.
        self.assertIn("sales_report", _REGISTRY)
        self.assertIn("wip_report", _REGISTRY)
        # Per-report intents are loaded from the report column config.
        self.assertIn("intents", _REGISTRY["sales_report"])
        self.assertIn("intents", _REGISTRY["wip_report"])

    def test_config_has_report_order(self):
        from app.services.intent import _report_order
        order = _report_order()
        # _report_order now returns canonical registry keys, not legacy aliases
        self.assertIn("sales_report", order)
        self.assertIsInstance(order, list)
        self.assertGreater(len(order), 0)

    def test_config_has_canonical_values(self):
        from app.services.column_registry import get_canonical_values
        canon = get_canonical_values("sales_summary", "categoryname")
        self.assertIn("rings", canon)
        self.assertEqual(canon["rings"], "Ring")
        self.assertEqual(canon["22k gold"], "GOLD 22K")

    def test_config_has_fallback_intents(self):
        from app.services.column_registry import get_fallback_intent
        fallback = get_fallback_intent("sales_summary")
        self.assertIsNotNone(fallback)
        self.assertIn("patterns", fallback)

    def test_reload_config(self):
        from app.services.intent import reload_config, _CONFIG
        original = _CONFIG
        reload_config()
        # Should still have the same structure
        self.assertIn("patterns", _CONFIG)
        self.assertIn("sales_summary", _CONFIG["patterns"])


class TestRealApiParser(unittest.TestCase):
    """Tests for the real API response parser (rd/rd1 format)."""

    def test_parse_sum_response(self):
        from app.services.real_api_client import _parse_real_api_response
        data = {
            "rd": [{"MetricValue": 3540000000, "DimensionValue": ""}],
            "rd1": [{"TotalCount": 69048}],
        }
        result = _parse_real_api_response(data, "sales_summary")
        self.assertEqual(result["total_count"], 69048)
        self.assertEqual(len(result["values"]), 1)
        self.assertEqual(result["values"][0], 3540000000.0)
        self.assertEqual(result["dimensions"], [""])

    def test_parse_ranking_response(self):
        from app.services.real_api_client import _parse_real_api_response
        data = {
            "rd": [
                {"MetricValue": 926000000, "DimensionValue": "Parth Shah"},
                {"MetricValue": 810000000, "DimensionValue": "Riya Patel"},
            ],
            "rd1": [{"TotalCount": 69048}],
        }
        result = _parse_real_api_response(data, "sales_summary")
        self.assertEqual(result["total_count"], 69048)
        self.assertEqual(len(result["values"]), 2)
        self.assertEqual(result["values"][0], 926000000.0)
        self.assertEqual(result["dimensions"][0], "Parth Shah")
        self.assertEqual(result["dimensions"][1], "Riya Patel")

    def test_parse_empty_response(self):
        from app.services.real_api_client import _parse_real_api_response
        data = {"rd": [], "rd1": [{"TotalCount": 0}]}
        result = _parse_real_api_response(data, "sales_summary")
        self.assertEqual(result["total_count"], 0)
        self.assertEqual(result["values"], [])
        self.assertEqual(result["dimensions"], [])

    def test_build_con_is_plain_json(self):
        import json
        from app.services.real_api_client import _build_con
        con_str = _build_con("admin@orail.co.in", "103.206.139.196")
        # Should be plain JSON, not base64
        decoded = json.loads(con_str)
        self.assertEqual(decoded["appuserid"], "admin@orail.co.in")
        self.assertEqual(decoded["IPAddress"], "103.206.139.196")
        self.assertEqual(decoded["mode"], "GetLLMChatSummary")

    def test_build_p_has_required_fields(self):
        import json
        from app.services.real_api_client import _build_p
        from app.services.intent import detect_intent
        spec = detect_intent("What's our total sales?", "sales_summary")
        p_json = _build_p("sales_summary", spec, {})
        p = json.loads(p_json)
        self.assertEqual(p["ReportId"], 19)
        self.assertEqual(p["Mode"], "GetLLMChatSummary")
        self.assertIn("MetricKey", p)
        self.assertIn("Aggregation", p)
        self.assertIn("Dimension", p)

    def test_build_p_with_filters(self):
        import json
        from app.services.real_api_client import _build_p
        from app.services.intent import detect_intent
        spec = detect_intent("Show me Mumbai branch sales", "sales_summary")
        p_json = _build_p("sales_summary", spec, {"categoryname": "Ring"})
        p = json.loads(p_json)
        self.assertIn("categoryname", p["FilterHeader"])
        self.assertIn("Ring", p["FilterValue"])

    def test_sanitize_rejects_injection_metric(self):
        """SQL injection in MetricKey must be rejected."""
        import json
        from app.services.real_api_client import _build_p
        from app.services.intent import IntentSpec
        spec = IntentSpec(report_key="sales_summary", intent="test",
                          metric_key="1=1; DROP TABLE--",
                          aggregation="sum", dimension="", sort="desc", limit=0,
                          unit="currency", is_field_metric=False, clear_filters=[],
                          override_filters={})
        p_json = _build_p("sales_summary", spec, {})
        p = json.loads(p_json)
        # Must fall back to safe default (design_TotalAmouont), not the injection string
        self.assertEqual(p["MetricKey"], "design_TotalAmouont")

    def test_sanitize_rejects_injection_dimension(self):
        """SQL injection in Dimension must be rejected."""
        import json
        from app.services.real_api_client import _build_p
        from app.services.intent import IntentSpec
        spec = IntentSpec(report_key="sales_summary", intent="test",
                          metric_key="Amount",
                          aggregation="sum", dimension="x'); DROP TABLE--",
                          sort="desc", limit=0, unit="currency",
                          is_field_metric=False, clear_filters=[], override_filters={})
        p_json = _build_p("sales_summary", spec, {})
        p = json.loads(p_json)
        # Must fall back to empty, not the injection string
        self.assertEqual(p["Dimension"], "")

    def test_sanitize_rejects_injection_filter_value(self):
        """SQL injection in filter values must be stripped."""
        import json
        from app.services.real_api_client import _build_p
        from app.services.intent import IntentSpec
        spec = IntentSpec(report_key="sales_summary", intent="test",
                          metric_key="Amount",
                          aggregation="sum", dimension="", sort="desc", limit=0,
                          unit="currency", is_field_metric=False, clear_filters=[],
                          override_filters={})
        p_json = _build_p("sales_summary", spec,
                          {"category": "Ring'; DROP TABLE--"})
        p = json.loads(p_json)
        # The dangerous SQL chars must be stripped (quotes, semicolons, comments)
        # Without these, the words can't execute as SQL
        self.assertNotIn(";", p["FilterValue"])
        self.assertNotIn("'", p["FilterValue"])
        self.assertNotIn("--", p["FilterValue"])

    def test_sanitize_rejects_unknown_filter_column(self):
        """Unknown filter columns must be rejected."""
        import json
        from app.services.real_api_client import _build_p
        from app.services.intent import IntentSpec
        spec = IntentSpec(report_key="sales_summary", intent="test",
                          metric_key="Amount",
                          aggregation="sum", dimension="", sort="desc", limit=0,
                          unit="currency", is_field_metric=False, clear_filters=[],
                          override_filters={})
        p_json = _build_p("sales_summary", spec,
                          {"evil_column": "value"})
        p = json.loads(p_json)
        # Unknown column must not appear in FilterHeader
        self.assertEqual(p["FilterHeader"], "")


class TestConfigDrivenScalability(unittest.TestCase):
    """Tests that the new report_columns/*.json first architecture scales."""

    def test_all_reports_have_valid_intents(self):
        from app.services.column_registry import _REGISTRY
        for report_key, cfg in _REGISTRY.items():
            intents = cfg.get("intents", {})
            columns = cfg.get("columns", {})
            special = cfg.get("special_metrics", {})
            catalog = cfg.get("metric_catalog", {})
            for intent_name, meta in intents.items():
                metric = meta.get("metric", "")
                if metric:
                    self.assertIn(
                        metric,
                        list(columns.keys()) + list(special.keys()) + list(catalog.keys()),
                        f"{report_key}.{intent_name} metric {metric} not found",
                    )
                dimension = meta.get("dimension", "")
                if dimension:
                    self.assertIn(
                        dimension,
                        columns,
                        f"{report_key}.{intent_name} dimension {dimension} not found",
                    )

    def test_synthetic_report_works_without_python_changes(self):
        """A report added only via JSON can be detected by the classifier/intent layer."""
        import json
        from app.services import column_registry
        from app.services import intent as intent_module

        tmp_cfg = {
            "description": "Synthetic demo report",
            "sp": "DynamicSyntheticReport",
            "report_id": 999,
            "default_metric": "demo_amount",
            "tables": ["synthetic_table"],
            "base_filter": "1=1",
            "columns": {
                "demo_amount": {
                    "sql": "demo_amount",
                    "type": "decimal",
                    "desc": "Demo amount",
                    "grp": "financial",
                }
            },
            "special_metrics": {},
            "filter_key_map": {},
            "metric_catalog": {
                "demo_amount": {"type": "amount", "label": "Demo amount"}
            },
            "report_keywords": ["demo", "synthetic"],
            "intents": {
                "demo_total": {
                    "patterns": [r"\b(demo|synthetic)\s+(total|sales?)\b"],
                    "metric": "demo_amount",
                    "aggregation": "sum",
                    "unit": "currency",
                    "label": "Total demo amount",
                }
            },
            "fallback_intent": {
                "patterns": ["demo", "synthetic"],
                "intent": "demo_total",
                "metric": "demo_amount",
                "aggregation": "sum",
                "unit": "currency",
            },
        }
        try:
            # Inject synthetic report into runtime registries (mirrors adding a JSON file)
            column_registry._REGISTRY["synthetic_demo_report"] = column_registry._expand_keys(tmp_cfg)
            intent_module._COLUMN_REGISTRY["synthetic_demo_report"] = column_registry._expand_keys(tmp_cfg)

            from app.services.classifier import keyword_classify
            from app.services.intent import detect_intent
            from app.services.answer_generator import _metric_label

            registry = {"synthetic_demo_report": type("E", (), {"description": "x"})()}
            routed = keyword_classify("show me demo total", registry)
            self.assertEqual(routed, "synthetic_demo_report")

            spec = detect_intent("demo total today", "synthetic_demo_report")
            self.assertEqual(spec.intent, "demo_total")
            self.assertEqual(spec.metric_key, "demo_amount")

            label = _metric_label("demo_amount", "demo_total", "synthetic_demo_report")
            self.assertEqual(label, "Total demo amount")
        finally:
            column_registry._REGISTRY.pop("synthetic_demo_report", None)
            intent_module._COLUMN_REGISTRY.pop("synthetic_demo_report", None)
            intent_module._PATTERN_CACHE.pop("synthetic_demo_report", None)

    def test_intent_label_is_config_driven(self):
        from app.services.answer_generator import _metric_label
        label = _metric_label("Amount", "total_sales", "sales_summary")
        self.assertEqual(label, "Total sales")

    def test_keyword_classifier_is_dynamic(self):
        from app.services.classifier import keyword_classify
        registry = {"sales_report": type("E", (), {"description": "x"})()}
        key = keyword_classify("total revenue today", registry)
        self.assertEqual(key, "sales_report")

    def test_semantic_catalog_scoped_to_one_report(self):
        """With many reports configured, catalog for a named report must stay small."""
        from app.services.semantic_query_parser import build_semantic_catalog
        from app.services import column_registry

        # Inject a small army of dummy reports
        original_registry = dict(column_registry._REGISTRY)
        try:
            for i in range(50):
                column_registry._REGISTRY[f"dummy_report_{i}"] = {
                    "description": f"Dummy report {i}",
                    "columns": {
                        f"metric_{i}": {"sql": f"metric_{i}", "type": "decimal", "desc": "x"}
                    },
                    "special_metrics": {},
                    "metric_catalog": {
                        f"metric_{i}": {"type": "amount", "label": f"Metric {i}", "aliases": []}
                    },
                }
            catalog = build_semantic_catalog("sales_report")
            # Should contain sales_report content but not every dummy report
            self.assertIn("Report: sales_report", catalog)
            self.assertNotIn("Report: dummy_report_49", catalog)
        finally:
            column_registry._REGISTRY.clear()
            column_registry._REGISTRY.update(original_registry)

    def test_keyword_classifier_scales_to_many_reports(self):
        """Keyword classifier should still pick the right report among many."""
        from app.services.classifier import keyword_classify
        from app.services import column_registry

        original_registry = dict(column_registry._REGISTRY)
        try:
            registry = {"sales_report": type("E", (), {"description": "x"})()}
            for i in range(50):
                registry[f"dummy_report_{i}"] = type("E", (), {"description": "x"})()
                column_registry._REGISTRY[f"dummy_report_{i}"] = {
                    "report_keywords": [f"dummy{i}"],
                }
            # Keep sales_report in column_registry too so keyword map includes it
            key = keyword_classify("total revenue today", registry)
            self.assertEqual(key, "sales_report")
        finally:
            column_registry._REGISTRY.clear()
            column_registry._REGISTRY.update(original_registry)


class TestMarketContext(unittest.TestCase):
    """External-market question detection + boundary notes."""

    def test_internal_question_not_flagged(self):
        from app.services.market_context import detect_market_context
        for q in ("total sales today", "ring sales last month",
                  "top 5 customers", "gold weight sold this month"):
            self.assertIsNone(detect_market_context(q), q)

    def test_market_trend_question_flagged(self):
        from app.services.market_context import detect_market_context
        ctx = detect_market_context("what is trending in the market")
        self.assertIsNotNone(ctx)
        self.assertTrue(ctx["is_external"])
        self.assertEqual(ctx["topic"], "trends")

    def test_market_comparison_flagged(self):
        from app.services.market_context import detect_market_context
        ctx = detect_market_context("compare our sales with market trends")
        self.assertIsNotNone(ctx)
        self.assertTrue(ctx["wants_comparison"])

    def test_live_gold_rate_flagged(self):
        from app.services.market_context import detect_market_context
        ctx = detect_market_context("what is the current gold rate today")
        self.assertIsNotNone(ctx)
        self.assertEqual(ctx["topic"], "gold_rate")

    def test_note_without_snapshot_is_honest(self):
        from app.services.market_context import build_market_note
        note = build_market_note(
            {"is_external": True, "wants_comparison": False, "topic": "trends"}, []
        )
        self.assertIn("don't have a live market-data source", note)
        self.assertIn("your own", note)

    def test_note_with_snapshot_cites_source(self):
        from app.services.market_context import build_market_note
        snap = [{
            "topic": "trends",
            "observation": "Lightweight jewellery demand rising",
            "source_name": "TestFeed",
            "source_url": "https://example.com/x",
            "retrieved_at": "2026-09-01T00:00:00Z",
        }]
        note = build_market_note(
            {"is_external": True, "wants_comparison": True, "topic": "trends"}, snap
        )
        self.assertIn("Lightweight jewellery demand rising", note)
        self.assertIn("TestFeed", note)
        self.assertIn("External market context", note)

    def test_unsourced_snapshot_entries_dropped(self):
        from app.services import market_context
        import json, tempfile
        fake = {
            "entries": [
                {"observation": "no source", "topic": "trends"},
                {"observation": "sourced", "topic": "trends",
                 "source_name": "S", "retrieved_at": "2026-01-01"},
            ]
        }
        with tempfile.NamedTemporaryFile(
            "w", suffix=".json", delete=False, encoding="utf-8"
        ) as fh:
            json.dump(fake, fh)
            tmp = fh.name
        orig = market_context._MARKET_DATA_PATH
        try:
            market_context._MARKET_DATA_PATH = tmp
            entries = market_context.load_market_snapshot()
            self.assertEqual(len(entries), 1)
            self.assertEqual(entries[0]["observation"], "sourced")
        finally:
            market_context._MARKET_DATA_PATH = orig
            os.unlink(tmp)


class TestInvalidFilterValues(unittest.TestCase):
    """Config-driven rejection of metric words leaked into filters."""

    def test_validated_filters_drops_invalid_category_value(self):
        from app.services.query_plan import QueryPlan, QueryFilter
        plan = QueryPlan(
            report_key="sales_report", intent="sum", metric="netwt",
            filters=[QueryFilter(field="category", value="net")],
        )
        self.assertEqual(plan.validated_filters(), {})

    def test_validated_filters_keeps_valid_category(self):
        from app.services.query_plan import QueryPlan, QueryFilter
        plan = QueryPlan(
            report_key="sales_report", intent="sum", metric="netwt",
            filters=[QueryFilter(field="category", value="Ring")],
        )
        self.assertEqual(plan.validated_filters(), {"category": "Ring"})

    def test_customer_filter_drops_diamond_value(self):
        from app.services.query_plan import QueryPlan, QueryFilter
        plan = QueryPlan(
            report_key="sales_report", intent="sum", metric="Cu_DiaWt",
            filters=[QueryFilter(field="customer", value="diamond")],
        )
        self.assertEqual(plan.validated_filters(), {})

    def test_ai_where_drops_invalid_name_clause(self):
        from app.services.parse_result import validate_ai_where
        clause = "ISNULL(DI.CustomerName,'') LIKE '%diamond%' AND DI.categoryname='ring'"
        out = validate_ai_where(clause, "sales_report")
        self.assertEqual(out, "DI.categoryname='ring'")

    def test_ai_where_keeps_real_customer_name(self):
        from app.services.parse_result import validate_ai_where
        clause = "ISNULL(DI.CustomerName,'') LIKE '%diamond traders%'"
        out = validate_ai_where(clause, "sales_report")
        # Real multi-word names are split into per-word AND LIKEs so stored
        # names with irregular spacing still match — both words must survive.
        self.assertIn("'%diamond%'", out)
        self.assertIn("'%traders%'", out)

    def test_customer_diamond_metrics_resolve(self):
        from app.services.metric_validator import validate_metric_intent
        # 'customer diamond weight' intent_type=weight; Cu_DiaWt is now type=weight
        # so it must NOT be overridden away.
        self.assertEqual(
            validate_metric_intent("Cu_DiaWt", "customer diamond weight"),
            "Cu_DiaWt",
        )
        self.assertEqual(
            validate_metric_intent("Co_DiaPCS", "company diamond pieces"),
            "Co_DiaPCS",
        )


class TestMultiPeriodDetection(unittest.TestCase):
    """detect_periods / is_time_dimension used by the multi-period path."""

    def test_multi_period_question(self):
        from app.services.orchestrator import detect_periods
        self.assertEqual(
            detect_periods("gross wt for this year today and month"),
            ["this_year", "this_month", "today"],
        )

    def test_comparison_question_keeps_both_periods(self):
        from app.services.orchestrator import detect_periods
        self.assertEqual(
            detect_periods("compare sales this month vs last month"),
            ["last_month", "this_month"],
        )

    def test_single_period_questions(self):
        from app.services.orchestrator import detect_periods
        self.assertEqual(detect_periods("total sales"), [])
        self.assertEqual(detect_periods("monthly trend"), ["this_month"])
        self.assertEqual(detect_periods("sales this year"), ["this_year"])

    def test_two_day_periods(self):
        from app.services.orchestrator import detect_periods
        self.assertEqual(
            detect_periods("sales today and yesterday"),
            ["yesterday", "today"],
        )

    def test_bare_month_only_with_named_period(self):
        from app.services.orchestrator import detect_periods
        # Bare "month"/"year" alone must not fire (ordinary phrasing)
        self.assertEqual(detect_periods("show month data"), [])
        self.assertEqual(detect_periods("year and month totals"), [])
        # But with a qualified period present, bare words count as current period
        self.assertIn("this_month", detect_periods("this year and month totals"))
        self.assertIn("this_year", detect_periods("today and year totals"))

    def test_dedup(self):
        from app.services.orchestrator import detect_periods
        self.assertEqual(
            detect_periods("today today today"), ["today"],
        )

    def test_is_time_dimension(self):
        from app.services.orchestrator import is_time_dimension
        self.assertTrue(is_time_dimension("month"))
        self.assertTrue(is_time_dimension("Year"))
        self.assertFalse(is_time_dimension("designname"))
        self.assertFalse(is_time_dimension(""))
        self.assertFalse(is_time_dimension(None))

    def test_resolve_preset_dates(self):
        from datetime import date
        from app.services.orchestrator import resolve_preset_dates
        s, e = resolve_preset_dates("today")
        self.assertEqual(s, date.today().isoformat())
        s, e = resolve_preset_dates("this_month")
        self.assertTrue(s.endswith("-01"))
        s, e = resolve_preset_dates("bogus")
        self.assertEqual((s, e), ("", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
