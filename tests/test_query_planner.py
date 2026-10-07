"""Deterministic query-planner coverage — every enumerable phrasing users
type must resolve without depending on LLM reliability.

Run: python -m unittest tests.test_query_planner -v
"""
import unittest
from datetime import date, timedelta
from unittest.mock import AsyncMock, patch

_TODAY = date.today().isoformat()

from app.services.query_planner import (
    _apply_deterministic_override,
    _apply_explicit_dates,
    _apply_ranking_overrides,
    _apply_dimension_words,
    _apply_metric_override,
    finalize_query,
    plan_query,
)
from app.services.semantic_query_parser import ParseResult


def _parse(q, **over):
    base = {"report_key": "sales_report", "metric": "Amount"}
    base.update(over)
    return ParseResult(base)


def _dates(q, **over):
    p = _parse(q, **over)
    _apply_explicit_dates(p, q)
    return p


class NamedMonthTests(unittest.TestCase):
    def test_two_months_abbrev(self):
        p = _dates("compare total sales aug and sep")
        self.assertEqual(p.date_filter, {"start": "2026-08-01", "end": "2026-09-30"})
        self.assertEqual(p.dimension, "month")

    def test_two_months_full_names(self):
        p = _dates("compare total sales august and september")
        self.assertEqual(p.date_filter, {"start": "2026-08-01", "end": "2026-09-30"})
        self.assertEqual(p.dimension, "month")

    def test_single_month_scalar(self):
        p = _dates("total sales in august")
        self.assertEqual(p.date_filter, {"start": "2026-08-01", "end": "2026-08-31"})
        self.assertIsNone(p.dimension)

    def test_future_month_goes_previous_year(self):
        # Today is Oct 2026 — 'december' means Dec 2025.
        p = _dates("sales in december")
        self.assertEqual(p.date_filter, {"start": "2025-12-01", "end": "2025-12-31"})

    def test_month_with_explicit_year(self):
        p = _dates("sales in march 2025")
        self.assertEqual(p.date_filter, {"start": "2025-03-01", "end": "2025-03-31"})

    def test_month_last_year(self):
        p = _dates("august sales last year")
        self.assertEqual(p.date_filter, {"start": "2025-08-01", "end": "2025-08-31"})

    def test_month_this_year(self):
        p = _dates("august sales this year")
        self.assertEqual(p.date_filter, {"start": "2026-08-01", "end": "2026-08-31"})

    def test_cross_year_month_pairing(self):
        p = _dates("jan 2025 to mar 2026 sales")
        self.assertEqual(p.date_filter, {"start": "2025-01-01", "end": "2026-03-31"})

    def test_relative_collision_skipped(self):
        for q in ("total sales this month", "compare this month vs august",
                  "sales last week", "sales today"):
            p = _dates(q)
            self.assertIsNone(p.date_filter, q)


class ExplicitDateTests(unittest.TestCase):
    def test_day_month_name(self):
        for q in ("sales on 15 aug", "sales on 15th august", "august 15 sales",
                  "aug 15, 2026 sales"):
            p = _dates(q)
            self.assertEqual(p.date_filter["start"], "2026-08-15", q)
            self.assertEqual(p.date_filter["end"], "2026-08-15", q)

    def test_numeric_dates(self):
        p = _dates("sales on 15/08/2026")
        self.assertEqual(p.date_filter, {"start": "2026-08-15", "end": "2026-08-15"})
        p = _dates("sales on 2026-08-15")
        self.assertEqual(p.date_filter, {"start": "2026-08-15", "end": "2026-08-15"})

    def test_two_dates_become_range(self):
        p = _dates("aug 15 to sep 20 sales")
        self.assertEqual(p.date_filter, {"start": "2026-08-15", "end": "2026-09-20"})


class RelativeRangeTests(unittest.TestCase):
    def test_last_n_months_complete(self):
        p = _dates("last 3 months sales")
        self.assertEqual(p.date_filter, {"start": "2026-07-01", "end": "2026-09-30"})

    def test_past_n_months_rolling(self):
        p = _dates("past 3 months sales")
        self.assertEqual(p.date_filter, {"start": "2026-08-01", "end": _TODAY})

    def test_last_n_days(self):
        p = _dates("last 7 days sales")
        self.assertEqual(p.date_filter, {"start": (date.today() - timedelta(days=6)).isoformat(), "end": _TODAY})

    def test_last_n_years(self):
        p = _dates("last 2 years sales")
        self.assertEqual(p.date_filter, {"start": "2024-01-01", "end": "2025-12-31"})

    def test_since_month(self):
        p = _dates("sales since january")
        self.assertEqual(p.date_filter, {"start": f"{date.today().year}-01-01", "end": _TODAY})

    def test_since_month_year(self):
        p = _dates("sales since aug 2025")
        self.assertEqual(p.date_filter, {"start": "2025-08-01", "end": _TODAY})

    def test_since_year(self):
        p = _dates("sales since 2024")
        self.assertEqual(p.date_filter, {"start": "2024-01-01", "end": _TODAY})


