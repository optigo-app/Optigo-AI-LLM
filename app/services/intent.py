"""Intent + metric mapping layer.

This module turns a free-form question into a structured, deterministic intent
spec. The answer generator uses the spec to pick the right metric, aggregation
and dimension instead of asking the LLM to do it.

The mapping is report-specific and uses keyword/regex rules with canonical
filters. It does NOT rely on the LLM to choose the aggregation or metric.

Patterns, canonical values, and fallback intents are loaded from each report's
JSON config in ``app/report_columns/*.json``. The legacy ``app/intent_config.json``
is still read as a fallback for reports that do not yet define their own intents.

Fallback chain when no regex matches:
  1. Config-driven fallback intents (keyword matching)
  2. Column-name matching from report_columns.json (sync, deterministic)
  3. LLM intent detection (async, called from _chat_impl if still unknown)
"""
import json
import logging
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


logger = logging.getLogger(__name__)

# ── Config loader ────────────────────────────────────────────────────────────
_CONFIG_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "intent_config.json")


def _load_config() -> Dict[str, Any]:
    """Load legacy intent configuration from JSON file."""
    try:
        with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


from app.services.column_registry import (
    _load_registry as _load_column_registry,
    get_intent_patterns,
    get_canonical_values,
    get_fallback_intent,
    get_location_aliases,
    get_generic_routing_terms,
)


_CONFIG = _load_config()
_COLUMN_REGISTRY = _load_column_registry()

# Legacy compiled-pattern cache, kept for backward compatibility.
_PATTERN_CACHE: Dict[str, List[Tuple[re.Pattern, Dict[str, Any]]]] = {}


def _get_intent_patterns(report_key: str) -> List[Tuple[re.Pattern, Dict[str, Any]]]:
    """Return compiled (regex, rule) tuples for a report, preferring per-report config.

    Order of lookup:
      1. Per-report `intents` in report_columns/<report_key>.json.
      2. Legacy intent_config.json `patterns` for the same report_key.
    """
    if report_key not in _PATTERN_CACHE:
        patterns = get_intent_patterns(report_key)
        if not patterns:
            # Legacy fallback for reports not yet migrated to per-report config.
            raw_patterns = _CONFIG.get("patterns", {}).get(report_key, [])
            compiled = []
            for entry in raw_patterns:
                regex_str = entry["regex"]
                rule = {k: v for k, v in entry.items() if k != "regex"}
                compiled.append((re.compile(regex_str), rule))
            patterns = compiled
        _PATTERN_CACHE[report_key] = patterns
    return _PATTERN_CACHE[report_key]


def reload_config() -> None:
    """Reload the intent config from disk (for hot-reload scenarios)."""
    global _CONFIG, _COLUMN_REGISTRY, _PATTERN_CACHE, _DIM_KEYWORDS_CACHE
    _CONFIG = _load_config()
    _COLUMN_REGISTRY = _load_column_registry()
    _PATTERN_CACHE = {}
    _DIM_KEYWORDS_CACHE = {}


# Resolve registry key ↔ intent-config key. New reports use the same key in both
# places; these aliases exist only for the original sales/wip split.
_REPORT_KEY_ALIAS = {
    "sales_report": "sales_summary",
    "wip_report": "wip_summary",
}


# Reports with defined intents, in deterministic evaluation order. New reports
# that define `report_keywords` and `intents` are included automatically.
def _report_order() -> List[str]:
    """Return the ordered list of report keys to check for explicit intent matches.

    Legacy ``intent_config.json`` report_order is used as a base; any report in
    ``report_columns/*.json`` that has intents or report_keywords is appended if
    not already present. Duplicate keys (e.g. legacy alias + registry key) are
    collapsed using the registry key as the canonical representation.
    """
    # Map legacy/intent-config names to registry keys
    alias_to_registry = {v: k for k, v in _REPORT_KEY_ALIAS.items()}

    base: List[str] = []
    present: Set[str] = set()

    def _add(key: str) -> None:
        canonical = alias_to_registry.get(key, key)
        if canonical not in present:
            base.append(canonical)
            present.add(canonical)

    for rk in _CONFIG.get("report_order", ["sales_summary", "purchase_summary", "party_outstanding", "stock_ledger", "order_status"]):
        _add(rk)

    for rk, cfg in _COLUMN_REGISTRY.items():
        if cfg.get("intents") or cfg.get("report_keywords"):
            _add(rk)

    return base


