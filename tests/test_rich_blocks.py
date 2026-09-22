"""Tests for the jewelry-industry rich block layout and formatting.

Covers:
1. format_currency_dual — large values show both normalized and raw.
2. normalize_unit_label — jewelry-native unit suffixes (g, ct, pcs).
3. block_builder.build_simple_blocks — emits metric_card + period blocks.
4. block_builder.build_ranking_blocks — emits period + glossary blocks.
5. block_builder.build_multi_metric_blocks — emits period + glossary blocks.
6. New block types validate against Pydantic models.
7. blocks_to_text flattens the new block types correctly.
8. parse_llm_blocks — LLM JSON validation + error fallback.
9. build_no_data_blocks — friendly no-data response with suggestions.
"""

import json
import unittest

from app.models import (
    MetricCardBlock, BreakdownBlock, PeriodBlock, GlossaryBlock,
)
from app.services.formatters import (
    format_currency, format_currency_dual, format_weight,
    normalize_unit_label,
)
from app.services import block_builder


class FormatCurrencyDualTests(unittest.TestCase):
    """Large currency values show normalized (crore/lakh) form only."""

    def test_large_value_shows_dual_format(self):
        out = format_currency_dual(247556602.25)
        self.assertIn("crore", out)
        self.assertNotIn("24,75,56,602", out)  # no raw duplicate

    def test_lakh_value_shows_dual_format(self):
        out = format_currency_dual(550000)
        self.assertIn("lakh", out)
        self.assertNotIn("5,50,000", out)

    def test_small_value_no_duplication(self):
        out = format_currency_dual(1250)
        # Small values should not duplicate
        self.assertEqual(out.count("₹"), 1)

    def test_zero(self):
        out = format_currency_dual(0)
        self.assertIn("₹", out)


class NormalizeUnitLabelTests(unittest.TestCase):
    """Internal unit codes map to jewelry-industry display suffixes."""

    def test_grams(self):
        self.assertEqual(normalize_unit_label("gms"), "gms")
        self.assertEqual(normalize_unit_label("gm"), "gms")
        self.assertEqual(normalize_unit_label("g"), "gms")

    def test_carat(self):
        self.assertEqual(normalize_unit_label("ctw"), "ctw")
        self.assertEqual(normalize_unit_label("ct"), "ctw")
        self.assertEqual(normalize_unit_label("carat"), "ctw")

    def test_pieces(self):
        self.assertEqual(normalize_unit_label("pcs"), "pcs")
        self.assertEqual(normalize_unit_label("pieces"), "pcs")

    def test_empty(self):
        self.assertEqual(normalize_unit_label(""), "")

    def test_unknown_passes_through(self):
        self.assertEqual(normalize_unit_label("foo"), "foo")

    def test_weight_uses_normalized_unit(self):
        self.assertEqual(format_weight(55.5, "gms"), "55.500 gms")
        self.assertEqual(format_weight(2.3, "ctw"), "2.300 ctw")


