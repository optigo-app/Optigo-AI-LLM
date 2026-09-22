"""Test the shared DynamicReport_LLMChatbeta SP via the real API.

Run:  python tests\test_shared_llm_sp.py
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings
from app.services.real_api_client import call_real_report_api, _build_p
from app.services.intent import IntentSpec

REPORT_KEY = "sales_report"
APPUSERID = "admin@orail.co.in"
IP = "103.206.139.196"
YEARCODE = settings.real_api_yearcode


def _make_spec(metric="Amount", agg="sum", dim="", limit=0, unit="currency"):
    return IntentSpec(
        report_key=REPORT_KEY, intent="test",
        metric_key=metric, aggregation=agg, dimension=dim,
        sort="desc", limit=limit, unit=unit,
        is_field_metric=False, clear_filters=[], override_filters={},
    )


async def run_test(label, spec, filters=None):
    filters = filters or {}
    print("=" * 70)
    print(f"TEST: {label}")
    print(f"  Metric: {spec.metric_key} | Agg: {spec.aggregation} | Dim: {spec.dimension or '(none)'} | Limit: {spec.limit}")

    p_json = _build_p(REPORT_KEY, spec, filters, "")
    p = json.loads(p_json)
    print(f"  SP number:    {settings.real_api_llm_chat_sp}")
    print(f"  Tables:       {p.get('Tables')}")
    print(f"  BaseFilter:   {p.get('BaseFilter')}")
    print(f"  MetricExpr:   {p.get('MetricExpr', '')[:80]}")
    print(f"  DimensionExpr:{p.get('DimensionExpr', '')[:80]}")
    print(f"  FilterHeader: {p.get('FilterHeader', '')}")
    print(f"  FilterValue:  {p.get('FilterValue', '')}")

    try:
        result = await call_real_report_api(
            report_key=REPORT_KEY,
            intent_spec=spec,
            validated_filters=filters,
            appuserid=APPUSERID,
            ip_address=IP,
            yearcode=YEARCODE,
        )
        print("  RESULT: SUCCESS")
        print(f"  Rows:        {result.get('rows', [])[:5]}")
        print(f"  Total count: {result.get('total_count')}")
        print(f"  Values:      {result.get('values', [])[:5]}")
        print(f"  Dimensions:  {result.get('dimensions', [])[:5]}")
        return True
    except Exception as e:
        print(f"  RESULT: FAILED - {type(e).__name__}: {e}")
        if hasattr(e, 'status_code'):
            print(f"    status_code: {e.status_code}")
        if hasattr(e, 'body'):
            print(f"    body: {e.body[:500]}")
        return False


async def main():
    print(f"Config: use_real_api={settings.use_real_api}, real_api_llm_chat_sp={settings.real_api_llm_chat_sp}")
    print(f"Base URL: {settings.real_api_base_url}")
    print()

    results = []

    # Test 1: Total sales (sum, no dimension)
    results.append(await run_test(
        "Total sales (sum, no dimension)",
        _make_spec(metric="Amount", agg="sum", dim=""),
    ))

    # Test 2: Top 5 customers (ranking with dimension + limit)
    results.append(await run_test(
        "Top 5 customers (sum + dimension + limit)",
        _make_spec(metric="Amount", agg="sum", dim="CustomerFullName", limit=5),
    ))

    # Test 3: Count of invoices (count aggregation)
    results.append(await run_test(
        "Invoice count (count, no dimension)",
        _make_spec(metric="total_count", agg="count", dim="", unit="count"),
    ))

    # Test 4: Sales by category (dimension, no limit)
    results.append(await run_test(
        "Sales by category (sum + dimension)",
        _make_spec(metric="Amount", agg="sum", dim="categoryname"),
    ))

    # Test 5: With a filter (category = Ring)
    results.append(await run_test(
        "Total sales with category filter",
        _make_spec(metric="Amount", agg="sum", dim=""),
        filters={"category": "Ring"},
    ))

    # Test 6: count_distinct (unique customers)
    results.append(await run_test(
        "Unique customers (count_distinct, no dimension)",
        _make_spec(metric="CustomerName", agg="count_distinct", dim="", unit="count"),
    ))

    # Test 7: avg aggregation
    results.append(await run_test(
        "Average sales amount (avg, no dimension)",
        _make_spec(metric="Amount", agg="avg", dim=""),
    ))

    # Summary
    print()
    print("=" * 70)
    passed = sum(1 for r in results if r)
    print(f"SUMMARY: {passed}/{len(results)} tests passed")
    if passed < len(results):
        print("Some tests FAILED - check output above.")
        sys.exit(1)
    else:
        print("ALL TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
