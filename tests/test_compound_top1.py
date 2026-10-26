"""Regression tests for production feedback failures:

- Top-1 breakdown ("most valuable sold tag") must render the winning
  entity, not collapse to a bare scalar.
- "tag"/"sold tag"/"serial" terminology maps to the barcode dimension.
- Dimension headers never leak `desc` prompt-instructional text.
- `why did <entity> perform well?` resolves the entity type from the
  previous ranking's dimension instead of always clarifying.
- Compound questions ("X and Y") split into independent governed clauses.
- SQL `ai_where` fragments never leak into why-headings.
"""
import asyncio
import unittest

from app.services import block_builder
from app.services.answer_generator import _why_entity_name, generate_answer
from app.services.column_registry import get_dimension_aliases, get_dimension_headers
from app.services.context_resolver import (
    contextual_entity_inject,
    split_compound_question,
)
from app.services.intent import IntentSpec
from app.services.query_planner import _apply_ranking_overrides
from app.services.semantic_query_parser import ParseResult


def _spec(**kw):
    spec = IntentSpec(report_key="sales_report", intent="breakdown")
    for k, v in kw.items():
        setattr(spec, k, v)
    return spec


def _parse(q, **over):
    base = {"report_key": "sales_report", "metric": "Amount"}
    base.update(over)
    return ParseResult(base)


class _Entry:
    report_key = "sales_report"


class TopOneRankingTests(unittest.TestCase):
    """limit=1 + dimension must render the winning entity — the entity IS
    the answer to 'most valuable sold tag'."""

    _data = {
        "rows": [{"DimensionValue": "1/12197", "MetricValue": 12300000}],
        "raw_rd": [{}],
    }

    def test_wide_mode_renders_top1_entity(self):
        spec = _spec(dimension="StockBarcode", limit=1, sort="desc",
                     metric_key="Amount", unit="currency", aggregation="sum")
        blocks = asyncio.run(generate_answer(
            self._data, _Entry(), {}, [], "most valuable sold tag in last month",
            intent_spec=spec, response_mode="wide",
        ))
        table = next(b for b in blocks if b.get("type") == "table")
        flat = str(table)
        self.assertIn("1/12197", flat)

    def test_normal_mode_renders_top1_entity(self):
        spec = _spec(dimension="StockBarcode", limit=1, sort="desc",
                     metric_key="Amount", unit="currency", aggregation="sum")
        text = asyncio.run(generate_answer(
            self._data, _Entry(), {}, [], "most valuable sold tag in last month",
            intent_spec=spec, response_mode="normal",
        ))
        self.assertIn("1/12197", text)

    def test_top1_without_entity_falls_back_to_scalar(self):
        # A dimension parse whose returned row carries no entity value must
        # not print a one-row table of blanks.
        data = {"rows": [{"DimensionValue": "", "MetricValue": 5500}],
                "raw_rd": [{}]}
        spec = _spec(dimension="StockBarcode", limit=1, sort="desc",
                     metric_key="Amount", unit="currency", aggregation="sum")
        text = asyncio.run(generate_answer(
            data, _Entry(), {}, [], "total sales",
            intent_spec=spec, response_mode="normal",
        ))
        self.assertNotIn("| 1 |", text)


class TagAliasTests(unittest.TestCase):
    def test_tag_terms_map_to_barcode_dimension(self):
        aliases = get_dimension_aliases("sales_report")
        for term in ("tag", "sold tag", "tag no", "serial", "serial no"):
            self.assertEqual(aliases.get(term), "StockBarcode", term)

    def test_dimension_header_never_leaks_desc(self):
        headers = get_dimension_headers("sales_report")
        for name, label in headers.items():
            self.assertNotIn("— derived", label, name)
            self.assertNotIn("not a separate column", label, name)
        # StockBarcode shares its sql name with serialjobno/jobno_excel —
        # the base column's label must win, not a derived column's desc.
        self.assertEqual(headers.get("StockBarcode"), "Barcode")


class WhyEntitySqlLeakTests(unittest.TestCase):
    """The 'Why ) LIKE performed well' heading bug — entity extraction must
    never surface SQL fragments."""

    def test_like_clause_extracts_literal(self):
        spec = _spec(ai_where="CustomerFullName LIKE '%Tushar%'")
        name = _why_entity_name({}, spec)
        self.assertNotIn("LIKE", name or "")
        self.assertNotIn(")", name or "")

    def test_like_clause_no_match_returns_clean(self):
        spec = _spec(ai_where="x LIKE 'a%' ) AND ( y LIKE '%z'")
        name = _why_entity_name({}, spec)
        self.assertNotIn(") LIKE", name or "")


