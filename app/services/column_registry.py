"""Shared column registry utilities — reads report_columns/ directory dynamically.

This module provides scalable, report-agnostic functions that replace
hardcoded dictionaries across answer_generator.py, block_builder.py, and
semantic_query_parser.py. New reports only need a JSON file in
app/report_columns/.

The JSON files use shortened keys for size:
  desc -> description, grp -> business_group, fo -> filter_only,
  calc -> computed, me -> metric_expr, de -> dimension_expr

The loader expands these to full keys so the rest of the code is unchanged.
"""
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

_COLUMNS_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "report_columns")
_SHARED_DIR = os.path.join(_COLUMNS_DIR, "_shared")
_LEGACY_COLUMNS_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "report_columns.json")
_REGISTRY_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "registry.json")


def _load_shared_knowledge() -> Dict[str, Any]:
    """Load shared jewelry domain knowledge from _shared/jewelry_knowledge.json.

    Returns a dict with keys: metric_type_rules, metal_knowledge,
    erp_conventions, negative_examples. Returns empty dict on failure.
    """
    path = os.path.join(_SHARED_DIR, "jewelry_knowledge.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.warning("Cannot load shared knowledge %s: %s", path, e)
        return {}


_SHARED_KNOWLEDGE = _load_shared_knowledge()

# Short key -> long key mapping (for backward compatibility with existing code)
_KEY_EXPANSION = {
    "desc": "description",
    "grp": "business_group",
    "fo": "filter_only",
    "calc": "computed",
    "me": "metric_expr",
    "de": "dimension_expr",
}


def _expand_keys(obj: Any) -> Any:
    """Recursively expand shortened JSON keys to full keys."""
    if isinstance(obj, dict):
        return {_KEY_EXPANSION.get(k, k): _expand_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_keys(item) for item in obj]
    return obj


def _load_registry() -> Dict[str, Any]:
    """Load all report column configs from app/report_columns/ directory.

    Falls back to legacy single-file report_columns.json if the directory
    doesn't exist (backward compatibility).

    Shortened keys are expanded to full keys automatically.
    """
    # New: load from split directory
    if os.path.isdir(_COLUMNS_DIR):
        result: Dict[str, Any] = {}
        for fname in os.listdir(_COLUMNS_DIR):
            if not fname.endswith(".json"):
                continue
            # Skip files in _shared subdirectory (handled separately)
            fpath = os.path.join(_COLUMNS_DIR, fname)
            if os.path.isdir(fpath):
                continue
            rk = fname[:-5]  # strip .json
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    result[rk] = _expand_keys(json.load(f))
            except Exception as e:
                logger.error("Cannot load %s: %s", fpath, e)
        # Merge shared knowledge into each report's prompt_rules
        if _SHARED_KNOWLEDGE:
            shared_rules = (
                _SHARED_KNOWLEDGE.get("metric_type_rules", [])
                + _SHARED_KNOWLEDGE.get("metal_knowledge", [])
                + _SHARED_KNOWLEDGE.get("erp_conventions", [])
            )
            shared_neg = _SHARED_KNOWLEDGE.get("negative_examples", [])
            for rk, cfg in result.items():
                pr = cfg.setdefault("prompt_rules", {})
                pr["special_rules"] = shared_rules + pr.get("special_rules", [])
                pr["negative_examples"] = shared_neg + pr.get("negative_examples", [])
        if result:
            return result

    # Legacy fallback: single file
    try:
        with open(_LEGACY_COLUMNS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


_REGISTRY = _load_registry()

# Add report key aliases (sales_summary is the intent-config name for sales_report)
if "sales_report" in _REGISTRY and "sales_summary" not in _REGISTRY:
    _REGISTRY["sales_summary"] = _REGISTRY["sales_report"]

# wip_summary is the intent-config name for wip_report
if "wip_report" in _REGISTRY and "wip_summary" not in _REGISTRY:
    _REGISTRY["wip_summary"] = _REGISTRY["wip_report"]


def get_dimension_headers(report_key: str = "sales_report") -> Dict[str, str]:
    """Build dimension header labels from report_columns.json.

    Maps column sql names to their description (or name if no description).
    This replaces the hardcoded _DIMENSION_HEADERS dict in answer_generator.py
    and block_builder.py.
    """
    report_cfg = _REGISTRY.get(report_key, {})
    columns = report_cfg.get("columns", {})
    headers: Dict[str, str] = {}
    for name, meta in columns.items():
        if meta.get("not_available"):
            continue
        sql = meta.get("sql", name)
        desc = meta.get("description", "")
        if desc:
            headers[sql] = desc
        headers[name] = desc or name
    return headers


def get_filter_only_columns(report_key: str = "sales_report") -> Set[str]:
    """Return set of filter-only column names for a report."""
    report_cfg = _REGISTRY.get(report_key, {})
    columns = report_cfg.get("columns", {})
    return {name for name, meta in columns.items() if meta.get("filter_only")}


def get_computed_metrics(report_key: str = "sales_report") -> Set[str]:
    """Return set of computed metric names (have metric_expr, not filter_only)."""
    report_cfg = _REGISTRY.get(report_key, {})
    columns = report_cfg.get("columns", {})
    return {
        name for name, meta in columns.items()
        if meta.get("computed") and (meta.get("metric_expr") or meta.get("table_metric_exprs")) and not meta.get("filter_only")
    }


def get_valid_base_columns(report_key: str = "sales_report") -> Set[str]:
    """Return set of physical column sql names valid in ai_where clauses.

    Includes:
    - Non-computed, non-filter-only columns (their sql names)
    - DI.<column> references extracted from computed columns' dimension_expr
      and metric_expr (physical base-table columns used inside expressions
      like CONCAT(DI.firstname, DI.lastname))
    """
    report_cfg = _REGISTRY.get(report_key, {})
    columns = report_cfg.get("columns", {})
    valid: Set[str] = set()
    for name, meta in columns.items():
        if meta.get("filter_only"):
            continue
        if meta.get("computed"):
            expressions = [meta.get("dimension_expr", ""), meta.get("metric_expr", "")]
            for expr_map_key in ("table_dimension_exprs", "table_metric_exprs"):
                expr_map = meta.get(expr_map_key, {}) or {}
                if isinstance(expr_map, dict):
                    expressions.extend(expr_map.values())
            for expr in expressions:
                if expr:
                    for ref in re.findall(r'DI\.(\w+)', expr, re.IGNORECASE):
                        valid.add(ref.lower())
            continue
        sql = meta.get("sql", "")
        if sql and not meta.get("not_available"):
            valid.add(sql.lower())
    return valid


def get_metric_unit(metric_name: str, report_key: str = "sales_report") -> str:
    """Determine metric unit from report_columns.json.

    Checks the 'unit' field first (e.g. 'ctw', 'gms' → weight, 'pcs' → count, '%' → rate).
    Falls back to the 'type' field for metrics without an explicit unit.
    Returns 'currency' for unknown metrics.
    """
    report_cfg = _REGISTRY.get(report_key, {})
    columns = report_cfg.get("columns", {})
    special = report_cfg.get("special_metrics", {})

    if metric_name in columns:
        col = columns[metric_name]
        unit_field = col.get("unit", "")
        if unit_field:
            if unit_field in ("ctw", "gms", "g"):
                return "weight"
            if unit_field in ("pcs",):
                return "count"
            if unit_field in ("%",):
                return "rate"
        col_type = col.get("type", "decimal")
        if col_type in ("string", "date", "text"):
            return "text"
        if col_type in ("decimal", "money", "float"):
            return "currency"
        if col_type in ("int", "integer", "number", "count", "count_distinct"):
            return "count"
        if col_type in ("weight", "grams"):
            return "weight"
        if col_type in ("rate", "ratio"):
            return "rate"
        return "currency"

    if metric_name in special:
        return "count"

    return "currency"


def get_metric_unit_label(metric_name: str, report_key: str = "sales_report") -> str:
    """Return the display unit suffix for a metric (e.g. 'ctw', 'pcs', 'gms', '%').

    Reads the 'unit' field from report_columns.json. Returns '' for the
    'currency' sentinel — it's a type marker, not a display suffix
    (₹ is added by format_currency).
    """
    report_cfg = _REGISTRY.get(report_key, {})
    columns = report_cfg.get("columns", {})

    if metric_name in columns:
        unit = columns[metric_name].get("unit", "")
        return "" if unit == "currency" else unit

    return ""


def get_report_keywords(report_key: str = "sales_report") -> List[str]:
    """Return the report-level keyword list used for fast report routing.

    New reports can add a `report_keywords` list in their JSON config.
    """
    report_cfg = _REGISTRY.get(report_key, {})
    return list(report_cfg.get("report_keywords", []))


def get_report_intents(report_key: str = "sales_report") -> Dict[str, Dict[str, Any]]:
    """Return the per-report intent definitions used by the legacy intent layer.

    Intent metadata moved from intent_config.json to report_columns/*.json so
    each report owns its own patterns, metrics, dimensions, and fallback rules.
    """
    report_cfg = _REGISTRY.get(report_key, {})
    return dict(report_cfg.get("intents", {}))


def get_intent_patterns(report_key: str = "sales_report") -> List[Tuple[Any, Dict[str, Any]]]:
    """Compile all intent regex patterns for a report into (pattern, rule) tuples.

    Mirrors the old _compile_patterns behavior, but reads from the per-report
    `intents` config. Returns an empty list if the report has no intents defined.
    """
    intents = get_report_intents(report_key)
    compiled: List[Tuple[Any, Dict[str, Any]]] = []
    for intent_name, meta in intents.items():
        rule = dict(meta)
        rule["intent"] = intent_name
        for pat in rule.get("patterns", []):
            try:
                compiled.append((re.compile(pat, re.IGNORECASE), rule))
            except re.error as exc:
                logger.warning("Invalid intent regex for %s.%s: %s", report_key, intent_name, exc)
    return compiled


def get_canonical_values(report_key: str = "sales_report", field: str = "") -> Dict[str, str]:
    """Return the canonical-value mapping for a report field, if any."""
    report_cfg = _REGISTRY.get(report_key, {})
    canonical = report_cfg.get("canonical_values", {})
    return dict(canonical.get(field, {}))


def get_fallback_intent(report_key: str = "sales_report") -> Optional[Dict[str, Any]]:
    """Return the per-report fallback intent definition, if any."""
    report_cfg = _REGISTRY.get(report_key, {})
    return report_cfg.get("fallback_intent")


def get_location_aliases(report_key: str = "sales_report") -> Dict[str, Optional[str]]:
    """Return location/branch alias map for a report. Empty dict if none."""
    report_cfg = _REGISTRY.get(report_key, {})
    return dict(report_cfg.get("location_aliases", {}))


def get_suggested_questions(report_key: str = "sales_report") -> List[str]:
    """Return the suggested-question list shown when the chatbot first opens.

    Each report owns its `suggested_questions` list in report_columns/*.json.
    """
    report_cfg = _REGISTRY.get(report_key, {})
    return list(report_cfg.get("suggested_questions", []))


def list_report_keys() -> List[str]:
    """Return all registered report keys."""
    return list(_REGISTRY.keys())


def get_computed_only_names(report_key: str = "sales_report") -> Set[str]:
    """Return names of columns that must NOT be used in ai_where.

    A column is "computed-only" when it has `computed: true` and is explicitly
    marked as not valid in a WHERE clause (e.g. a virtual metric that is a
    CASE expression over physical columns). New reports opt into this guard by
    adding `"not_in_where": true` (or `"computed_only": true`) to the column.
    """
    report_cfg = _REGISTRY.get(report_key, {})
    columns = report_cfg.get("columns", {})
    result: Set[str] = set()
    for name, meta in columns.items():
        if not meta.get("computed"):
            continue
        if meta.get("not_in_where") or meta.get("computed_only"):
            result.add(name)
    return result


def resolve_metric_alias(report_key: str, metric: str) -> Optional[str]:
    """Resolve an LLM-emitted metric name to a metric_catalog/column key.

    The LLM sometimes writes a label or friendly name (``Amount``, ``total
    sales``) instead of the catalog key. Match catalog keys, labels, and
    aliases case-insensitively, then column keys and their ``sql`` names.
    """
    if not metric:
        return None
    report_cfg = _REGISTRY.get(report_key, {})
    catalog = report_cfg.get("metric_catalog", {}) or {}
    columns = report_cfg.get("columns", {}) or {}
    needle = metric.strip().lower()

    for key, meta in catalog.items():
        if key.lower() == needle:
            return key
        if str(meta.get("label", "")).strip().lower() == needle:
            return key
        for alias in meta.get("aliases", []) or []:
            if str(alias).strip().lower() == needle:
                return key

    for key, meta in columns.items():
        if key.lower() == needle:
            return key
        if str(meta.get("sql", "")).strip().lower() == needle and meta.get("sql"):
            return key
    return None


def get_dimension_aliases(report_key: str) -> Dict[str, str]:
    """Natural-language alias -> column key for GROUP BY dimensions.

    The LLM often emits friendly names (``branch``, ``customer``) instead of
    the configured column key (``Mastermanagement_FG_StockLockername``,
    ``Job_customerfirmname``). Configured per report as
    ``"dimension_aliases": {"branch": "..."}``; keys are matched
    case-insensitively.
    """
    report_cfg = _REGISTRY.get(report_key, {})
    aliases = report_cfg.get("dimension_aliases", {}) or {}
    return {str(k).lower(): str(v) for k, v in aliases.items()}


_DETAIL_ENTITY_ALIASES = {
    # Detail rows show the entity NAME (CustomerFullName, designno...).
    # Identity columns (code + name) are only a fallback for reports that
    # lack a plain name dimension.
    "unique_customers": ("customer", "customer identity"),
    "unique_designs": ("design", "design identity"),
    "total_count": ("bill",),
}


def get_detail_dimension(report_key: str, metric: str) -> Optional[str]:
    """Dimension that lists the entities behind a count-style metric
    (unique_customers -> customer, unique_designs -> design, total_count -> bill).
    Used when the user asks for "details/list/breakup" of a count, and to
    detect same-entity count_distinct+dimension plans (meaningless per group).
    """
    alias_words = _DETAIL_ENTITY_ALIASES.get(metric)
    if not alias_words:
        return None
    aliases = get_dimension_aliases(report_key)
    dim = next((aliases[w] for w in alias_words if aliases.get(w)), None)
    if dim is None and metric == "total_count":
        dim = aliases.get("job no") or aliases.get("serial job")
    return dim


def get_default_metric(report_key: str) -> Optional[str]:
    """The report's configured ``default_metric`` (e.g. Amount for sales)."""
    return (_REGISTRY.get(report_key, {}) or {}).get("default_metric")


def get_entity_dimensions(report_key: str, metric: str) -> Set[str]:
    """All dimension columns that represent the entity behind a count-style
    metric (e.g. unique_customers -> {CustomerFullName, CustomerIdentity}).
    Used to upgrade a name-only dimension to the identity dimension so grouped
    detail rows match the DISTINCT-count headline number.
    """
    aliases = get_dimension_aliases(report_key)
    dims = {aliases[w] for w in _DETAIL_ENTITY_ALIASES.get(metric, ()) if aliases.get(w)}
    if metric == "total_count":
        dims |= {d for w in ("job no", "serial job") if (d := aliases.get(w))}
    return dims


def get_filter_invalid_values(report_key: str, field: str) -> Set[str]:
    """Values that must never be accepted for a filter key (lowercased set).

    Merges, for the key and its filter_key_map target:
    - top-level ``filter_invalid_values`` config (covers name_filter_map keys
      that are not real columns, e.g. ``customername``)
    - ``columns[<col>].filter.invalid_values`` and ``columns[<col>].invalid_values``
    """
    report_cfg = _REGISTRY.get(report_key, {})
    filter_key_map = report_cfg.get("filter_key_map", {})
    columns = report_cfg.get("columns", {})
    fiv = report_cfg.get("filter_invalid_values", {})
    target = filter_key_map.get(field.lower(), field) if isinstance(field, str) else field

    values: Set[str] = set()
    for key in (field, target):
        values.update(str(v).lower() for v in fiv.get(key, []) or [])
        col = columns.get(key) or {}
        f = col.get("filter") or {}
        values.update(str(v).lower() for v in f.get("invalid_values") or [])
        values.update(str(v).lower() for v in col.get("invalid_values") or [])
    return values


def get_invalid_value_columns(report_key: str) -> Dict[str, Set[str]]:
    """``{di_column_lower: set(invalid literals)}`` for ai_where cleanup.

    Maps every DI.<column> that can appear in a generated WHERE clause to the
    literals that are not legal filter values for it — built from
    name_filter_map expressions (with their filter_invalid_values entries) and
    physical/computed column sql names.
    """
    report_cfg = _REGISTRY.get(report_key, {})
    name_filter_map = report_cfg.get("name_filter_map", {})
    columns = report_cfg.get("columns", {})

    out: Dict[str, Set[str]] = {}

    def _add(col_name: str, values) -> None:
        vals = {str(v).lower() for v in values or []}
        if vals:
            out.setdefault(col_name.lower(), set()).update(vals)

    for key, expr in name_filter_map.items():
        invalid = get_filter_invalid_values(report_key, key)
        for ref in re.findall(r"DI\.(\w+)", expr or "", re.IGNORECASE):
            _add(ref, invalid)

    for cname, meta in columns.items():
        invalid = get_filter_invalid_values(report_key, cname)
        _add(meta.get("sql") or cname, invalid)

    return out


def derive_filter_schema(report_key: str) -> Dict[str, Dict[str, Any]]:
    """Auto-derive a filter_schema from report_columns.json.

    This replaces the manually-maintained filter_schema in registry.json.
    For each alias in filter_key_map, the column type and description are
    looked up from the columns registry. Date filters (start_date, end_date)
    are always added. Internal keys (_date, _name_filter) are skipped.

    Returns: {filter_key: {"type": str, "required": false, "default": None,
                            "description": str}}
    """
    report_cfg = _REGISTRY.get(report_key, {})
    columns = report_cfg.get("columns", {})
    filter_key_map: Dict[str, str] = report_cfg.get("filter_key_map", {})

    schema: Dict[str, Dict[str, Any]] = {}

    # Always include date filters
    schema["start_date"] = {
        "type": "date",
        "required": False,
        "default": None,
        "description": "ISO date (YYYY-MM-DD). Filters from this date.",
    }
    schema["end_date"] = {
        "type": "date",
        "required": False,
        "default": None,
        "description": "ISO date (YYYY-MM-DD). Filters up to this date.",
    }

    # Derive filters from filter_key_map. Multiple aliases may point to the
    # same governed target (for example supplier -> Manufacturer), and each
    # alias must remain a valid public filter key.
    for alias, target in filter_key_map.items():
        # Skip internal mapping targets
        if target in ("_date", "_name_filter"):
            continue

        # Look up the target column in the columns registry
        col_meta = columns.get(target, {})
        if col_meta.get("not_available"):
            continue  # column is not actually available in chat mode

        col_type = col_meta.get("type", "string")
        # Map JSON column types to filter schema types
        if col_type in ("int", "integer"):
            ftype = "integer"
        elif col_type in ("decimal", "money", "float", "number"):
            ftype = "number"
        elif col_type in ("bool", "boolean"):
            ftype = "boolean"
        elif col_type == "date":
            ftype = "date"
        else:
            ftype = "string"

        schema[alias] = {
            "type": ftype,
            "required": False,
            "default": None,
            "description": col_meta.get("description", alias),
        }

    return schema


def check_registry_sync() -> List[str]:
    """Verify registry.json and report_columns.json are in sync.

    Returns a list of warning messages. Empty list = all good.
    Checks:
    1. Every report in registry.json exists in report_columns.json
    2. Every report in report_columns.json exists in registry.json
    3. registry.json should NOT have manual filter_schema (should be auto-derived)
    """
    warnings: List[str] = []

    # Load columns via the shared loader (handles split dir + key expansion)
    cols_data = _load_registry()
    if not cols_data:
        return ["Cannot load report_columns — no reports found"]

    try:
        with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
            reg_data = json.load(f)
    except Exception as e:
        return [f"Cannot load registry.json: {e}"]

    reg_reports = {r["report_key"] for r in reg_data.get("reports", [])}
    col_reports = set(cols_data.keys())

    # Check 1: reports in registry but not in columns
    missing_in_cols = reg_reports - col_reports
    for rk in missing_in_cols:
        warnings.append(f"Report '{rk}' in registry.json but NOT in report_columns.json")

    # Check 2: reports in columns but not in registry
    missing_in_reg = col_reports - reg_reports
    for rk in missing_in_reg:
        warnings.append(f"Report '{rk}' in report_columns.json but NOT in registry.json")

    # Check 3: registry should not have manual filter_schema
    for entry in reg_data.get("reports", []):
        if "filter_schema" in entry:
            warnings.append(
                f"Report '{entry['report_key']}' has manual filter_schema in registry.json "
                f"— remove it (auto-derived from report_columns.json)"
            )

    return warnings
