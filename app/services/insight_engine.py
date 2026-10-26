"""Deterministic insight rules over verified report data.

Every insight is computed from rows/values the governed SP actually returned —
the engine never invents numbers. Enabled per report via the config ``insights``
list (see ``column_registry.get_insights``).

Insight names:
  pareto        — concentration: top-1/top-3 share of a breakdown's total
  vs_average    — top row compared against the average of the rest
  outlier       — top row far above the runner-up (3x+)
  period_delta  — current period vs previous period (needs prior_scalar)
  aging         — backlog aging: rows past a day threshold + worst group
                  (needs a companion query on the report's `aging` config)
  deadstock     — entities that ever sold but had no sales in N days
                  (needs two companions: unique count all-time + last N days)
"""

import logging
import re
from statistics import mean
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Default when a report config doesn't declare insights.
DEFAULT_INSIGHTS = ("pareto", "vs_average", "outlier")

_DELTA_LABELS = {
    "today": "yesterday",
    "yesterday": "the day before",
    "this_week": "last week",
    "last_week": "the week before",
    "this_month": "last month",
    "last_month": "the month before",
    "this_year": "last year",
    "last_year": "the year before",
}


def _extract_rows(data: Any) -> List[Tuple[str, float]]:
    """(dimension_label, metric_value) pairs from a breakdown result set."""
    rows: List[Tuple[str, float]] = []
    if not isinstance(data, dict):
        return rows
    for r in data.get("rows") or []:
        try:
            v = float(r.get("MetricValue"))
        except (TypeError, ValueError):
            continue
        d = str(r.get("DimensionValue") or "").strip()
        if d:
            rows.append((d, v))
    return rows


def _fmt(v: float, unit: str, unit_label: str) -> str:
    from app.services.block_builder import _display_value
    return _display_value(v, unit or "", "INR", unit_label or "")


def _pareto(rows: List[Tuple[str, float]], dim_label: str,
            fmt) -> List[str]:
    total = sum(v for _, v in rows)
    if total <= 0 or len(rows) < 3:
        return []
    top_name, top_val = rows[0]
    out = []
    # Top-3 share is only meaningful when it clearly exceeds the uniform
    # expectation (3/n): uniform n=5 is already 60%, so require >= 70%.
    top3 = sum(v for _, v in rows[:3])
    top3_share = top3 / total * 100.0
    if len(rows) >= 5 and top3_share >= 70:
        out.append(f"Top 3 {dim_label}s contribute {top3_share:.0f}% of the total")
    top_share = top_val / total * 100.0
    if top_share >= 50:
        out.append(f"{top_name} alone is {top_share:.0f}% of the total")
    return out


def _vs_average(rows: List[Tuple[str, float]], dim_label: str,
                fmt) -> List[str]:
    if len(rows) < 4:
        return []
    top_name, top_val = rows[0]
    rest = [v for _, v in rows[1:]]
    avg = mean(rest) if rest else 0.0
    if avg > 0 and top_val >= 1.5 * avg:
        return [f"{top_name} ({fmt(top_val)}) is {top_val / avg:.1f}x the average of the other {dim_label}s"]
    return []


def _outlier(rows: List[Tuple[str, float]], dim_label: str,
             fmt) -> List[str]:
    if len(rows) < 3:
        return []
    top_name, top_val = rows[0]
    second_val = rows[1][1]
    if second_val > 0 and top_val >= 3 * second_val:
        return [f"{top_name} is a clear outlier — {top_val / second_val:.1f}x the next {dim_label}"]
    return []


def period_delta_parts(
    current: Optional[float],
    previous: Optional[float],
    previous_label: str,
    unit: str = "",
    unit_label: str = "",
) -> Optional[Dict[str, Any]]:
    """Structured delta for metric-card badges / severity colouring:
    {"delta": "+25.5%", "trend": "up|down|flat", "comparison": "vs last month",
     "previous_display": "₹35.22 lakh", "current_display": "₹44.21 lakh"}"""
    if current is None or previous is None:
        return None
    label = previous_label or "previous period"
    cur_d = _fmt(current, unit, unit_label)
    prev_d = _fmt(previous, unit, unit_label)
    if previous == 0:
        if current == 0:
            return None
        return {"delta": "new", "trend": "up", "comparison": f"no {label} data",
                "previous_display": prev_d, "current_display": cur_d}
    pct = (current - previous) / abs(previous) * 100.0
    if abs(pct) < 0.5:
        return {"delta": "flat", "trend": "flat", "comparison": f"vs {label}",
                "previous_display": prev_d, "current_display": cur_d}
    return {
        "delta": f"{'+' if pct > 0 else '-'}{abs(pct):.1f}%",
        "trend": "up" if pct > 0 else "down",
        "comparison": f"vs {label}",
        "previous_display": prev_d,
        "current_display": cur_d,
        "pct": pct,
    }


