import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from app.config import settings
from app.models import ReportRegistryEntry
from app.services import llm_gateway
from app.services.formatters import format_count, format_currency, format_number, format_weight
from app.services.column_registry import _REGISTRY as _COL_REGISTRY
from app.services.intent import detect_intent, IntentSpec
from app.services.output_filter import sanitize_answer, validate_answer
from app.services.token_budget import check_token_budget, truncate_payload_for_budget
from app.services import block_builder
from app.services.column_registry import get_dimension_headers as _get_dimension_headers

logger = logging.getLogger(__name__)


@dataclass
class AnswerResult:
    """Validated, structured result before natural-language formatting."""
    state: str  # SUCCESS, NO_DATA, ZERO, UNSUPPORTED, DATA_UNVERIFIED, ERROR
    value: Any = None
    label: str = ""
    unit: str = ""
    unit_label: str = ""  # display suffix like 'ctw', 'pcs', 'gms', '%'
    aggregation: str = ""
    currency: str = "INR"
    record_count: int = 0
    complete_data: bool = True
    verified: bool = True
    dimension: Optional[str] = None
    dimension_value: Optional[str] = None
    period: Optional[Dict[str, str]] = None
    source_report: str = ""
    error_reason: str = ""


# ── Dimension header labels for ranking tables (dynamic from report_columns.json) ──
def _dim_header(dim: str, report_key: str = "sales_report") -> str:
    headers = _get_dimension_headers(report_key)
    return headers.get(dim, (dim or "Item").replace("_", " ").title())


def _metric_header(spec: IntentSpec) -> str:
    """Column header for the metric in a ranking table (jewelry-native labels)."""
    from app.services.formatters import normalize_unit_label
    jl = normalize_unit_label(spec.unit_label)
    if spec.unit == "currency":
        return "Revenue"
    if spec.unit == "count":
        return "Count" + (f" ({jl})" if jl else "")
    if spec.unit == "weight":
        return "Weight" + (f" ({jl})" if jl else "")
    if spec.unit == "rate" and jl:
        return f"Value ({jl})"
    return "Value"


def _format_ranking_value(value: float, spec: IntentSpec) -> str:
    """Format a single metric value for the ranking table (no currency normalization)."""
    from app.services.formatters import normalize_unit_label
    if spec.unit == "count":
        return format_count(value, normalize_unit_label(spec.unit_label))
    if spec.unit == "weight":
        return format_weight(value, spec.unit_label or "gms")
    if spec.unit == "rate" and spec.unit_label:
        return f"{format_number(value, 2)} {normalize_unit_label(spec.unit_label)}"
    return format_number(value, 3)


def _format_ranking_table(
    data: Dict[str, Any],
    spec: IntentSpec,
    question: str,
    source_report: str,
) -> str:
    """Format a top-N ranking as a tab-separated table with a total line.

    Used when spec.dimension is set and spec.limit > 1 (e.g. "top 5 customers").
    Works with both the real API format (rows with MetricValue/DimensionValue)
    and the list-mode format (sample rows with column values).
    """
    limit = spec.limit or 1
    dim_header = _dim_header(spec.dimension, spec.report_key)
    metric_hdr = _metric_header(spec)
    title = question.strip()

    # ── Real API mode: rows have MetricValue / DimensionValue ──
    if isinstance(data, dict) and "rows" in data and "raw_rd" in data:
        all_rows = data.get("rows", [])
        display_rows = all_rows[:limit]
        if not display_rows:
            return f"No matching records found.\nSources: {source_report}"

        lines = [title, f"Rank\t{dim_header}\t{metric_hdr}"]
        total = 0.0
        for i, row in enumerate(display_rows, 1):
            dim_val = str(row.get("DimensionValue", "") or "")
            metric_val = _to_float(row.get("MetricValue")) or 0.0
            total += metric_val
            lines.append(f"{i}\t{dim_val}\t{_format_ranking_value(metric_val, spec)}")

        lines.append("")
        lines.append(f"Total {metric_hdr.lower()} for top {len(display_rows)}: {_format_ranking_value(total, spec)}")
        lines.append(f"Sources: {source_report}")
        return "\n".join(lines)

    # ── List mode (dummy DB): sample rows with column values ──
    if isinstance(data, dict) and data.get("mode") == "list":
        sample = data.get("sample", [])
        metric_col = spec.metric_key
        dim_col = spec.dimension
        valid = []
        for r in sample:
            v = _to_float(r.get(metric_col))
            d = r.get(dim_col)
            if v is not None and d is not None:
                valid.append((v, str(d)))
        if not valid:
            return f"No matching records found.\nSources: {source_report}"
        reverse = spec.sort == "desc"
        valid.sort(key=lambda x: x[0], reverse=reverse)
        display_rows = valid[:limit]

        lines = [title, f"Rank\t{dim_header}\t{metric_hdr}"]
        total = 0.0
        for i, (metric_val, dim_val) in enumerate(display_rows, 1):
            total += metric_val
            lines.append(f"{i}\t{dim_val}\t{_format_ranking_value(metric_val, spec)}")

        lines.append("")
        lines.append(f"Total {metric_hdr.lower()} for top {len(display_rows)}: {_format_ranking_value(total, spec)}")
        lines.append(f"Sources: {source_report}")
        return "\n".join(lines)

    return f"No matching records found.\nSources: {source_report}"


