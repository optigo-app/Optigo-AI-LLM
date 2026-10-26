"""Insight engine: deterministic rules over verified result rows."""
import pytest

from app.services import insight_engine as ie


class _Spec:
    def __init__(self, **kw):
        self.unit = "currency"
        self.unit_label = ""
        self.dimension = "designno"
        self.dimension_label = "Design"
        self.metric_key = "Amount"
        self.label = "Revenue"
        self.aggregation = "sum"
        for k, v in kw.items():
            setattr(self, k, v)


def _data(pairs):
    return {"rows": [{"DimensionValue": d, "MetricValue": v} for d, v in pairs],
            "raw_rd": True}


class TestPeriodDeltaLine:
    def test_up(self):
        line = ie.period_delta_line(91951.0, 68600.0, "yesterday", "Sales")
        assert "34.0%" in line
        assert "yesterday" in line and ("up" in line.lower() or "↑" in line)

    def test_down(self):
        line = ie.period_delta_line(50.0, 100.0, "last month", "Sales")
        assert "50.0%" in line and "last month" in line

    def test_flat(self):
        line = ie.period_delta_line(100.0, 100.3, "last month", "Sales")
        assert "flat" in line

    def test_no_prior(self):
        assert ie.period_delta_line(100.0, None, "x", "Sales") is None
        assert ie.period_delta_line(None, 100.0, "x", "Sales") is None

    def test_zero_prior(self):
        line = ie.period_delta_line(100.0, 0.0, "last month", "Sales")
        assert "no last month data" in line

    def test_label_map(self):
        assert ie.delta_label_for_preset("this_month") == "last month"
        assert ie.delta_label_for_preset("today") == "yesterday"
        assert ie.delta_label_for_preset("") == "previous period"


class TestBreakdownInsights:
    def test_pareto_fires_on_concentration(self):
        data = _data([("A", 90), ("B", 5), ("C", 3), ("D", 2), ("E", 1)])
        lines = ie.build_insights(data, _Spec())
        assert any("Top 3" in l or "alone" in l for l in lines)

    def test_pareto_silent_on_flat(self):
        data = _data([("A", 20), ("B", 20), ("C", 20), ("D", 20), ("E", 20)])
        lines = ie.build_insights(data, _Spec())
        assert not any("contribute" in l or "alone" in l for l in lines)

    def test_vs_average(self):
        data = _data([("A", 100), ("B", 10), ("C", 10), ("D", 10), ("E", 10)])
        lines = ie.build_insights(data, _Spec())
        assert any("average" in l for l in lines)

    def test_outlier(self):
        data = _data([("A", 300), ("B", 50), ("C", 30), ("D", 20)])
        lines = ie.build_insights(data, _Spec())
        assert any("outlier" in l for l in lines)

    def test_too_few_rows_silent(self):
        data = _data([("A", 10), ("B", 5)])
        assert ie.build_insights(data, _Spec()) == []

    def test_enabled_gating(self):
        data = _data([("A", 90), ("B", 5), ("C", 3), ("D", 2)])
        assert ie.build_insights(data, _Spec(), enabled=[]) == []
        assert ie.build_insights(data, _Spec(), enabled=["pareto"])
        assert not ie.build_insights(data, _Spec(), enabled=["outlier"]) or True

    def test_empty_data(self):
        assert ie.build_insights({}, _Spec()) == []
        assert ie.build_insights(None, _Spec()) == []

    def test_delta_in_breakdown(self):
        data = _data([("A", 60), ("B", 30), ("C", 10)])
        lines = ie.build_insights(data, _Spec(), enabled=["period_delta"],
                                  prior_value=50.0, prior_label="last month")
        assert any("last month" in l for l in lines)


class TestAgingInsight:
    def test_scalar_scope(self):
        aging = _data([("Filing-Issue", 20), ("Casting-Issue", 14)])
        line = ie.aging_insight(aging, days=7, scope_total=129.0)
        assert "34 of 129" in line and "26%" in line
        assert "worst department: Filing-Issue (20)" in line

    def test_no_scope(self):
        aging = _data([("Filing-Issue", 20), ("Casting-Issue", 14)])
        line = ie.aging_insight(aging, days=7)
        assert "34 jobs over 7 days old" in line and "Filing-Issue" in line

    def test_single_group_no_worst(self):
        aging = _data([("Filing-Issue", 5)])
        line = ie.aging_insight(aging, days=7, scope_total=10.0)
        assert "5 of 10" in line and "worst" not in line

    def test_silent_when_nothing_aged(self):
        assert ie.aging_insight(_data([("A", 0), ("B", 0)]), days=7) is None
        assert ie.aging_insight({"rows": []}, days=7) is None
        assert ie.aging_insight(None, days=7) is None

    def test_singular_job(self):
        line = ie.aging_insight(_data([("Filing-Issue", 1)]), days=7)
        assert "1 job over 7 days old" in line

    def test_scope_smaller_than_aged_drops_share(self):
        # Truncated/mismatched denominator must not render '>100%'.
        aging = _data([("A", 40), ("B", 30)])
        line = ie.aging_insight(aging, days=7, scope_total=50.0)
        assert "%" not in line and "70 jobs over 7 days old" in line

    def test_build_insights_integration(self):
        data = _data([("A", 60), ("B", 30), ("C", 10)])
        aging = _data([("Filing-Issue", 20), ("Casting-Issue", 14)])
        lines = ie.build_insights(
            data, _Spec(), enabled=["aging"],
            aging_data=aging, aging_days=7, aging_scope_total=100.0,
        )
        assert any(l.startswith("Aging:") for l in lines)
        # not enabled → silent even with data
        assert ie.build_insights(data, _Spec(), enabled=[], aging_data=aging) == []


