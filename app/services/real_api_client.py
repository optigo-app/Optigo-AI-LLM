"""Real report API client for the Optigoapps report endpoint.

Calls http://<host>/api/report with:
  - Headers: Yearcode, sp, sv, Content-Type
  - Body: {"con": "...", "p": "...", "f": "DynamicReport ( data )"}

The `con` object carries session/auth info (appuserid, IPAddress, mode).
The `p` object carries report params (ReportId, MetricKey, Aggregation, filters).

The SP mode `GetLLMChatSummary` returns:
  rd:  [{ MetricValue, DimensionValue }]  — aggregated metric (or top N rows)
  rd1: [{ TotalCount }]                  — total row count for truncation detection
"""

import base64
import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx

from app.config import settings
from app.middleware.audit_log import log_sql_execution, log_report_api_request, AuditTimer, log_metric_validation

logger = logging.getLogger(__name__)

# ============================================================
# COLUMN REGISTRY — loaded from app/report_columns/ directory
# Maps report column names (what chatbot/intent sends) to
# actual SQL table column expressions.
# Python validates + translates here, SP just uses
# DI.<column> directly. This is the SQL injection guard:
# only registered values are sent to the SP.
# ============================================================

from app.services.column_registry import _REGISTRY as _COLUMN_REGISTRY

# source_query_file contents, loaded once per file (restart picks up changes)
_SOURCE_QUERY_CACHE: Dict[str, str] = {}


def _get_report_columns(report_key: str) -> Dict[str, Any]:
    """Get the column config for a specific report."""
    return _COLUMN_REGISTRY.get(report_key, {})


def _resolve_metric(report_key: str, value: str) -> str:
    """Translate a metric alias to the actual SQL column expression.

    Returns the registered SQL expression, or 'design_TotalAmouont' as safe default.
    """
    if not value:
        return "design_TotalAmouont"
    report_cols = _get_report_columns(report_key)
    columns = report_cols.get("columns", {})
    special = report_cols.get("special_metrics", {})

    # Check regular columns
    if value in columns:
        return columns[value]["sql"]
    # Check special metrics (like total_count)
    if value in special:
        return special[value]["sql"]
    # Not found
    logger.warning("Rejected MetricKey=%r not in registry for %s, using default", value, report_key)
    return "design_TotalAmouont"


def _xml_escape(text: str) -> str:
    """Escape XML special characters for the AIWhereClause.

    The real API SP parses the `p` JSON as XML, so <, >, & must be escaped.
    """
    if not text:
        return ""
    from xml.sax.saxutils import escape
    return escape(text)


def _resolve_dimension(report_key: str, value: str) -> str:
    """Translate a dimension alias to the actual SQL column name.

    Returns empty string if not registered (no dimension).
    """
    if not value:
        return ""
    report_cols = _get_report_columns(report_key)
    columns = report_cols.get("columns", {})

    if value in columns:
        return columns[value]["sql"]
    logger.warning("Rejected Dimension=%r not in registry for %s, using empty", value, report_key)
    return ""


def _resolve_filter_target(report_key: str, chatbot_key: str) -> Tuple[str, Dict[str, Any]]:
    """Translate a chatbot filter key to its report column name and metadata."""
    report_cols = _get_report_columns(report_key)
    columns = report_cols.get("columns", {})

    # Load filter_key_map from report_columns.json (config-driven, not hardcoded)
    filter_key_map: Dict[str, str] = report_cols.get("filter_key_map", {})
    name_filter_map: Dict[str, str] = report_cols.get("name_filter_map", {})

    # Map chatbot key to report column name (case-insensitive)
    report_col_name = filter_key_map.get(chatbot_key.lower(), chatbot_key)
    if report_col_name in ("_date", "_name_filter"):
        return report_col_name, {}
    if any(report_col_name.lower() == name.lower() for name in name_filter_map):
        return "_name_filter", {}

    if report_col_name in columns:
        return report_col_name, columns[report_col_name]

    for column_name, column_meta in columns.items():
        if column_name.lower() == report_col_name.lower():
            return column_name, column_meta

    logger.warning("Rejected filter column %r (key=%r) not in registry for %s",
                   report_col_name, chatbot_key, report_key)
    return "", {}


def _resolve_filter_column(report_key: str, chatbot_key: str) -> str:
    """Translate a chatbot filter key to the actual SQL column name.

    Returns empty string if not registered.
    """
    report_col_name, col_meta = _resolve_filter_target(report_key, chatbot_key)
    if report_col_name in ("_date", "_name_filter"):
        return report_col_name
    return col_meta.get("sql", "") if col_meta else ""