# ── Deterministic response templates ──────────────────────────────────────────
# Short, direct, professional. No markdown bold so output is clean in plain text
# and HTML. Always end with a Sources line so tests pass.
def _display_value(result: AnswerResult) -> str:
    unit = result.unit
    value = result.value
    ulabel = result.unit_label

    if unit == "text" or isinstance(value, str):
        return str(value)
    if unit == "currency":
        from app.services.formatters import format_currency_dual
        return format_currency_dual(value, result.currency)
    elif unit == "count":
        from app.services.formatters import normalize_unit_label
        return format_count(value, normalize_unit_label(ulabel))
    elif unit == "weight":
        return format_weight(value, ulabel or "gms")
    elif unit == "rate" and ulabel:
        from app.services.formatters import normalize_unit_label
        return f"{format_number(value, 2)} {normalize_unit_label(ulabel)}"
    return format_number(value, 2)


def _value_label_for_ranking(spec: IntentSpec) -> str:
    """Human label for the metric value in a ranking answer (jewelry-native)."""
    # Use the metric catalog label when available (config-driven)
    if spec.report_key:
        report_cfg = _COL_REGISTRY.get(spec.report_key, {})
        cat = report_cfg.get("metric_catalog", {}).get(spec.metric_key, {})
        if cat.get("label"):
            return cat["label"]
    if spec.report_key == "sales_report":
        return "Sales"
    return "Amount"


def _format_answer(
    result: AnswerResult,
    spec: IntentSpec,
    filters: Dict[str, Any],
    assumptions: List[str],
) -> str:
    """Build the final user-facing answer from a validated result."""
    lines = []
    display = _display_value(result)

    if result.dimension and result.dimension_value:
        # Ranking / top-N questions
        entity_label = result.label
        lines.append(f"{entity_label.title()}: {result.dimension_value}")
        lines.append(f"{_value_label_for_ranking(spec)}: {display}")
    else:
        label = result.label
        if spec.aggregation == "avg" and not spec.dimension:
            label = label.replace("Total ", "Average ")
        lines.append(f"{label}: {display}")

    # Show transaction count only when it adds context beyond the metric itself.
    # For count-type metrics the value IS the count, so repeating it is redundant.
    # For ranking questions the count is not meaningful.
    if result.record_count > 0 and spec.aggregation != "count" and not spec.dimension and result.unit != "text":
        lines.append(f"Transactions: {format_count(result.record_count)}")

    # Sources line
    source = result.source_report if result.verified else "not available"
    lines.append(f"Sources: {source}")

    return "\n".join(lines)


@dataclass
class _ResolvedMetric:
    value: Any
    label: str
    unit: str
    aggregation: str
    dimension_value: Optional[str] = None


