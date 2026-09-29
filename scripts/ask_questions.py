r"""Run natural-language questions through the full governed pipeline.

plan_query -> call_real_report_api -> real SP (SourceQuery)

Usage:
    .\.venv\Scripts\python.exe scripts\ask_questions.py
    .\.venv\Scripts\python.exe scripts\ask_questions.py "total sales today"
"""

import argparse
import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app.services.api_client import get_report_registry
from app.services.query_planner import plan_query
from app.services.real_api_client import call_real_report_api

DEFAULT_QUESTIONS = [
    "total sales",
    "total sales today",
    "total gold weight sold",
    "net weight today",
    "total diamond amount",
    "wastage amount today",
    "total labour amount",
    "how many units sold today",
    "sales by customer",
    "top 5 designs by sales amount",
]


async def ask(question: str, registry, appuserid: str, ip_address: str, yearcode: str) -> None:
    try:
        planning = await plan_query(question, registry=registry)
    except Exception as exc:
        print(f"\nQ: {question}\n   PLAN ERROR: {exc}")
        return
    parsed = planning.parsed
    spec = planning.intent_spec
    print(
        f"\nQ: {question}\n"
        f"   report={parsed.report_key} metric={spec.metric_key} agg={spec.aggregation} "
        f"dim={spec.dimension or '-'} filters={planning.validated_filters} ai_where={planning.ai_where or '-'}"
    )
    try:
        result = await call_real_report_api(
            report_key=parsed.report_key,
            intent_spec=spec,
            validated_filters=planning.validated_filters,
            ai_where_clause=planning.ai_where,
            appuserid=appuserid,
            ip_address=ip_address,
            yearcode=yearcode,
        )
    except Exception as exc:
        print(f"   API ERROR: {exc}")
        return
    labels = result.get("labels") or []
    values = result.get("values") or []
    print(f"   RESULT: {list(zip(labels, values)) if labels else values}  rows={result.get('total_count')}")
    if result.get("error"):
        print(f"   ERROR: {result.get('error')}")


async def main(questions, appuserid, ip_address, yearcode):
    registry = get_report_registry()
    for q in questions:
        await ask(q, registry, appuserid, ip_address, yearcode)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("questions", nargs="*", default=[])
    parser.add_argument("--appuserid", default="admin@orail.co.in")
    parser.add_argument("--ip-address", default="")
    parser.add_argument("--yearcode", default="")
    args = parser.parse_args()
    qs = args.questions or DEFAULT_QUESTIONS
    asyncio.run(main(qs, args.appuserid, args.ip_address, args.yearcode))
