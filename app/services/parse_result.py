"""ParseResult — structured query intent returned by the LLM, plus the helpers
that convert it into IntentSpec / validated filters / SP parameters.

Also holds ``validate_ai_where`` (the ai_where safety guard) since it is only
consumed by ``ParseResult.generate_where_clause``.
"""
import logging
import re
from typing import Any, Dict, List, Optional

from app.services.intent import IntentSpec
from app.services.column_registry import _REGISTRY as _COLUMN_REGISTRY
from app.services.column_registry import (
    get_filter_invalid_values,
    get_invalid_value_columns,
)
from app.services.catalog_builder import _get_cached_valid_columns
from app.services.metric_validator import get_metric_unit, get_metric_unit_label

logger = logging.getLogger(__name__)


def expand_computed_where_refs(ai_where: str, report_key: str = "sales_report") -> str:
    """Rewrite ``DI.<computed-column>`` references inside a WHERE clause.

    The LLM sometimes emits filters on computed output columns (e.g.
    ``DI.SoldPending = 'pending'``). Those names don't exist as physical
    columns, so we substitute the column's configured ``dimension_expr``
    (falling back to ``metric_expr``) wrapped in parentheses. Columns marked
    ``not_in_where``/``computed_only`` are never expanded — they stay as refs
    and get rejected by the caller's validation pass.
    """
    if not ai_where:
        return ai_where
    columns = _COLUMN_REGISTRY.get(report_key, {}).get("columns", {})
    if not columns:
        return ai_where
    from app.services.column_registry import get_computed_only_names, get_canonical_values
    computed_only = {n.lower() for n in get_computed_only_names(report_key)}
    name_map = {name.lower(): meta for name, meta in columns.items()}

    # Normalize LIKE literals whose exact value is a canonical-vocabulary key:
    # 'polishing' -> 'Polish' so LIKE '%Polish%' matches 'Pre Polish-Issue'.
    # Applies to both `DI.<field> LIKE` and inlined-expression forms.
    canon_all: dict = {}
    report_cfg = _COLUMN_REGISTRY.get(report_key, {})
    for field_map in (report_cfg.get("canonical_values") or {}).values():
        for k, v in field_map.items():
            canon_all.setdefault(str(k).strip().lower(), str(v))

    def _canon_like(match: "re.Match") -> str:
        lit = match.group(2)
        mapped = canon_all.get(lit.strip().lower())
        return f"{match.group(1)}'%{mapped if mapped else lit}%'"

    if canon_all:
        ai_where = re.sub(
            r"(LIKE\s+)'%([^'%]*)%'",
            _canon_like, ai_where, flags=re.IGNORECASE,
        )

    for _ in range(5):  # bounded: expressions may nest other computed refs
        refs = re.findall(r'DI\.(\w+)', ai_where, re.IGNORECASE)
        rewritten = False
        for ref in refs:
            key = ref.lower()
            if key in computed_only:
                continue
            meta = name_map.get(key)
            if meta and meta.get("computed"):
                expr = meta.get("dimension_expr") or meta.get("metric_expr") or ""
                if expr:
                    ai_where = re.sub(
                        r'\bDI\.' + re.escape(ref) + r'\b',
                        f"({expr})", ai_where, flags=re.IGNORECASE,
                    )
                    rewritten = True
        if not rewritten:
            break
    return ai_where