@dataclass
class IntentSpec:
    """Deterministic intent result for a question/report pair."""
    report_key: str
    intent: str = "unknown"
    metric_key: str = ""
    aggregation: str = "sum"  # sum, count, avg, max, min, list
    dimension: str = ""       # for ranking / group-by questions
    sort: str = "desc"
    limit: int = 1
    unit: str = ""
    unit_label: str = ""  # display suffix like "ctw", "pcs", "g", "%"
    is_field_metric: bool = False  # metric refers to a column/field directly
    override_metric: bool = False  # deterministic intent may replace a parser-selected metric
    state: str = "SUCCESS"
    state_reason: str = ""
    clear_filters: Dict[str, Any] = None  # filters to remove because they were hallucinated
    override_filters: Dict[str, Any] = None  # filters to enforce/replace
    ai_where: str = ""  # LLM-generated WHERE clause (kept for entity extraction in answers)

    def __post_init__(self):
        if self.clear_filters is None:
            self.clear_filters = {}
        if self.override_filters is None:
            self.override_filters = {}


def _report_patterns(report_key: str) -> List[Tuple[re.Pattern, Dict[str, Any]]]:
    """Return compiled regex patterns for a report (registry or intent-config key)."""
    return _get_intent_patterns(report_key)


def _apply_pattern(spec: IntentSpec, rule: Dict[str, Any]) -> IntentSpec:
    """Copy rule fields into a new spec.

    Supports both per-report config keys ("metric") and legacy intent_config keys
    ("metric_key"). Other legacy keys like "limit", "aggregation", etc. are copied
    verbatim.
    """
    # Map per-report intent schema to IntentSpec fields
    if "metric" in rule:
        spec.metric_key = rule["metric"]
    for key in [
        "intent", "metric_key", "aggregation", "dimension", "sort", "limit",
        "unit", "is_field_metric", "override_metric", "state", "state_reason", "clear_filters", "override_filters"
    ]:
        if key in rule:
            setattr(spec, key, rule[key])
    return spec


def _branch_from_question(question: str, report_key: str) -> Optional[str]:
    """If the user names a branch directly, normalize it using per-report aliases."""
    lowered = question.lower()
    aliases = get_location_aliases(report_key) or {}
    # Order matters: longer phrases first so "head office" beats "head".
    for alias in sorted(aliases, key=len, reverse=True):
        if alias.lower() in lowered:
            return aliases[alias]
    return None


def _party_type_from_question(question: str) -> Optional[str]:
    """Infer customer/supplier from a party_outstanding question."""
    lowered = question.lower()
    if "supplier" in lowered or "payable" in lowered or "owe to" in lowered:
        return "supplier"
    if "customer" in lowered or "receivable" in lowered or "owe us" in lowered or "owe from" in lowered:
        return "customer"
    return None




def _detect_explicit_intent(question: str, report_key: str) -> Optional[IntentSpec]:
    """Detect an explicit rule-based intent; return None if no rule matches."""
    lowered = question.lower()
    patterns = _report_patterns(report_key)
    for pattern, rule in patterns:
        if pattern.search(lowered):
            spec = IntentSpec(report_key=report_key)
            spec = _apply_pattern(spec, rule)
            return spec
    return None


def classify_question_by_intent(question: str, registry: Dict[str, Any]) -> Optional[str]:
    """Return the registry report key that an explicit intent rule maps to.

    This runs before embedding/LLM classifiers so deterministic customer-sales,
    ranking, and field-metric questions cannot be mis-routed.

    The evaluation order is derived from reports that define `intents` or
    `report_keywords` in their report_columns/*.json config (plus the legacy
    ``intent_config.json`` order for backward compatibility). The registry may
    be keyed by either intent-config names or registry names
    (e.g. sales_report, wip_report). We check both so Stage 0 can match
    regardless of which key set the caller used, and always return the
    canonical registry key.
    """
    # Build reverse alias: intent-config key → registry key
    _intent_to_registry = {v: k for k, v in _REPORT_KEY_ALIAS.items()}
    lowered = question.lower()

    # ── Keyword-based prioritization: score each report by how many of its
    # keywords appear in the question. Reports with more keyword matches are
    # checked first. This prevents broad intents in earlier reports (e.g.
    # sales_report's "gross weight") from stealing questions that clearly
    # belong to a later report (e.g. "total gross weight of orders" → order_report
    # because "order"+"orders" = 2 hits vs "gross weight" = 1 hit).
    ordered = _report_order()
    generic_terms = get_generic_routing_terms()
    scored: List[tuple] = []  # (score, original_index, report_key)
    for idx, rk in enumerate(ordered):
        kws = _COLUMN_REGISTRY.get(rk, {}).get("report_keywords", [])
        # Generic entity nouns ("jobs", "records") match almost every ERP
        # question — count them at half weight so a distinctive keyword
        # ("quote" -> order_report) outranks a generic hit on a tied intent.
        score = sum(0.5 if kw.lower() in generic_terms else 1.0
                    for kw in kws if kw in lowered)
        scored.append((score, idx, rk))
    # Sort by score descending, then by original order (stable)
    scored.sort(key=lambda x: (-x[0], x[1]))
    ordered = [rk for _, _, rk in scored]

    for report_key in ordered:
        # report_key is now always the canonical registry key
        registry_key = report_key
        # Match if either the registry key or its legacy intent-config alias is present
        if registry_key not in registry and _REPORT_KEY_ALIAS.get(registry_key, registry_key) not in registry:
            continue
        spec = _detect_explicit_intent(question, registry_key)
        if spec is not None:
            return registry_key
    return None