class SimpleBlocksRichLayoutTests(unittest.TestCase):
    """build_simple_blocks emits metric_card + period blocks."""

    def test_emits_metric_card_for_simple_value(self):
        blocks = block_builder.build_simple_blocks(
            label="Total Sales", value=247556602.25, unit="currency",
            currency="INR", record_count=2123, source_report="Sales Report",
            unit_label="", filters={"start_date": "2026-09-01", "end_date": "2026-09-15"},
        )
        types = [b.get("type") for b in blocks]
        self.assertIn("period", types)
        self.assertIn("metric_card", types)

    def test_metric_card_has_dual_currency(self):
        blocks = block_builder.build_simple_blocks(
            label="Total Sales", value=247556602.25, unit="currency",
            currency="INR", record_count=2123, source_report="Sales Report",
        )
        card = next(b for b in blocks if b.get("type") == "metric_card")
        self.assertIn("crore", card["value"])
        self.assertNotIn("24,75,56,602", card["value"])
        self.assertEqual(card["label"], "Total Sales")
        self.assertIn("Transactions", card["subtext"])

    def test_period_block_from_date_filters(self):
        blocks = block_builder.build_simple_blocks(
            label="Total Sales", value=100, unit="currency",
            currency="INR", record_count=5, source_report="Sales Report",
            filters={"start_date": "2026-09-01", "end_date": "2026-09-15"},
        )
        period = next(b for b in blocks if b.get("type") == "period")
        self.assertEqual(period["label"], "Date Range")
        self.assertEqual(period["value"], "2026-09-01 to 2026-09-15")

    def test_no_period_block_without_dates(self):
        blocks = block_builder.build_simple_blocks(
            label="Total Sales", value=100, unit="currency",
            currency="INR", record_count=5, source_report="Sales Report",
        )
        types = [b.get("type") for b in blocks]
        self.assertNotIn("period", types)

    def test_weight_metric_card_uses_jewelry_unit(self):
        blocks = block_builder.build_simple_blocks(
            label="Gold Weight", value=55680.634, unit="weight",
            currency="INR", record_count=2118, source_report="Sales Report",
            unit_label="gms",
        )
        card = next(b for b in blocks if b.get("type") == "metric_card")
        self.assertIn("gms", card["value"])
        self.assertEqual(card["unit_label"], "gms")


class RankingBlocksRichLayoutTests(unittest.TestCase):
    """build_ranking_blocks emits period + glossary blocks."""

    def _spec(self):
        class S:
            limit = 2
            dimension = "CustomerFullName"
            report_key = "sales_report"
            unit = "currency"
            unit_label = ""
            sort = "desc"
            metric_key = "Amount"
        return S()

    def test_ranking_emits_period_block(self):
        data = {"rows": [
            {"DimensionValue": "Customer A", "MetricValue": 1000},
            {"DimensionValue": "Customer B", "MetricValue": 500},
        ], "raw_rd": [{}]}
        blocks = block_builder.build_ranking_blocks(
            data, self._spec(), "top 2 customers", "Sales Report",
            filters={"start_date": "2026-09-01", "end_date": "2026-09-15"},
        )
        types = [b.get("type") for b in blocks]
        self.assertIn("period", types)
        self.assertIn("table", types)
        self.assertIn("chart", types)

    def test_ranking_weight_emits_glossary(self):
        class S:
            limit = 2
            dimension = "CustomerFullName"
            report_key = "sales_report"
            unit = "weight"
            unit_label = "gms"
            sort = "desc"
            metric_key = "grosswt"
        data = {"rows": [
            {"DimensionValue": "Customer A", "MetricValue": 100.5},
            {"DimensionValue": "Customer B", "MetricValue": 50.2},
        ], "raw_rd": [{}]}
        blocks = block_builder.build_ranking_blocks(
            data, S(), "top 2 by gross weight", "Sales Report",
        )
        types = [b.get("type") for b in blocks]
        self.assertIn("glossary", types)
        glossary = next(b for b in blocks if b.get("type") == "glossary")
        self.assertIn("gms", glossary["terms"])


class MultiMetricBlocksRichLayoutTests(unittest.TestCase):
    """build_multi_metric_blocks emits period + glossary blocks."""

    def test_emits_period_and_glossary(self):
        results = [
            {"metric_key": "GoldAmt", "value": 100000, "unit": "currency", "unit_label": "", "label": "Gold Value"},
            {"metric_key": "GoldWt", "value": 55.5, "unit": "weight", "unit_label": "gms", "label": "Gold Wt"},
        ]
        blocks = block_builder.build_multi_metric_blocks(
            results, "gold details", 100, "Sales Report",
            filters={"start_date": "2026-09-01"},
        )
        types = [b.get("type") for b in blocks]
        self.assertIn("period", types)
        self.assertIn("table", types)
        self.assertIn("glossary", types)