def validate_ai_where(ai_where: str, report_key: str = "sales_report") -> str:
    """Validate the LLM-generated WHERE clause.

    Scalable guards:
    1. Reject any clause containing SELECT (subqueries) — we can't validate
       subquery column references against unknown table schemas, and the SP
       doesn't support them reliably.
    2. Extract all DI.<column> references and check each against the column
       registry. If any reference is a computed metric (not a physical column),
       the entire clause is nullified to prevent SQL errors.

    No matter what the LLM generates, invalid column references are caught
    before reaching the SP.
    """
    if not ai_where or not ai_where.strip():
        return ""

    ai_where = ai_where.strip()

    # Guard 1: Reject subqueries entirely
    if re.search(r'\bSELECT\b', ai_where, re.IGNORECASE):
        logger.warning(
            "ai_where validation: subquery (SELECT) detected — nullifying ai_where: %s",
            ai_where[:100]
        )
        return ""

    # Guard 2a: expand DI.<computed-column> refs into their configured expressions
    ai_where = expand_computed_where_refs(ai_where, report_key)

    # Guard 2b: drop AND-ed clauses whose literal is a declared-invalid filter
    # value for the DI column they reference (e.g. the LLM leaks a metric word
    # like 'diamond' into a CustomerName LIKE clause).
    invalid_cols = get_invalid_value_columns(report_key)
    if invalid_cols:
        kept = []
        dropped_any = False
        for clause in re.split(r"\s+AND\s+", ai_where, flags=re.IGNORECASE):
            clause = clause.strip()
            if not clause:
                continue
            refs = {r.lower() for r in re.findall(r"DI\.(\w+)", clause, re.IGNORECASE)}
            literals = [
                re.sub(r"%", "", lit).replace("''", "'").strip().lower()
                for lit in re.findall(r"'((?:''|[^'])*)'", clause)
            ]
            drop = False
            for ref in refs:
                invalid = invalid_cols.get(ref)
                if invalid and any(lit in invalid for lit in literals):
                    drop = True
                    break
            if drop:
                dropped_any = True
                logger.warning(
                    "ai_where validation: dropping clause with invalid value "
                    "for DI column: %s", clause[:100]
                )
            else:
                kept.append(clause)
        if dropped_any:
            ai_where = " AND ".join(kept)
            if not ai_where:
                return ""

    # Guard 2c: Validate all DI.<column> references against the column registry
    valid_cols = _get_cached_valid_columns(report_key)
    refs = re.findall(r'DI\.(\w+)', ai_where, re.IGNORECASE)

    for ref in refs:
        if ref.lower() not in valid_cols:
            logger.warning(
                "ai_where validation: DI.%s is not a valid base-table column "
                "(computed or unknown) — nullifying ai_where: %s",
                ref, ai_where[:100]
            )
            return ""

    return ai_where


