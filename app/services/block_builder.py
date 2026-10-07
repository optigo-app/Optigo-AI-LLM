"""Block builder for structured (wide) response mode.

Builds JSON blocks (text, table, chart, list, assumption, error) from
AnswerResult / report data.  Deterministic for simple + ranking queries;
LLM fallback for complex multi-section answers.
"""

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from app.services.formatters import (
    format_count,
    format_currency,
    format_currency_dual,
    format_number,
    format_weight,
    normalize_unit_label,
)
from app.services.column_registry import get_dimension_headers as _get_dimension_headers

logger = logging.getLogger(__name__)

# ── Dimension header labels (dynamic from report_columns.json) ──
def _dim_header(dim: str, report_key: str = "sales_report") -> str:
    headers = _get_dimension_headers(report_key)
    return headers.get(dim, (dim or "Item").replace("_", " ").title())


_MONTH_ABBR = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def format_date_friendly(iso: Any) -> str:
    """'2026-08-01' -> '01 Aug 2026'; '2026-08' -> 'Aug 2026'. Non-ISO input
    passes through unchanged."""
    parts = str(iso or "").split("-")
    try:
        if len(parts) == 3:
            return f"{int(parts[2]):02d} {_MONTH_ABBR[int(parts[1]) - 1]} {parts[0]}"
        if len(parts) == 2:
            return f"{_MONTH_ABBR[int(parts[1]) - 1]} {parts[0]}"
    except (ValueError, IndexError):
        pass
    return str(iso or "")


def _format_dimension_value(dim_val: str, dimension: str) -> str:
    """Friendly labels for time dimensions: '2026-10' -> 'Oct 2026'."""
    if (dimension or "").lower() in ("month", "date", "entrydate", "jobdate"):
        friendly = format_date_friendly(dim_val)
        return friendly if friendly != dim_val else dim_val
    return dim_val


def _metric_header(unit: str, unit_label: str = "") -> str:
    """Column header for the metric in a ranking table, using jewelry-native labels."""
    jl = normalize_unit_label(unit_label)
    if unit == "currency":
        return "Revenue"
    if unit == "count":
        return "Count" + (f" ({jl})" if jl else "")
    if unit == "weight":
        return "Weight" + (f" ({jl})" if jl else "")
    if unit == "rate" and jl:
        return f"Value ({jl})"
    return "Value"


def _to_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _format_ranking_value(value: float, unit: str, unit_label: str = "") -> str:
    if unit == "currency":
        return format_currency(value, "INR")
    if unit == "count":
        return format_count(value, normalize_unit_label(unit_label))
    if unit == "weight":
        return format_weight(value, unit_label or "gms")
    if unit == "rate" and unit_label:
        return f"{format_number(value, 2)} {normalize_unit_label(unit_label)}"
    return format_number(value, 3)


def _display_value(value: Any, unit: str, currency: str = "INR", unit_label: str = "") -> str:
    if unit == "currency":
        return format_currency_dual(value, currency)
    elif unit == "count":
        return format_count(value, normalize_unit_label(unit_label))
    elif unit == "weight":
        return format_weight(value, unit_label or "gms")
    elif unit == "rate" and unit_label:
        return f"{format_number(value, 2)} {normalize_unit_label(unit_label)}"
    return format_number(value, 2)


# ── Deterministic block builders ─────────────────────────────────────────────