class BlockModelValidationTests(unittest.TestCase):
    """New block types validate against Pydantic models."""

    def test_metric_card_block(self):
        b = MetricCardBlock(type="metric_card", label="Total Sales", value="₹2.47 crore")
        self.assertEqual(b.label, "Total Sales")
        self.assertEqual(b.unit, "")  # default

    def test_breakdown_block(self):
        b = BreakdownBlock(type="breakdown", rows=[["Metal", "₹1,00,000", "50.000 g", "—"]])
        self.assertEqual(len(b.rows), 1)
        self.assertEqual(b.columns, ["Component", "Amount", "Weight", "Pieces"])  # default

    def test_period_block(self):
        b = PeriodBlock(type="period", value="2026-09-01 to 2026-09-15")
        self.assertEqual(b.value, "2026-09-01 to 2026-09-15")

    def test_glossary_block(self):
        b = GlossaryBlock(type="glossary", terms={"ct": "Carat", "g": "Gram"})
        self.assertEqual(b.terms["ct"], "Carat")


class BlocksToTextTests(unittest.TestCase):
    """blocks_to_text flattens the new block types for the `answer` field."""

    def test_metric_card_to_text(self):
        blocks = [{"type": "metric_card", "label": "Total Sales", "value": "₹2.47 crore", "subtext": "Transactions: 2,123"}]
        text = block_builder.blocks_to_text(blocks)
        self.assertIn("Total Sales: ₹2.47 crore", text)
        self.assertIn("Transactions: 2,123", text)

    def test_period_to_text(self):
        blocks = [{"type": "period", "label": "Date Range", "value": "2026-09-01 to 2026-09-15"}]
        text = block_builder.blocks_to_text(blocks)
        self.assertIn("Date Range: 2026-09-01 to 2026-09-15", text)

    def test_glossary_to_text(self):
        blocks = [{"type": "glossary", "title": "Glossary", "terms": {"ct": "Carat", "g": "Gram"}}]
        text = block_builder.blocks_to_text(blocks)
        self.assertIn("Glossary", text)
        self.assertIn("ct = Carat", text)

    def test_breakdown_to_text(self):
        blocks = [{"type": "breakdown", "title": "Breakdown", "columns": ["Component", "Amount"],
                   "rows": [["Metal", "₹1,00,000"]]}]
        text = block_builder.blocks_to_text(blocks)
        self.assertIn("Component", text)
        self.assertIn("Metal", text)


class MetricDisplayLabelsTests(unittest.TestCase):
    """Jewelry-native metric display labels are used in multi-metric tables."""

    def test_gold_amount_label(self):
        self.assertEqual(block_builder._METRIC_DISPLAY_LABELS["GoldAmt"], "Gold Value")

    def test_making_charges_label(self):
        self.assertEqual(block_builder._METRIC_DISPLAY_LABELS["LabourAmount"], "Making Charges")

    def test_diamond_ctw_label(self):
        self.assertEqual(block_builder._METRIC_DISPLAY_LABELS["dctw"], "Diamond Ctw")

    def test_gross_wt_label(self):
        self.assertEqual(block_builder._METRIC_DISPLAY_LABELS["grosswt"], "Gross Wt")

    def test_metal_value_label(self):
        self.assertEqual(block_builder._METRIC_DISPLAY_LABELS["MetalAmount"], "Metal Value")