def period_delta_line(
    current: Optional[float],
    previous: Optional[float],
    previous_label: str,
    metric_label: str,
    unit: str = "",
    unit_label: str = "",
) -> Optional[str]:
    """'↑ 34% vs last month (₹68,600 → ₹91,951)' — or None when not computable."""
    parts = period_delta_parts(current, previous, previous_label, unit, unit_label)
    if parts is None:
        return None
    if parts["trend"] == "flat":
        return f"{metric_label} flat {parts['comparison']} ({parts['previous_display']})"
    if parts["delta"] == "new":
        return f"{metric_label}: {parts['current_display']} — {parts['comparison']} to compare"
    arrow_sym = "\u2191" if parts["trend"] == "up" else "\u2193"
    return (
        f"{arrow_sym} {abs(parts['pct']):.1f}% {parts['trend']} {parts['comparison']} "
        f"({parts['previous_display']} → {parts['current_display']})"
    )


def delta_label_for_preset(preset: str) -> str:
    return _DELTA_LABELS.get((preset or "").lower(), "previous period")


def aging_insight(
    aging_data: Any,
    days: int = 7,
    scope_total: Optional[float] = None,
    dim_label: str = "department",
    phrase: str = "",
    noun: str = "job",
) -> Optional[str]:
    """'Aging: 34 of 129 jobs are over 7 days old — worst department: Filing-Issue (20)'.

    ``aging_data`` is the companion result grouped by the aging dimension
    (rows of DimensionValue/MetricValue where the metric counts aged rows).
    ``scope_total`` is the answering query's row total — only meaningful when
    both count the same grain (e.g. job counts). None disables the share part.
    ``phrase``/``noun`` adapt the wording per report ("orders past their
    promised date" vs "jobs over 7 days old").
    Silent (None) when nothing is aged — 'no stuck jobs' isn't news.
    """
    rows = _extract_rows(aging_data)
    aged = sum(v for _, v in rows)
    if aged <= 0:
        return None
    aged_i = int(round(aged))
    nouns = noun if aged_i == 1 else noun + "s"
    condition = phrase or f"over {days} days old"
    # A scope smaller than the aged count means a truncated/mismatched
    # denominator — drop the share part rather than print '123%'.
    if scope_total and scope_total >= aged:
        share = aged / scope_total * 100.0
        line = f"Aging: {aged_i:,} of {int(round(scope_total)):,} {nouns} {condition} ({share:.0f}%)"
    else:
        line = f"Aging: {aged_i:,} {nouns} {condition}"
    if len(rows) > 1:
        top_name, top_val = rows[0]
        if top_val > 0:
            line += f" — worst {dim_label}: {top_name} ({int(round(top_val)):,})"
    return line


def deadstock_insight(
    ever_count: Optional[float],
    recent_count: Optional[float],
    days: int = 90,
    noun: str = "design",
) -> Optional[str]:
    """'Dead stock: 214 of 1,830 designs had no sales in the last 90 days (12%)'.

    ``ever_count`` = unique entities that ever sold; ``recent_count`` = unique
    entities with sales in the last ``days`` days. dead = ever - recent.
    Silent when nothing is dead or the numbers don't bracket (recent > ever
    means the companions disagreed — say nothing rather than lie).
    """
    if ever_count is None or recent_count is None or ever_count <= 0:
        return None
    dead = int(round(ever_count - recent_count))
    if dead <= 0 or dead > ever_count:
        return None
    share = dead / ever_count * 100.0
    return (
        f"Dead stock: {dead:,} of {int(round(ever_count)):,} {noun}s that ever "
        f"sold had no sales in the last {days} days ({share:.0f}%)"
    )


