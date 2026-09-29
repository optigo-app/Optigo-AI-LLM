"""Validate all report column configs against the expected schema.

Run with:
    python scripts/validate_report_configs.py

This is a standalone linter; it does not need the FastAPI app to run.
"""
import json
import os
import re
import sys
from typing import Any, Dict, List, Set, Tuple


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COLUMNS_DIR = os.path.join(ROOT, "app", "report_columns")

REQUIRED_TOP_LEVEL_KEYS = {
    "description", "sp", "report_id", "default_metric", "tables",
    "base_filter", "columns", "special_metrics", "filter_key_map",
}
OPTIONAL_TOP_LEVEL_KEYS = {
    "default_dimension", "metric_catalog", "intents", "canonical_values",
    "report_keywords", "prompt_rules", "location_aliases", "party_type_rules",
    "chat_enabled", "fallback_intent", "name_filter_map", "date_column",
    "suggested_questions", "table_filters", "source_query_file",
    "filter_invalid_values", "dimension_aliases",
}
OPTIONAL_COLUMN_KEYS = {
    "sql", "type", "desc", "description", "grp", "business_group",
    "unit", "aliases", "filter", "calc", "computed", "me", "metric_expr",
    "de", "dimension_expr", "filter_only", "fo", "not_available",
    "not_in_where", "computed_only", "label", "table_metric_exprs",
    "table_dimension_exprs",
}
OPTIONAL_INTENT_KEYS = {
    "patterns", "metric", "metric_key", "aggregation", "dimension", "sort",
    "limit", "unit", "is_field_metric", "override_metric", "clear_filters", "override_filters",
    "state", "state_reason", "label",
}

# Short keys expanded by app/services/column_registry.py
SHORT_EXPR_KEYS = {"me", "de"}
EXPR_KEYS = {"metric_expr", "dimension_expr"} | SHORT_EXPR_KEYS
_BLOCKED_EXPR_KEYWORDS = re.compile(
    r"\b(DROP|DELETE|INSERT|UPDATE|EXEC|EXECUTE|XP_CMDSHELL|ALTER|CREATE|GRANT|TRUNCATE|MERGE|OPENROWSET|OPENDATASOURCE)\b",
    re.IGNORECASE,
)


def _load_json(path: str) -> Tuple[Any, List[str]]:
    errors: List[str] = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), errors
    except json.JSONDecodeError as exc:
        errors.append(f"Invalid JSON: {exc}")
        return {}, errors
    except OSError as exc:
        errors.append(f"Cannot read file: {exc}")
        return {}, errors