def _resolve_metric(data: Any, spec: IntentSpec, question: str) -> _ResolvedMetric:
    """Extract or compute the requested metric from the report data."""
    # Real API mode: data has "rows" with MetricValue/DimensionValue from the SP
    if isinstance(data, dict) and "rows" in data and "raw_rd" in data:
        return _resolve_real_api_metric(data, spec, question)

    # Aggregate-mode reports (purchase_summary, etc.) expose data field
    if isinstance(data, dict) and data.get("mode") == "aggregate" and "data" in data:
        agg_data = data["data"]
        metric = spec.metric_key

        # AVG: derive from total / count when both are present
        if spec.aggregation == "avg":
            count = agg_data.get("total_invoices") or agg_data.get("total_orders") or 0
            if metric in agg_data and count:
                value = float(agg_data[metric]) / float(count)
                return _ResolvedMetric(value=value, label=_metric_label(metric, spec.intent, spec.report_key), unit=spec.unit, aggregation=spec.aggregation)

        value = _get_aggregate_value(agg_data, metric, spec.aggregation)
        label = _metric_label(metric, spec.intent, spec.report_key)
        return _ResolvedMetric(value=value, label=label, unit=spec.unit, aggregation=spec.aggregation)

    # List-mode reports (sales_report, stock_ledger, party_outstanding, order_status)
    if isinstance(data, dict) and data.get("mode") == "list":
        rows = data.get("sample", [])
        all_rows = rows  # dummy DB returns all rows; truncate flag tells completeness
        total_count = data.get("total_count", len(rows))
        aggregates = data.get("aggregates", {})

        # Dimension/ranking queries (top customer, best branch, who owes most, etc.)
        if spec.dimension:
            return _resolve_ranking(data, spec, question)

        # Count is a special case: use the report's total_count
        if spec.aggregation == "count":
            return _ResolvedMetric(
                value=total_count,
                label=_metric_label(spec.intent, spec.intent, spec.report_key),
                unit=spec.unit,
                aggregation=spec.aggregation,
            )

        metric = spec.metric_key

        # For SUM, prefer the pre-computed aggregate if it exists.
        if spec.aggregation == "sum" and metric in aggregates:
            raw = aggregates[metric]
            return _ResolvedMetric(
                value=float(raw),
                label=_metric_label(metric, spec.intent, spec.report_key),
                unit=spec.unit,
                aggregation=spec.aggregation,
            )

        # For COUNT, use the report's total_count
        if spec.aggregation == "count":
            return _ResolvedMetric(
                value=total_count,
                label=_metric_label(spec.intent, spec.intent, spec.report_key),
                unit=spec.unit,
                aggregation=spec.aggregation,
            )

        # For AVG/MAX/MIN, prefer pre-computed aggregates if present
        if spec.aggregation == "avg" and f"avg_{metric}" in aggregates:
            return _ResolvedMetric(
                value=float(aggregates[f"avg_{metric}"]),
                label=_metric_label(metric, spec.intent, spec.report_key),
                unit=spec.unit,
                aggregation=spec.aggregation,
            )
        if spec.aggregation == "max" and f"max_{metric}" in aggregates:
            return _ResolvedMetric(
                value=float(aggregates[f"max_{metric}"]),
                label=_metric_label(metric, spec.intent, spec.report_key),
                unit=spec.unit,
                aggregation=spec.aggregation,
            )

        # Compute MAX/MIN/AVG from the sample rows
        if not rows:
            return _ResolvedMetric(value=None, label="", unit=spec.unit, aggregation=spec.aggregation)

        values = _extract_values(rows, metric)
        computed = _compute_from_values(values, total_count, spec.aggregation)
        return _ResolvedMetric(
            value=computed,
            label=_metric_label(metric, spec.intent, spec.report_key),
            unit=spec.unit,
            aggregation=spec.aggregation,
        )

    # Fallback: the data is not in a known shape
    return _ResolvedMetric(value=None, label="", unit="", aggregation=spec.aggregation)