# Allowed aggregation functions
_ALLOWED_AGGREGATIONS: set = {"sum", "count", "count_distinct", "avg", "max", "min"}

# Allowed sort directions
_ALLOWED_SORT_DIRECTIONS: set = {"asc", "desc"}

# Regex for safe identifiers (letters, digits, underscore only)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _sanitize_identifier(value: str, allowed: set, default: str, field_name: str) -> str:
    """Validate an identifier against an allowlist.

    Returns the value if allowed, else the default.
    Logs a warning if an unexpected value is rejected.
    """
    if not value:
        return default
    if value in allowed:
        return value
    if _SAFE_IDENTIFIER.match(value) and value in allowed:
        return value
    logger.warning("Rejected %s=%r not in allowlist, using default %r", field_name, value, default)
    return default


def _sanitize_filter_value(value: str) -> str:
    """Sanitize a single filter value for use in SQL IN clause.

    Escapes single quotes by doubling them (TSQL standard escaping).
    Blocks semicolons, comment markers, and dangerous SQL keywords.
    The SP wraps values in quotes, so we escape dangerous chars here.
    Defense in depth: even if the SP has a quote-handling bug, dangerous
    keywords are stripped so they can't form a valid SQL statement.
    """
    if not value:
        return ""
    # Escape single quotes by doubling (TSQL standard: ' becomes '')
    v = value.replace("'", "''")
    # Block stacked queries and comment injection
    v = v.replace(";", "").replace("--", "").replace("/*", "").replace("*/", "")
    # Block null bytes and control characters
    v = re.sub(r"[\x00-\x1f\x7f]", "", v)
    # Block dangerous SQL keywords (defense in depth — even inside quoted strings)
    _DANGEROUS_FILTER_KEYWORDS = [
        "DROP", "DELETE", "INSERT", "UPDATE", "TRUNCATE", "MERGE",
        "ALTER", "CREATE", "GRANT", "REVOKE", "DENY",
        "EXEC", "EXECUTE", "SP_EXECUTESQL", "XP_CMDSHELL",
        "OPENROWSET", "OPENDATASOURCE", "OPENQUERY",
        "SHUTDOWN", "KILL", "WAITFOR",
        "UNION", "INFORMATION_SCHEMA", "SYSOBJECTS", "SYSCOLUMNS",
    ]
    upper = v.upper()
    for kw in _DANGEROUS_FILTER_KEYWORDS:
        if kw in upper:
            logger.warning("Filter value rejected — contains keyword %s: %r", kw, value[:100])
            return ""
    return v.strip()


def _computed_filter_clause(report_key: str, expression: str, values: List[str], match: str = "exact", allow_subquery: bool = False) -> str:
    """Build a safe AIWhere clause for a computed string dimension.

    Computed dimensions such as Manufacturer have business logic that cannot be
    represented by FilterHeader/FilterValue (contractor code with supplier-code
    fallback). The expression comes only from trusted report configuration;
    values are sanitized by _sanitize_filter_value before reaching this helper.
    """
    if not expression or not values:
        return ""
    unsafe = r'\b(DROP|DELETE|INSERT|UPDATE|EXEC|EXECUTE|XP_CMDSHELL|ALTER|CREATE|GRANT|TRUNCATE|MERGE)\b|;|--|/\*|\*/'
    if not allow_subquery:
        unsafe = r'\bSELECT\b|' + unsafe
    if re.search(unsafe, expression, re.IGNORECASE):
        logger.warning("Rejected computed filter expression — unsafe SQL: %s", expression[:120])
        return ""

    from app.services.column_registry import get_valid_base_columns
    valid_cols = get_valid_base_columns(report_key)
    refs = re.findall(r'DI\.(\w+)', expression, re.IGNORECASE)
    if any(ref.lower() not in valid_cols for ref in refs):
        logger.warning("Rejected computed filter expression — invalid DI reference: %s", expression[:120])
        return ""

    if match == "like":
        clauses = [f"({expression}) LIKE '%{value}%'" for value in values]
        return "(" + " OR ".join(clauses) + ")"

    quoted = ", ".join(f"'{value}'" for value in values)
    return f"({expression}) IN ({quoted})"


