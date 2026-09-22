"""Catalog builders — extract metric/dimension/filter definitions and build the
compact semantic catalog text the LLM prompt consumes.

All functions read from the shared column registry (``_COLUMN_REGISTRY``) which
is populated from ``app/report_columns/*.json``. No LLM calls happen here.
"""
import logging
from typing import Any, Dict, List

from app.services.column_registry import _REGISTRY as _COLUMN_REGISTRY

logger = logging.getLogger(__name__)


def _get_valid_base_columns(report_key: str = "sales_report") -> set:
    """Return set of physical column names valid in ai_where clauses.

    Delegates to column_registry.get_valid_base_columns which includes
    DI.<column> refs from computed columns' expressions.
    """
    from app.services.column_registry import get_valid_base_columns
    return get_valid_base_columns(report_key)


# Cache per report
_VALID_COL_CACHE: Dict[str, set] = {}


def _get_cached_valid_columns(report_key: str = "sales_report") -> set:
    if report_key not in _VALID_COL_CACHE:
        _VALID_COL_CACHE[report_key] = _get_valid_base_columns(report_key)
    return _VALID_COL_CACHE[report_key]


def _load_report_columns() -> Dict[str, Any]:
    """Load report columns config (returns shared registry)."""
    return _COLUMN_REGISTRY


def _get_metric_catalog(report_key: str = "sales_report") -> Dict[str, Dict[str, Any]]:
    """Load the canonical metric catalog from report_columns.json."""
    cfg = _load_report_columns()
    report_cfg = cfg.get(report_key, {})
    return report_cfg.get("metric_catalog", {})


def _build_metric_catalog(report_key: str, cols: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract metric definitions for a report.

    Includes the semantic type (amount/weight/count/rate) from metric_catalog
    so the LLM can distinguish between monetary value and physical quantity.
    """
    metric_catalog = cols.get("metric_catalog", {})
    metrics = []
    columns = cols.get("columns", {})
    for name, meta in columns.items():
        if meta.get("filter_only") or meta.get("not_available"):
            continue
        if meta.get("type") in ("string", "date"):
            # Advertise text columns as unit="text" so the LLM can select them
            # for "give me the name" lookups. Without this the LLM guesses a
            # wrong numeric metric (e.g. unique_customers for "customer name").
            entry = {"name": name, "unit": "text"}
            if meta.get("description"):
                entry["label"] = meta["description"]
            metrics.append(entry)
            continue
        entry = {"name": name, "unit": "currency" if meta.get("type") == "decimal" else meta.get("type", "number")}
        if name in metric_catalog:
            entry["stype"] = metric_catalog[name].get("type", "amount")
        if meta.get("computed"):
            entry["computed"] = True
        if meta.get("description"):
            entry["label"] = meta["description"]
        metrics.append(entry)
    for name, meta in cols.get("special_metrics", {}).items():
        entry = {"name": name, "unit": "count", "special": True}
        if name in metric_catalog:
            entry["stype"] = metric_catalog[name].get("type", "count")
        if meta.get("description"):
            entry["label"] = meta["description"]
        metrics.append(entry)
    return metrics


def _build_dimension_catalog(report_key: str, cols: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract dimension definitions for a report."""
    dims = []
    columns = cols.get("columns", {})
    for name, meta in columns.items():
        if meta.get("type") != "string":
            continue
        if meta.get("filter_only") or meta.get("not_available"):
            continue
        entry = {"name": name}
        if meta.get("computed"):
            entry["computed"] = True
        if meta.get("description"):
            entry["label"] = meta["description"]
        dims.append(entry)
    return dims


def _build_filter_catalog(report_key: str, cols: Dict[str, Any]) -> List[Dict[str, str]]:
    """Extract filter definitions for a report."""
    filters = []
    columns = cols.get("columns", {})
    for name, meta in columns.items():
        if meta.get("not_available"):
            continue
        if not meta.get("filter_only") and meta.get("type") != "string":
            continue
        if meta.get("type") == "string" or meta.get("filter_only"):
            entry = {"name": name, "column": meta.get("sql", name)}
            if meta.get("description"):
                entry["label"] = meta["description"]
            filters.append(entry)
    return filters


def _build_prompt_column_sections(report_key: str = "sales_report") -> Dict[str, str]:
    """Auto-generate prompt sections from report_columns.json.

    Returns a dict with keys: base_columns, computed_metrics, filter_only, dimensions.
    This replaces hardcoded lists in the system prompt — scalable for any report.
    """
    cfg = _load_report_columns()
    cols = cfg.get(report_key, {})
    columns = cols.get("columns", {})

    base_cols = []
    computed_metrics = []
    filter_only_cols = []
    dimensions = []

    for name, meta in columns.items():
        if meta.get("not_available"):
            continue
        sql = meta.get("sql", name)
        if meta.get("filter_only"):
            filter_only_cols.append(name)
        elif meta.get("computed"):
            if meta.get("metric_expr"):
                computed_metrics.append(name)
            elif meta.get("dimension_expr"):
                dimensions.append(name)
        else:
            if meta.get("type") == "string":
                dimensions.append(name)
                base_cols.append(f"DI.{sql}")
            elif meta.get("type") not in ("date",):
                base_cols.append(f"DI.{sql}")

    return {
        "base_columns": ", ".join(base_cols),
        "computed_metrics": ", ".join(computed_metrics),
        "filter_only": ", ".join(filter_only_cols),
        "dimensions": ", ".join(dimensions),
    }


def build_semantic_catalog(report_name: str = "") -> str:
    """Build a compact text catalog of reports for the LLM prompt.

    If report_name is provided, only that report is included (frontend-specified).
    Otherwise all reports in report_columns.json are included.
    """
    cfg = _load_report_columns()
    lines = []

    # If frontend specified a report, use only that one
    if report_name and report_name in cfg:
        report_keys = [report_name]
    else:
        report_keys = list(cfg.keys())

    for report_key in report_keys:
        cols = cfg[report_key]
        desc = cols.get("description", report_key)

        lines.append(f"\n## Report: {report_key}")
        lines.append(f"Description: {desc}")

        metrics = _build_metric_catalog(report_key, cols)
        if metrics:
            lines.append("Metrics: " + ", ".join(
                m["name"]
                + (f" [{m['stype']}]" if m.get("stype") else "")
                + (f" ({m.get('label','')})" if m.get("label") else "")
                for m in metrics
            ))

        dims = _build_dimension_catalog(report_key, cols)
        if dims:
            lines.append("Dimensions: " + ", ".join(
                d["name"] + (f" ({d.get('label','')})" if d.get("label") else "")
                for d in dims
            ))

        filters = _build_filter_catalog(report_key, cols)
        if filters:
            lines.append("Filters: " + ", ".join(
                f["name"] for f in filters
            ))

    return "\n".join(lines)


# Cache the catalog per report_name so we don't rebuild it every call
_catalog_cache: Dict[str, str] = {}


def _get_catalog(report_name: str = "") -> str:
    cache_key = report_name or "__all__"
    if cache_key not in _catalog_cache:
        _catalog_cache[cache_key] = build_semantic_catalog(report_name)
    return _catalog_cache[cache_key]