def detect_intent(question: str, report_key: str) -> IntentSpec:
    """Detect deterministic intent from a natural-language question.

    Fallback chain:
      1. Explicit regex patterns from intent_config.json
      2. Config-driven fallback intents (keyword matching)
      3. Column-name matching from report_columns.json (sync, deterministic)
    """
    lowered = question.lower()
    spec = _detect_explicit_intent(question, report_key)
    if spec is None:
        spec = IntentSpec(report_key=report_key)

    # Fallback 1: config-driven fallback intents
    if spec.intent == "unknown":
        fallback = get_fallback_intent(report_key)
        if not fallback:
            # Legacy fallback for reports not yet migrated to per-report config.
            intent_key = _REPORT_KEY_ALIAS.get(report_key, report_key)
            fallback = _CONFIG.get("fallback_intents", {}).get(intent_key) or _CONFIG.get("fallback_intents", {}).get(report_key)
        if fallback:
            keywords = fallback.get("keywords", fallback.get("patterns", []))
            if any(w in lowered for w in keywords):
                spec.intent = fallback.get("intent", "total_sales")
                spec.metric_key = fallback.get("metric_key", fallback.get("metric", "totalAmount"))
                spec.aggregation = fallback.get("aggregation", "sum")
                spec.unit = fallback.get("unit", "currency")

    # Fallback 2: column-name matching from report_columns.json
    if spec.intent == "unknown":
        spec = _column_name_fallback(question, report_key, spec)

    # If intent was matched but no dimension was set, try keyword dimension detection
    if spec.intent != "unknown" and not spec.dimension:
        dim = _detect_dimension(lowered, report_key)
        if dim:
            spec.dimension = dim
            spec.sort = "desc"
            spec.limit = max(spec.limit, 5)

    # Extract "top N" / "best N" / "biggest N" limit from the question
    if spec.dimension and spec.intent != "unknown":
        m = re.search(r"\b(?:top|best|biggest|highest|worst|lowest)\s+(\d+)\b", lowered)
        if m:
            spec.limit = int(m.group(1))

    return spec


# ── Aggregation keyword detection ────────────────────────────────────────────
_AGG_KEYWORDS: List[Tuple[str, str]] = [
    ("average", "avg"), ("avg", "avg"), ("mean", "avg"),
    ("max", "max"), ("maximum", "max"), ("highest", "max"), ("biggest", "max"),
    ("largest", "max"), ("most", "max"),
    ("min", "min"), ("minimum", "min"), ("lowest", "min"),
    ("smallest", "min"), ("least", "min"),
    ("how many", "count"), ("count of", "count"), ("number of", "count"),
    ("total count", "count"),
    ("total", "sum"), ("sum", "sum"), ("overall", "sum"),
]