def build_insights(
    data: Any,
    spec: Any,
    enabled: Optional[List[str]] = None,
    prior_value: Optional[float] = None,
    prior_label: str = "",
    current_value: Optional[float] = None,
    aging_data: Any = None,
    aging_days: int = 7,
    aging_dim_label: str = "department",
    aging_scope_total: Optional[float] = None,
    aging_phrase: str = "",
    aging_noun: str = "job",
    deadstock_ever: Optional[float] = None,
    deadstock_recent: Optional[float] = None,
    deadstock_days: int = 90,
    deadstock_noun: str = "design",
    severity_out: Optional[List[str]] = None,
) -> List[str]:
    """Compute insight lines for a verified result set.

    ``spec`` is the IntentSpec (metric_key, dimension, unit, unit_label,
    aggregation). ``prior_value`` is the same metric for the previous period
    (when a period_delta companion query ran). All lines are plain strings —
    the caller decides how to render them (text line vs block).
    """
    enabled_set = set(enabled if enabled is not None else DEFAULT_INSIGHTS)
    unit = getattr(spec, "unit", "") or ""
    unit_label = getattr(spec, "unit_label", "") or ""
    dim_label = getattr(spec, "dimension_label", "") or ""
    if not dim_label:
        raw_dim = getattr(spec, "dimension", "") or "row"
        # Prettify raw column keys — 'CustomerFullName' -> 'customer full
        # name' so insights never leak internal schema names.
        dim_label = re.sub(r"(?<![_ ])(?=[A-Z])", " ", raw_dim).replace("_", " ").strip().lower() or raw_dim
    metric_label = getattr(spec, "label", "") or getattr(spec, "metric_key", "") or "Value"
    fmt = lambda v: _fmt(v, unit, unit_label)

    # (line, severity) — severity drives UI colouring: warning = needs
    # attention (aging/dead stock), positive/negative = delta direction,
    # info = neutral fact.
    tagged: List[Tuple[str, str]] = []
    rows = _extract_rows(data)
    if rows:
        if "pareto" in enabled_set:
            tagged += [(l, "info") for l in _pareto(rows, dim_label, fmt)]
        if "vs_average" in enabled_set:
            tagged += [(l, "info") for l in _vs_average(rows, dim_label, fmt)]
        if "outlier" in enabled_set:
            tagged += [(l, "info") for l in _outlier(rows, dim_label, fmt)]

    if "aging" in enabled_set and aging_data is not None:
        line = aging_insight(
            aging_data, days=aging_days,
            scope_total=aging_scope_total, dim_label=aging_dim_label,
            phrase=aging_phrase, noun=aging_noun,
        )
        if line:
            tagged.append((line, "warning"))

    if "deadstock" in enabled_set and deadstock_ever is not None:
        line = deadstock_insight(
            deadstock_ever, deadstock_recent,
            days=deadstock_days, noun=deadstock_noun,
        )
        if line:
            tagged.append((line, "warning"))

    if "period_delta" in enabled_set and prior_value is not None:
        cur = current_value
        if cur is None and rows:
            cur = sum(v for _, v in rows)
        line = period_delta_line(cur, prior_value, prior_label, metric_label, unit, unit_label)
        if line:
            parts = period_delta_parts(cur, prior_value, prior_label, unit, unit_label)
            trend = (parts or {}).get("trend", "flat")
            sev = "positive" if trend == "up" else ("negative" if trend == "down" else "info")
            tagged.append((line, sev))

    if severity_out is not None:
        severity_out.extend(sev for _, sev in tagged)
    return [line for line, _ in tagged]


_COMPARISON_WORDS = re.compile(
    r"\b(last month|last year|last week|previous|prior|growth|compared|compare|vs\b|versus)\b"
)


def _pluralize(noun: str) -> str:
    """'customer' -> 'customers', 'category' -> 'categories', 'branch' -> 'branches'."""
    if re.search(r"(s|x|z|ch|sh)$", noun, re.IGNORECASE):
        return noun + "es"
    if re.search(r"[^aeiou]y$", noun, re.IGNORECASE):
        return noun[:-1] + "ies"
    return noun + "s"


def followup_suggestions(
    question: str,
    dimension_label: str = "",
    has_date: bool = False,
    default_dimension_label: str = "",
    entity_hint: str = "",
    limit: int = 0,
) -> List[str]:
    """Deterministic 'next question' chips after an answer — the questions a
    user most plausibly asks next given what they just saw.

    Breakdown answer → deeper cut + period comparison.
    Scalar answer → comparison + breakdown by the report's default dimension.
    """
    q = (question or "").lower()
    out: List[str] = []
    already_compared = bool(_COMPARISON_WORDS.search(q))

    if dimension_label:
        if has_date and not already_compared:
            out.append("Same breakdown for last month")
        top_n = max(limit, 10)
        if limit and limit < 10:
            out.append(f"Show top {top_n} {_pluralize(dimension_label)}")
        if entity_hint:
            out.append(f"Why did {entity_hint} perform well?")
    else:
        if has_date and not already_compared:
            out.append("Compare with last month")
        if default_dimension_label:
            out.append(f"Break it down by {default_dimension_label}")
        if entity_hint:
            out.append(f"Why did {entity_hint} perform well?")
    # de-dupe while preserving order
    seen = set()
    return [s for s in out if not (s in seen or seen.add(s))][:3]
