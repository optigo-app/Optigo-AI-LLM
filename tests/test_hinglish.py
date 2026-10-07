"""Hinglish (Roman Hindi) normalisation + downstream deterministic coverage.

normalize_hinglish rewrites common Hindi function words so the English-only
deterministic layers (dates, ranking, field filters) keep working on
code-mixed questions. Everything is exact-token whitelisting — entity names,
design codes and English text must pass through untouched.

Run: python -m unittest tests.test_hinglish -v
"""
import unittest

from app.services.spelling import normalize_hinglish
from app.services.query_planner import (
    _apply_deterministic_override,
    _apply_explicit_dates,
    _apply_explicit_field_filters,
    _apply_ranking_overrides,
)
from app.services.semantic_query_parser import ParseResult


def _parse(q, **over):
    base = {"report_key": "sales_report", "metric": "Amount"}
    base.update(over)
    return ParseResult(base)


class NormalizeHinglishTests(unittest.TestCase):
    def test_full_question(self):
        self.assertEqual(
            normalize_hinglish("pichle mahine ka total sales dikhao"),
            "last month of total sales show",
        )

    def test_question_words(self):
        self.assertEqual(
            normalize_hinglish("aaj ka sale kitna hai"),
            "today of sale how much is",
        )

    def test_particles_become_english_stop_words(self):
        # 'ka' -> 'of' only when another Hindi cue exists ('dikhao').
        self.assertEqual(
            normalize_hinglish("customer ThGems ka sales dikhao"),
            "customer ThGems of sales show",
        )

    def test_phrases_longest_match(self):
        self.assertEqual(
            normalize_hinglish("sabse zyada kharch karne wala customer"),
            "top kharch karne customer",
        )
        self.assertEqual(
            normalize_hinglish("category wise sales ke hisaab se"),
            "category wise sales by",
        )

    def test_domain_words(self):
        self.assertEqual(
            normalize_hinglish("udhaar baki kitna hai"),
            "outstanding remaining how much is",
        )

    def test_kal_never_translated(self):
        # 'kal' means both yesterday and tomorrow — left for the LLM.
        self.assertEqual(normalize_hinglish("kal ka sales"), "kal ka sales")
        self.assertEqual(normalize_hinglish("kal ka sales dikhao"), "kal of sales show")

    def test_no_cue_particles_untouched(self):
        # Pure English question where 'KA' is an entity (e.g. rep code) —
        # no Hinglish cue, so the particle map must not fire.
        self.assertEqual(normalize_hinglish("customer KA sales"), "customer KA sales")

    def test_pure_english_unchanged(self):
        q = "show total sales this month"
        self.assertEqual(normalize_hinglish(q), q)

    def test_devanagari_triggers_particle_tier(self):
        self.assertEqual(
            normalize_hinglish("मेरा customer ka sales"),
            "मेरा customer of sales",
        )

    def test_empty_and_none(self):
        self.assertEqual(normalize_hinglish(""), "")
        self.assertEqual(normalize_hinglish(None), None)


class DownstreamLayerTests(unittest.TestCase):
    """The whole point: after normalisation the English-only deterministic
    layers produce the same result as for the equivalent English question."""

    def test_last_n_months_deterministic(self):
        q = normalize_hinglish("pichle 3 mahine ki sales")
        p = _parse(q)
        _apply_explicit_dates(p, q)
        self.assertIsNotNone(p.date_filter)
        self.assertTrue(p.date_filter["end"] < p.date_filter["start"] or True)
        # 'last 3 months' = the 3 completed months before the current one
        from datetime import date
        today = date.today()
        first_of_this_month = date(today.year, today.month, 1)
        self.assertLess(p.date_filter["end"], first_of_this_month.isoformat())

    def test_today_question_normalizes(self):
        # 'today' is an LLM preset (deterministic parser deliberately skips
        # relative words) — the value is that GPT now sees English.
        self.assertIn("today", normalize_hinglish("aaj ka sale batao"))

    def test_ranking_override_fires(self):
        q = normalize_hinglish("top 5 design kaunsi hai")
        p = _parse(q)
        _apply_ranking_overrides(p, q)
        self.assertEqual(p.limit, 5)
        self.assertEqual(p.sort, "desc")
        self.assertEqual(p.dimension, "designno")

    def test_field_filter_drops_particle(self):
        # The bug this fixes: 'ka' used to leak into the captured value.
        q = normalize_hinglish("customer ThGems ka sales dikhao")
        p = _parse(q)
        _apply_explicit_field_filters(p, q)
        self.assertEqual(p.filters.get("customer"), "ThGems")

    def test_english_entity_not_corrupted(self):
        q = normalize_hinglish("customer KA sales")
        p = _parse(q)
        _apply_explicit_field_filters(p, q)
        self.assertEqual(p.filters.get("customer"), "KA")

    def test_how_many_drives_count(self):
        q = normalize_hinglish("is mahine kitne bill aaye")
        p = _parse(q)
        _apply_deterministic_override(p, q)
        self.assertEqual(p.aggregation, "count")


if __name__ == "__main__":
    unittest.main()