def _build_dimension_keywords(report_key: str) -> List[Tuple[str, str]]:
    """Build dimension keyword tuples for a report from its column metadata.

    For every string-type, non-filter-only column we generate the common
    grouping phrases ("by X", "X wise", "X-wise", "per X", "top X"). Aliases
    from column metadata and filter aliases are included so the user can say
    "by customer name" or "product wise" without hardcoding report-specific
    phrases in Python.
    """
    report_cfg = _COLUMN_REGISTRY.get(report_key, {})
    columns = report_cfg.get("columns", {})
    keywords: List[Tuple[str, str]] = []
    seen_dim: Set[str] = set()

    for col_name, meta in columns.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("filter_only") or meta.get("not_available"):
            continue
        if meta.get("type") != "string" and not meta.get("dimension"):
            continue
        # Use the column KEY as the canonical dimension target — _build_p and
        # resolve_sp_params resolve keys to sql/dimension_expr. (sql names and
        # empty sql on computed columns previously produced unusable targets.)
        dim_target = col_name
        seen_dim.add(dim_target)

        # Collect all names the user might use for this dimension
        aliases: Set[str] = set()
        aliases.add(col_name)
        aliases.update(meta.get("aliases", []))
        f = meta.get("filter", {})
        if isinstance(f, dict):
            aliases.update(f.get("aliases", []))

        for alias in aliases:
            al = alias.lower().strip()
            if not al:
                continue
            # Avoid ambiguous short aliases becoming standalone dimension phrases
            if len(al) < 2:
                continue
            keywords.append((f"by {al}", dim_target))
            keywords.append((f"per {al}", dim_target))
            keywords.append((f"{al} wise", dim_target))
            keywords.append((f"{al}-wise", dim_target))
            keywords.append((f"top {al}", dim_target))
            # Question-framing patterns: "which salesperson", "best category",
            # "customer generating", "branch ranking", etc.
            keywords.append((f"which {al}", dim_target))
            keywords.append((f"highest {al}", dim_target))
            keywords.append((f"best {al}", dim_target))
            keywords.append((f"most {al}", dim_target))
            keywords.append((f"{al} generating", dim_target))
            keywords.append((f"{al} having", dim_target))
            keywords.append((f"{al} ranking", dim_target))
            keywords.append((f"{al} performance", dim_target))

    # Legacy hardcoded sales-specific phrases are kept as fallback until each
    # report fully populates aliases. They are appended after dynamic ones so
    # report config takes precedence.
    legacy = [
        ("by customer", "CustomerName"), ("per customer", "CustomerName"),
        ("customer wise", "CustomerName"), ("top customer", "CustomerFullName"),
        ("by category", "categoryname"), ("per category", "categoryname"),
        ("category wise", "categoryname"), ("category-wise", "categoryname"),
        ("by subcategory", "subcategoryname"), ("per subcategory", "subcategoryname"),
        ("subcategory wise", "subcategoryname"), ("by sub category", "subcategoryname"),
        ("sub category wise", "subcategoryname"), ("subcategory-wise", "subcategoryname"),
        ("sub-category-wise", "subcategoryname"),
        ("by product", "producttypename"), ("product wise", "producttypename"),
        ("by metal", "goldtypename"), ("metal wise", "goldtypename"),
        ("by brand", "mastermanagement_brandname"), ("brand wise", "mastermanagement_brandname"),
        ("by sales rep", "SalesRep"), ("sales rep wise", "SalesRep"),
        ("by supplier", "Manufacturer"), ("by manufacturer", "Manufacturer"),
        ("by status", "statusname"), ("status wise", "statusname"),
        ("by order type", "OrderTypeName"), ("order type wise", "OrderTypeName"),
        ("by customer type", "CustomerType"), ("customer type wise", "CustomerType"),
        ("by collection", "collection"), ("collection wise", "collection"),
        ("by gender", "gender"), ("gender wise", "gender"),
        ("by occasion", "occasion"), ("occasion wise", "occasion"),
        ("by style", "style"), ("style wise", "style"),
    ]
    for phrase, dim in legacy:
        if dim in seen_dim:
            continue
        keywords.append((phrase, dim))

    return keywords


_DIM_KEYWORDS_CACHE: Dict[str, List[Tuple[str, str]]] = {}


def _detect_aggregation(lowered: str) -> str:
    """Detect aggregation from keywords in the question."""
    for keyword, agg in _AGG_KEYWORDS:
        if keyword in lowered:
            return agg
    return "sum"


_PERSON_TERMS = {
    "salesperson", "sales person", "sales rep", "salesrep", "employee",
    "staff", "worker", "karigar", "agent", "representative", "seller",
}
_PERSON_QUESTION_WORDS = (
    "who", "salesperson", "sales person", "employee", "staff",
    "agent", "worker", "karigar", "representative",
)
_RANK_WORDS = (
    "most", "highest", "best", "top", "generat", "sales", "revenue",
    "sold", "performance", "ranking", "orders",
)