# SP mapping loaded dynamically from report_columns.json (sp, report_id, default_metric, default_dimension)
# Falls back to empty dict for reports not yet configured.
def _build_report_sp_map() -> Dict[str, Dict[str, Any]]:
    sp_map: Dict[str, Dict[str, Any]] = {}
    for report_key, cfg in _COLUMN_REGISTRY.items():
        if cfg.get("sp") is not None:
            sp_map[report_key] = {
                "sp": cfg["sp"],
                "report_id": cfg.get("report_id", 0),
                "default_metric": cfg.get("default_metric", "Amount"),
                "default_dimension": cfg.get("default_dimension", ""),
            }
    return sp_map

_REPORT_SP_MAP: Dict[str, Dict[str, Any]] = _build_report_sp_map()


def get_report_sp_map() -> Dict[str, Dict[str, Any]]:
    """Return the SP mapping (for external callers like main.py)."""
    return _REPORT_SP_MAP

# Shared HTTP client
_http_client: Optional[httpx.AsyncClient] = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            timeout=settings.real_api_timeout,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            verify=settings.real_api_verify_tls,
        )
    return _http_client


def _build_con(appuserid: str, ip_address: str, mode: str = "GetLLMChatSummary", yearcode: str = "") -> str:
    """Build the `con` JSON object as a plain JSON string.

    The API middleware expects a plain JSON string (like the sample curl).
    The SP internally decodes it via Base64Decode — the API middleware handles encoding.
    """
    con_obj = {
        "mode": mode,
        "appuserid": appuserid,
        "IPAddress": ip_address,
    }
    return json.dumps(con_obj, separators=(",", ":"))