class ParseLlmBlocksTests(unittest.TestCase):
    """parse_llm_blocks validates LLM JSON output and falls back to an error block."""

    def test_valid_json_multiple_blocks(self):
        raw = json.dumps({"blocks": [
            {"type": "text", "content": "Total sales for the period."},
            {"type": "metric_card", "label": "Total Sales", "value": "₹2.47 crore",
             "raw_value": 247556602.25, "unit": "currency", "currency": "INR",
             "subtext": "Transactions: 2,123"},
            {"type": "period", "label": "Date Range", "value": "2026-09-01 to 2026-09-15"},
        ]})
        blocks = block_builder.parse_llm_blocks(raw)
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[0]["type"], "text")
        self.assertEqual(blocks[1]["type"], "metric_card")
        self.assertEqual(blocks[2]["type"], "period")

    def test_invalid_json_returns_error_block(self):
        blocks = block_builder.parse_llm_blocks("not json at all")
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "error")
        self.assertIn("try again", blocks[0]["content"].lower())

    def test_missing_blocks_array_returns_error(self):
        blocks = block_builder.parse_llm_blocks(json.dumps({"answer": "hi"}))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "error")

    def test_empty_blocks_array_returns_error(self):
        blocks = block_builder.parse_llm_blocks(json.dumps({"blocks": []}))
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "error")

    def test_unknown_block_type_skipped(self):
        raw = json.dumps({"blocks": [
            {"type": "text", "content": "kept"},
            {"type": "unknown_type", "content": "dropped"},
        ]})
        blocks = block_builder.parse_llm_blocks(raw)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "text")
        self.assertEqual(blocks[0]["content"], "kept")

    def test_invalid_block_skipped(self):
        # table block missing required "rows" field should be dropped
        raw = json.dumps({"blocks": [
            {"type": "table", "columns": ["A", "B"]},
            {"type": "text", "content": "kept"},
        ]})
        blocks = block_builder.parse_llm_blocks(raw)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "text")

    def test_all_blocks_invalid_returns_error(self):
        raw = json.dumps({"blocks": [
            {"type": "table", "columns": ["A"]},  # missing rows
            {"type": "chart"},  # missing required fields
        ]})
        blocks = block_builder.parse_llm_blocks(raw)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "error")

    def test_non_dict_block_skipped(self):
        raw = json.dumps({"blocks": ["just a string", 42, {"type": "text", "content": "kept"}]})
        blocks = block_builder.parse_llm_blocks(raw)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["content"], "kept")

    def test_breakdown_block_validates(self):
        raw = json.dumps({"blocks": [
            {"type": "breakdown", "title": "Component Breakdown",
             "rows": [["Metal", "₹1,00,000", "50.000 g", "—"]]},
        ]})
        blocks = block_builder.parse_llm_blocks(raw)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["type"], "breakdown")
        self.assertEqual(blocks[0]["rows"][0][0], "Metal")


class NoDataBlocksTests(unittest.TestCase):
    """build_no_data_blocks emits a friendly no-data response with suggestions."""

    def test_emits_text_and_suggestions(self):
        blocks = block_builder.build_no_data_blocks(
            "Total Sales", {}, "Sales Report",
        )
        types = [b.get("type") for b in blocks]
        self.assertIn("text", types)
        self.assertIn("suggestions", types)
        text_block = next(b for b in blocks if b.get("type") == "text")
        self.assertIn("no total sales found", text_block["content"].lower())
        suggestions = next(b for b in blocks if b.get("type") == "suggestions")
        self.assertGreater(len(suggestions["items"]), 0)

    def test_emits_period_block_when_filters_present(self):
        blocks = block_builder.build_no_data_blocks(
            "Total Sales",
            {"start_date": "2026-09-01", "end_date": "2026-09-15"},
            "Sales Report",
        )
        types = [b.get("type") for b in blocks]
        self.assertIn("period", types)
        period = next(b for b in blocks if b.get("type") == "period")
        self.assertEqual(period["label"], "Date Range")
        self.assertEqual(period["value"], "2026-09-01 to 2026-09-15")

    def test_no_period_block_without_filters(self):
        blocks = block_builder.build_no_data_blocks(
            "Total Sales", {}, "Sales Report",
        )
        types = [b.get("type") for b in blocks]
        self.assertNotIn("period", types)

    def test_emits_sources_line(self):
        blocks = block_builder.build_no_data_blocks(
            "Total Sales", {}, "Sales Report",
        )
        sources_blocks = [b for b in blocks if b.get("type") == "sources"]
        self.assertEqual(len(sources_blocks), 1)
        self.assertIn("Sales Report", sources_blocks[0]["items"])

    def test_suggestions_are_relevant_to_label(self):
        blocks = block_builder.build_no_data_blocks(
            "Gold Weight", {}, "Sales Report",
        )
        suggestions = next(b for b in blocks if b.get("type") == "suggestions")
        joined = " ".join(suggestions["items"]).lower()
        # Should reference the metric or common follow-ups
        self.assertTrue("gold weight" in joined or "top 5" in joined)


if __name__ == "__main__":
    unittest.main()