def _find_person_dimension(report_key: str) -> str:
    """Return the report's person/worker dimension column key, if any.

    Scans columns for a string, non-filter-only column whose name or aliases
    include person terms (salesperson/employee/worker/...). Used to resolve
    'who generated the most ...' style questions.
    """
    report_cfg = _COLUMN_REGISTRY.get(report_key, {})
    columns = report_cfg.get("columns", {})
    for col_name, meta in columns.items():
        if not isinstance(meta, dict):
            continue
        if meta.get("filter_only") or meta.get("not_available"):
            continue
        if meta.get("type") != "string":
            continue
        names = {col_name.lower()}
        names.update(a.lower() for a in meta.get("aliases", []))
        f = meta.get("filter", {})
        if isinstance(f, dict):
            names.update(a.lower() for a in f.get("aliases", []))
        if names & _PERSON_TERMS:
            return col_name
    return ""


def _detect_dimension(lowered: str, report_key: str) -> str:
    """Detect dimension (group-by) from keywords in the question."""
    if report_key not in _DIM_KEYWORDS_CACHE:
        _DIM_KEYWORDS_CACHE[report_key] = _build_dimension_keywords(report_key)
    # Normalize hyphens to spaces so "branch-wise" matches "branch wise"
    normalized = lowered.replace("-", " ")
    # Sort by phrase length descending so "by customer name" beats "by customer"
    for keyword, dim in sorted(_DIM_KEYWORDS_CACHE[report_key], key=lambda x: len(x[0]), reverse=True):
        if keyword in normalized:
            return dim
    # Entity/person fallback: "who generated the most revenue",
    # "which employee sold the most" → the report's person dimension.
    if (any(w in normalized for w in _PERSON_QUESTION_WORDS)
            and any(r in normalized for r in _RANK_WORDS)):
        return _find_person_dimension(report_key)
    return ""


def _column_name_fallback(question: str, report_key: str, spec: IntentSpec) -> IntentSpec:
    """Try to match column names/aliases from report_columns.json against the question.

    This is a deterministic sync fallback that runs when no regex pattern matched.
    It scans the question for any column name or alias and picks the best match.
    """
    report_cols = _COLUMN_REGISTRY.get(report_key, {})
    columns = report_cols.get("columns", {})
    special = report_cols.get("special_metrics", {})
    lowered = question.lower()

    # Build a list of (alias, column_name, type) for matching
    candidates: List[Tuple[str, str, str]] = []
    for col_name, meta in columns.items():
        if not isinstance(meta, dict):
            continue
        col_type = meta.get("type", "")
        # Skip filter-only columns (not metrics)
        if meta.get("filter_only"):
            continue
        # Add the column name itself as a candidate
        candidates.append((col_name.lower(), col_name, col_type))
        # Add common variations
        if col_name.lower() != col_name:
            candidates.append((col_name.lower(), col_name, col_type))

    # Also check special metrics (total_count, unique_customers, etc.)
    for col_name, meta in special.items():
        if not isinstance(meta, dict):
            continue
        col_type = meta.get("type", "count")
        candidates.append((col_name.lower(), col_name, col_type))

    # Add metric_catalog aliases so users can ask with business terms
    # (e.g. "bill count" for total_count, "unique customer" for unique_customers)
    catalog = report_cols.get("metric_catalog", {})
    for metric_name, meta in catalog.items():
        if not isinstance(meta, dict):
            continue
        target_name = metric_name
        if target_name not in columns and target_name not in special:
            # Catalog-only metric that maps to a physical column or special metric
            # Heuristic: prefer special metric if name matches
            target_name = metric_name
        catalog_type = meta.get("type", "decimal")
        for alias in [metric_name] + meta.get("aliases", []):
            candidates.append((alias.lower(), target_name, catalog_type))

    # Find the best matching column (longest match wins to avoid partial matches)
    best_match: Optional[Tuple[str, str, str]] = None
    best_len = 0
    for alias, col_name, col_type in candidates:
        if alias in lowered and len(alias) > best_len:
            best_match = (alias, col_name, col_type)
            best_len = len(alias)

    if best_match is None:
        return spec

    _, col_name, col_type = best_match

    # Determine aggregation from keywords
    aggregation = _detect_aggregation(lowered)

    # Determine unit from column type
    if col_type == "count":
        unit = "count"
    elif col_type == "decimal":
        unit = "currency"
    elif col_type == "int":
        unit = "count"
    else:
        unit = ""

    # Determine dimension from keywords
    dimension = _detect_dimension(lowered, report_key)

    spec.intent = f"column_match_{col_name}"
    spec.metric_key = col_name
    spec.aggregation = aggregation
    spec.unit = unit
    spec.is_field_metric = True
    if dimension:
        spec.dimension = dimension
        spec.sort = "desc"
        spec.limit = 5

    logger.info("Column-name fallback matched: metric=%s agg=%s dim=%s for Q=%s",
                col_name, aggregation, dimension, question[:80])
    return spec