def _build_p(
    report_key: str,
    intent_spec: Any,
    validated_filters: Dict[str, Any],
    ai_where_clause: str = "",
) -> str:
    """Build the `p` JSON object with MetricKey, Aggregation, Dimension, and filters.

    All values are sanitized against allowlists before being sent to the SP.
    The SP trusts that Python has already validated these values.
    """
    report_meta = _REPORT_SP_MAP.get(report_key, {})
    report_id = report_meta.get("report_id", 0)

    # Full report config (for tables + base_filter, sent to the shared LLM-chat SP)
    report_cfg = _get_report_columns(report_key)
    report_tables: List[str] = report_cfg.get("tables", []) or []
    report_base_filter: str = report_cfg.get("base_filter", "") or ""

    # Optional generated source query: the legacy SP's row-producing SELECT.
    # When present, the shared SP wraps it as FROM (<SourceQuery>) AS DI and
    # ignores the configured table list.  No DB objects are needed in tenant DBs.
    # Cached like the report JSONs — a restart picks up file changes.
    source_query = ""
    source_query_file = report_cfg.get("source_query_file")
    if source_query_file:
        if source_query_file in _SOURCE_QUERY_CACHE:
            source_query = _SOURCE_QUERY_CACHE[source_query_file]
        else:
            sq_path = (
                Path(__file__).resolve().parent.parent.parent
                / "app"
                / "report_queries"
                / source_query_file
            )
            if sq_path.exists():
                source_query = sq_path.read_text(encoding="utf-8").strip()
            else:
                logger.warning("source_query_file not found: %s", sq_path)
            _SOURCE_QUERY_CACHE[source_query_file] = source_query

    # ── Resolve report aliases ──
    # Python builds the full SQL expression for both computed and non-computed metrics.
    # The SP uses MetricExpr directly (no IF/ELSE mapping needed).
    raw_metric = getattr(intent_spec, "metric_key", "Amount") or "Amount"
    report_cols = report_cfg.get("columns", {})
    special = report_cfg.get("special_metrics", {})
    metric_catalog = report_cfg.get("metric_catalog", {})

    # ── Layer 3: Metric catalog validation (final guard before SP) ──
    # If the metric is in the catalog, verify its type matches the intent's
    # aggregation. E.g. a [weight] metric with aggregation "count" is suspicious.
    # Computed metrics (calc/metric_expr) are exempt from the count-forcing
    # guard: their expression is evaluated per-row and SUMmed, so COUNT(*)
    # would wrongly return the row count instead of the expression total.
    is_computed_metric = bool(
        raw_metric in report_cols and report_cols[raw_metric].get("computed")
    )
    if metric_catalog and raw_metric in metric_catalog:
        cat_type = metric_catalog[raw_metric].get("type", "amount")
        raw_agg_check = (getattr(intent_spec, "aggregation", "sum") or "sum").lower()
        # "count" aggregation on a non-count metric is a mismatch
        if raw_agg_check == "count" and cat_type != "count" and not is_computed_metric:
            logger.warning(
                "Layer 3: metric %r is type=%s but aggregation=count — "
                "falling back to total_count",
                raw_metric, cat_type
            )
            log_metric_validation(
                layer=3,
                report_key=report_key,
                original_metric=raw_metric,
                corrected_metric="total_count",
                original_type=cat_type,
                corrected_type="count",
                action="fallback",
                reason=f"aggregation=count on {cat_type} metric, falling back to total_count",
            )
            raw_metric = "total_count"
        # Symmetric guard: a count-type metric needs aggregation=count —
        # SUM(id) or AVG(id) grouped by a dimension fails inside the SP.
        # Exception: computed metrics carry their own expression (e.g.
        # CASE WHEN status IN (1,12) THEN 1 ELSE 0 END) which must be SUMmed,
        # not COUNT(*)ed — forcing count would return the raw row count.
        if (
            cat_type == "count"
            and raw_agg_check != "count"
            and not is_computed_metric
            and raw_metric in special
            and special.get(raw_metric, {}).get("type") == "count"
        ):
            logger.warning(
                "Layer 3: metric %r is type=count but aggregation=%s — forcing count",
                raw_metric, raw_agg_check,
            )
            log_metric_validation(
                layer=3,
                report_key=report_key,
                original_metric=raw_metric,
                corrected_metric=raw_metric,
                original_type=cat_type,
                corrected_type=cat_type,
                action="override",
                reason=f"aggregation={raw_agg_check} on count metric, forcing count",
            )
            intent_spec.aggregation = "count"
    elif metric_catalog and raw_metric not in metric_catalog and raw_metric not in report_cols and raw_metric not in special:
        # Unknown metric not in catalog, columns, or special — hard-blocked:
        # _resolve_metric below falls back to the safe default column, so the
        # raw value never reaches the SP. Log it as blocked (not a passive warn).
        logger.warning(
            "Layer 3: metric %r not in metric_catalog, columns, or special_metrics for %s — blocked, using default",
            raw_metric, report_key
        )
        log_metric_validation(
            layer=3,
            report_key=report_key,
            original_metric=raw_metric,
            corrected_metric=report_cfg.get("default_metric", "Amount"),
            action="blocked",
            reason=f"metric {raw_metric} not in metric_catalog, columns, or special_metrics — fell back to default",
        )

    table_metric_exprs: Dict[str, str] = {}
    if raw_metric in report_cols:
        configured_exprs = report_cols[raw_metric].get("table_metric_exprs", {}) or {}
        table_metric_exprs = {
            table: _xml_escape(expression) for table, expression in configured_exprs.items()
            if table in report_tables and isinstance(expression, str) and expression.strip()
        }

    # Build MetricExpr: the full SQL expression the SP plugs into SUM/AVG/etc.
    if raw_metric in report_cols and report_cols[raw_metric].get("computed"):
        # Computed metric: use metric_expr, or fall back to dimension_expr for computed dimensions
        metric_expr = report_cols[raw_metric].get("metric_expr") or report_cols[raw_metric].get("dimension_expr", "") or report_cols[raw_metric].get("sql", "")
        metric_key = raw_metric  # keep for logging
    elif raw_metric in report_cols:
        # Non-computed: wrap physical column in ISNULL
        metric_key = report_cols[raw_metric]["sql"]
        metric_expr = f"ISNULL(DI.{metric_key},0)"
    elif raw_metric in special:
        metric_key = special[raw_metric]["sql"]
        metric_expr = f"DI.{metric_key}"
    else:
        metric_key = _resolve_metric(report_key, raw_metric)
        metric_expr = f"ISNULL(DI.{metric_key},0)"

    raw_agg = (getattr(intent_spec, "aggregation", "sum") or "sum").lower()
    aggregation = raw_agg if raw_agg in _ALLOWED_AGGREGATIONS else "sum"
    # Computed metrics evaluate an expression per row; "count" would ignore the
    # expression and return a raw row count — map it to sum so the expression
    # total (e.g. conditional job counts, summed pieces) is returned.
    if is_computed_metric and aggregation == "count":
        aggregation = "sum"

    # Build DimensionExpr: the full SQL expression for GROUP BY
    raw_dim = getattr(intent_spec, "dimension", "") or ""
    table_dimension_exprs: Dict[str, str] = {}
    if raw_dim in report_cols:
        configured_exprs = report_cols[raw_dim].get("table_dimension_exprs", {}) or {}
        table_dimension_exprs = {
            table: _xml_escape(expression) for table, expression in configured_exprs.items()
            if table in report_tables and isinstance(expression, str) and expression.strip()
        }
    if raw_dim in report_cols and report_cols[raw_dim].get("computed"):
        # Computed dimension: use dimension_expr directly
        dimension_expr = report_cols[raw_dim].get("dimension_expr", "")
        dimension = raw_dim  # keep for logging
    elif raw_dim in report_cols:
        # Non-computed: wrap in ISNULL
        dimension = report_cols[raw_dim]["sql"]
        dimension_expr = f"ISNULL(DI.{dimension},'')"
    else:
        dimension = _resolve_dimension(report_key, raw_dim)
        dimension_expr = f"ISNULL(DI.{dimension},'')" if dimension else ""

    # ── Scalable guards: validate dimension before sending to SP ──
    # dimension_expr is trusted config (report_columns/*.json), not LLM/user
    # input, so a correlated scalar subquery is allowed — the SP evaluates it in
    # the derived-table SELECT and groups by the __DV alias (valid SQL Server).
    if dimension_expr:
        # Guard: reject bare aggregate functions in dimension_expr (invalid in GROUP BY)
        if re.search(r'\b(SUM|AVG|MAX|MIN|COUNT)\s*\(', dimension_expr, re.IGNORECASE):
            logger.warning("dimension_expr contains aggregate function — clearing: %s", dimension_expr[:100])
            dimension = ""
            dimension_expr = ""
    # Guard 3: If dimension was set to a computed metric (has metric_expr but no dimension_expr), clear it
    if raw_dim in report_cols and report_cols[raw_dim].get("computed") and not (report_cols[raw_dim].get("dimension_expr") or report_cols[raw_dim].get("table_dimension_exprs")):
        logger.warning("dimension '%s' is a computed metric without dimension_expr — clearing", raw_dim)
        dimension = ""
        dimension_expr = ""

    raw_sort = (getattr(intent_spec, "sort", "desc") or "desc").lower()
    sort_direction = raw_sort if raw_sort in _ALLOWED_SORT_DIRECTIONS else "desc"

    raw_limit = getattr(intent_spec, "limit", 0) or 0
    try:
        limit = int(raw_limit)
        if limit < 0:
            limit = 0
    except (ValueError, TypeError):
        limit = 0

    # ── Second-layer guard: validate ai_where before sending to SP ──
    if ai_where_clause:
        # Reject subqueries
        if re.search(r'\bSELECT\b', ai_where_clause, re.IGNORECASE):
            logger.warning("_build_p: ai_where contains subquery — clearing: %s", ai_where_clause[:100])
            ai_where_clause = ""
        # Validate DI.<column> references against registry
        if ai_where_clause:
            from app.services.column_registry import get_valid_base_columns
            from app.services.parse_result import expand_computed_where_refs
            ai_where_clause = expand_computed_where_refs(ai_where_clause, report_key)
            valid_cols = get_valid_base_columns(report_key)
            refs = re.findall(r'DI\.(\w+)', ai_where_clause, re.IGNORECASE)
            for ref in refs:
                if ref.lower() not in valid_cols:
                    logger.warning("_build_p: DI.%s not valid base column — clearing ai_where: %s", ref, ai_where_clause[:100])
                    ai_where_clause = ""
                    break

    table_dimension_filters: Dict[str, List[str]] = {}
    p_obj: Dict[str, Any] = {
        "ReportId": report_id,
        "IsMaster": 0,
        "Mode": "GetLLMChatSummary",
        "MetricKey": metric_key,
        "MetricExpr": _xml_escape(metric_expr),
        "Aggregation": aggregation,
        "Dimension": dimension,
        "DimensionExpr": _xml_escape(dimension_expr),
        "SortDirection": sort_direction,
        "Limit": limit,
        "AIWhereClause": "",
        "FilterHeader": "",
        "FilterValue": "",
        "FilterStartDate": "",
        "FilterEndDate": "",
        # Report metadata for the shared LLM-chat SP (DynamicReport_LLMChatbeta).
        # Table names are config-sourced (trusted); validated again SP-side.
        # SQL expression fields are XML-escaped because the API gateway parses
        # p as XML and expressions may contain <> or & operators.
        "Tables": report_tables,
        "SourceQuery": _xml_escape(source_query) if source_query else "",
        "BaseFilter": _xml_escape(report_base_filter),
        # Date column used by the shared SP for FilterStartDate/FilterEndDate
        # (defaults to entrydate; reports like order_report use jobdate).
        "DateColumn": report_cfg.get("date_column") or "entrydate",
        "TableFilters": {
            table: _xml_escape(expression)
            for table, expression in (report_cfg.get("table_filters", {}) or {}).items()
            if isinstance(expression, str) and expression.strip()
        },
        "TableMetricExprs": table_metric_exprs,
        "TableDimensionExprs": table_dimension_exprs,
    }

    # ── Build filters with sanitization ──
    filter_headers: List[str] = []
    filter_values: List[str] = []
    computed_filter_clauses: List[str] = []

    for key, value in validated_filters.items():
        if value is None or value == "":
            continue

        # Date filters handled separately
        if key in ("start_date", "end_date"):
            date_val = str(value).strip()
            # Validate YYYY-MM-DD format to prevent injection
            if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_val):
                logger.warning("Rejected invalid date format: %s=%r", key, value)
                continue
            if key == "start_date":
                p_obj["FilterStartDate"] = date_val
            else:
                p_obj["FilterEndDate"] = date_val
            continue

        report_col_name, col_meta = _resolve_filter_target(report_key, key)
        if not report_col_name or report_col_name in ("_date", "_name_filter"):
            continue  # rejected, date, or name-based (handled by ai_where)

        raw_values = value if isinstance(value, list) else [value]
        sanitized_vals = [_sanitize_filter_value(str(v)) for v in raw_values]
        sanitized_vals = [v for v in sanitized_vals if v]
        if not sanitized_vals:
            continue

        if col_meta.get("computed"):
            filter_meta = col_meta.get("filter", {}) if isinstance(col_meta.get("filter"), dict) else {}
            configured_table_exprs = col_meta.get("table_dimension_exprs", {}) or {}
            if configured_table_exprs:
                for table in report_tables:
                    expression = configured_table_exprs.get(table, "")
                    clause = _computed_filter_clause(
                        report_key,
                        expression,
                        sanitized_vals,
                        filter_meta.get("match", "exact"),
                        allow_subquery=True,
                    )
                    if clause:
                        table_dimension_filters.setdefault(table, []).append(clause)
                    else:
                        logger.warning("Skipped computed filter %r for table %r — no safe filter expression", report_col_name, table)
            else:
                clause = _computed_filter_clause(
                    report_key,
                    col_meta.get("dimension_expr", ""),
                    sanitized_vals,
                    filter_meta.get("match", "exact"),
                )
                if clause:
                    computed_filter_clauses.append(clause)
                else:
                    logger.warning("Skipped computed filter %r — no safe filter expression", report_col_name)
            continue

        col_name = col_meta.get("sql", "")
        if not col_name:
            continue
        filter_headers.append(col_name)
        filter_values.append(",".join(sanitized_vals))

    if computed_filter_clauses:
        if len(report_tables) == 1:
            # The SP caps AIWhereClause at 2000 chars but TableDimensionFilters
            # get 4000 each — expanded CASE predicates overflow the smaller
            # budget fast. Single-table reports can carry the same clause
            # through the per-table channel with identical semantics.
            table_dimension_filters.setdefault(report_tables[0], []).extend(
                computed_filter_clauses
            )
        else:
            generated_where = " AND ".join(f"({clause})" for clause in computed_filter_clauses)
            ai_where_clause = f"({ai_where_clause}) AND {generated_where}" if ai_where_clause else generated_where

    p_obj["AIWhereClause"] = _xml_escape(ai_where_clause)
    p_obj["FilterHeader"] = "#".join(filter_headers)
    p_obj["FilterValue"] = _xml_escape("#".join(filter_values))
    p_obj["TableDimensionFilters"] = {
        table: _xml_escape(" AND ".join(f"({clause})" for clause in clauses))
        for table, clauses in table_dimension_filters.items()
    }

    return json.dumps(p_obj, separators=(",", ":"))