class QuarterTests(unittest.TestCase):
    def test_named_quarter(self):
        p = _dates("q3 sales")
        self.assertEqual(p.date_filter, {"start": "2026-07-01", "end": "2026-09-30"})

    def test_completed_quarter_rolls_back(self):
        # Oct 2026 is Q4 — bare 'q4' means last completed Q4 (2025).
        p = _dates("q4 sales")
        self.assertEqual(p.date_filter, {"start": "2025-10-01", "end": "2025-12-31"})

    def test_this_quarter(self):
        p = _dates("this quarter sales")
        self.assertEqual(p.date_filter, {"start": "2026-10-01", "end": _TODAY})

    def test_last_quarter(self):
        p = _dates("last quarter sales")
        self.assertEqual(p.date_filter, {"start": "2026-07-01", "end": "2026-09-30"})

    def test_quarter_compare_spans_both(self):
        p = _dates("compare q1 and q2 sales")
        self.assertEqual(p.date_filter, {"start": "2026-01-01", "end": "2026-06-30"})
        self.assertEqual(p.dimension, "month")


class FinancialYearTests(unittest.TestCase):
    def test_fy_two_digit(self):
        p = _dates("fy26 sales")
        self.assertEqual(p.date_filter, {"start": "2025-04-01", "end": "2026-03-31"})

    def test_fy_full_range(self):
        p = _dates("fy 2025-26 sales")
        self.assertEqual(p.date_filter, {"start": "2025-04-01", "end": "2026-03-31"})

    def test_this_financial_year(self):
        p = _dates("this financial year sales")
        self.assertEqual(p.date_filter, {"start": "2026-04-01", "end": _TODAY})

    def test_last_fy(self):
        p = _dates("last fy sales")
        self.assertEqual(p.date_filter, {"start": "2025-04-01", "end": "2026-03-31"})


class YearTests(unittest.TestCase):
    def test_single_year_with_prep(self):
        p = _dates("sales in 2025")
        self.assertEqual(p.date_filter, {"start": "2025-01-01", "end": "2025-12-31"})

    def test_year_compare(self):
        p = _dates("compare 2024 and 2026 sales")
        self.assertEqual(p.date_filter, {"start": "2024-01-01", "end": "2026-12-31"})
        self.assertEqual(p.dimension, "year")

    def test_bare_number_not_year(self):
        # 'design 2025' — the 4-digit could be an entity ID, no prep context.
        p = _dates("design 2025 total sales")
        self.assertIsNone(p.date_filter)


class RankingTests(unittest.TestCase):
    def test_top_n_limit(self):
        p = _parse("top 3 customers by sales", dimension="CustomerFullName")
        _apply_ranking_overrides(p, "top 3 customers by sales")
        self.assertEqual(p.limit, 3)
        self.assertEqual(p.sort, "desc")

    def test_top_n_sets_dimension(self):
        p = _parse("top 5 customers", dimension=None)
        _apply_ranking_overrides(p, "top 5 customers")
        self.assertEqual(p.dimension, "CustomerFullName")
        self.assertEqual(p.limit, 5)

    def test_bottom_n_asc(self):
        p = _parse("bottom 5 designs", dimension="designno")
        _apply_ranking_overrides(p, "bottom 5 designs")
        self.assertEqual(p.limit, 5)
        self.assertEqual(p.sort, "asc")

    def test_worst_no_number(self):
        p = _parse("worst customers by sales", dimension="CustomerFullName")
        _apply_ranking_overrides(p, "worst customers by sales")
        self.assertEqual(p.sort, "asc")

    def test_last_n_months_not_a_limit(self):
        p = _parse("sales last 3 months", dimension=None)
        _apply_ranking_overrides(p, "sales last 3 months")
        self.assertEqual(p.limit, 1)


