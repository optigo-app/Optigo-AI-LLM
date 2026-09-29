import asyncio
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from app.services.query_plan import QueryPlan, QueryStep

# Ordered by specificity — first match wins per mention.
_PERIOD_PATTERNS: List[Tuple[str, str]] = [
    ("last_year", r"\blast\s+year\b"),
    ("this_year", r"\bthis\s+year\b|\byearly\b|\byear\s+to\s+date\b|\bytd\b"),
    ("last_month", r"\blast\s+month\b"),
    ("this_month", r"\bthis\s+month\b|\bmonthly\b|\bmonth\s+to\s+date\b|\bmtd\b"),
    ("last_week", r"\blast\s+week\b"),
    ("this_week", r"\bthis\s+week\b|\bweekly\b|\bweek\s+to\s+date\b|\bwtd\b"),
    ("yesterday", r"\byesterday\b"),
    ("today", r"\btoday\b|\bdaily\b|\btodays\b"),
]


def detect_periods(question: str) -> List[str]:
    """Named periods mentioned in the question, in canonical order.

    Returns preset keys (e.g. ``["this_year", "this_month", "today"]``),
    de-duplicated. Used for multi-period questions like "sales for this year,
    this month and today".
    """
    if not question:
        return []
    q = question.lower()
    found = [preset for preset, pat in _PERIOD_PATTERNS if re.search(pat, q)]

    # Bare "month"/"year"/"week" (no this/last qualifier) — count as current
    # period, but only when at least one named period is already present
    # (avoids firing on ordinary single-period phrasing).
    if found:
        if "this_month" not in found and "last_month" not in found \
                and re.search(r"\bmonths?\b", q):
            found.append("this_month")
        if "this_year" not in found and "last_year" not in found \
                and re.search(r"\byears?\b", q):
            found.append("this_year")
        if "this_week" not in found and "last_week" not in found \
                and re.search(r"\bweeks?\b", q):
            found.append("this_week")

    # Present in canonical order (year → month → week → day)
    order = {preset: i for i, (preset, _) in enumerate(_PERIOD_PATTERNS)}
    return sorted(set(found), key=lambda p: order.get(p, 99))


def resolve_preset_dates(preset: str) -> Tuple[str, str]:
    """Resolve a named period preset to ``(start_date, end_date)`` ISO strings."""
    from datetime import date, timedelta

    today = date.today()
    if preset == "today":
        return today.isoformat(), today.isoformat()
    if preset == "yesterday":
        y = today - timedelta(days=1)
        return y.isoformat(), y.isoformat()
    if preset == "this_month":
        return today.replace(day=1).isoformat(), today.isoformat()
    if preset == "last_month":
        first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
        last = today.replace(day=1) - timedelta(days=1)
        return first.isoformat(), last.isoformat()
    if preset == "this_year":
        return today.replace(month=1, day=1).isoformat(), today.isoformat()
    if preset == "last_year":
        return f"{today.year - 1}-01-01", f"{today.year - 1}-12-31"
    if preset == "this_week":
        monday = today - timedelta(days=today.weekday())
        return monday.isoformat(), today.isoformat()
    if preset == "last_week":
        monday = today - timedelta(days=today.weekday() + 7)
        return monday.isoformat(), (monday + timedelta(days=6)).isoformat()
    return "", ""


_PERIOD_LABELS = {
    "today": "Today", "yesterday": "Yesterday",
    "this_week": "This week", "last_week": "Last week",
    "this_month": "This month", "last_month": "Last month",
    "this_year": "This year", "last_year": "Last year",
}


def period_label(preset: str) -> str:
    return _PERIOD_LABELS.get(preset, preset.replace("_", " ").title())


# Dimension keys that describe time, not a business grouping. When the LLM
# picks one of these on a question that also names 2+ periods, it is usually
# reinterpreting the period words — multi-period wording should win.
_TIME_DIMENSIONS = {"month", "day", "date", "year", "quarter", "week", "period"}


