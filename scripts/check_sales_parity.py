r"""Run deterministic sales-report parity checks against the real report API.

Usage:
    .\.venv\Scripts\python.exe scripts\check_sales_parity.py
    .\.venv\Scripts\python.exe scripts\check_sales_parity.py --period all
    .\.venv\Scripts\python.exe scripts\check_sales_parity.py --only WastageAmount,units_sold
"""

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict, Iterable, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.services.column_registry import _REGISTRY
from app.services.intent import IntentSpec
from app.services.real_api_client import RealApiError, call_real_report_api

FIXTURE_PATH = os.path.join(ROOT, "tests", "sales_parity_expected.json")
TOLERANCE = 0.011


def _load_fixture(path: str) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def _configured_metrics(report_key: str) -> set[str]:
    cfg = _REGISTRY.get(report_key, {})
    return set(cfg.get("columns", {})) | set(cfg.get("special_metrics", {}))


def _period_filters(fixture: Dict[str, Any], period: str) -> Dict[str, str]:
    return dict(fixture.get("periods", {}).get(period, {}) or {})


async def _run_metric(
    report_key: str,
    metric: Dict[str, Any],
    period: str,
    filters: Dict[str, str],
    appuserid: str,
    ip_address: str,
    yearcode: str,
) -> Dict[str, Any]:
    name = metric["name"]
    expected = metric.get("expected", {}).get(period)
    spec = IntentSpec(
        report_key=report_key,
        intent="parity_check",
        metric_key=name,
        aggregation=metric.get("aggregation", "sum"),
        limit=1,
    )
    try:
        result = await call_real_report_api(
            report_key=report_key,
            intent_spec=spec,
            validated_filters=filters,
            appuserid=appuserid,
            ip_address=ip_address,
            yearcode=yearcode,
        )
        actual: Optional[float] = result.get("values", [None])[0]
        return {
            "metric": name,
            "label": metric.get("label", name),
            "period": period,
            "expected": expected,
            "actual": actual,
            "difference": None if expected is None or actual is None else actual - expected,
            "status": "ok" if expected is not None and actual is not None and abs(actual - expected) <= TOLERANCE else ("missing_expected" if expected is None else "mismatch"),
            "error": result.get("error", ""),
            "total_count": result.get("total_count"),
        }
    except Exception as exc:
        return {
            "metric": name,
            "label": metric.get("label", name),
            "period": period,
            "expected": expected,
            "actual": None,
            "difference": None,
            "status": "error",
            "error": str(exc),
            "total_count": None,
        }


async def _run(fixture: Dict[str, Any], periods: Iterable[str], only: set[str], appuserid: str, ip_address: str, yearcode: str) -> List[Dict[str, Any]]:
    report_key = fixture.get("report_key", "sales_report")
    configured = _configured_metrics(report_key)
    results: List[Dict[str, Any]] = []
    for period in periods:
        filters = _period_filters(fixture, period)
        for metric in fixture.get("metrics", []):
            name = metric.get("name", "")
            if only and name not in only:
                continue
            if name not in configured:
                results.append({
                    "metric": name,
                    "label": metric.get("label", name),
                    "period": period,
                    "expected": metric.get("expected", {}).get(period),
                    "actual": None,
                    "difference": None,
                    "status": "not_configured",
                    "error": "metric is not present in sales_report.json",
                    "total_count": None,
                })
                continue
            results.append(await _run_metric(report_key, metric, period, filters, appuserid, ip_address, yearcode))
    return results


def _fmt_num(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return f"{value:,.3f}"


def _print_table(results: List[Dict[str, Any]]) -> None:
    print(f"{'PERIOD':<7} {'METRIC':<20} {'EXPECTED':>18} {'ACTUAL':>18} {'DIFF':>15}  STATUS")
    print("-" * 96)
    for row in results:
        status = row["status"]
        if row.get("error") and status == "error":
            status = f"error: {row['error'][:70]}"
        elif row.get("error"):
            status = f"{status}: {row['error'][:55]}"
        print(
            f"{row['period']:<7} {row['metric']:<20} "
            f"{_fmt_num(row.get('expected')):>18} {_fmt_num(row.get('actual')):>18} "
            f"{_fmt_num(row.get('difference')):>15}  {status}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", default=FIXTURE_PATH)
    parser.add_argument("--period", choices=["all", "today", "month"], action="append", help="Period to check; repeatable. Defaults to all periods.")
    parser.add_argument("--only", default="", help="Comma-separated metric names to check")
    parser.add_argument("--appuserid", default="admin@orail.co.in")
    parser.add_argument("--ip-address", default="127.0.0.1")
    parser.add_argument("--yearcode", default="")
    parser.add_argument("--json", dest="json_out", default="", help="Optional output JSON file")
    args = parser.parse_args()

    fixture = _load_fixture(args.fixture)
    periods = args.period or list(fixture.get("periods", {}))
    only = {part.strip() for part in args.only.split(",") if part.strip()}
    results = asyncio.run(_run(fixture, periods, only, args.appuserid, args.ip_address, args.yearcode))
    _print_table(results)

    if args.json_out:
        with open(args.json_out, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        print(f"\nWrote {args.json_out}")

    failures = [row for row in results if row["status"] in {"mismatch", "error", "not_configured"}]
    print(f"\n{len(results)} checks; {len(results) - len(failures)} matched/missing-expected; {len(failures)} need attention")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