class ParseResult:
    """Structured query intent from the LLM."""
    def __init__(self, data: Dict[str, Any]):
        self.report_key: str = data.get("report_key", "sales_report")
        self.metric: str = data.get("metric", "Amount")
        self.dimension: Optional[str] = data.get("dimension")
        self.aggregation: str = data.get("aggregation", "sum")
        self.limit: int = int(data.get("limit", 1) or 1)
        self.filters: Dict[str, str] = data.get("filters", {}) or {}
        self.date_filter: Optional[Dict[str, str]] = data.get("date_filter")
        self.sort: str = data.get("sort", "desc")
        self.ai_where: Optional[str] = data.get("ai_where")
        self.clarify: Optional[str] = data.get("clarify")
        self.extra_metrics: List[str] = data.get("extra_metrics", []) or []
        self.intent: Optional[str] = data.get("intent")
        self.confidence: float = max(0.0, min(1.0, float(data.get("confidence", 0.8) or 0.8)))
        self.alternatives: List[Dict[str, Any]] = data.get("alternatives", []) or []
        self.raw: Dict[str, Any] = data

    def to_intent_spec(self) -> IntentSpec:
        """Convert to IntentSpec for the existing pipeline."""
        # Dynamically read filter_only columns from registry (scalable — no hardcoding)
        report_cfg = _COLUMN_REGISTRY.get(self.report_key, {})
        columns = report_cfg.get("columns", {})
        _FILTER_ONLY = {
            name for name, meta in columns.items()
            if meta.get("filter_only")
        }
        metric = self.metric
        if metric in _FILTER_ONLY:
            metric = "Amount"
        spec = IntentSpec(report_key=self.report_key)
        spec.intent = self.intent or f"semantic_{metric}"
        spec.metric_key = metric
        spec.aggregation = self.aggregation
        spec.dimension = self.dimension or ""
        # Guard: filter-only columns cannot be used as dimensions (GROUP BY)
        if spec.dimension in _FILTER_ONLY:
            spec.dimension = ""
        spec.limit = self.limit
        spec.sort = self.sort

        # Determine unit using centralized classification
        spec.unit = get_metric_unit(metric, self.report_key)
        spec.unit_label = get_metric_unit_label(metric, self.report_key)
        return spec

    def to_validated_filters(self) -> Dict[str, Any]:
        """Convert filters + date_filter to the format expected by _build_p."""
        result: Dict[str, Any] = {}
        for fname, fval in self.filters.items():
            result[fname] = fval
        if self.date_filter:
            preset = self.date_filter.get("preset", "")
            from datetime import date, timedelta
            today = date.today()
            if preset == "today":
                result["start_date"] = today.isoformat()
                result["end_date"] = today.isoformat()
            elif preset == "yesterday":
                y = today - timedelta(days=1)
                result["start_date"] = y.isoformat()
                result["end_date"] = y.isoformat()
            elif preset == "this_month":
                result["start_date"] = today.replace(day=1).isoformat()
                result["end_date"] = today.isoformat()
            elif preset == "last_month":
                first = (today.replace(day=1) - timedelta(days=1)).replace(day=1)
                last = today.replace(day=1) - timedelta(days=1)
                result["start_date"] = first.isoformat()
                result["end_date"] = last.isoformat()
            elif preset == "this_year":
                result["start_date"] = today.replace(month=1, day=1).isoformat()
                result["end_date"] = today.isoformat()
            elif preset == "this_week":
                monday = today - timedelta(days=today.weekday())
                result["start_date"] = monday.isoformat()
                result["end_date"] = today.isoformat()
            elif preset == "last_week":
                monday = today - timedelta(days=today.weekday() + 7)
                sunday = monday + timedelta(days=6)
                result["start_date"] = monday.isoformat()
                result["end_date"] = sunday.isoformat()
            elif preset == "last_year":
                result["start_date"] = f"{today.year - 1}-01-01"
                result["end_date"] = f"{today.year - 1}-12-31"
            else:
                # Explicit date range: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD"}
                start = self.date_filter.get("start", "")
                end = self.date_filter.get("end", "")
                if start and end:
                    # Validate start <= end to prevent empty results from reversed ranges
                    if start <= end:
                        result["start_date"] = start
                        result["end_date"] = end
                    else:
                        # Swap reversed range
                        result["start_date"] = end
                        result["end_date"] = start
        return result

    def generate_where_clause(self) -> str:
        """Generate SQL WHERE clause for name-based filters and AI-generated conditions.

        Code-based filters (metal_type, category, etc.) are handled by the SP's
        FilterHeader/FilterValue mechanism. Name-based filters (sales_rep, customer)
        need LIKE matching against concatenated name columns.

        Name-filter SQL expressions now come from the per-report `name_filter_map`
        in report_columns/*.json, so any report can define its own customer/name
        filter logic without editing Python.

        The ai_where field from the LLM is also included here. It will be
        validated by sql_guard.validate_where_clause() before being sent to the SP.
        """
        clauses = []
        report_cfg = _COLUMN_REGISTRY.get(self.report_key, {})
        name_filter_map = report_cfg.get("name_filter_map", {})
        # Expand aliases from filter_key_map that point to name_filter_map keys.
        filter_key_map = report_cfg.get("filter_key_map", {})
        _NAME_FILTERS: Dict[str, str] = dict(name_filter_map)
        for alias, target in filter_key_map.items():
            if target in name_filter_map and alias.lower() not in _NAME_FILTERS:
                _NAME_FILTERS[alias.lower()] = name_filter_map[target]

        for fname, fval in self.filters.items():
            col_expr = _NAME_FILTERS.get(fname.lower())
            if col_expr:
                fkey = fname.lower()
                # Resolve invalid values against the alias AND its name_filter_map target
                invalid = get_filter_invalid_values(self.report_key, fkey)
                for alias, target in filter_key_map.items():
                    if name_filter_map.get(target) == col_expr:
                        invalid |= get_filter_invalid_values(self.report_key, target)
                if str(fval).lower().strip() in invalid:
                    logger.warning(
                        "generate_where_clause: dropping %s filter — '%s' is not a valid value",
                        fname, fval
                    )
                    continue
                safe_val = str(fval).replace("'", "''")
                clauses.append(f"{col_expr} LIKE '%{safe_val}%'")

        # Add AI-generated WHERE clause (validated against column registry)
        if self.ai_where and self.ai_where.strip():
            validated = validate_ai_where(self.ai_where.strip(), self.report_key)
            if validated:
                clauses.append(validated)

        if clauses:
            return " AND ".join(clauses)
        return ""

    def to_query_plan(self, question: str = ""):
        from app.services.query_plan import QueryPlan
        plan = QueryPlan.from_parse_result(self)
        plan.classify_complexity(question)
        return plan

    def __repr__(self) -> str:
        return (f"ParseResult(report={self.report_key}, metric={self.metric}, "
                f"dim={self.dimension}, agg={self.aggregation}, limit={self.limit}, "
                f"filters={self.filters}, date={self.date_filter})")