class MetricOverrideTests(unittest.TestCase):
    def test_weight_alias(self):
        p = _parse("total gold weight this month")
        _apply_metric_override(p, "total gold weight this month")
        self.assertEqual(p.metric, "GoldWt")

    def test_discount_alias(self):
        p = _parse("total discount in august")
        _apply_metric_override(p, "total discount in august")
        self.assertEqual(p.metric, "Discount")

    def test_default_metric_kept(self):
        p = _parse("total sales in august")
        _apply_metric_override(p, "total sales in august")
        self.assertEqual(p.metric, "Amount")

    def test_nondefault_metric_not_overridden(self):
        # LLM already picked a non-default metric — don't second-guess it.
        p = _parse("gold weight by customer", metric="GoldWt")
        _apply_metric_override(p, "gold weight by customer")
        self.assertEqual(p.metric, "GoldWt")


class DimensionWordTests(unittest.TestCase):
    def test_wise(self):
        p = _parse("category wise sales")
        _apply_dimension_words(p, "category wise sales")
        self.assertEqual(p.dimension, "categoryname")

    def test_by(self):
        p = _parse("sales by customer")
        _apply_dimension_words(p, "sales by customer")
        self.assertEqual(p.dimension, "CustomerFullName")

    def test_per(self):
        p = _parse("sales per branch")
        _apply_dimension_words(p, "sales per branch")
        # 'branch' resolves via the report's dimension_aliases (locker column
        # is where branch lives in this ERP's data model).
        self.assertEqual(p.dimension, "Mastermanagement_FG_StockLockername")

    def test_existing_dimension_kept(self):
        p = _parse("sales by customer", dimension="designno")
        _apply_dimension_words(p, "sales by customer")
        self.assertEqual(p.dimension, "designno")

    def test_unknown_word_ignored(self):
        p = _parse("sales per gram")
        _apply_dimension_words(p, "sales per gram")
        self.assertIsNone(p.dimension)


class AggregationTests(unittest.TestCase):
    def _full(self, q, **over):
        p = _parse(q, **over)
        _apply_deterministic_override(p, q)
        return p

    def test_how_many_is_count(self):
        p = self._full("how many bills in august")
        self.assertEqual(p.aggregation, "count")

    def test_unique_is_distinct(self):
        p = self._full("how many unique customers")
        self.assertIn(p.aggregation, ("count", "count_distinct"))

    def test_average_is_avg(self):
        p = self._full("average bill value")
        self.assertEqual(p.aggregation, "avg")


class RelativePresetTests(unittest.TestCase):
    """tomorrow/next-week/next-month must resolve deterministically —
    the LLM was observed guessing 'tomorrow' as 14 May 2025."""

    def _full(self, q, **over):
        p = _parse(q, **over)
        _apply_deterministic_override(p, q)
        return p

    def test_tomorrow(self):
        p = self._full("tomorrow total sale")
        self.assertEqual(p.date_filter, {"preset": "tomorrow"})
        vf = p.to_validated_filters()
        self.assertEqual(vf["start_date"], vf["end_date"])
        self.assertGreater(vf["start_date"], date.today().isoformat())

    def test_day_after_tomorrow(self):
        p = self._full("day after tomorrow sales")
        vf = p.to_validated_filters()
        self.assertEqual(vf["start_date"], vf["end_date"])

    def test_next_month(self):
        p = self._full("next month sales")
        vf = p.to_validated_filters()
        self.assertEqual(vf["start_date"][:7],
                         "2026-11" if date.today().month == 10 else "")

    def test_next_week(self):
        p = self._full("next week orders", report_key="order_report")
        vf = p.to_validated_filters()
        self.assertEqual(vf["start_date"], vf["start_date"])  # non-empty
        self.assertTrue(vf["start_date"])

    def test_today_still_llm(self):
        # 'today' is left to the LLM preset path — no forced override.
        p = self._full("today total sales")
        self.assertIn((p.date_filter or {}).get("preset", ""), ("", "today"))