def _resolve_real_api_metric(data: Dict[str, Any], spec: IntentSpec, question: str) -> _ResolvedMetric:
    """Resolve metric from the real API response format (rd/rd1).

    The SP returns:
      rows: [{ MetricValue, DimensionValue }]
      total_count: int
      values: [float]
      dimensions: [str]
    """
    rows = data.get("rows", [])
    total_count = data.get("total_count", 0)
    values = data.get("values", [])
    dimensions = data.get("dimensions", [])

    label = _metric_label(spec.metric_key, spec.intent, spec.report_key)

    # Ranking / dimension queries: return top row
    if spec.dimension and rows:
        top_row = rows[0]
        metric_value = _to_float(top_row.get("MetricValue"))
        dim_value = str(top_row.get("DimensionValue", ""))
        return _ResolvedMetric(
            value=metric_value,
            label=label,
            unit=spec.unit,
            aggregation=spec.aggregation,
            dimension_value=dim_value,
        )

    # Count: prefer the SP's computed MetricValue (e.g. COUNT(DISTINCT x) for
    # unique_customers); total_count is the row count and is only a fallback.
    if spec.aggregation == "count":
        return _ResolvedMetric(
            value=values[0] if values else total_count,
            label=label,
            unit=spec.unit,
            aggregation=spec.aggregation,
        )

    # Sum/Avg/Max/Min: single value from rd
    if values:
        if spec.aggregation == "sum":
            return _ResolvedMetric(value=values[0], label=label, unit=spec.unit, aggregation=spec.aggregation)
        if spec.aggregation == "avg":
            return _ResolvedMetric(value=values[0], label=label, unit=spec.unit, aggregation=spec.aggregation)
        if spec.aggregation == "max":
            return _ResolvedMetric(value=values[0], label=label, unit=spec.unit, aggregation=spec.aggregation)
        if spec.aggregation == "min":
            return _ResolvedMetric(value=values[0], label=label, unit=spec.unit, aggregation=spec.aggregation)
        # Default: sum
        return _ResolvedMetric(value=values[0], label=label, unit=spec.unit, aggregation=spec.aggregation)

    # Text metrics (e.g. "give me its customer name" -> CustomerFullName)
    text_values = data.get("text_values", [])
    if text_values:
        return _ResolvedMetric(
            value=text_values[0],
            label=label,
            unit="text",
            aggregation=spec.aggregation,
        )

    # No data
    return _ResolvedMetric(value=None, label=label, unit=spec.unit, aggregation=spec.aggregation)


def _resolve_ranking(data: Dict[str, Any], spec: IntentSpec, question: str) -> _ResolvedMetric:
    """Resolve a top-N / ranking metric along a dimension."""
    aggregates = data.get("aggregates", {})
    # Prefer pre-computed top-* aggregates (authoritative over the whole dataset)
    dim = spec.dimension
    if dim:
        top_key = f"top_{dim}"
        top_amount_key = f"top_{dim}_amount"
        if top_key in aggregates and top_amount_key in aggregates:
            return _ResolvedMetric(
                value=_to_float(aggregates[top_amount_key]),
                label=_metric_label(spec.metric_key, spec.intent, spec.report_key),
                unit=spec.unit,
                aggregation=spec.aggregation,
                dimension_value=str(aggregates[top_key]),
            )

    # Fallback: compute from the sample rows
    rows = data.get("sample", [])
    metric = spec.metric_key
    valid = []
    for r in rows:
        v = _to_float(r.get(metric))
        d = r.get(dim)
        if v is not None and d is not None:
            valid.append((v, d, r))

    if not valid:
        return _ResolvedMetric(value=None, label="", unit=spec.unit, aggregation=spec.aggregation)

    reverse = spec.sort == "desc"
    valid.sort(key=lambda x: x[0], reverse=reverse)
    top = valid[0]
    return _ResolvedMetric(
        value=top[0],
        label=_metric_label(metric, spec.intent, spec.report_key),
        unit=spec.unit,
        aggregation=spec.aggregation,
        dimension_value=str(top[1]),
    )