class ContextualEntityTests(unittest.TestCase):
    def test_value_from_prior_ranking_resolves_dimension(self):
        prev = ("Revenue by Design No\n| Rank | Design No | Revenue |\n"
                "| 1 | 440 | ₹1.23 crore |\n| 2 | H_FR37 | ₹47.25 lakh |")
        aliases = get_dimension_aliases("sales_report")
        inject = contextual_entity_inject("440", "designno", prev, aliases)
        self.assertEqual(inject, "design")

    def test_value_not_in_prior_answer_stays_ambiguous(self):
        prev = "| Rank | Design No | Revenue |\n| 1 | 440 | ₹1.23 crore |"
        aliases = get_dimension_aliases("sales_report")
        self.assertEqual(
            contextual_entity_inject("999", "designno", prev, aliases), "")

    def test_no_prior_dimension_stays_ambiguous(self):
        aliases = get_dimension_aliases("sales_report")
        self.assertEqual(
            contextual_entity_inject("440", "", "| 1 | 440 |", aliases), "")

    def test_substring_inside_larger_number_does_not_match(self):
        prev = "| Rank | Design No | Revenue |\n| 1 | 14407 | ₹1.00 crore |"
        aliases = get_dimension_aliases("sales_report")
        self.assertEqual(
            contextual_entity_inject("440", "designno", prev, aliases), "")


class SuperlativePlannerTests(unittest.TestCase):
    """Deterministic superlative handling — 'most valuable X' is a top-1
    breakdown of X, regardless of what the LLM picked."""

    def _plan(self, q, **over):
        p = _parse(q, **over)
        _apply_ranking_overrides(p, q)
        return p

    def test_most_valuable_sold_tag(self):
        p = self._plan("most valuable sold tag in last month")
        self.assertEqual(p.dimension, "StockBarcode")
        self.assertEqual(p.limit, 1)
        self.assertEqual(p.sort, "desc")

    def test_wrong_llm_dimension_retargeted(self):
        # LLM grouped 'most valuable sold tag' by categoryname — the named
        # noun wins.
        p = self._plan("most valuable sold tag in last month",
                       dimension="categoryname", limit=50)
        self.assertEqual(p.dimension, "StockBarcode")
        self.assertEqual(p.limit, 1)

    def test_highest_selling_category(self):
        p = self._plan("highest selling category last month")
        self.assertEqual(p.dimension, "categoryname")
        self.assertEqual(p.limit, 1)
        self.assertEqual(p.sort, "desc")

    def test_worst_branch_sorts_asc(self):
        p = self._plan("worst performing branch")
        self.assertEqual(p.sort, "asc")

    def test_scalar_superlative_untouched(self):
        # 'highest sales' names a metric, not a dimension — stays scalar
        p = self._plan("highest sales")
        self.assertFalse(p.dimension)

    def test_plural_tail_keeps_llm_limit(self):
        p = self._plan("top designs this month", dimension="designno", limit=10)
        self.assertEqual(p.limit, 10)


class CompoundSplitTests(unittest.TestCase):
    def test_category_and_tag_split(self):
        parts = split_compound_question(
            "last month highest selling category and most valuable sold tag")
        self.assertEqual(len(parts), 2)
        self.assertIn("category", parts[0])
        self.assertIn("tag", parts[1])
        # shared date phrase inherited by the undated clause
        self.assertIn("last month", parts[1])

    def test_metric_pair_is_not_compound(self):
        # "wt and pcs" is one metric set — extra_metrics handles it
        self.assertEqual(
            split_compound_question("today total diamind wt and pcs"),
            ["today total diamind wt and pcs"])

    def test_two_metric_nouns_not_compound(self):
        # no entity ask -> keep single so extra_metrics renders one table
        self.assertEqual(
            split_compound_question("total sales and net weight"),
            ["total sales and net weight"])

    def test_entity_name_with_and_not_split(self):
        self.assertEqual(
            split_compound_question("Tushar and Sons sales"),
            ["Tushar and Sons sales"])

    def test_two_top_lists_split(self):
        parts = split_compound_question(
            "top 5 designs and top 5 customers this month")
        self.assertEqual(len(parts), 2)
        self.assertIn("this month", parts[0])
        self.assertIn("this month", parts[1])

    def test_bare_by_modifier_not_independent(self):
        self.assertEqual(
            split_compound_question("sales by branch and by month"),
            ["sales by branch and by month"])

    def test_single_question_untouched(self):
        q = "most valuable sold tag in last month"
        self.assertEqual(split_compound_question(q), [q])


class CrossReportSplitTests(unittest.TestCase):
    """No entity ask is fine when each clause clearly targets a different
    report — extra_metrics can only add metrics *within* one report, so
    'total sales and total GST' must split for both halves to be answered."""

    def test_sales_and_tax_split(self):
        parts = split_compound_question(
            "Show total sales this month and total GST collected this month")
        self.assertEqual(len(parts), 2)

    def test_orders_and_wip_split(self):
        parts = split_compound_question(
            "How many orders were booked last month and how many jobs are in production")
        self.assertEqual(len(parts), 2)
        # shared date phrase copied to the undated clause
        self.assertIn("last month", parts[1])

    def test_three_way_cross_report_split(self):
        parts = split_compound_question(
            "Total sales this month and tax collected this month and jobs in production")
        self.assertEqual(len(parts), 3)

    def test_same_report_metric_pair_stays_single(self):
        # gold/silver are both sales_report vocabulary — not cross-report
        self.assertEqual(
            split_compound_question("gross wt and net wt"),
            ["gross wt and net wt"])

    def test_unresolvable_clause_stays_single(self):
        # second clause has no report signal — splitting would orphan it
        self.assertEqual(
            split_compound_question("total sales and something vague"),
            ["total sales and something vague"])


if __name__ == "__main__":
    unittest.main()