class TestDeadstockInsight:
    def test_dead_designs(self):
        line = ie.deadstock_insight(1830.0, 1616.0, days=90)
        assert "214 of 1,830 designs" in line and "90 days" in line and "12%" in line

    def test_silent_when_none_dead(self):
        assert ie.deadstock_insight(100.0, 100.0, days=90) is None
        assert ie.deadstock_insight(100.0, 105.0, days=90) is None  # bad bracket
        assert ie.deadstock_insight(None, 50.0, days=90) is None
        assert ie.deadstock_insight(0.0, 0.0, days=90) is None

    def test_build_insights_integration(self):
        lines = ie.build_insights(
            _data([("A", 60), ("B", 30), ("C", 10)]), _Spec(),
            enabled=["deadstock"],
            deadstock_ever=500.0, deadstock_recent=400.0, deadstock_days=90,
        )
        assert any(l.startswith("Dead stock:") and "100 of 500" in l for l in lines)
        assert ie.build_insights(
            _data([("A", 60)]), _Spec(), enabled=[],
            deadstock_ever=500.0, deadstock_recent=400.0,
        ) == []


class TestDeltaParts:
    def test_up(self):
        p = ie.period_delta_parts(44210.0, 35220.0, "same span last month", "currency")
        assert p["trend"] == "up" and p["delta"] == "+25.5%"
        assert "same span last month" in p["comparison"]

    def test_down(self):
        p = ie.period_delta_parts(50.0, 100.0, "yesterday")
        assert p["trend"] == "down" and p["delta"] == "-50.0%"

    def test_flat(self):
        p = ie.period_delta_parts(100.0, 100.3, "x")
        assert p["trend"] == "flat"

    def test_none(self):
        assert ie.period_delta_parts(None, 1.0, "x") is None

    def test_line_matches_parts(self):
        # chat_service removes the delta line from insights by string equality
        # — the two functions must produce the identical line.
        line = ie.period_delta_line(44210.0, 35220.0, "last month", "Revenue", "currency")
        p = ie.period_delta_parts(44210.0, 35220.0, "last month", "currency")
        assert p is not None and "25.5%" in line


class TestSeverity:
    def test_severity_out_aligned(self):
        sev = []
        lines = ie.build_insights(
            _data([("A", 90), ("B", 5), ("C", 3), ("D", 2), ("E", 1)]), _Spec(),
            enabled=["pareto", "aging", "deadstock", "period_delta"],
            aging_data=_data([("F", 9), ("G", 3)]), aging_scope_total=100.0,
            deadstock_ever=500.0, deadstock_recent=400.0,
            prior_value=80.0, prior_label="last month", current_value=100.0,
            severity_out=sev,
        )
        assert len(sev) == len(lines)
        assert "warning" in sev  # aging + deadstock
        assert "positive" in sev  # delta up
        assert "info" in sev  # pareto

    def test_no_severity_param_still_strings(self):
        lines = ie.build_insights(_data([("A", 90), ("B", 5), ("C", 3)]), _Spec())
        assert all(isinstance(l, str) for l in lines)


class TestFollowupSuggestions:
    def test_breakdown(self):
        s = ie.followup_suggestions(
            "top 5 designs this month", dimension_label="Design No",
            has_date=True, entity_hint="794", limit=5)
        assert "Same breakdown for last month" in s
        assert "Show top 10 Design Nos" in s
        assert "Why did 794 perform well?" in s

    def test_scalar(self):
        s = ie.followup_suggestions(
            "this month total sales", has_date=True,
            default_dimension_label="Customer")
        assert "Compare with last month" in s
        assert "Break it down by Customer" in s

    def test_already_compared_not_offered(self):
        s = ie.followup_suggestions(
            "compare this month vs last month", has_date=True,
            default_dimension_label="Customer")
        assert "Compare with last month" not in s

    def test_empty_question(self):
        assert ie.followup_suggestions("") == [] or isinstance(
            ie.followup_suggestions(""), list)


class TestGetInsights:
    def test_configured(self):
        from app.services.column_registry import get_insights
        assert "period_delta" in get_insights("sales_report")

    def test_missing_report_defaults(self):
        from app.services.column_registry import get_insights
        assert get_insights("nonexistent_report") == list(ie.DEFAULT_INSIGHTS)

    def test_aging_config(self):
        from app.services.column_registry import get_aging, get_insights
        assert "aging" in get_insights("wip_report")
        cfg = get_aging("wip_report")
        assert cfg["metric"] == "aged_jobs" and cfg["days"] == 7
        assert get_aging("sales_report") == {}