def _build_period_block(filters: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Build a period chip block from the resolved date filters.

    Returns None when no date range is present (so the frontend doesn't
    render an empty chip).
    """
    if not filters:
        return None
    start = filters.get("start_date") or filters.get("from_date") or filters.get("date_from")
    end = filters.get("end_date") or filters.get("to_date") or filters.get("date_to")
    if not start and not end:
        # No date range — but maybe other filters are present; show them as a chip
        other = {k: v for k, v in filters.items() if v not in (None, "", [])}
        if not other:
            return None
        return {"type": "period", "label": "Filters", "value": "; ".join(f"{k}={v}" for k, v in other.items()),
                "filters": filters}
    if start and end and start == end:
        value = format_date_friendly(start)
    elif start and end:
        value = f"{format_date_friendly(start)} to {format_date_friendly(end)}"
    else:
        value = format_date_friendly(start or end)
    return {"type": "period", "label": "Date Range", "value": value, "filters": filters}


# Jewelry-industry glossary terms that may appear in answers. Only terms
# actually referenced by the answer's unit_label / metric are included so the
# glossary stays relevant and small.
_JEWELRY_GLOSSARY: Dict[str, str] = {
    "ctw": "Carat Total Weight — total carat weight of stones in a piece",
    "gms": "Grams — metal weight unit",
    "pcs": "Pieces — count of items",
    "tunch": "Tunch — gold purity percentage (e.g. 99.5% = 24K)",
    "wastage": "Wastage — making charges calculated as a % of metal weight",
    "making": "Making Charges — labour charge for crafting the jewellery",
    "netwt": "Net Weight — metal weight excluding stones",
    "grosswt": "Gross Weight — total weight including stones",
    "pure": "Pure Weight — pure metal weight after applying purity",
}


def _build_glossary_block(used_terms: List[str]) -> Optional[Dict[str, Any]]:
    """Build a glossary block for jewelry terms used in the answer.

    `used_terms` is a list of lowercase keys present in _JEWELRY_GLOSSARY.
    Returns None when no terms apply (so the frontend doesn't render an
    empty legend).
    """
    if not used_terms:
        return None
    terms = {}
    for t in used_terms:
        key = t.lower()
        if key in _JEWELRY_GLOSSARY:
            terms[key] = _JEWELRY_GLOSSARY[key]
    if not terms:
        return None
    return {"type": "glossary", "title": "Glossary", "terms": terms}


def build_simple_blocks(
    label: str,
    value: Any,
    unit: str,
    currency: str,
    record_count: int,
    source_report: str,
    assumptions: Optional[List[str]] = None,
    dimension_value: Optional[str] = None,
    dimension_label: str = "",
    aggregation: str = "",
    unit_label: str = "",
    filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Build blocks for a simple single-value answer (total, count, etc.).

    Emits a metric_card block (hero metric) for the primary value, a period
    block when a date range is present, and a sources line. Keeps the legacy
    `metric` block too so older frontends still render.
    """
    blocks: List[Dict[str, Any]] = []

    # Period chip — show the date range / filters the numbers belong to
    period_block = _build_period_block(filters)
    if period_block:
        blocks.append(period_block)

    if value is None and record_count == 0:
        blocks.append({"type": "text", "content": f"No {label.lower()} found for the selected filters."})
        _no_data_suggests = [
            "Try a different date range",
            f"Overall total {label.lower()} without filters",
            "What were the total sales this month?",
            "Top 5 customers by revenue",
        ]
        blocks.append({"type": "suggestions", "items": _no_data_suggests,
                       "options": _chip_options(_no_data_suggests)})
        if source_report:
            blocks.append({"type": "sources", "items": [source_report]})
        if assumptions:
            blocks.append({"type": "assumption", "content": "; ".join(assumptions)})
        return blocks

    if value is not None and dimension_value:
        blocks.append({"type": "text", "content": f"{dimension_label or 'Top'}: {dimension_value}"})
        display = _display_value(value, unit, currency, unit_label)
        blocks.append({"type": "metric_card",
                        "label": label,
                        "value": display,
                        "raw_value": float(value) if _to_float(value) is not None else None,
                        "unit": unit, "currency": currency if unit == "currency" else None,
                        "unit_label": normalize_unit_label(unit_label)})
    elif value is not None:
        display = _display_value(value, unit, currency, unit_label)
        display_label = label
        if aggregation == "avg":
            display_label = display_label.replace("Total ", "Average ")
        subtext = ""
        if record_count > 0 and aggregation != "count":
            subtext = f"Transactions: {format_count(record_count)}"
        # Hero metric card
        blocks.append({"type": "metric_card",
                        "label": display_label,
                        "value": display,
                        "raw_value": float(value) if _to_float(value) is not None else None,
                        "unit": unit, "currency": currency if unit == "currency" else None,
                        "unit_label": normalize_unit_label(unit_label),
                        "subtext": subtext})
        if value == 0 and record_count == 0:
            blocks.append({"type": "text", "content": "No transactions found. Try a different date range or remove filters."})
    else:
        blocks.append({"type": "text", "content": f"No {label.lower()} found for the selected filters."})

    if source_report:
        blocks.append({"type": "sources", "items": [source_report]})

    if assumptions:
        combined = "; ".join(assumptions)
        blocks.append({"type": "assumption", "content": combined})

    return blocks


# ── Metric display labels for multi-metric table ──
# Jewelry-industry-native short labels (shorter than _metric_label's full sentences).
# Uses industry-standard terms: "Gross Wt", "Net Wt", "Diamond Ctw", "Making Charges",
# "Metal Value", "Pure Gold", "Tunch", "Wastage %", etc.
_METRIC_DISPLAY_LABELS: Dict[str, str] = {
    "Amount": "Total Sales",
    "MetalAmount": "Metal Value",
    "MetalRate": "Metal Rate",
    "DiamondAmount": "Diamond Value",
    "ColorStoneAmount": "Colourstone Value",
    "LabourAmount": "Making Charges",
    "OtherAmount": "Other Charges",
    "WastageAmount": "Wastage Charges",
    "GoldAmt": "Gold Value",
    "SilverAmt": "Silver Value",
    "PlatinumAmt": "Platinum Value",
    "OtherAmt": "Other Metal Value",
    "grosswt": "Gross Wt",
    "netwt": "Net Wt",
    "MetalLoss": "Metal Loss",
    "NetWtWithLoss": "Net Wt (with Loss)",
    "GoldWt": "Gold Wt",
    "SilverWt": "Silver Wt",
    "PlatinumWt": "Platinum Wt",
    "OtherWt": "Other Metal Wt",
    "Pure_Gold_Wt": "Pure Gold Wt",
    "Pure_Silver_Wt": "Pure Silver Wt",
    "Pure_Platinum_Wt": "Pure Platinum Wt",
    "Pure_Other_Wt": "Pure Other Metal Wt",
    "Tunch": "Tunch",
    "Wastage": "Wastage %",
    "Discount": "Discount",
    "total_count": "Bill Count",
    "unique_customers": "Unique Customers",
    "unique_designs": "Unique Designs",
    "dctw": "Diamond Ctw",
    "dpcs": "Diamond Pcs",
    "csctw": "Colourstone Ctw",
    "cspcs": "Colourstone Pcs",
    "miscwt": "Misc Wt",
    "miscpcs": "Misc Pcs",
    "UnitCost": "Unit Cost",
    "TotalSettingCost": "Setting Cost",
    "TotalDiamondHandling": "Diamond Handling",
    "totalLabourAmt": "Total Making Charges",
    "totalOtherAmt": "Total Other Charges",
    # WIP-specific
    "JobCost": "Job Cost",
    "MountAmount": "Mount Value",
    "FindingAmount": "Finding Value",
    "GrossWeightgm": "Gross Wt",
    "NetWtgm": "Net Wt",
    "PureWt": "Pure Wt",
    "Quantity": "Quantity",
}


def build_multi_metric_blocks(
    results: List[Dict[str, Any]],
    question: str,
    record_count: int,
    source_report: str,
    assumptions: Optional[List[str]] = None,
    filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Build blocks for a multi-metric single-value answer (e.g. metal rate + metal amount).

    Args:
        results: Ordered list of dicts with keys: metric_key, value, unit, label.
                 Order is preserved (primary metric first, then extras).
    """
    blocks: List[Dict[str, Any]] = []

    # Period chip — show the date range the numbers belong to
    period_block = _build_period_block(filters)
    if period_block:
        blocks.append(period_block)

    # Check if all values are None (no data)
    all_none = all(r.get("value") is None for r in results)
    if all_none:
        blocks.append({"type": "text", "content": "No data found for the requested metrics. Try a different date range or remove filters."})
        return blocks

    # Heading
    blocks.append({"type": "heading", "content": question.strip()})

    # Build table with Metric / Value columns
    table_rows: List[List[str]] = []
    raw_rows: List[Dict[str, Any]] = []
    for r in results:
        metric_key = r.get("metric_key", "")
        value = r.get("value")
        unit = r.get("unit", "currency")
        ulabel = r.get("unit_label", "")
        label = _METRIC_DISPLAY_LABELS.get(metric_key, r.get("label", metric_key))
        if value is not None:
            display = _display_value(value, unit, "INR", ulabel)
        else:
            display = "N/A"
        table_rows.append([label, display])
        raw_rows.append({"metric_key": metric_key, "label": label, "raw_value": value, "unit": unit, "unit_label": ulabel})

    if table_rows:
        blocks.append({"type": "table", "columns": ["Metric", "Value"], "rows": table_rows, "raw_data": raw_rows})

    if record_count > 0:
        blocks.append({"type": "text", "content": f"Transactions: {format_count(record_count)}"})

    if source_report:
        blocks.append({"type": "sources", "items": [source_report]})

    # Glossary for jewelry terms used in the multi-metric table
    used_terms = []
    for r in results:
        ul = r.get("unit_label", "")
        if ul:
            used_terms.append(ul)
            used_terms.append(normalize_unit_label(ul))
    glossary = _build_glossary_block(used_terms)
    if glossary:
        blocks.append(glossary)

    if assumptions:
        combined = "; ".join(assumptions)
        blocks.append({"type": "assumption", "content": combined})

    return blocks


def build_ranking_blocks(
    data: Dict[str, Any],
    spec: Any,
    question: str,
    source_report: str,
    assumptions: Optional[List[str]] = None,
    filters: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """Build table blocks for a top-N ranking answer."""
    blocks: List[Dict[str, Any]] = []

    # Period chip — show the date range the ranking belongs to
    period_block = _build_period_block(filters)
    if period_block:
        blocks.append(period_block)

    limit = getattr(spec, "limit", None) or 1
    dim_header = _dim_header(getattr(spec, "dimension", ""), getattr(spec, "report_key", "sales_report"))
    metric_hdr = _metric_header(getattr(spec, "unit", ""), getattr(spec, "unit_label", ""))
    unit = getattr(spec, "unit", "")
    unit_label = getattr(spec, "unit_label", "")

    rows_data: List[Dict[str, Any]] = []
    if isinstance(data, dict) and "rows" in data and "raw_rd" in data:
        all_rows = data.get("rows", [])
        rows_data = all_rows[:limit]
    elif isinstance(data, dict) and data.get("mode") == "list":
        sample = data.get("sample", [])
        metric_col = getattr(spec, "metric_key", "")
        dim_col = getattr(spec, "dimension", "")
        valid = []
        for r in sample:
            v = _to_float(r.get(metric_col))
            d = r.get(dim_col)
            if v is not None and d is not None:
                valid.append({"MetricValue": v, "DimensionValue": str(d)})
        reverse = getattr(spec, "sort", "") == "desc"
        valid.sort(key=lambda x: x["MetricValue"], reverse=reverse)
        rows_data = valid[:limit]

    if not rows_data:
        blocks.append({"type": "text", "content": "No matching records found. Try a different date range or adjust your filters."})
        if source_report:
            blocks.append({"type": "sources", "items": [source_report]})
        return blocks

    dimension_name = getattr(spec, "dimension", "")
    # Time dimensions read naturally in chronological order (Aug then Sep),
    # not revenue rank. ISO 'YYYY-MM'/'YYYY-MM-DD' strings sort lexically.
    if dimension_name.lower() in ("date", "entrydate", "jobdate", "month", "year", "week"):
        rows_data.sort(key=lambda r: str(r.get("DimensionValue", "")))
    table_rows: List[List[str]] = []
    raw_rows: List[Dict[str, Any]] = []
    total = 0.0
    for i, row in enumerate(rows_data, 1):
        dim_val_raw = str(row.get("DimensionValue", "") or "")
        dim_val = _format_dimension_value(dim_val_raw, dimension_name)
        metric_val = _to_float(row.get("MetricValue")) or 0.0
        total += metric_val
        table_rows.append([str(i), dim_val, _format_ranking_value(metric_val, unit, unit_label)])
        raw_rows.append({"rank": i, "dimension_value": dim_val_raw, "raw_value": metric_val, "unit": unit, "unit_label": unit_label})

    blocks.append({"type": "heading", "content": f"{metric_hdr} by {dim_header}"})

    _is_time = dimension_name.lower() in ("date", "entrydate", "jobdate", "month", "year", "week")
    _rank_col = "#" if _is_time else "Rank"
    blocks.append({"type": "table", "columns": [_rank_col, dim_header, metric_hdr], "rows": table_rows, "raw_data": raw_rows})
    _total_label = (f"Total {metric_hdr.lower()} across {len(rows_data)} periods" if _is_time
                    else f"Total {metric_hdr.lower()} for top {len(rows_data)}")
    blocks.append({
        "type": "metric",
        "content": f"{_total_label}: {_format_ranking_value(total, unit, unit_label)}",
        "raw_value": total, "unit": unit, "unit_label": unit_label, "label": f"Total {metric_hdr.lower()}",
    })

    # Add charts for ranking comparisons (2+ numeric data points)
    if len(rows_data) >= 2:
        chart_data = [{"x": _format_dimension_value(str(row.get("DimensionValue", "") or ""), dimension_name),
                       "y": _to_float(row.get("MetricValue")) or 0.0}
                      for row in rows_data]

        # Determine chart type based on dimension and row count
        dim_lower = (getattr(spec, "dimension", "") or "").lower()
        _TIME_DIMENSIONS = {"date", "entrydate", "month", "year", "week"}
        _CATEGORY_DIMENSIONS = {
            "metal_type_name", "categoryname", "mastermanagement_categoryname",
            "mastermanagement_brandname", "mastermanagement_goldtypename",
            "mastermanagement_producttypename", "goldtypename",
            "iswithhallmark", "jobtype", "orderform",
        }

        if dim_lower in _TIME_DIMENSIONS:
            chart_type = "line"
        elif dim_lower in _CATEGORY_DIMENSIONS and len(rows_data) <= 8:
            chart_type = "pie"
        else:
            chart_type = "bar"

        blocks.append({
            "type": "chart",
            "chart_type": chart_type,
            "x_key": dim_header,
            "series": [{"name": metric_hdr, "data": chart_data}],
        })

    if source_report:
        blocks.append({"type": "sources", "items": [source_report]})

    # Glossary for jewelry terms used in this ranking (weight units, etc.)
    used_terms = []
    if unit in ("weight", "rate") and unit_label:
        used_terms.append(unit_label)
        used_terms.append(normalize_unit_label(unit_label))
    if unit == "count" and unit_label:
        used_terms.append(unit_label)
        used_terms.append(normalize_unit_label(unit_label))
    glossary = _build_glossary_block(used_terms)
    if glossary:
        blocks.append(glossary)

    if assumptions:
        combined = "; ".join(assumptions)
        blocks.append({"type": "assumption", "content": combined})

    return blocks


def why_explanation(
    data: Dict[str, Any],
    spec: Any,
    entity_name: str,
    record_count: int,
) -> Tuple[List[str], str]:
    """Compute governed 'why did X perform well' facts from breakdown rows.

    Every sentence is derived from the executed result set — nothing is
    invented. Returns (fact_lines, insight_sentence).
    """
    unit = getattr(spec, "unit", "")
    unit_label = getattr(spec, "unit_label", "")
    metric_hdr = _metric_header(unit, unit_label)
    dim_header = _dim_header(getattr(spec, "dimension", ""), getattr(spec, "report_key", "sales_report"))
    entity = entity_name or "This item"

    rows: List[Tuple[str, float]] = []
    if isinstance(data, dict) and "rows" in data:
        for r in data.get("rows", []):
            v = _to_float(r.get("MetricValue"))
            d = str(r.get("DimensionValue", "") or "")
            if v is not None and d:
                rows.append((d, v))
    elif isinstance(data, dict) and data.get("mode") == "list":
        metric_col = getattr(spec, "metric_key", "")
        dim_col = getattr(spec, "dimension", "")
        for r in data.get("sample", []):
            v = _to_float(r.get(metric_col))
            d = r.get(dim_col)
            if v is not None and d is not None:
                rows.append((str(d), v))

    if not rows:
        return [], ""

    def _fmt(v: float) -> str:
        return _display_value(v, unit, "INR", unit_label)

    total = sum(v for _, v in rows)
    n = len(rows)
    top_name, top_val = rows[0]
    top_share = (top_val / total * 100.0) if total else 0.0

    facts: List[str] = []
    txn = f", {format_count(record_count)} transaction{'s' if record_count != 1 else ''}" if record_count else ""
    facts.append(f"{metric_hdr}: {_fmt(total)} across {n} {dim_header.lower()}{'s' if n != 1 else ''}{txn}")
    if n == 1:
        facts.append(f"Single-driver concentration: {top_name} contributes 100% ({_fmt(top_val)})")
    else:
        facts.append(f"Top contributor: {top_name} — {_fmt(top_val)} ({top_share:.0f}% of total)")
        if n >= 3:
            top3 = sum(v for _, v in rows[:3])
            facts.append(f"Top 3 {dim_header.lower()}s together contribute {top3 / total * 100.0:.0f}%")

    if n == 1 or top_share >= 70:
        insight = (f"{entity}'s performance is concentrated — driven mainly by {top_name}, "
                   f"not broad-based {dim_header.lower()} demand.")
    else:
        insight = (f"{entity} shows broad-based demand — spread across {n} {dim_header.lower()}s "
                   f"with no single one dominating.")
    return facts, insight


def build_why_blocks(
    data: Dict[str, Any],
    spec: Any,
    question: str,
    source_report: str,
    assumptions: Optional[List[str]] = None,
    filters: Optional[Dict[str, Any]] = None,
    entity_name: str = "",
    record_count: int = 0,
) -> List[Dict[str, Any]]:
    """Ranking blocks plus a governed 'why' explanation layer.

    Produces the layout the UI shows for 'why did X perform well':
    heading -> fact bullets -> insight -> breakdown table/chart -> sources.
    """
    blocks = build_ranking_blocks(data, spec, question, source_report, assumptions, filters=filters)

    facts, insight = why_explanation(data, spec, entity_name, record_count)
    if not facts:
        return blocks

    # Retitle the heading to the entity being explained
    title_entity = entity_name or question.strip()
    for i, b in enumerate(blocks):
        if b.get("type") == "heading":
            blocks[i] = {"type": "heading", "content": f"Why {title_entity} performed well"}
            insert_at = i + 1
            break
    else:
        insert_at = 0
        blocks.insert(0, {"type": "heading", "content": f"Why {title_entity} performed well"})

    explain_blocks: List[Dict[str, Any]] = [
        {"type": "list", "title": "Key drivers", "items": facts},
        {"type": "text", "title": "AI Insight", "content": f"AI Insight: {insight}"},
    ]
    blocks[insert_at:insert_at] = explain_blocks
    return blocks


def build_error_blocks(message: str) -> List[Dict[str, Any]]:
    """Build a single error block."""
    return [{"type": "error", "content": message}]


def _chip_options(items: List[str]) -> List[Dict[str, Any]]:
    """Turn suggestion strings into structured options carrying send_message
    actions. The click contract is explicit ({type, payload}) rather than
    'send the label text' — labels stay free to change without breaking
    behavior."""
    return [
        {
            "label": item,
            "action": {
                "type": "send_message",
                "handler": "server",
                "payload": {"message": item},
                "display": item,
            },
        }
        for item in items
    ]


# Entity kinds offered by the disambiguation widget. `inject` is the word the
# server prefixes to the entity value when reconstructing the question — it
# matches the filter_key_map aliases so the deterministic field extractor
# lands the filter without depending on the LLM. Ordered by jewellery-ERP
# likelihood: codes like TR62/JS4 are design or SKU numbers far more often
# than they are brands or branches.
ENTITY_OPTION_INJECT = {
    "design": "design",
    "sku": "sku",
    "customer": "customer",
    "invoice": "invoice",
    "salesperson": "sales rep",
    "brand": "brand",
    "category": "category",
    "branch": "branch",
}

# Display labels for option ids that .title() would mangle.
ENTITY_OPTION_LABELS = {
    "sku": "SKU",
    "salesperson": "Salesperson",
}


def build_date_range_input_block(content: str) -> Dict[str, Any]:
    return {
        "type": "date_range_input",
        "title": "Select date range",
        "content": content,
        "start_field": "start_date",
        "end_field": "end_date",
        "presets": [
            {"label": "Today", "value": "today"},
            {"label": "This month", "value": "this_month"},
            {"label": "Last month", "value": "last_month"},
            {"label": "This year", "value": "this_year"},
        ],
        "submit_label": "Apply date range",
        "submit_message_template": "Use date range {start_date} to {end_date}",
        # Structured path — /chat/action applies these to the pending question.
        "submit_action": {
            "type": "set_date_range",
            "handler": "server",
            "payload": {"start_field": "start_date", "end_field": "end_date"},
            "display": "Use date range {start_date} to {end_date}",
        },
        "blocking": True,
    }


def build_entity_choice_block(value: str, content: str) -> Dict[str, Any]:
    options = []
    for option_id, inject in ENTITY_OPTION_INJECT.items():
        label = ENTITY_OPTION_LABELS.get(option_id, option_id.title())
        options.append({
            "label": label,
            "value": option_id,
            # Legacy fallback for frontends that still send option.message.
            "message": f"{label}: {value}",
            "action": {
                "type": "select_option",
                "handler": "server",
                "payload": {"option_id": option_id, "value": value},
                "display": f"{label}: {value}",
            },
        })
    return {
        "type": "choice_input",
        "title": "Confirm field",
        "content": content,
        "field": "entity_type",
        "value": value,
        "options": options,
        "allow_custom": False,
        "blocking": True,
    }


def build_clarify_blocks(report_key: str) -> List[Dict[str, Any]]:
    """Build a clarification prompt with report-aware suggestions.

    Generates short, clickable suggestions from the report's metric_catalog
    and intents so the user knows what they can ask about.
    """
    from app.services.column_registry import _REGISTRY as _COL_REGISTRY

    report_cfg = _COL_REGISTRY.get(report_key, {})
    report_title = report_key.replace("_", " ").title()

    # ── Build suggestions from metric_catalog (top 4 by relevance) ──
    catalog = report_cfg.get("metric_catalog", {})
    intents = report_cfg.get("intents", {})

    suggestions: List[str] = []
    seen: set = set()

    # Priority 1: intent labels (most natural phrasing)
    for name, rule in intents.items():
        label = rule.get("label", "")
        if label and label not in seen and len(suggestions) < 4:
            # Use the intent label as a suggestion
            suggestions.append(label)
            seen.add(label)

    # Priority 2: metric catalog labels
    for metric_name, meta in catalog.items():
        label = meta.get("label", "")
        if label and label not in seen and len(suggestions) < 5:
            suggestions.append(f"Total {label.lower()}")
            seen.add(label)

    # Fallback: generic suggestions if nothing found
    if not suggestions:
        suggestions = [
            f"Total amount for {report_title.lower()}",
            f"Top 5 customers by {report_title.lower()}",
            f"How many records in {report_title.lower()}",
        ]

    blocks: List[Dict[str, Any]] = [
        {
            "type": "clarify",
            "content": f"I'm not sure what you're asking about. Could you be more specific?",
            "suggestions": suggestions,
            "options": _chip_options(suggestions),
            # Generic clarify — the user should still be able to type freely.
            "blocking": False,
        },
        {"type": "text", "content": f"Sources: {report_title}"},
    ]
    return blocks


def build_no_data_blocks(label: str, filters: Dict[str, Any], source_report: str) -> List[Dict[str, Any]]:
    """Build blocks for a short, user-friendly no-data response."""
    blocks: List[Dict[str, Any]] = []

    # Period chip — show the date range that returned no data
    period_block = _build_period_block(filters)
    if period_block:
        blocks.append(period_block)

    blocks.append({
        "type": "text",
        "content": f"No {label.lower()} found for the selected filters.",
    })
    if filters.get("start_date") or filters.get("end_date"):
        blocks.append(build_date_range_input_block("Select another date range and try again."))

    suggestions = [
        f"Overall total {label.lower()} without filters",
        f"Top 5 by {label.lower()}",
        "What were the total sales this month?",
        "Top 5 customers by revenue",
        "Total diamond weight and pieces",
        "Gold amount by branch",
    ]

    blocks.append({"type": "suggestions", "items": suggestions,
                   "options": _chip_options(suggestions)})

    if source_report:
        blocks.append({"type": "sources", "items": [source_report]})
    return blocks


# ── LLM block parsing ──────────────────────────────────────────────────────────

_BLOCK_SYSTEM_PROMPT = """You are OptigoBot, the internal ERP data assistant for OptigoApps, a jewellery ERP platform.

You must respond with ONLY a single valid JSON object — no markdown fences, no prose outside the JSON, no trailing commentary.

Output schema:
{
  "blocks": [
    { "type": "text", "content": "string, plain sentence(s), no markdown syntax" },
    { "type": "heading", "content": "string" },
    { "type": "table", "columns": ["string", ...], "rows": [["string", ...], ...] },
    { "type": "list", "style": "bullet" | "number", "items": ["string", ...] },
    { "type": "chart", "chart_type": "line" | "bar" | "pie", "x_key": "string", "series": [{ "name": "string", "data": [{ "x": "string", "y": number }] }] },
    { "type": "metric_card", "label": "string", "value": "string (pre-formatted)", "raw_value": number, "unit": "currency|weight|count|rate|text", "currency": "INR", "unit_label": "gms|ctw|pcs|%", "subtext": "string" },
    { "type": "breakdown", "title": "string", "columns": ["Component","Amount","Weight","Pieces"], "rows": [["string","string","string","string"], ...] },
    { "type": "period", "label": "Date Range", "value": "string (e.g. 2026-09-01 to 2026-09-15)", "filters": {} },
    { "type": "glossary", "title": "Glossary", "terms": { "ctw": "Carat Total Weight - diamond weight unit", "gms": "Grams - metal weight unit" } },
    { "type": "assumption", "content": "string — state any default filter/date range/company you assumed" },
    { "type": "error", "content": "string — user-facing explanation, never a stack trace or SQL error" },
    { "type": "clarify", "content": "string — question asking the user to be more specific", "suggestions": ["string", ...] }
  ]
}

Jewellery-industry conventions (use these terms in labels and content):
- Currency: ₹ with Indian grouping (lakh/crore). For large values show both: "₹2.47 crore (₹24,75,56,602)".
- Weight units: gms (grams) for metal, ctw (carat total weight) for diamonds/colourstones, pcs for pieces.
- Common terms: Gross Wt, Net Wt, Pure Wt, Making Charges, Metal Value, Diamond Value, Tunch (gold purity %), Wastage %.
- Component breakdown order: Metal, Diamond, Colourstone, Making Charges, Other.

Rules:
1. Always return at least one block.
2. If a required filter (date range, company, module) was not given, DO NOT ask the user first — pick the most sensible default, answer immediately using it, and add exactly one "assumption" block. Also add a "period" block showing the date range used.
3. Never fabricate numbers. If the report/API returned no data, use a "text" block saying so plainly.
4. Use "metric_card" for the primary single-value answer (total sales, total gold weight, etc.). Use "breakdown" for metal/diamond/colourstone component splits. Use "table" for other tabular data. Use "chart" only when there are 2+ numeric data points showing a trend or comparison. Use "list" for steps or short enumerations. Use "text" for everything else.
5. Add a "glossary" block when the answer uses jewellery abbreviations (ctw, gms, pcs, tunch, wastage) so the user can understand them.
6. Never include SQL, stored procedure names, internal error messages, or API payloads in any block.
7. Keep each "text" block to 1-3 sentences. Split long explanations into multiple text blocks.
8. Numbers must be formatted as returned by the source data.
9. Respond in the same language the user asked in.

You will be given the user's question plus any report data already fetched for them (as JSON). Only use the data given to you — never invent figures that aren't present in it."""


def parse_llm_blocks(raw_text: str) -> List[Dict[str, Any]]:
    """Parse and validate LLM JSON block output against Pydantic Block models.

    Falls back to a single error block on invalid JSON so the frontend
    NEVER receives a broken payload.
    """
    from app.models import (
        TextBlock, HeadingBlock, TableBlock, ListBlock,
        ChartBlock, AssumptionBlock, ErrorBlock,
        MetricCardBlock, BreakdownBlock, PeriodBlock, GlossaryBlock,
        ClarifyBlock, DateRangeInputBlock, ChoiceInputBlock,
    )
    _BLOCK_MODELS = {
        "text": TextBlock,
        "heading": HeadingBlock,
        "table": TableBlock,
        "list": ListBlock,
        "chart": ChartBlock,
        "assumption": AssumptionBlock,
        "error": ErrorBlock,
        "metric_card": MetricCardBlock,
        "breakdown": BreakdownBlock,
        "period": PeriodBlock,
        "glossary": GlossaryBlock,
        "clarify": ClarifyBlock,
        "date_range_input": DateRangeInputBlock,
        "choice_input": ChoiceInputBlock,
    }
    try:
        data = json.loads(raw_text)
        blocks = data.get("blocks")
        if not isinstance(blocks, list) or len(blocks) == 0:
            raise ValueError("Missing or empty 'blocks' array")
        validated = []
        for b in blocks:
            if not isinstance(b, dict) or "type" not in b:
                continue
            btype = b.get("type")
            model = _BLOCK_MODELS.get(btype)
            if model is None:
                continue
            try:
                model(**b)
            except Exception as ve:
                logger.warning("Block validation failed for type=%s: %s", btype, ve)
                continue
            validated.append(b)
        if not validated:
            raise ValueError("No valid blocks found")
        return validated
    except Exception as exc:
        logger.warning("Failed to parse LLM blocks: %s | raw=%s", exc, raw_text[:200])
        return [{"type": "error", "content": "Something went wrong generating this response. Please try again."}]


def get_block_system_prompt() -> str:
    return _BLOCK_SYSTEM_PROMPT


# ── Utility ────────────────────────────────────────────────────────────────────

def blocks_to_text(blocks: List[Dict[str, Any]]) -> str:
    """Flatten blocks to plain text for backward-compatible `answer` field."""
    parts = []
    for b in blocks:
        btype = b.get("type", "")
        if btype == "text":
            parts.append(b.get("content", ""))
        elif btype == "heading":
            parts.append(b.get("content", ""))
        elif btype in ("table", "breakdown"):
            cols = b.get("columns", [])
            rows = b.get("rows", [])
            if cols:
                parts.append("| " + " | ".join(str(c) for c in cols) + " |")
                parts.append("|" + "|".join(" --- " for _ in cols) + "|")
            for row in rows:
                parts.append("| " + " | ".join(str(c) for c in row) + " |")
        elif btype == "list":
            for item in b.get("items", []):
                parts.append(f"• {item}")
        elif btype == "suggestions":
            for item in b.get("items", []):
                parts.append(f"• {item}")
        elif btype == "assumption":
            parts.append(f"Assumed: {b.get('content', '')}")
        elif btype == "error":
            parts.append(b.get("content", ""))
        elif btype == "chart":
            parts.append("[chart]")
        elif btype == "metric":
            parts.append(b.get("content", ""))
        elif btype == "metric_card":
            label = b.get("label", "")
            value = b.get("value", "")
            subtext = b.get("subtext", "")
            line = f"{label}: {value}"
            if subtext:
                line += f"\n{subtext}"
            parts.append(line)
        elif btype == "period":
            label = b.get("label", "")
            value = b.get("value", "")
            if label and value:
                parts.append(f"{label}: {value}")
            elif value:
                parts.append(value)
        elif btype in ("clarify", "date_range_input", "choice_input"):
            parts.append(b.get("content", ""))
        elif btype == "glossary":
            terms = b.get("terms", {})
            if terms:
                parts.append("Glossary: " + "; ".join(f"{k} = {v}" for k, v in terms.items()))
    return "\n".join(p for p in parts if p)