async def call_real_report_api(
    report_key: str,
    intent_spec: Any,
    validated_filters: Dict[str, Any],
    appuserid: str,
    ip_address: str,
    yearcode: str,
    sp_number: Optional[int] = None,
    ai_where_clause: str = "",
) -> Dict[str, Any]:
    """Call the real report API and return the parsed response.

    Returns a dict with:
      - rd: list of { MetricValue, DimensionValue } rows
      - rd1: list of { TotalCount } rows
      - total_count: int (extracted from rd1)
      - values: list of float (extracted from rd)
      - dimensions: list of str (extracted from rd, for ranking)
    """
    report_meta = _REPORT_SP_MAP.get(report_key, {})
    # Route chat-mode requests to the shared metadata-driven LLM-chat SP when
    # configured, so the GetLLMChatSummary block isn't duplicated across 100+
    # report SPs. When real_api_llm_chat_sp=0, fall back to legacy behaviour
    # (each report's own SP, which must contain the inlined chat block).
    if settings.real_api_llm_chat_sp > 0:
        sp = settings.real_api_llm_chat_sp
    else:
        sp = sp_number or report_meta.get("sp") or settings.real_api_sp

    # Use static yearcode from config if not provided by frontend
    if not yearcode:
        yearcode = settings.real_api_yearcode

    # ── SQL Guard: validate AI-generated WHERE clause ──
    # Multi-layer validation: string blocklist → AST → column allowlist → operators
    # If any layer fails, the WHERE clause is rejected (fail-closed).
    from app.services.sql_guard import validate_where_clause
    safe_where, guard_reason = validate_where_clause(ai_where_clause, report_key)
    ai_where_dropped = bool(ai_where_clause and not safe_where)
    if ai_where_dropped:
        logger.warning(
            "SQL Guard REJECTED ai_where_clause for report=%s reason=%s | raw=%s",
            report_key, guard_reason, ai_where_clause[:200],
        )
    ai_where_clause = safe_where

    con_encoded = _build_con(appuserid, ip_address, yearcode=yearcode)
    p_encoded = _build_p(report_key, intent_spec, validated_filters, ai_where_clause)

    headers = {
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/json",
        "Yearcode": yearcode,
        "sp": str(sp),
        "sv": settings.real_api_sv,
        "version": settings.real_api_version,
    }

    body = {
        "con": con_encoded,
        "p": p_encoded,
        "f": "DynamicReport ( data )",
    }

    url = settings.real_api_base_url
    client = _get_client()

    logger.info(
        "Real API call: report=%s sp=%s metric=%s agg=%s dimension=%s",
        report_key,
        sp,
        getattr(intent_spec, "metric_key", "?"),
        getattr(intent_spec, "aggregation", "?"),
        getattr(intent_spec, "dimension", ""),
    )

    # Log the full API request body
    log_report_api_request(
        report_key=report_key,
        api_url=url,
        request_body=body,
        ai_where_clause=ai_where_clause,
    )

    timer = AuditTimer()
    with timer:
        try:
            response = await client.post(url, headers=headers, json=body)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            log_sql_execution(
                caller="call_real_report_api",
                sp_mode="GetLLMChatSummary",
                metric_key=getattr(intent_spec, "metric_key", "?"),
                aggregation=getattr(intent_spec, "aggregation", "?"),
                dimension=getattr(intent_spec, "dimension", ""),
                limit=getattr(intent_spec, "limit", 0),
                ai_where_clause=ai_where_clause,
                filters=validated_filters,
                latency_ms=timer.ms,
                success=False,
                error=f"HTTP {exc.response.status_code}",
            )
            raise RealApiError(
                f"Real API returned {exc.response.status_code}: {exc.response.text[:500]}",
                status_code=exc.response.status_code,
                body=exc.response.text,
            ) from exc
        except httpx.RequestError as exc:
            log_sql_execution(
                caller="call_real_report_api",
                sp_mode="GetLLMChatSummary",
                metric_key=getattr(intent_spec, "metric_key", "?"),
                aggregation=getattr(intent_spec, "aggregation", "?"),
                dimension=getattr(intent_spec, "dimension", ""),
                limit=getattr(intent_spec, "limit", 0),
                ai_where_clause=ai_where_clause,
                filters=validated_filters,
                latency_ms=timer.ms,
                success=False,
                error=str(exc),
            )
            raise RealApiError(f"Real API request failed: {exc}", status_code=0, body="") from exc

    raw = response.json()

    # Check API-level status
    if isinstance(raw, dict) and raw.get("Status") != "200":
        log_sql_execution(
            caller="call_real_report_api",
            sp_mode="GetLLMChatSummary",
            metric_key=getattr(intent_spec, "metric_key", "?"),
            aggregation=getattr(intent_spec, "aggregation", "?"),
            dimension=getattr(intent_spec, "dimension", ""),
            limit=getattr(intent_spec, "limit", 0),
            ai_where_clause=ai_where_clause,
            filters=validated_filters,
            latency_ms=timer.ms,
            success=False,
            error=f"API Status={raw.get('Status')}",
        )
        raise RealApiError(
            f"Real API error: {raw.get('Message', 'Unknown error')}",
            status_code=200,
            body=json.dumps(raw),
        )

    data = raw.get("Data", {}) if isinstance(raw, dict) else {}
    parsed = _parse_real_api_response(data, report_key)

    # Check for stat_code errors — the SP returns an error row instead of
    # raising. Surface it as a data error so the answer layer shows a clean
    # failure message instead of rendering the error row as a fake result.
    raw_rd = parsed.get("raw_rd", [])
    stat_code = None
    if raw_rd and isinstance(raw_rd[0], dict) and raw_rd[0].get("stat_code"):
        stat_code = raw_rd[0].get("stat_code")
        stat_msg = raw_rd[0].get("stat_msg") or raw_rd[0].get("message") or ""
        parsed["error"] = stat_msg or f"report query failed (stat_code={stat_code})"

    log_sql_execution(
        caller="call_real_report_api",
        sp_mode="GetLLMChatSummary",
        metric_key=getattr(intent_spec, "metric_key", "?"),
        aggregation=getattr(intent_spec, "aggregation", "?"),
        dimension=getattr(intent_spec, "dimension", ""),
        limit=getattr(intent_spec, "limit", 0),
        ai_where_clause=ai_where_clause,
        filters=validated_filters,
        row_count=len(parsed.get("rows", [])),
        stat_code=stat_code,
        latency_ms=timer.ms,
        success=stat_code is None,
        error="" if stat_code is None else f"stat_code={stat_code}",
    )

    if ai_where_dropped:
        # Surface the guard rejection to the caller so the answer can disclose
        # that the query ran unfiltered (silent drop = confidently wrong).
        parsed["ai_where_dropped"] = True

    return parsed