class HinglishDateTests(unittest.TestCase):
    def _full(self, q, **over):
        p = _parse(q, **over)
        _apply_deterministic_override(p, q)
        return p

    def test_aaj(self):
        p = self._full("aaj kitna sale hua hai")
        self.assertEqual(p.date_filter, {"preset": "today"})

    def test_aj_ka(self):
        p = self._full("aj ka total sale gw")
        self.assertEqual(p.date_filter, {"preset": "today"})
        self.assertEqual(p.metric, "grosswt")  # 'gw' abbreviation

    def test_kal(self):
        p = self._full("kal ka total sale")
        self.assertEqual(p.date_filter, {"preset": "yesterday"})

    def test_is_mahine(self):
        p = self._full("is mahine ka total sale")
        self.assertEqual(p.date_filter, {"preset": "this_month"})

    def test_pichle_mahine(self):
        p = self._full("pichle mahine ka total sale")
        self.assertEqual(p.date_filter, {"preset": "last_month"})


class MetricCompanionTests(unittest.TestCase):
    def _full(self, q, **over):
        p = _parse(q, **over)
        _apply_deterministic_override(p, q)
        return p

    def test_count_gets_amount_companion(self):
        p = self._full("how many bills", metric="total_count", aggregation="count")
        self.assertIn("Amount", p.extra_metrics or [])

    def test_only_skips_companion(self):
        p = self._full("how many bills only", metric="total_count", aggregation="count")
        self.assertEqual(p.extra_metrics or [], [])

    def test_dimension_skips_companion(self):
        p = self._full("bill count by customer",
                       metric="total_count", aggregation="count",
                       dimension="CustomerName")
        self.assertEqual(p.extra_metrics or [], [])

    def test_value_metric_no_companion(self):
        p = self._full("total sales")
        self.assertEqual(p.extra_metrics or [], [])


class AttributeLookupTests(unittest.TestCase):
    def _full(self, q, **over):
        p = _parse(q, **over)
        _apply_deterministic_override(p, q)
        return p

    def test_its_type(self):
        p = self._full("customer code vidsy tell its type")
        self.assertEqual(p.metric, "CustomerType")
        self.assertEqual(p.aggregation, "max")

    def test_normal_question_untouched(self):
        p = self._full("total sales")
        self.assertEqual(p.metric, "Amount")


class RateAggregationTests(unittest.TestCase):
    def test_wastage_defaults_avg(self):
        p = _parse("total wastage")
        _apply_deterministic_override(p, "total wastage")
        plan = p.to_query_plan("total wastage")
        plan.validate_against_registry()
        self.assertEqual(plan.aggregation, "avg")


class AmbiguousEntityTests(unittest.TestCase):
    def test_suggestion_text_not_entity(self):
        from app.services.context_resolver import ambiguous_entity_value
        self.assertIsNone(ambiguous_entity_value(
            "Overall total total sales without filters"))
        self.assertIsNone(ambiguous_entity_value("Top 5 by total sales"))
        self.assertIsNone(ambiguous_entity_value("overall total sales for brand"))

    def test_hinglish_not_entity(self):
        from app.services.context_resolver import ambiguous_entity_value
        self.assertIsNone(ambiguous_entity_value("aaj kitna sale hua hai"))
        self.assertIsNone(ambiguous_entity_value("aj ka total sale gw"))

    def test_person_name_detected(self):
        from app.services.context_resolver import (
            ambiguous_entity_value, is_probable_person_name)
        v = ambiguous_entity_value("DEEPAK PAREEK total sales")
        self.assertEqual(v, "DEEPAK PAREEK")
        self.assertTrue(is_probable_person_name(v))

    def test_code_still_ambiguous(self):
        from app.services.context_resolver import (
            ambiguous_entity_value, is_probable_person_name)
        v = ambiguous_entity_value("TR62 total sales")
        self.assertEqual(v, "TR62")
        self.assertFalse(is_probable_person_name(v))


class ReferentialFollowupTests(unittest.TestCase):
    def test_above_design_is_followup(self):
        from app.services.context_resolver import looks_like_followup
        history = [{"role": "user", "content": "total sales"}]
        # 'above' + a metric word must still resolve against context
        self.assertTrue(looks_like_followup(
            "above design no. total diamond weight?", history))
        self.assertTrue(looks_like_followup(
            "tell me its customer type", history))

    def test_standalone_metric_not_followup(self):
        from app.services.context_resolver import looks_like_followup
        history = [{"role": "user", "content": "total sales today"}]
        self.assertFalse(looks_like_followup("total sales this year", history))