def _check_required_keys(report_key: str, cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    missing = REQUIRED_TOP_LEVEL_KEYS - set(cfg.keys())
    if missing:
        errors.append(f"Missing required keys: {sorted(missing)}")
    unknown = set(cfg.keys()) - REQUIRED_TOP_LEVEL_KEYS - OPTIONAL_TOP_LEVEL_KEYS
    if unknown:
        warnings.append(f"Unknown top-level keys (ignored): {sorted(unknown)}")
    return errors, warnings


def _check_types(report_key: str, cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    type_checks = {
        "description": str,
        "sp": int,
        "report_id": int,
        "default_metric": str,
        "default_dimension": str,
        "date_column": str,
        "tables": list,
        "base_filter": str,
        "table_filters": dict,
        "columns": dict,
        "special_metrics": dict,
        "filter_key_map": dict,
        "metric_catalog": dict,
        "intents": dict,
        "canonical_values": dict,
        "report_keywords": list,
    }
    for key, expected_type in type_checks.items():
        value = cfg.get(key)
        if value is not None and not isinstance(value, expected_type):
            errors.append(f"'{key}' should be {expected_type.__name__}, got {type(value).__name__}")

    source_query_file = cfg.get("source_query_file")
    if source_query_file is not None:
        if not isinstance(source_query_file, str) or not source_query_file.strip():
            errors.append("'source_query_file' must be a non-empty string")
        else:
            sq_path = os.path.join(ROOT, "app", "report_queries", source_query_file)
            if not os.path.isfile(sq_path):
                errors.append(f"'source_query_file' not found: {sq_path}")

    table_filters = cfg.get("table_filters")
    if isinstance(table_filters, dict):
        unknown_tables = set(table_filters) - set(cfg.get("tables", []))
        if unknown_tables:
            errors.append(f"'table_filters' contains unknown tables: {sorted(unknown_tables)}")
        for table, expression in table_filters.items():
            if not isinstance(expression, str) or not expression.strip():
                errors.append(f"'table_filters' entry for '{table}' must be a non-empty string")
            elif any(token in expression for token in (";", "--", "/*", "*/")) or _BLOCKED_EXPR_KEYWORDS.search(expression):
                errors.append(f"'table_filters' entry for '{table}' contains blocked SQL tokens")
    return errors, warnings


def _has_expression(meta: Dict[str, Any]) -> bool:
    return any(meta.get(k) for k in EXPR_KEYS) or bool(meta.get("table_metric_exprs")) or bool(meta.get("table_dimension_exprs"))


def _check_columns(report_key: str, cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    columns = cfg.get("columns", {})
    special = cfg.get("special_metrics", {})

    for col_name, meta in columns.items():
        if not isinstance(meta, dict):
            errors.append(f"Column '{col_name}' metadata must be a dict")
            continue
        col_type = meta.get("type", "")
        if not col_type:
            errors.append(f"Column '{col_name}' missing 'type'")
        unknown_col_keys = set(meta.keys()) - OPTIONAL_COLUMN_KEYS
        if unknown_col_keys:
            warnings.append(
                f"Column '{col_name}' has unknown keys: {sorted(unknown_col_keys)}"
            )
        if meta.get("calc") and not _has_expression(meta):
            errors.append(
                f"Computed column '{col_name}' must have 'metric_expr'/'me' or 'dimension_expr'/'de'"
            )
        if meta.get("filter") and not isinstance(meta["filter"], dict):
            errors.append(f"Column '{col_name}' 'filter' must be a dict")
        for flag in ("not_in_where", "computed_only"):
            if flag in meta and not isinstance(meta[flag], bool):
                errors.append(f"Column '{col_name}' '{flag}' must be boolean")
        for expr_key in ("table_metric_exprs", "table_dimension_exprs"):
            table_exprs = meta.get(expr_key)
            if table_exprs is None:
                continue
            if not isinstance(table_exprs, dict) or not table_exprs:
                errors.append(f"Column '{col_name}' '{expr_key}' must be a non-empty dict")
                continue
            unknown_tables = set(table_exprs) - set(cfg.get("tables", []))
            if unknown_tables:
                errors.append(f"Column '{col_name}' has {expr_key} for unknown tables: {sorted(unknown_tables)}")
            missing_tables = set(cfg.get("tables", [])) - set(table_exprs)
            if missing_tables:
                errors.append(f"Column '{col_name}' missing {expr_key} for tables: {sorted(missing_tables)}")
            for table, expression in table_exprs.items():
                if not isinstance(expression, str) or not expression.strip():
                    errors.append(f"Column '{col_name}' {expr_key} for '{table}' must be a non-empty string")
                elif any(token in expression for token in (";", "--", "/*", "*/")) or _BLOCKED_EXPR_KEYWORDS.search(expression):
                    errors.append(f"Column '{col_name}' {expr_key} for '{table}' contains blocked SQL tokens")

    for sm_name, meta in special.items():
        if not isinstance(meta, dict):
            errors.append(f"Special metric '{sm_name}' metadata must be a dict")
            continue
        if "type" not in meta:
            errors.append(f"Special metric '{sm_name}' missing 'type'")

    return errors, warnings


def _check_metric_catalog(report_key: str, cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    columns = cfg.get("columns", {})
    special = cfg.get("special_metrics", {})
    catalog = cfg.get("metric_catalog", {})
    chat_enabled = cfg.get("chat_enabled", True)

    if not catalog:
        if chat_enabled is not False:
            warnings.append(
                "'metric_catalog' is missing; chat-mode reports should define one"
            )
        return errors, warnings

    if not isinstance(catalog, dict):
        errors.append("'metric_catalog' must be a dict")
        return errors, warnings

    for metric_name, meta in catalog.items():
        if not isinstance(meta, dict):
            errors.append(f"metric_catalog entry '{metric_name}' must be a dict")
            continue
        if metric_name not in columns and metric_name not in special:
            warnings.append(
                f"metric_catalog '{metric_name}' not found in columns or special_metrics"
            )
        if "type" not in meta:
            errors.append(f"metric_catalog '{metric_name}' missing 'type'")
        if "label" not in meta or not meta["label"]:
            warnings.append(f"metric_catalog '{metric_name}' missing or empty 'label'")
        if not isinstance(meta.get("aliases", []), list):
            errors.append(f"metric_catalog '{metric_name}' 'aliases' must be a list")

    return errors, warnings


def _check_name_filter_map(report_key: str, cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    name_filter_map = cfg.get("name_filter_map", {})
    if not name_filter_map:
        return errors, warnings
    if not isinstance(name_filter_map, dict):
        errors.append("'name_filter_map' must be a dict")
        return errors, warnings
    for key, expr in name_filter_map.items():
        if not isinstance(expr, str):
            errors.append(f"name_filter_map['{key}'] must be a string SQL expression")
            continue
        if not expr.strip():
            errors.append(f"name_filter_map['{key}'] expression is empty")
    return errors, warnings


def _check_filter_key_map(report_key: str, cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    columns = cfg.get("columns", {})
    filter_map = cfg.get("filter_key_map", {})
    name_filter_map = cfg.get("name_filter_map", {})

    for alias, target in filter_map.items():
        if target in ("_date",):
            continue
        if target in name_filter_map:
            continue
        if target not in columns:
            errors.append(
                f"filter_key_map alias '{alias}' -> '{target}' not found in columns or name_filter_map"
            )

    return errors, warnings


def _check_intents(report_key: str, cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    columns = cfg.get("columns", {})
    special = cfg.get("special_metrics", {})
    catalog = cfg.get("metric_catalog", {})
    intents = cfg.get("intents", {})

    if not isinstance(intents, dict):
        errors.append("'intents' must be a dict")
        return errors, warnings

    for intent_name, meta in intents.items():
        if not isinstance(meta, dict):
            errors.append(f"Intent '{intent_name}' must be a dict")
            continue
        unknown_intent_keys = set(meta.keys()) - OPTIONAL_INTENT_KEYS
        if unknown_intent_keys:
            warnings.append(
                f"Intent '{intent_name}' has unknown keys: {sorted(unknown_intent_keys)}"
            )
        for req in ("patterns", "metric", "aggregation"):
            if req not in meta:
                errors.append(f"Intent '{intent_name}' missing '{req}'")
        metric = meta.get("metric", "")
        if metric and metric not in columns and metric not in special and metric not in catalog:
            errors.append(
                f"Intent '{intent_name}' metric '{metric}' not found in columns/special/catalog"
            )
        dimension = meta.get("dimension", "")
        if dimension and dimension not in columns and dimension not in special:
            errors.append(
                f"Intent '{intent_name}' dimension '{dimension}' not found in columns/special"
            )
        agg = meta.get("aggregation", "")
        if agg and agg not in {"sum", "count", "count_distinct", "avg", "max", "min"}:
            errors.append(f"Intent '{intent_name}' has invalid aggregation '{agg}'")
        patterns = meta.get("patterns", [])
        if not isinstance(patterns, list):
            errors.append(f"Intent '{intent_name}' 'patterns' must be a list")
            continue
        for pat in patterns:
            try:
                re.compile(pat)
            except re.error as exc:
                errors.append(f"Intent '{intent_name}' invalid regex '{pat}': {exc}")

    return errors, warnings


def _check_canonical_values(report_key: str, cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    canonical = cfg.get("canonical_values", {})
    if not isinstance(canonical, dict):
        errors.append("'canonical_values' must be a dict")
        return errors, warnings

    columns = cfg.get("columns", {})
    for field, mapping in canonical.items():
        if not isinstance(mapping, dict):
            errors.append(f"canonical_values['{field}'] must be a dict")
            continue
        if field not in columns:
            warnings.append(f"canonical_values field '{field}' not found in columns")

    return errors, warnings


def _resolve_alias_target(
    alias: str,
    col_name: str,
    meta: Dict[str, Any],
    catalog: Dict[str, Any],
) -> str:
    """Return the canonical target for an alias for duplicate detection.

    Columns that share the same `sql` are treated as the same target, because they
    resolve to the same physical column in the SP. If a column name is also a
    metric_catalog key, aliases from both map to the same canonical metric.
    """
    sql = meta.get("sql", col_name)
    if sql:
        return f"sql:{sql.lower()}"
    if col_name in catalog:
        return f"metric:{col_name}"
    return f"column:{col_name}"


def _check_duplicate_aliases(report_key: str, cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    errors: List[str] = []
    warnings: List[str] = []
    columns = cfg.get("columns", {})
    catalog = cfg.get("metric_catalog", {})

    # Filter aliases: same alias mapping to different physical SQL columns is ambiguous.
    filter_alias_to_sql: Dict[str, str] = {}
    for col_name, meta in columns.items():
        if not isinstance(meta, dict):
            continue
        sql = meta.get("sql", col_name)
        # Register the column name itself as a filterable term if it's a string dimension
        if meta.get("type") == "string":
            for alias in [col_name] + meta.get("aliases", []):
                lowered = alias.lower().strip()
                if lowered in filter_alias_to_sql and filter_alias_to_sql[lowered] != sql:
                    errors.append(
                        f"Ambiguous filter alias '{alias}' maps to SQL '{filter_alias_to_sql[lowered]}' and '{sql}'"
                    )
                else:
                    filter_alias_to_sql[lowered] = sql
        f = meta.get("filter")
        if isinstance(f, dict):
            for alias in f.get("aliases", []):
                lowered = alias.lower().strip()
                if lowered in filter_alias_to_sql and filter_alias_to_sql[lowered] != sql:
                    errors.append(
                        f"Ambiguous filter alias '{alias}' maps to SQL '{filter_alias_to_sql[lowered]}' and '{sql}'"
                    )
                else:
                    filter_alias_to_sql[lowered] = sql

    # Metric aliases: same alias mapping to different metric names is ambiguous.
    metric_alias_to_metric: Dict[str, str] = {}
    for metric_name, meta in catalog.items():
        if not isinstance(meta, dict):
            continue
        for alias in [metric_name] + meta.get("aliases", []):
            lowered = alias.lower().strip()
            if lowered in metric_alias_to_metric and metric_alias_to_metric[lowered] != metric_name:
                errors.append(
                    f"Ambiguous metric alias '{alias}' maps to '{metric_alias_to_metric[lowered]}' and '{metric_name}'"
                )
            else:
                metric_alias_to_metric[lowered] = metric_name

    return errors, warnings


def validate_report_config(report_key: str, cfg: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """Return (errors, warnings) for a single report config."""
    errors: List[str] = []
    warnings: List[str] = []

    checks = [
        _check_required_keys,
        _check_types,
        _check_columns,
        _check_metric_catalog,
        _check_filter_key_map,
        _check_name_filter_map,
        _check_intents,
        _check_canonical_values,
        _check_duplicate_aliases,
    ]

    for check in checks:
        e, w = check(report_key, cfg)
        errors.extend(e)
        warnings.extend(w)

    return errors, warnings


def main() -> int:
    if not os.path.isdir(COLUMNS_DIR):
        print(f"ERROR: Directory not found: {COLUMNS_DIR}")
        return 1

    exit_code = 0
    total_reports = 0
    total_errors = 0
    total_warnings = 0

    for fname in sorted(os.listdir(COLUMNS_DIR)):
        fpath = os.path.join(COLUMNS_DIR, fname)
        if os.path.isdir(fpath) or not fname.endswith(".json"):
            continue
        report_key = fname[:-5]
        total_reports += 1
        cfg, load_errors = _load_json(fpath)
        if load_errors:
            print(f"\n[FAIL] {fname}")
            for err in load_errors:
                print(f"  - {err}")
            total_errors += len(load_errors)
            exit_code = 1
            continue

        errors, warnings = validate_report_config(report_key, cfg)
        if errors or warnings:
            status = "FAIL" if errors else "WARN"
            print(f"\n[{status}] {fname}")
            for err in errors:
                print(f"  - {err}")
            for warn in warnings:
                print(f"  ~ {warn}")
            total_errors += len(errors)
            total_warnings += len(warnings)
            if errors:
                exit_code = 1
        else:
            print(f"[OK]   {fname}")

    print(f"\n{total_reports} report(s) checked, {total_errors} error(s), {total_warnings} warning(s) found.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