def _parse_real_api_response(data: Dict[str, Any], report_key: str) -> Dict[str, Any]:
    """Parse the rd/rd1 response format into the shape the answer generator expects.

    The SP returns:
      - No-dimension mode: rd has { MetricValue, TotalCount } in a single row
      - Dimension mode:    rd has [{ DimensionValue, MetricValue }] rows,
                           rd1 has [{ TotalCount }]
    """
    rd = data.get("rd", []) or []
    rd1 = data.get("rd1", []) or []

    # Extract total count
    total_count = 0
    # First try rd1 (dimension mode)
    if rd1 and isinstance(rd1, list) and isinstance(rd1[0], dict):
        total_count = rd1[0].get("TotalCount", 0) or 0
    # If rd1 is empty, try rd (no-dimension mode returns TotalCount in rd)
    if total_count == 0 and rd and isinstance(rd, list) and isinstance(rd[0], dict):
        total_count = rd[0].get("TotalCount", 0) or 0

    # Extract values and dimensions from rd
    values: List[float] = []
    text_values: List[str] = []  # non-numeric metrics (e.g. customer name lookups)
    dimensions: List[str] = []
    rows: List[Dict[str, Any]] = []

    for row in rd:
        if not isinstance(row, dict):
            continue
        metric_value = row.get("MetricValue", 0)
        dimension_value = row.get("DimensionValue", "") or ""
        num_value: Optional[float]
        try:
            num_value = float(metric_value)
        except (TypeError, ValueError):
            num_value = None
        if num_value is not None:
            values.append(num_value)
        elif metric_value not in (None, ""):
            # Text metric (e.g. CustomerFullName for "give me its customer name")
            text_values.append(str(metric_value))
        dimensions.append(str(dimension_value))
        rows.append({
            "MetricValue": metric_value,
            "DimensionValue": dimension_value,
        })

    return {
        "rows": rows,
        "total_count": total_count,
        "truncated": False,  # SP returns pre-aggregated data, no truncation
        "values": values,
        "text_values": text_values,
        "dimensions": dimensions,
        "raw_rd": rd,
        "raw_rd1": rd1,
    }


class RealApiError(Exception):
    def __init__(self, message: str, status_code: int, body: str):
        super().__init__(message)
        self.status_code = status_code
        self.body = body


def get_report_sp_map() -> Dict[str, Dict[str, Any]]:
    """Return the static SP number mapping. Used by the chat flow to look up
    the SP number when the frontend doesn't provide one."""
    return _REPORT_SP_MAP.copy()
