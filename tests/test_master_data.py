"""Master vocabulary service: /reports/{key}/masters + runtime value matching."""
import pytest
from unittest.mock import AsyncMock, patch

from app.services import master_data
from app.services.query_plan import QueryPlan


@pytest.fixture(autouse=True)
def _clear_master_cache():
    master_data.clear_master_cache()
    yield
    master_data.clear_master_cache()


class TestMasterColumns:
    def test_declared_masters_returned(self):
        cols = master_data.get_master_columns("wip_report")
        for expected in ("department", "jobtype", "OrderTypeName", "priority", "JobLocation"):
            assert expected in cols

    def test_unknown_columns_filtered_out(self):
        cols = master_data.get_master_columns("wip_report")
        from app.services.column_registry import _REGISTRY
        assert all(c in _REGISTRY["wip_report"]["columns"] for c in cols)

    def test_high_cardinality_columns_never_masters(self):
        for rk in ("sales_report", "order_report", "wip_report", "tax_report"):
            cols = {c.lower() for c in master_data.get_master_columns(rk)}
            for banned in ("stockdocumentno", "stockbarcode", "skuno", "jobno"):
                assert banned not in cols


class TestMastersFetch:
    @pytest.mark.anyio
    async def test_static_canonical_values_without_live_api(self):
        with patch.object(master_data.settings, "use_real_api", False):
            res = await master_data.get_report_masters("order_report", company_code="T1")
        sold = res["masters"]["SoldPending"]
        assert sold["source"] == "static"
        assert set(sold["values"]) == {"sold", "pending"}

    @pytest.mark.anyio
    async def test_live_values_merged_with_static(self):
        async def fake_fetch(report_key, column, **scope):
            return {"priority": ["High", "Normal", "Urgent"]}.get(column, [])

        with patch.object(master_data.settings, "use_real_api", True), \
             patch.object(master_data, "_fetch_column_values", new=fake_fetch):
            res = await master_data.get_report_masters("wip_report", company_code="T1")
        pr = res["masters"]["priority"]
        assert pr["source"] == "live"
        assert pr["values"] == ["High", "Normal", "Urgent"]

    @pytest.mark.anyio
    async def test_live_merges_static_canonical(self):
        async def fake_fetch(report_key, column, **scope):
            return {"SoldPending": ["sold", "pending", "invoiced"]}.get(column, [])

        with patch.object(master_data.settings, "use_real_api", True), \
             patch.object(master_data, "_fetch_column_values", new=fake_fetch):
            res = await master_data.get_report_masters("order_report", company_code="T1")
        sp = res["masters"]["SoldPending"]
        assert sp["source"] == "live+static"
        assert "invoiced" in sp["values"]

    @pytest.mark.anyio
    async def test_per_column_failure_degrades(self):
        async def boom(report_key, column, **scope):
            raise RuntimeError("api down")

        with patch.object(master_data.settings, "use_real_api", True), \
             patch.object(master_data, "_fetch_column_values", new=boom):
            res = await master_data.get_report_masters("order_report", company_code="T1")
        # canonical-backed columns still return static values
        assert res["masters"]["SoldPending"]["source"] == "static"
        assert res["masters"]["SoldPending"]["values"]
        # non-canonical columns degrade to error, not exception
        assert res["masters"]["jobflowfrom"]["source"] == "error"

    @pytest.mark.anyio
    async def test_cache_stores_vocab(self):
        async def fake_fetch(report_key, column, **scope):
            return ["High", "Low"] if column == "priority" else []

        with patch.object(master_data.settings, "use_real_api", True), \
             patch.object(master_data, "_fetch_column_values", new=fake_fetch):
            await master_data.get_report_masters("wip_report", company_code="T1")
        cached = master_data.get_cached_masters("wip_report", "T1")
        assert cached["priority"] == ["High", "Low"]

    @pytest.mark.anyio
    async def test_repeat_call_serves_cache_no_refetch(self):
        calls = []

        async def fake_fetch(report_key, column, **scope):
            calls.append(column)
            return ["High"] if column == "priority" else []

        with patch.object(master_data.settings, "use_real_api", True), \
             patch.object(master_data, "_fetch_column_values", new=fake_fetch):
            first = await master_data.get_report_masters("wip_report", company_code="T1")
            n_first = len(calls)
            second = await master_data.get_report_masters("wip_report", company_code="T1")
        assert first["cached"] is False
        assert second["cached"] is True
        assert len(calls) == n_first  # zero new SP calls on second load
        assert second["masters"] == first["masters"]

    @pytest.mark.anyio
    async def test_refresh_forces_refetch(self):
        calls = []

        async def fake_fetch(report_key, column, **scope):
            calls.append(column)
            return ["High"] if column == "priority" else []

        with patch.object(master_data.settings, "use_real_api", True), \
             patch.object(master_data, "_fetch_column_values", new=fake_fetch):
            await master_data.get_report_masters("wip_report", company_code="T1")
            await master_data.get_report_masters("wip_report", company_code="T1", refresh=True)
        n_masters = len(master_data.get_master_columns("wip_report"))
        assert len(calls) == 2 * n_masters  # both runs fetch every wip master

    @pytest.mark.anyio
    async def test_company_scoped_cache(self):
        async def fake_fetch(report_key, column, **scope):
            return {"priority": [scope.get("appuserid", "?")]}.get(column, [])

        with patch.object(master_data.settings, "use_real_api", True), \
             patch.object(master_data, "_fetch_column_values", new=fake_fetch):
            await master_data.get_report_masters("wip_report", company_code="T1", appuserid="a1")
            await master_data.get_report_masters("wip_report", company_code="T2", appuserid="a2")
        assert master_data.get_cached_masters("wip_report", "T1")["priority"] == ["a1"]
        assert master_data.get_cached_masters("wip_report", "T2")["priority"] == ["a2"]