def is_time_dimension(dimension: Optional[str]) -> bool:
    return bool(dimension) and dimension.strip().lower() in _TIME_DIMENSIONS


def compute_previous_period(
    validated_filters: Dict[str, Any],
    date_filter: Optional[Dict[str, Any]],
) -> Optional[Tuple[Dict[str, Any], str, str]]:
    """Compute the previous-period start/end for a growth/comparison query.

    Uses the LLM's ``date_filter`` preset when present (this_month, this_year,
    this_week, today); for explicit ranges it shifts back by the range
    duration. When the question had no date at all, compares against last
    month (i.e. treats the current period as this month).

    Returns ``(prev_filters, cur_start, cur_end)`` — the previous-period
    filters dict plus the resolved current-period dates — or None if no
    period can be determined.
    """
    from datetime import date, timedelta

    filters = dict(validated_filters or {})
    if not filters.get("start_date"):
        today = date.today()
        filters["start_date"] = today.replace(day=1).isoformat()
        filters["end_date"] = today.isoformat()

    cur_start = filters.get("start_date", "")
    cur_end = filters.get("end_date", "")
    prev = dict(filters)

    date_filter = date_filter or {}
    preset = date_filter.get("preset", "")
    if not preset and not date_filter.get("start"):
        preset = "this_month"

    if preset == "this_month":
        today = date.today()
        first_of_month = today.replace(day=1)
        prev_end = first_of_month - timedelta(days=1)
        prev_start = prev_end.replace(day=1)
    elif preset == "this_year":
        today = date.today()
        prev_start = date(today.year - 1, 1, 1)
        prev_end = date(today.year - 1, 12, 31)
    elif preset == "this_week":
        today = date.today()
        monday = today - timedelta(days=today.weekday())
        prev_start = monday - timedelta(days=7)
        prev_end = prev_start + timedelta(days=6)
    elif preset == "today":
        today = date.today()
        prev_start = prev_end = today - timedelta(days=1)
    elif preset == "last_month":
        today = date.today()
        first = today.replace(day=1)
        cur_first = (first - timedelta(days=1)).replace(day=1)
        prev_start = (cur_first - timedelta(days=1)).replace(day=1)
        prev_end = cur_first - timedelta(days=1)
    elif cur_start and cur_end:
        from datetime import date as _date
        s = _date.fromisoformat(cur_start)
        e = _date.fromisoformat(cur_end)
        duration = (e - s).days + 1
        prev_end = s - timedelta(days=1)
        prev_start = prev_end - timedelta(days=duration - 1)
    else:
        return None

    prev["start_date"] = prev_start.isoformat() if hasattr(prev_start, "isoformat") else str(prev_start)
    prev["end_date"] = prev_end.isoformat() if hasattr(prev_end, "isoformat") else str(prev_end)
    return prev, cur_start, cur_end


@dataclass
class StepResult:
    step: QueryStep
    data: Any = None
    error: str = ""


async def execute_plan(
    plan: QueryPlan,
    executor: Callable[[QueryStep], Awaitable[Any]],
) -> List[StepResult]:
    if not plan.steps:
        plan.steps = [QueryStep(type="metric_fetch", metric=plan.metric, date_range=plan.date_range, limit=plan.limit)]
    outcomes = await asyncio.gather(*(executor(step) for step in plan.steps), return_exceptions=True)
    return [
        StepResult(step=step, error=str(value)) if isinstance(value, Exception) else StepResult(step=step, data=value)
        for step, value in zip(plan.steps, outcomes)
    ]


def build_comparison_steps(plan: QueryPlan, previous_date_range: Any) -> QueryPlan:
    plan.steps = [
        QueryStep(type="metric_fetch", metric=plan.metric, date_range=plan.date_range, limit=plan.limit),
        QueryStep(type="period_comparison", metric=plan.metric, date_range=previous_date_range, limit=plan.limit),
    ]
    return plan