class NameFilterWhereTests(unittest.TestCase):
    def test_filler_words_stripped(self):
        from app.services.parse_result import ParseResult
        p = ParseResult({"report_key": "sales_report", "metric": "Amount",
                         "filters": {"customer": "code vidsy"}})
        where = p.generate_where_clause()
        self.assertIn("'%vidsy%'", where)
        self.assertNotIn("'%code%'", where)

    def test_ai_where_name_duplicate_dropped(self):
        from app.services.parse_result import ParseResult, _split_top_level_and
        p = ParseResult({"report_key": "sales_report", "metric": "Amount",
                         "filters": {"customer": "DEEPAK PAREEK"},
                         "ai_where": "ISNULL(DI.CustomerFullName,'') LIKE '%DEEPAK PAREEK%'"})
        where = p.generate_where_clause()
        # Only the name-filter clause remains — the redundant ai_where on a
        # different name column must not AND-fail the query.
        self.assertIn("'%DEEPAK%'", where)
        self.assertEqual(len(_split_top_level_and(where)), 1)

    def test_ai_where_extra_condition_kept(self):
        from app.services.parse_result import ParseResult
        p = ParseResult({"report_key": "sales_report", "metric": "Amount",
                         "filters": {"customer": "DEEPAK PAREEK"},
                         "ai_where": "ISNULL(DI.CustomerFullName,'') LIKE '%DEEPAK PAREEK%' "
                                     "AND ISNULL(DI.categoryname,'') = 'ring'"})
        where = p.generate_where_clause()
        self.assertIn("ring", where)

    def test_top_level_and_split(self):
        from app.services.parse_result import _split_top_level_and
        parts = _split_top_level_and(
            "(a LIKE 'x' AND b LIKE 'y') AND c = 'A AND B' AND d = 1")
        self.assertEqual(len(parts), 3)


class PresetResolutionTests(unittest.TestCase):
    def test_future_presets_resolve(self):
        from datetime import date, timedelta
        from app.services.query_plan import DateRange
        today = date.today()
        self.assertEqual(
            DateRange(preset="tomorrow").resolved(),
            {"start_date": (today + timedelta(days=1)).isoformat(),
             "end_date": (today + timedelta(days=1)).isoformat()})
        r = DateRange(preset="next_month").resolved()
        self.assertEqual(r["start_date"][:7],
                         (today.replace(day=28) + timedelta(days=5)).strftime("%Y-%m"))


class ParseFailureSafetyTests(unittest.TestCase):
    """An LLM outage must never surface a confident unfiltered grand total."""

    def test_failure_result_clarifies(self):
        from app.services.semantic_query_parser import _parse_failure_result
        p = _parse_failure_result("sales_report")
        self.assertEqual(p.confidence, 0.0)
        self.assertTrue(p.clarify)

    def test_explicit_zero_confidence_not_inflated(self):
        # `x or 0.8` would silently turn a genuine 0.0 into 0.8.
        p = ParseResult({"report_key": "sales_report", "metric": "Amount",
                         "confidence": 0.0})
        self.assertEqual(p.confidence, 0.0)


class CountVsUnitIntentTests(unittest.TestCase):
    """'how many <unit>' is a weight/quantity question, not a row count."""

    def test_how_many_units_is_weight(self):
        from app.services.metric_validator import _detect_intent_type
        for q in ("how many grams of gold", "how many carats in stock",
                  "number of grams used", "how many kg of silver"):
            self.assertEqual(_detect_intent_type(q), "weight", q)

    def test_how_many_entities_still_count(self):
        from app.services.metric_validator import _detect_intent_type
        for q in ("how many bills", "how many customers", "total orders",
                  "number of designs"):
            self.assertEqual(_detect_intent_type(q), "count", q)

    def test_weight_metric_not_overridden_to_count(self):
        from app.services.metric_validator import validate_metric_intent
        # LLM picked the right metric — leave it alone.
        self.assertEqual(
            validate_metric_intent("grosswt", "how many grams of gold",
                                   "sales_report"), "grosswt")
        # LLM picked the wrong type — fall back to a weight metric, never
        # total_count.
        self.assertEqual(
            validate_metric_intent("Amount", "how many grams of gold",
                                   "sales_report"), "grosswt")

    def test_planner_keeps_sum_for_unit_question(self):
        p = _parse("how many grams of gold", metric="grosswt")
        _apply_deterministic_override(p, "how many grams of gold")
        self.assertEqual(p.metric, "grosswt")
        self.assertEqual(p.aggregation, "sum")

    def test_planner_counts_entities(self):
        p = _parse("how many bills")
        _apply_deterministic_override(p, "how many bills")
        self.assertEqual(p.aggregation, "count")