def _metric_label(metric: str, intent: str, report_key: str = "") -> str:
    """Human-friendly metric label / role.

    Order:
      1. Per-report intent `label` from report_columns/*.json (config-driven)
      2. metric_catalog label (config-driven)
      3. Backward-compatible hardcoded metric labels for legacy columns
      4. Derive from intent/metric name
    """
    # Layer 1: per-report intent label
    if report_key:
        report_cfg = _COL_REGISTRY.get(report_key, {})
        intent_meta = report_cfg.get("intents", {}).get(intent, {})
        label = intent_meta.get("label", "")
        if label:
            return label

    # Layer 2: metric catalog label (current report only — not all reports)
    if report_key:
        cat = _COL_REGISTRY.get(report_key, {}).get("metric_catalog", {})
        if metric in cat and cat[metric].get("label"):
            return cat[metric]["label"]

    # Layer 2.5: column description from the report's columns config
    if report_key:
        col_meta = _COL_REGISTRY.get(report_key, {}).get("columns", {}).get(metric, {})
        desc = col_meta.get("description", "")
        if desc:
            return desc

    # Layer 3: Backward-compatible hardcoded metric labels for legacy columns
    # that are not yet described in metric_catalog. New reports should add those
    # columns to metric_catalog instead.
    metric_labels = {
        "design_TotalAmouont": "Total design amount",
        "Discount": "Total discount",
        "totaltaxAmount": "Total tax amount",
        "UnitCost": "Unit cost",
        "TotalSettingCost": "Total setting cost",
        "TotalDiamondHandling": "Total diamond handling",
        "totalLabourAmt": "Total labour amount",
        "totalOtherAmt": "Total other amount",
        "OtherAmt": "Total other metal amount",
        "netwt_24k": "Net weight (24K)",
        "packageWt": "Package weight",
        "MetalLoss": "Metal loss",
        "NetWtWithLoss": "Net weight with loss",
        "OtherWt": "Other metal weight",
        "Pure_Silver_Wt": "Pure silver weight",
        "Pure_Platinum_Wt": "Pure platinum weight",
        "Pure_Other_Wt": "Pure other metal weight",
        "dpcs": "Diamond pieces count",
        "csctw": "Total colourstone carat weight",
        "cspcs": "Colourstone pieces count",
        "miscpcs": "Misc pieces count",
        "D_Wt_Cm": "Diamond weight (cm)",
        "D_Wt_Ct": "Diamond weight (ct)",
        "D_Pcs_Cm": "Diamond pieces (cm)",
        "D_Pcs_Ct": "Diamond pieces (ct)",
        "C_Wt_Cm": "Colourstone weight (cm)",
        "C_Wt_Ct": "Colourstone weight (ct)",
        "C_Pcs_Cm": "Colourstone pieces (cm)",
        "C_Pcs_Ct": "Colourstone pieces (ct)",
        "Tunch": "Tunch",
        "Wastage": "Wastage",
        "Wastage_24k": "Wastage (24K)",
        "LeadAge": "Lead age",
        "PromiseAge": "Promise age",
        "CurrentAge": "Current age",
    }
    if metric in metric_labels:
        return metric_labels[metric]
    if intent:
        return intent.replace("_", " ").title()
    return metric.replace("_", " ").replace("Amount", " Amount").title()


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _extract_values(rows: List[Dict[str, Any]], key: str) -> List[float]:
    out = []
    for r in rows:
        v = _to_float(r.get(key))
        if v is not None:
            out.append(v)
    return out


def _compute_from_values(values: List[float], total_count: int, aggregation: str) -> Optional[float]:
    if not values and total_count == 0:
        return None
    if aggregation == "count":
        return total_count
    if not values:
        return 0.0
    if aggregation == "sum":
        return sum(values)
    if aggregation == "avg":
        # Always divide by the number of rows we actually have.
        # Scaling by total_count would understate the average when the
        # sample is smaller than the full dataset.
        return sum(values) / len(values)
    if aggregation == "max":
        return max(values)
    if aggregation == "min":
        return min(values)
    return sum(values)


def _get_aggregate_value(agg_data: Dict[str, Any], metric: str, aggregation: str) -> Optional[float]:
    """Read a pre-computed aggregate value."""
    if metric in agg_data:
        raw = agg_data[metric]
        if isinstance(raw, (int, float)):
            return float(raw)
    return None


def _apply_aggregation(raw: Any, total_count: int, metric: str, aggregation: str) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


# ── Public API ───────────────────────────────────────────────────────────────