def resolve_to_sp_params(parsed: ParseResult, report_cols: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve ParseResult to SP parameters using report_columns.json.

    Returns dict with: MetricKey, MetricExpr, Dimension, DimensionExpr,
    FilterHeader, FilterValue, Aggregation, Limit, FilterStartDate, FilterEndDate
    """
    report_cfg = report_cols.get(parsed.report_key, {})
    columns = report_cfg.get("columns", {})
    special_metrics = report_cfg.get("special_metrics", {})

    # Resolve metric — look up by key name, then by SQL name as fallback
    metric_key = parsed.metric
    metric_expr = ""
    col = columns.get(metric_key)
    if col is None:
        # Fallback: match by SQL column name
        for cname, cmeta in columns.items():
            if cmeta.get("sql", "").lower() == metric_key.lower():
                col = cmeta
                metric_key = cname
                break
    if col is not None and not col.get("filter_only"):
        if col.get("computed") and col.get("metric_expr"):
            metric_expr = col["metric_expr"]
        elif col.get("computed") and col.get("dimension_expr"):
            # Computed dimension used as metric (e.g. CustomerFullName in single-record lookup)
            metric_expr = col["dimension_expr"]
        else:
            sql = col.get("sql", metric_key)
            if not sql:
                metric_key = "Amount"
                metric_expr = "CONVERT(DECIMAL(38,2),ISNULL(DI.design_TotalAmouont,0))"
            else:
                metric_expr = f"ISNULL(DI.{sql},0)"
    elif col is not None and col.get("filter_only"):
        metric_key = "Amount"
        metric_expr = "CONVERT(DECIMAL(38,2),ISNULL(DI.design_TotalAmouont,0))"
    elif metric_key in special_metrics:
        metric_expr = special_metrics[metric_key].get("sql", metric_key)

    # Resolve dimension — look up by key name, then by SQL name as fallback
    dimension = parsed.dimension or ""
    dimension_expr = ""
    if dimension:
        col = columns.get(dimension)
        if col is None:
            for cname, cmeta in columns.items():
                if cmeta.get("sql", "").lower() == dimension.lower():
                    col = cmeta
                    dimension = cname
                    break
        if col is not None and not col.get("filter_only"):
            if col.get("computed") and col.get("dimension_expr"):
                dimension_expr = col["dimension_expr"]
            else:
                sql = col.get("sql", dimension)
                if not sql:
                    dimension = ""
                    dimension_expr = ""
                else:
                    dimension_expr = f"ISNULL(DI.{sql},'')"
        elif col is not None and col.get("filter_only"):
            dimension = ""
            dimension_expr = ""

    # Resolve filters — map filter names to SP filter headers
    filter_headers = []
    filter_values = []

    # Build a per-report filter map from filter_key_map + column metadata.
    # filter_key_map targets can be:
    #   - a physical column name  -> use that column's sql
    #   - a name_filter key     -> use the SQL expression from name_filter_map
    #   - "_date"               -> handled separately as date filters
    # Physical column names and their aliases are also registered directly.
    _FILTER_MAP: Dict[str, str] = {}
    name_filter_map = report_cfg.get("name_filter_map", {})
    filter_key_map = report_cfg.get("filter_key_map", {})
    for alias, target in filter_key_map.items():
        if target == "_date":
            continue
        if target in name_filter_map:
            _FILTER_MAP[alias.lower()] = name_filter_map[target]
            continue
        target_col = columns.get(target, {})
        if isinstance(target_col, dict):
            sql = target_col.get("sql", target)
        else:
            sql = target
        _FILTER_MAP[alias.lower()] = sql

    # Also allow direct column names and aliases
    for cname, cmeta in columns.items():
        sql = cmeta.get("sql", cname)
        _FILTER_MAP.setdefault(cname.lower(), sql)
        f = cmeta.get("filter", {})
        if isinstance(f, dict):
            for calias in f.get("aliases", []):
                _FILTER_MAP.setdefault(calias.lower(), sql)

    for fname, fval in parsed.filters.items():
        # Check merged map first
        header = _FILTER_MAP.get(fname.lower())
        if header is None:
            # Try direct column lookup
            col = columns.get(fname)
            if col:
                header = col.get("sql", fname)
            else:
                # Try case-insensitive match
                for cname, cmeta in columns.items():
                    if cname.lower() == fname.lower():
                        header = cmeta.get("sql", cname)
                        break
                else:
                    header = fname
        filter_headers.append(header)
        filter_values.append(str(fval))

    # Resolve date filter
    date_start = ""
    date_end = ""
    if parsed.date_filter:
        preset = parsed.date_filter.get("preset", "")
        if preset == "today":
            date_start = "TODAY"
            date_end = "TODAY"
        elif preset == "yesterday":
            date_start = "YESTERDAY"
            date_end = "YESTERDAY"
        elif preset == "this_month":
            date_start = "THIS_MONTH_START"
            date_end = "THIS_MONTH_END"
        elif preset == "last_month":
            date_start = "LAST_MONTH_START"
            date_end = "LAST_MONTH_END"
        elif preset == "this_year":
            date_start = "THIS_YEAR_START"
            date_end = "THIS_YEAR_END"
        elif preset == "this_week":
            date_start = "THIS_WEEK_START"
            date_end = "THIS_WEEK_END"

    return {
        "MetricKey": metric_key,
        "MetricExpr": metric_expr,
        "Dimension": dimension,
        "DimensionExpr": dimension_expr,
        "Aggregation": parsed.aggregation,
        "Limit": parsed.limit,
        "FilterHeader": "#".join(filter_headers),
        "FilterValue": "#".join(filter_values),
        "FilterStartDate": date_start,
        "FilterEndDate": date_end,
    }
