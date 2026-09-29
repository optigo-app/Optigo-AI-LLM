import argparse
import asyncio
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DATASET = ROOT / "tests" / "eval_dataset.json"
FIELDS = ("report_key", "metric", "dimension", "aggregation", "sort", "limit", "date_preset", "complexity")
STAGES = {
    "report_key": "routing",
    "metric": "semantic",
    "dimension": "semantic",
    "aggregation": "planning",
    "sort": "planning",
    "limit": "planning",
    "date_preset": "date",
    "complexity": "planning",
}


def validate_dataset(cases: list[dict]) -> list[str]:
    errors = []
    seen = set()
    for index, case in enumerate(cases, 1):
        question = case.get("question", "").strip()
        if not question:
            errors.append(f"case {index}: question is required")
        elif question.lower() in seen:
            errors.append(f"case {index}: duplicate question")
        seen.add(question.lower())
        if not case.get("report_key"):
            errors.append(f"case {index}: report_key is required")
        if not any(field in case for field in ("metric", "dimension", "aggregation")):
            errors.append(f"case {index}: at least one plan expectation is required")
    return errors


async def evaluate(live: bool = False) -> dict:
    cases = json.loads(DATASET.read_text(encoding="utf-8"))
    errors = validate_dataset(cases)
    if not live:
        return {"cases": len(cases), "mode": "schema", "valid": not errors, "errors": errors}
    if errors:
        return {"cases": len(cases), "mode": "live", "valid": False, "errors": errors}

    from app.services.api_client import get_report_registry
    from app.services.query_planner import plan_query

    registry = get_report_registry()
    correct = 0
    details = []
    field_scores = defaultdict(lambda: [0, 0])
    stage_scores = defaultdict(lambda: [0, 0])
    latencies = []
    for case in cases:
        failures = []
        started = time.perf_counter()
        try:
            result = await plan_query(case["question"], registry=registry)
            plan = result.plan
            actual = {
                "report_key": plan.report_key,
                "metric": plan.metric,
                "dimension": plan.dimension,
                "aggregation": plan.aggregation,
                "sort": plan.sort_by,
                "limit": plan.limit,
                "date_preset": plan.date_range.preset if plan.date_range else None,
                "complexity": plan.complexity.value,
            }
            for field in FIELDS:
                if field not in case:
                    continue
                matched = actual[field] == case[field]
                field_scores[field][1] += 1
                stage_scores[STAGES[field]][1] += 1
                if matched:
                    field_scores[field][0] += 1
                    stage_scores[STAGES[field]][0] += 1
                else:
                    failures.append(f"{field}: expected {case[field]!r}, got {actual[field]!r}")
        except Exception as exc:
            actual = {}
            failures.append(f"error: {exc}")
        latency_ms = round((time.perf_counter() - started) * 1000, 1)
        latencies.append(latency_ms)
        passed = not failures
        correct += int(passed)
        details.append({
            "question": case["question"], "passed": passed,
            "failures": failures, "actual": actual, "latency_ms": latency_ms,
        })

    def accuracy(values):
        return round(values[0] / values[1], 4) if values[1] else 0.0

    return {
        "cases": len(cases),
        "correct": correct,
        "accuracy": round(correct / len(cases), 4) if cases else 0.0,
        "field_accuracy": {key: accuracy(value) for key, value in field_scores.items()},
        "stage_accuracy": {key: accuracy(value) for key, value in stage_scores.items()},
        "latency_ms": {
            "average": round(sum(latencies) / len(latencies), 1) if latencies else 0,
            "maximum": max(latencies, default=0),
        },
        "details": details,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(evaluate(args.live)), indent=2))