async def generate_answer(
    data: Any,
    entry: ReportRegistryEntry,
    validated_filters: Dict[str, Any],
    assumptions: List[str],
    question: str = "",
    token_usage: Optional[List[Dict[str, int]]] = None,
    history: Optional[List[Dict[str, str]]] = None,
    intent_spec: Optional[IntentSpec] = None,
    response_mode: str = "normal",
) -> Any:
    """Generate a natural-language answer or structured blocks.

    Normal mode returns a string (deterministic pipeline).
    Wide mode returns a list of block dicts (structured JSON blocks).
    """
    if isinstance(data, dict) and data.get("error"):
        if response_mode == "wide":
            return block_builder.build_error_blocks(
                f"I couldn't retrieve the {entry.report_key} report: {data['error']}"
            )
        return f"I couldn't retrieve the {entry.report_key} report: {data['error']}"

    spec = intent_spec if intent_spec is not None else detect_intent(question, entry.report_key)
    source_report = entry.report_key.replace("_", " ").title()

    # If intent is still unknown after all fallbacks, ask a clarifying question
    if spec.intent == "unknown" or spec.state == "FALLBACK_FAILED":
        if response_mode == "wide":
            return block_builder.build_clarify_blocks(entry.report_key)
        # Normal mode: short text with report-aware suggestions
        from app.services.column_registry import _REGISTRY as _COL_REGISTRY
        report_cfg = _COL_REGISTRY.get(entry.report_key, {})
        intents = report_cfg.get("intents", {})
        suggestions = []
        seen = set()
        for name, rule in intents.items():
            label = rule.get("label", "")
            if label and label not in seen and len(suggestions) < 3:
                suggestions.append(label)
                seen.add(label)
        if not suggestions:
            suggestions = ["Total amount", "Top 5 customers", "How many records"]
        return (
            f"I'm not sure what you're asking about. Could you be more specific?\n\n"
            f"Try asking about:\n"
            + "\n".join(f"- {s}" for s in suggestions)
            + f"\n\nSources: {source_report}"
        )

    # --- Multi-row ranking: format as a table (top-N customers, branches, etc.) ---
    if spec.dimension and (spec.limit or 1) > 1:
        if response_mode == "wide":
            return block_builder.build_ranking_blocks(data, spec, question, source_report, assumptions, filters=validated_filters)
        return _format_ranking_table(data, spec, question, source_report)

    # --- Compute the metric deterministically --------------------------------
    resolved = _resolve_metric(data, spec, question)

    # Determine record count and completeness
    record_count = _record_count(data)
    complete_data = not (isinstance(data, dict) and data.get("truncated", False))

    # Build the answer result
    result = AnswerResult(
        state="SUCCESS",
        value=resolved.value,
        label=resolved.label,
        unit=resolved.unit,
        unit_label=getattr(spec, "unit_label", ""),
        aggregation=spec.aggregation,
        currency="INR",
        record_count=record_count,
        complete_data=complete_data,
        verified=True,
        dimension=spec.dimension,
        dimension_value=resolved.dimension_value,
        source_report=source_report,
    )

    # State handling
    if resolved.value is None:
        if record_count == 0:
            result.state = "NO_DATA"
            if response_mode == "wide":
                return block_builder.build_no_data_blocks(result.label, validated_filters, source_report)
            return _no_data_answer(result, validated_filters)
        # aggregate returned but no rows for this dimension
        result.state = "DATA_UNVERIFIED"

    if resolved.value == 0 and record_count == 0:
        result.state = "NO_DATA"
        if response_mode == "wide":
            return block_builder.build_no_data_blocks(result.label, validated_filters, source_report)
        return _no_data_answer(result, validated_filters)

    if resolved.value == 0:
        result.state = "ZERO"
        if response_mode == "wide":
            return block_builder.build_simple_blocks(
                label=result.label, value=0, unit=result.unit, currency=result.currency,
                record_count=record_count, source_report=source_report,
                assumptions=assumptions, aggregation=spec.aggregation,
                unit_label=result.unit_label, filters=validated_filters,
            )
        return _zero_answer(result)

    # Format the deterministic answer
    answer = _format_answer(result, spec, validated_filters, assumptions)

    # Validate the formatted answer against source data (catch any formatting bugs)
    try:
        validation = validate_answer(answer, data, question)
        if not validation['passed']:
            logger.warning("Answer validation warnings: %s", validation['warnings'])
    except Exception:
        pass  # Don't block the answer if validation itself fails

    if response_mode == "wide":
        return block_builder.build_simple_blocks(
            label=result.label, value=result.value, unit=result.unit, currency=result.currency,
            record_count=record_count, source_report=source_report,
            assumptions=assumptions,
            dimension_value=result.dimension_value, aggregation=spec.aggregation,
            unit_label=result.unit_label, filters=validated_filters,
        )

    return answer


def _record_count(data: Any) -> int:
    if isinstance(data, dict):
        if "total_count" in data:
            return int(data["total_count"])
        if data.get("mode") == "aggregate" and "data" in data:
            # For aggregate, we don't know exact count unless exposed
            return data["data"].get("total_invoices") or data["data"].get("total_orders") or 0
        if "rows" in data and "raw_rd" in data:
            # Real API mode: total_count is already extracted from rd1
            return int(data.get("total_count", 0))
    if isinstance(data, list):
        return len(data)
    return 0


# Suggested follow-up questions for no-data responses
_SUGGESTED_QUESTIONS = [
    "What were the total sales this month?",
    "Top 5 customers by revenue",
    "Total diamond weight and pieces",
    "Gold amount by branch",
]