class TestRetargetMisplacedFilters:
    """LLM parks qualifier words on whatever column it knows — the retarget
    pass moves them to the column whose vocabulary actually owns the value."""

    @pytest.fixture(autouse=True)
    def _vocab(self):
        master_data._store_masters("ORAIL25", "wip_report", {
            "priority": ["Normal", "Any Time", "High", "Do not use", "LOW"],
            "OrderTypeName": ["Regular", "Corporate", "-Select-", "RND Order"],
            "jobtype": ["Regular Job", "Repair Job", "Recast Job", "Sample Line Job"],
            "department": ["Pending Request", "Casting-Issue", "setting-Issue"],
        })
        yield
        master_data.clear_master_cache()

    def _run(self, question, ai_where="", filters=None):
        from app.services.parse_result import ParseResult, retarget_misplaced_filters
        p = ParseResult({"report_key": "wip_report", "ai_where": ai_where,
                         "filters": dict(filters or {})})
        retarget_misplaced_filters(question, p)
        return p

    def test_retargets_wrong_column_literal(self):
        p = self._run("rnd order jobs", ai_where="department LIKE '%RND%'")
        assert p.ai_where == ""
        assert p.filters.get("order_type") == "RND"

    def test_retargets_priority(self):
        p = self._run("high priority jobs", ai_where="department LIKE '%High Priority%'")
        assert p.filters.get("priority") == "High"
        assert p.ai_where == ""

    def test_keeps_legit_column_match(self):
        p = self._run("jobs in casting", ai_where="department LIKE '%Casting%'")
        assert p.ai_where == "DI.department LIKE '%Casting%'"
        assert p.filters == {}

    def test_keeps_unknown_literal(self):
        p = self._run("customer named vidhi", ai_where="department LIKE '%vidhi%'")
        assert p.ai_where == "DI.department LIKE '%vidhi%'"

    def test_rescues_dropped_qualifier(self):
        p = self._run("repair jobs in casting",
                      ai_where="department LIKE '%Casting-Issue%'")
        assert p.filters.get("jobtype") == "Repair Job"
        assert "Casting-Issue" in p.ai_where  # legit clause untouched

    def test_no_tailword_false_positive(self):
        # 'order' is the tail of 'Sales Order' (IsCompanyJob) — must NOT be
        # rescued as a qualifier in 'rnd order jobs'.
        p = self._run("rnd order jobs", ai_where="department LIKE '%RND%'")
        assert "company_job" not in p.filters

    def test_full_phrase_match(self):
        p = self._run("sales order jobs")
        assert p.filters.get("company_job") == "Sales Order"

    def test_ambiguous_word_not_rescued(self):
        # 'regular' is exact in OrderTypeName AND inside 'Regular Job' — ambiguous.
        p = self._run("regular jobs")
        assert not p.filters


class TestMasterMatch:
    def _seed(self):
        master_data._store_masters("T1", "wip_report", {
            "OrderTypeName": ["Regular", "Corporate", "RND Order"],
            "department": ["Casting-Issue", "Wax Setting"],
        })

    def test_exact_ci_match(self):
        self._seed()
        assert master_data.match_master_value("wip_report", "OrderTypeName", "corporate", "T1") == "Corporate"

    def test_unambiguous_substring(self):
        self._seed()
        assert master_data.match_master_value("wip_report", "OrderTypeName", "rnd", "T1") == "RND Order"

    def test_ambiguous_returns_original(self):
        self._seed()
        assert master_data.match_master_value("wip_report", "department", "a", "T1") == "a"

    def test_no_vocab_returns_original(self):
        assert master_data.match_master_value("wip_report", "OrderTypeName", "rnd", "T9") == "rnd"


class TestValidatedFiltersMasterFallback:
    def test_live_vocab_normalizes_filter_value(self):
        # 'rnd orders' has no static canonical entry — live vocab resolves it
        master_data._store_masters("T1", "wip_report", {"OrderTypeName": ["Regular", "Corporate", "RND Order"]})
        qp = QueryPlan(report_key="wip_report", intent="t", metric="JobCost",
                       aggregation="sum", filters=[{"field": "order_type", "value": "rnd orders"}])
        assert qp.validated_filters()["order_type"] == "RND Order"

    def test_static_canonical_applies_before_live_vocab(self):
        # 'rnd' IS in static canonical -> stem 'RND' (LIKE still matches 'RND Order')
        master_data._store_masters("T1", "wip_report", {"OrderTypeName": ["Regular", "Corporate", "RND Order"]})
        qp = QueryPlan(report_key="wip_report", intent="t", metric="JobCost",
                       aggregation="sum", filters=[{"field": "order_type", "value": "rnd"}])
        assert qp.validated_filters()["order_type"] == "RND"

    def test_static_canonical_still_wins(self):
        master_data._store_masters("T1", "wip_report", {"jobtype": ["Regular Job", "Repair Job"]})
        qp = QueryPlan(report_key="wip_report", intent="t", metric="JobCost",
                       aggregation="sum", filters=[{"field": "job_type", "value": "recast jobs"}])
        # static canonical maps to 'Recast Job' even though live vocab lacks it
        assert qp.validated_filters()["job_type"] == "Recast Job"

    def test_unknown_value_passes_through(self):
        qp = QueryPlan(report_key="wip_report", intent="t", metric="JobCost",
                       aggregation="sum", filters=[{"field": "order_type", "value": "xyzzy"}])
        assert qp.validated_filters()["order_type"] == "xyzzy"