class AliasWordBoundaryTests(unittest.TestCase):
    """validate_metric_intent must not substring-match short aliases."""

    def test_short_alias_no_substring_match(self):
        from unittest.mock import patch
        from app.services import metric_validator
        fake_catalog = {
            "total_count": {"type": "count", "aliases": []},
            "wrong_metric": {"type": "count", "aliases": ["it"]},
        }
        with patch.object(metric_validator, "_get_metric_catalog",
                          return_value=fake_catalog):
            # 'it' substring-matches inside 'without' — a raw `in` check would
            # pick wrong_metric; word boundaries fall back to total_count.
            self.assertEqual(
                metric_validator.validate_metric_intent(
                    "Amount", "how many items without", "sales_report"),
                "total_count")


class DatePredicateStripTests(unittest.TestCase):
    def test_and_inside_literal_preserved(self):
        from app.services.query_planner import _strip_date_predicates
        p = ParseResult({
            "report_key": "sales_report", "metric": "Amount",
            "date_filter": {"start": "2026-08-01", "end": "2026-08-31"},
            "ai_where": ("DI.EntryDate >= '2026-08-01' AND "
                         "ISNULL(DI.CustomerFullName,'') LIKE '%Rock and Roll%'"),
        })
        _strip_date_predicates(p)
        # Date clause dropped; the AND inside the LIKE literal must survive
        # intact — a naive \bAND\b split mangles it into broken SQL.
        self.assertEqual(
            p.ai_where,
            "ISNULL(DI.CustomerFullName,'') LIKE '%Rock and Roll%'")


class DroppedFilterNoticeTests(unittest.TestCase):
    def test_invalid_filter_value_recorded(self):
        p = ParseResult({"report_key": "sales_report", "metric": "Amount",
                         "filters": {"categoryname": "total"}})
        plan = p.to_query_plan("category total sales")
        plan.validate_against_registry()
        vf = plan.validated_filters()
        # 'total' is an invalid value for categoryname — dropped, and the
        # drop is recorded so the answer can disclose it.
        self.assertNotIn("categoryname", vf)
        self.assertEqual(plan.dropped_filters, ["categoryname"])


class KeywordRoutingHintTests(unittest.IsolatedAsyncioTestCase):
    """Keyword routing is a hint — the LLM (full catalog) may overrule it;
    frontend/intent routes still lock the catalog."""

    _REG = {"sales_report": None, "order_report": None}

    @patch("app.services.query_planner.parse_query", new_callable=AsyncMock)
    async def test_llm_overrules_keyword_pick(self, parse):
        # 'turnover' keyword-routes to sales_report but has no intent regex.
        parse.return_value = ParseResult(
            {"report_key": "order_report", "metric": "Amount"})
        result = await plan_query("show turnover please", registry=self._REG)
        # Full catalog was offered (not locked to sales_report) ...
        self.assertEqual(parse.call_args.kwargs["report_name"], "")
        # ... and the LLM's valid choice wins.
        self.assertEqual(result.plan.report_key, "order_report")
        self.assertEqual(result.routing_source, "llm")

    @patch("app.services.query_planner.parse_query", new_callable=AsyncMock)
    async def test_keyword_pick_when_llm_report_invalid(self, parse):
        parse.return_value = ParseResult(
            {"report_key": "nonsense_report", "metric": "Amount"})
        result = await plan_query("show turnover please", registry=self._REG)
        self.assertEqual(result.plan.report_key, "sales_report")
        self.assertEqual(result.routing_source, "keyword")


class ModelConfidenceTests(unittest.TestCase):
    """The LLM's self-confidence survives routing-confidence overwrite so a
    pinned report can still clarify on a confused parse."""

    def test_model_confidence_preserved_on_frontend_route(self):
        p = _parse("something vague", confidence=0.2)
        result = finalize_query(p, "something vague", routing_source="frontend")
        self.assertEqual(result.parsed.confidence, 1.0)        # routing score
        self.assertEqual(result.parsed.model_confidence, 0.2)  # LLM's own score

    def test_model_confidence_preserved_on_keyword_route(self):
        p = _parse("something vague", confidence=0.1)
        result = finalize_query(p, "something vague", routing_source="keyword")
        self.assertEqual(result.parsed.confidence, 0.75)
        self.assertEqual(result.parsed.model_confidence, 0.1)


if __name__ == "__main__":
    unittest.main()