def _no_data_answer(result: AnswerResult, filters: Dict[str, Any]) -> str:
    """Generate a short, user-friendly response when no data is found."""
    label = result.label or "data"

    lines = [f"No {label.lower()} found for the selected filters."]
    lines.append("")
    lines.append("You can try:")
    lines.append("- Asking for a different date range")
    lines.append(f"- Overall total {label.lower()} without filters")
    lines.append(f"- Ranking like \"top 5 by {label.lower()}\"")
    lines.append("")
    lines.append("Some questions you can ask:")
    for q in _SUGGESTED_QUESTIONS:
        lines.append(f"- {q}")

    return "\n".join(lines)


def _zero_answer(result: AnswerResult) -> str:
    """Handle zero-value results with helpful context."""
    if result.unit == "currency":
        display = format_currency(0, result.currency)
    elif result.unit == "count":
        display = format_count(0, result.unit_label)
    elif result.unit == "weight":
        display = format_weight(0, result.unit_label or "gms")
    elif result.unit == "rate" and result.unit_label:
        display = f"{format_number(0, 2)} {result.unit_label}"
    else:
        display = format_number(0, 3)

    lines = [f"{result.label}: {display}"]

    if result.record_count == 0:
        lines.append("")
        lines.append("No transactions found. Try a different date range or remove filters.")
    else:
        lines.append(f"Transactions: {format_count(result.record_count)}")
    if result.source_report:
        lines.append(f"Sources: {result.source_report}")

    return "\n".join(lines)


# ── LLM fallback (kept for complex / unmatched questions) ─────────────────────
# The remaining helpers are used by /chat/stream and for the rare non-deterministic case.

def _prepare_payload(data: Any, response_mode: str) -> Dict[str, Any]:
    """Build a compact, LLM-safe payload from the API response."""
    if response_mode == "aggregate":
        if isinstance(data, dict) and "data" in data:
            agg_data = data["data"]
        else:
            agg_data = data
        payload = {
            "mode": "aggregate",
            "data": agg_data,
            "formatted_data": _format_aggregates(agg_data) if isinstance(agg_data, dict) else {},
        }
        if isinstance(agg_data, dict):
            has_data = any(isinstance(v, (int, float)) and v != 0 for v in agg_data.values())
            payload["record_count"] = 1 if has_data else 0
        return payload

    if isinstance(data, dict):
        sample = data.get("sample", [])
        total_count = data.get("total_count", len(sample))
        aggregates = data.get("aggregates", {})
        truncated = data.get("truncated", False)
    elif isinstance(data, list):
        sample = data
        total_count = len(data)
        aggregates = {}
        truncated = False
    else:
        sample = []
        total_count = 0
        aggregates = {}
        truncated = False

    limit = settings.llm_safe_row_limit
    if len(sample) > limit:
        sample = sample[:limit]
        truncated = True

    _key_fields = {
        "invoiceNo", "invoice_no", "invoiceDate", "invoice_date",
        "customerName", "customer_name", "customerCode", "customer_code",
        "totalAmount", "total_amount", "amount",
        "metalAmount", "metal_amount",
        "diamondAmount", "diamond_amount",
        "category", "brand", "branch",
        "itemNo", "item_no", "itemName", "item_name",
        "inQty", "in_qty", "outQty", "out_qty", "balanceQty", "balance_qty",
        "orderNo", "order_no", "status",
        "partyName", "party_name", "outstandingAmount", "outstanding_amount",
        "qty", "quantity",
    }
    compact_sample = []
    for row in sample:
        if isinstance(row, dict):
            compact = {k: v for k, v in row.items() if k in _key_fields or k in ("id",)}
            if len(compact) < 3:
                compact = {k: v for k, v in list(row.items())[:8]}
            compact_sample.append(compact)
        else:
            compact_sample.append(row)

    return {
        "mode": "list",
        "total_count": total_count,
        "aggregates": aggregates,
        "formatted_aggregates": _format_aggregates(aggregates, total_count),
        "sample": compact_sample,
        "truncated": truncated,
    }


