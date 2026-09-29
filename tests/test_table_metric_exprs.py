import copy
import importlib.util
import json
import unittest
from pathlib import Path
from xml.sax.saxutils import unescape

from app.services.context_resolver import ambiguous_entity_value
from app.services.intent import IntentSpec, _detect_explicit_intent
from app.services.parse_result import ParseResult
from app.services.query_planner import finalize_query
from app.services.real_api_client import _build_p

ROOT = Path(__file__).resolve().parents[1]


def _load_validator():
    spec = importlib.util.spec_from_file_location(
        "validate_report_configs", ROOT / "scripts" / "validate_report_configs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TableMetricExpressionTests(unittest.TestCase):
    def _spec(self, metric="Amount", aggregation="sum", dimension="", unit="count"):
        return IntentSpec(
            report_key="sales_report", intent="test",
            metric_key=metric, aggregation=aggregation, dimension=dimension,
            sort="desc", limit=1, unit=unit,
        )

    def test_company_customer_diamond_metrics_use_source_query(self):
        metrics = ["Co_DiaPCS", "Co_DiaWt", "Cu_DiaPCS", "Cu_DiaWt"]
        for metric in metrics:
            with self.subTest(metric=metric):
                payload = json.loads(_build_p("sales_report", self._spec(metric), {}, ""))
                self.assertEqual(
                    payload["Tables"],
                    [
                        "Stockmanagement_dcbdesignInfo_history",
                        "Stockmanagement_dcbdesignInfo_history_Archive",
                        "SideUp_Sales_Report_Job",
                    ],
                )
                self.assertTrue(payload.get("SourceQuery", ""))
                self.assertEqual(payload["TableMetricExprs"], {})
                self.assertEqual(payload["MetricKey"], metric)
                self.assertEqual(payload["MetricExpr"], f"ISNULL(DI.{metric},0)")

    def test_company_customer_diamond_intents(self):
        cases = {
            "company wise diamond pcs": "Co_DiaPCS",
            "company diamond weight": "Co_DiaWt",
            "customer diamond pcs": "Cu_DiaPCS",
            "customer diamond weight": "Cu_DiaWt",
        }
        for question, metric in cases.items():
            with self.subTest(question=question):
                spec = _detect_explicit_intent(question, "sales_report")
                self.assertIsNotNone(spec)
                self.assertEqual(spec.metric_key, metric)

    def test_total_tax_uses_source_query(self):
        payload = json.loads(_build_p("sales_report", self._spec("TotalTax", unit="currency"), {}, ""))
        self.assertIn("SourceQuery", payload)
        self.assertTrue(payload["SourceQuery"])
        self.assertEqual(payload["TableMetricExprs"], {})
        self.assertEqual(payload["MetricKey"], "totaltaxAmount")
        self.assertEqual(payload["MetricExpr"], "ISNULL(DI.totaltaxAmount,0)")

    def test_total_tax_does_not_steal_sales_routing(self):
        spec = _detect_explicit_intent("What is the total tax value this year?", "sales_report")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.metric_key, "TotalTax")

    def test_manufacturer_dimension_uses_view_column(self):
        payload = json.loads(_build_p(
            "sales_report",
            self._spec("Amount", dimension="Manufacturer", unit="currency"),
            {},
            "",
        ))
        self.assertEqual(payload["Dimension"], "Manufacturer")
        self.assertEqual(payload["DimensionExpr"], "ISNULL(DI.Manufacturer,'')")

    def test_manufacturer_filter_uses_view_column(self):
        payload = json.loads(_build_p(
            "sales_report",
            self._spec("Amount", unit="currency"),
            {"manufacturer": "ABC"},
            "",
        ))
        self.assertEqual(payload["FilterHeader"], "Manufacturer")
        self.assertEqual(payload["FilterValue"], "ABC")
        self.assertEqual(payload["AIWhereClause"], "")

    def test_explicit_supplier_value_uses_manufacturer_filter(self):
        planning = finalize_query(
            ParseResult({"report_key": "sales_report", "metric": "Amount", "aggregation": "sum"}),
            "supplier ABC total sales",
        )
        self.assertEqual(planning.validated_filters.get("supplier"), "ABC")
        payload = json.loads(_build_p("sales_report", planning.intent_spec, planning.validated_filters, ""))
        self.assertEqual(payload["FilterHeader"], "Manufacturer")
        self.assertEqual(payload["FilterValue"], "ABC")

    def test_manufacturer_wise_sales_intent(self):
        spec = _detect_explicit_intent("manufacturer wise sales", "sales_report")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.dimension, "Manufacturer")

    def test_units_pieces_uses_conditional_sum(self):
        spec = _detect_explicit_intent("How many units/pieces were sold?", "sales_report")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.metric_key, "units_sold")
        self.assertEqual(spec.aggregation, "sum")
        self.assertEqual(spec.intent, "units_sold")
        self.assertTrue(spec.override_metric)

        planning = finalize_query(
            ParseResult({"report_key": "sales_report", "metric": "total_count", "aggregation": "count"}),
            "How many units/pieces were sold?",
        )
        self.assertEqual(planning.plan.metric, "units_sold")
        self.assertEqual(planning.plan.aggregation, "sum")
        self.assertEqual(planning.plan.intent, "units_sold")
        self.assertEqual(planning.intent_spec.intent, "units_sold")

        payload = json.loads(_build_p("sales_report", planning.intent_spec, {}, ""))
        self.assertEqual(payload["MetricKey"], "units_sold")
        self.assertEqual(payload["Aggregation"], "sum")
        self.assertIn("IsERPreturn", unescape(payload["MetricExpr"]))
        self.assertIn("1", unescape(payload["MetricExpr"]))

    def test_unique_dimensions_keep_count_distinct_aggregation(self):
        for metric in ("unique_customers", "unique_designs"):
            with self.subTest(metric=metric):
                payload = json.loads(_build_p(
                    "sales_report",
                    self._spec(metric, aggregation="count_distinct"),
                    {},
                    "",
                ))
                self.assertEqual(payload["Aggregation"], "count_distinct")

    def test_sales_report_has_no_invoice_join_table_filter(self):
        payload = json.loads(_build_p("sales_report", self._spec("Amount", unit="currency"), {}, ""))
        self.assertEqual(payload["TableFilters"], {})

    def test_wastage_amount_is_governed_not_ambiguous(self):
        self.assertIsNone(ambiguous_entity_value("total wastage amount"))
        spec = _detect_explicit_intent("total wastage amount", "sales_report")
        self.assertIsNotNone(spec)
        self.assertEqual(spec.metric_key, "WastageAmount")
        self.assertEqual(spec.aggregation, "sum")

    def test_customer_segment_dimension_uses_view_column(self):
        payload = json.loads(_build_p(
            "sales_report",
            self._spec("Amount", dimension="CustomerType", unit="currency"),
            {},
            "",
        ))
        self.assertEqual(payload["Dimension"], "CustomerType")
        self.assertEqual(payload["TableDimensionExprs"], {})
        self.assertEqual(payload["DimensionExpr"], "ISNULL(DI.CustomerType,'')")

    def test_customer_segment_filter_uses_view_column(self):
        payload = json.loads(_build_p(
            "sales_report",
            self._spec("Amount", unit="currency"),
            {"customer_type": "Retail"},
            "",
        ))
        self.assertEqual(payload["FilterHeader"], "CustomerType")
        self.assertEqual(payload["FilterValue"], "Retail")
        self.assertEqual(payload["AIWhereClause"], "")
        self.assertEqual(payload["TableDimensionFilters"], {})

    def test_explicit_customer_type_value_becomes_governed_filter(self):
        planning = finalize_query(
            ParseResult({"report_key": "sales_report", "metric": "Amount", "aggregation": "sum", "dimension": "CustomerType"}),
            "customer type Retailer total sales",
        )
        self.assertEqual(planning.validated_filters.get("customer_type"), "Retailer")
        self.assertIsNone(planning.plan.dimension)
        payload = json.loads(_build_p("sales_report", planning.intent_spec, planning.validated_filters, ""))
        self.assertEqual(payload["FilterHeader"], "CustomerType")
        self.assertEqual(payload["FilterValue"], "Retailer")
        self.assertEqual(payload["TableDimensionFilters"], {})

    def test_explicit_customer_value_uses_customer_filter(self):
        planning = finalize_query(
            ParseResult({"report_key": "sales_report", "metric": "Amount", "aggregation": "sum"}),
            "customer ThGems total sale this year",
        )
        self.assertEqual(planning.validated_filters.get("customer"), "ThGems")
        payload = json.loads(_build_p("sales_report", planning.intent_spec, planning.validated_filters, planning.ai_where))
        self.assertEqual(payload["FilterHeader"], "")
        self.assertIn("DI.CustomerName", unescape(payload["AIWhereClause"]))
        self.assertIn("LIKE '%ThGems%'", unescape(payload["AIWhereClause"]))

    def test_query_plan_accepts_new_metrics(self):
        for metric in ("Co_DiaPCS", "Co_DiaWt", "Cu_DiaPCS", "Cu_DiaWt", "TotalTax", "WastageAmount", "units_sold"):
            with self.subTest(metric=metric):
                plan = ParseResult({"report_key": "sales_report", "metric": metric}).to_query_plan().validate_against_registry()
                self.assertEqual(plan.metric, metric)

    def test_normal_metric_has_no_table_expression_map(self):
        payload = json.loads(_build_p("sales_report", self._spec("Amount"), {}, ""))
        self.assertEqual(payload["TableMetricExprs"], {})

    def test_sales_config_uses_source_query_not_table_expressions(self):
        cfg = json.loads((ROOT / "app" / "report_columns" / "sales_report.json").read_text(encoding="utf-8"))
        self.assertEqual(cfg.get("source_query_file"), "sales_report.sql")
        self.assertTrue((ROOT / "app" / "report_queries" / cfg["source_query_file"]).exists())
        for metric in ("Co_DiaPCS", "Co_DiaWt", "Cu_DiaPCS", "Cu_DiaWt", "TotalTax"):
            self.assertNotIn("table_metric_exprs", cfg["columns"][metric])
        self.assertNotIn("table_dimension_exprs", cfg["columns"]["CustomerType"])
        self.assertNotIn("computed", cfg["columns"]["CustomerType"])

    def test_validator_rejects_blocked_tokens_in_table_metric_expr(self):
        validator = _load_validator()
        cfg = json.loads((ROOT / "app" / "report_columns" / "sales_report.json").read_text(encoding="utf-8"))
        unsafe = copy.deepcopy(cfg)
        unsafe["columns"]["Amount"]["table_metric_exprs"] = {
            "[dbo].[vw_LLMSalesReport]": "1; DROP TABLE x"
        }
        errors, _ = validator.validate_report_config("sales_report", unsafe)
        self.assertTrue(any("blocked SQL tokens" in error for error in errors))

    def test_shared_sp_consumes_and_validates_table_metric_expressions(self):
        sql = (ROOT / "Sample_SQL_Sp" / "llm_chat_sp.sql").read_text(encoding="utf-8")
        self.assertIn("TableMetricExprs", sql)
        self.assertIn("TableDimensionExprs", sql)
        self.assertIn("TableDimensionFilters", sql)
        self.assertIn("@TblMetricExpr", sql)
        self.assertIn("@TblDimensionExpr", sql)
        self.assertIn("Missing table-specific metric expressions", sql)
        self.assertIn("Missing table-specific dimension expressions", sql)
        self.assertIn("TableMetricExprs contains an unknown report table", sql)
        self.assertIn("TableDimensionExprs contains an unknown report table", sql)
        self.assertIn("RuleSet IN ('EXPR','FILTER')", sql)
        self.assertIn("@InputChecks", sql)
        self.assertIn("@BlockedKeywords", sql)
        self.assertIn("FilterHeader/FilterValue rejected by SP-side validation", sql)
        self.assertIn("@SortDirection NOT IN ('asc','desc')", sql)
        self.assertIn("SP[_]EXECUTESQL", sql)
        self.assertNotIn("+ @MetricExpr +", sql)


if __name__ == "__main__":
    unittest.main()