def _format_aggregates(aggregates: Dict[str, Any], total_count: int = 0) -> Dict[str, str]:
    """Pre-format aggregate values so the legacy LLM path can use them as-is."""
    if not isinstance(aggregates, dict):
        return {}
    formatted = {}
    for key, value in aggregates.items():
        if value is None:
            formatted[key] = "N/A"
        elif isinstance(value, (int, float)):
            kl = key.lower()
            if any(k in kl for k in ("amount", "total", "value", "gross", "net", "discount", "tax")):
                formatted[key] = format_currency(value)
            elif any(k in kl for k in ("qty", "quantity", "count", "pcs", "pieces")):
                formatted[key] = format_count(int(value))
            else:
                formatted[key] = format_number(value, 2)
        else:
            formatted[key] = str(value)

    _add_derived_metrics(aggregates, formatted, total_count)
    return formatted


def _add_derived_metrics(aggregates: Dict[str, Any], formatted: Dict[str, str], total_count: int = 0) -> None:
    total_key = None
    for key in aggregates:
        kl = key.lower()
        if "totalamount" in kl or kl == "total" or "total_amount" in kl:
            total_key = key
            break

    count = total_count if total_count > 0 else 0
    if count == 0:
        for key in aggregates:
            if "count" in key.lower() or "invoices" in key.lower() or "orders" in key.lower():
                try:
                    count = float(aggregates[key])
                except (ValueError, TypeError):
                    pass
                break

    if total_key and count > 0:
        try:
            total = float(aggregates[total_key])
            avg = total / count
            if "average" not in formatted and "avg" not in formatted:
                formatted["average_amount"] = format_currency(avg)
        except (ValueError, TypeError):
            pass


def _build_prompt(
    question: str,
    entry: ReportRegistryEntry,
    payload: Dict[str, Any],
    validated_filters: Dict[str, Any],
    assumptions: List[str],
) -> str:
    """Build a prompt for the legacy LLM fallback."""
    filters_text = _render_filters(validated_filters)
    assumptions_text = _render_assumptions(assumptions)

    data_summary = ""
    mode = payload.get("mode", "")
    if mode == "aggregate":
        formatted = payload.get("formatted_data", {})
        non_zero = {k: v for k, v in formatted.items() if isinstance(v, str) and any(c.isdigit() for c in v) and v != "0"}
        if non_zero:
            data_summary = f"DATA STATUS: Aggregate data available. {len(non_zero)} metrics found. Use these values directly.\n"
        else:
            data_summary = "DATA STATUS: No data returned for these filters.\n"
    elif mode == "list":
        count = payload.get("total_count", 0)
        if count > 0:
            data_summary = f"DATA STATUS: {count} records found. Use formatted_aggregates for totals.\n"
        else:
            data_summary = "DATA STATUS: No records found for these filters.\n"

    return (
        f"User question: {question}\n\n"
        f"Report: {entry.report_key.replace('_', ' ')}\n"
        f"{data_summary}\n"
        f"Report data:\n{json.dumps(payload, indent=2, ensure_ascii=False, default=str)}\n\n"
        f"Filters used:\n{filters_text}\n\n"
        f"Assumptions / defaults applied:\n{assumptions_text}\n\n"
        "Answer briefly and directly. Use **bold** for key amounts. "
        "Do not say the data is missing if formatted values are present. "
        "End with a Sources line."
    )


def _render_assumptions(assumptions: List[str]) -> str:
    if not assumptions:
        return "- none"
    return "\n".join(f"- {a}" for a in assumptions)


async def _llm_fallback(
    data: Any,
    entry: ReportRegistryEntry,
    validated_filters: Dict[str, Any],
    assumptions: List[str],
    question: str,
    token_usage: Optional[List[Dict[str, int]]] = None,
    history: Optional[List[Dict[str, str]]] = None,
) -> str:
    """Old LLM path; used for streaming and open-ended questions."""
    payload = _prepare_payload(data, entry.response_mode)

    system = (
        "You're a direct ERP assistant. Answer in 2-3 sentences. "
        "Use **bold** for key numbers. End with a Sources line. "
        "If data is missing, say so plainly."
    )
    user_prompt = _build_prompt(question, entry, payload, validated_filters, assumptions)
    messages = [{"role": "system", "content": system}]
    if history:
        messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})

    result = await llm_gateway.chat(
        tier="cheap",
        messages=messages,
        temperature=0.3,
        max_tokens=1024,
    )
    if token_usage is not None:
        token_usage.append(result.usage)

    validation = validate_answer(result.text, data, question)
    if not validation['passed']:
        return sanitize_answer(result.text, validation)
    return result.text
