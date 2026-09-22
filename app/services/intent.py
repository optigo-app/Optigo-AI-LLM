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
    state: str = "SUCCESS"
    state_reason: str = ""
    clear_filters: Dict[str, Any] = None  # filters to remove because they were hallucinated
    override_filters: Dict[str, Any] = None  # filters to enforce/replace

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
        "unit", "is_field_metric", "state", "state_reason", "clear_filters", "override_filters"
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


def _normalize_dimension_value(field: str, value: Any, report_key: str) -> Any:
    """Normalize a filter value to canonical vocabulary where known."""
    if value is None or not isinstance(value, str):
        return value
    lowered = value.lower().strip()
    canon = get_canonical_values(report_key, field)
    if canon:
        return canon.get(lowered, value)
    # Legacy support: some callers use short filter keys that map to canonical columns.
    # Try the canonical field names from the report config as well.
    report_cfg = _COLUMN_REGISTRY.get(report_key, {})
    filter_map = report_cfg.get("filter_key_map", {})
    for alias, target in filter_map.items():
        if target == field:
            canon = get_canonical_values(report_key, alias)
            if canon:
                return canon.get(lowered, value)
    return value


def normalize_filters(
    question: str,
    report_key: str,
    filters: Dict[str, Any],
    mentioned_fields: Optional[List[str]] = None,
) -> Tuple[Dict[str, Any], List[str], IntentSpec]:
    """Normalize extracted filters and return the IntentSpec.

    The intent spec may request clearing filters that were hallucinated (e.g.
    'diamond' mapped to category when the question is actually about the
    diamondAmount field).
    """
    spec = detect_intent(question, report_key)
    cleaned = dict(filters or {})
    mentioned = list(mentioned_fields or [])

    # Remove hallucinated category filters for field-metric questions
    for field in spec.clear_filters or []:
        if field in cleaned:
            del cleaned[field]
            if field in mentioned:
                mentioned.remove(field)

    # Apply explicit overrides from the intent
    for field, value in spec.override_filters.items():
        cleaned[field] = value
        if field not in mentioned:
            mentioned.append(field)

    # Add branch from question if not already set and question is branch-specific
    branch = _branch_from_question(question, report_key)
    if branch and "branch" in cleaned and cleaned["branch"] is None:
        cleaned["branch"] = branch
        if "branch" not in mentioned:
            mentioned.append("branch")
    if report_key == "party_outstanding" and branch:
        cleaned["branch_code"] = branch
        if "branch_code" not in mentioned:
            mentioned.append("branch_code")

    # Infer party_type from question for party_outstanding
    if report_key == "party_outstanding" and "party_type" not in cleaned:
        pt = _party_type_from_question(question)
        if pt:
            cleaned["party_type"] = pt
            if "party_type" not in mentioned:
                mentioned.append("party_type")

    # Normalize any string filter values to canonical vocabulary
    for field, value in list(cleaned.items()):
        cleaned[field] = _normalize_dimension_value(field, value, report_key)

    # Field re-mapping: a value that the LLM put in the wrong field often
    # belongs to another known dimension. Move it, don't silently leave it wrong.
    _remap_dimensions(cleaned, mentioned, report_key)

    return cleaned, mentioned, spec


def _remap_dimensions(
    cleaned: Dict[str, Any], mentioned: List[str], report_key: str
) -> None:
    """Move filter values that were placed in the wrong field.

    Uses each report's canonical_values: if field A holds a value that appears
    in field B's canonical vocabulary (and A's own vocabulary does not claim it),
    move the value to B. This is fully data-driven and works for any report that
    defines overlapping canonical value sets.
    """
    report_cfg = _COLUMN_REGISTRY.get(report_key, {})
    canonical = report_cfg.get("canonical_values", {})
    if not canonical:
        return

    # Build lowercase lookup maps for each canonical field
    vocab: Dict[str, Dict[str, str]] = {}
    for field, mapping in canonical.items():
        if not isinstance(mapping, dict):
            continue
        vocab[field] = {k.lower().strip(): v for k, v in mapping.items()}

    for src_field, src_value in list(cleaned.items()):
        if not isinstance(src_value, str):
            continue
        lowered = src_value.lower().strip()
        src_vocab = vocab.get(src_field, {})
        # If this value is already canonical for src_field, keep it there
        if lowered in src_vocab:
            continue
        # Check if the value belongs to another field's vocabulary
        for tgt_field, tgt_vocab in vocab.items():
            if tgt_field == src_field:
                continue
            if cleaned.get(tgt_field) is not None:
                continue
            if lowered in tgt_vocab:
                cleaned[tgt_field] = tgt_vocab[lowered]
                cleaned.pop(src_field, None)
                if src_field in mentioned:
                    mentioned.remove(src_field)
                if tgt_field not in mentioned:
                    mentioned.append(tgt_field)
                break


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
    scored: List[tuple] = []  # (score, original_index, report_key)
    for idx, rk in enumerate(ordered):
        kws = _COLUMN_REGISTRY.get(rk, {}).get("report_keywords", [])
        score = sum(1 for kw in kws if kw in lowered)
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


# ── LLM intent detection (async fallback) ────────────────────────────────────

async def llm_intent_detection(
    question: str,
    report_key: str,
    history: Optional[list] = None,
) -> IntentSpec:
    """Use LLM to detect intent when all deterministic fallbacks fail.

    Returns an IntentSpec with the LLM's best guess for metric, aggregation,
    and dimension. Validates against the column registry to ensure safety.
    """
    report_cols = _COLUMN_REGISTRY.get(report_key, {})
    columns = report_cols.get("columns", {})
    special = report_cols.get("special_metrics", {})

    # Build column list for the prompt
    col_list = []
    for name, meta in columns.items():
        if isinstance(meta, dict) and not meta.get("filter_only"):
            col_type = meta.get("type", "string")
            col_list.append(f"  - {name} ({col_type})")
    for name, meta in special.items():
        if isinstance(meta, dict):
            col_list.append(f"  - {name} (special metric)")
    cols_text = "\n".join(col_list[:40])

    # Build dimension list
    dim_list = [name for name, meta in columns.items()
                if isinstance(meta, dict) and meta.get("type") == "string"
                and not meta.get("filter_only")]
    dims_text = ", ".join(dim_list[:20])

    prompt = f"""You are an intent detector for a business report query.
Given a user question and the available columns, determine the best metric, aggregation, and dimension.

Available metrics (columns):
{cols_text}

Available dimensions (for grouping/ranking): {dims_text}

Allowed aggregations: sum, count, count_distinct, avg, max, min

Respond ONLY with a JSON object, no markdown, no explanation:
{{"metric_key": "<column name from the list above>", "aggregation": "<one of sum/count/count_distinct/avg/max/min>", "dimension": "<column name for grouping, or empty string>", "sort": "desc or asc", "limit": <number or 1>}}

User question: {question}
Report: {report_key}

JSON:"""

    try:
        from app.services import llm_gateway
        result = await llm_gateway.chat(
            tier="cheap",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=200,
        )
        raw = result.text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*", "", raw)
            raw = re.sub(r"\s*```$", "", raw)

        parsed = json.loads(raw)
    except Exception as e:
        logger.warning("LLM intent detection failed: %s", e)
        return IntentSpec(report_key=report_key, state="FALLBACK_FAILED",
                          state_reason=f"LLM intent detection error: {str(e)[:100]}")

    # Validate metric_key against column registry
    metric_key = parsed.get("metric_key", "")
    allowed_metrics = set(columns.keys()) | set(special.keys())
    if metric_key not in allowed_metrics:
        logger.warning("LLM intent: metric_key=%r not in registry, using default", metric_key)
        metric_key = "Amount" if "Amount" in allowed_metrics else next(iter(allowed_metrics), "")

    # Validate aggregation
    aggregation = parsed.get("aggregation", "sum").lower()
    if aggregation not in {"sum", "count", "count_distinct", "avg", "max", "min"}:
        aggregation = "sum"

    # Validate dimension
    dimension = parsed.get("dimension", "")
    if dimension and dimension not in allowed_metrics:
        logger.warning("LLM intent: dimension=%r not in registry, ignoring", dimension)
        dimension = ""

    # Determine unit from column type
    col_meta = columns.get(metric_key, {})
    col_type = col_meta.get("type", "") if isinstance(col_meta, dict) else ""
    if col_type == "count" or col_type == "int":
        unit = "count"
    elif col_type == "decimal":
        unit = "currency"
    else:
        unit = ""

    # Parse sort and limit
    sort = parsed.get("sort", "desc").lower()
    if sort not in {"asc", "desc"}:
        sort = "desc"
    try:
        limit = int(parsed.get("limit", 1))
    except (ValueError, TypeError):
        limit = 1

    spec = IntentSpec(
        report_key=report_key,
        intent=f"llm_detected_{metric_key}",
        metric_key=metric_key,
        aggregation=aggregation,
        dimension=dimension,
        sort=sort,
        limit=limit,
        unit=unit,
        is_field_metric=True,
    )

    logger.info("LLM intent detection: metric=%s agg=%s dim=%s for Q=%s",
                metric_key, aggregation, dimension, question[:80])
    return spec


# ── Spell correction (async, LLM-based) ──────────────────────────────────────

# Common business-term typos for fast sync correction (no LLM call needed)
_COMMON_TYPOS: Dict[str, str] = {
    "toal": "total",
    "totla": "total",
    "amoutn": "amount",
    "amont": "amount",
    "amunt": "amount",
    "grosst": "grosswt",
    "groswt": "grosswt",
    "grosweight": "grosswt",
    "gros_wt": "grosswt",
    "nett": "netwt",
    "net wt": "netwt",
    "netweight": "netwt",
    "grossweight": "grosswt",
    "gross wt": "grosswt",
    "saels": "sales",
    "sals": "sales",
    "purchse": "purchase",
    "purchas": "purchase",
    "custmer": "customer",
    "catgory": "category",
    "catogry": "category",
    "brnch": "branch",
    "brach": "branch",
    "dimond": "diamond",
    "silvar": "silver",
    "goldn": "gold",
    "platnum": "platinum",
    "purchses": "purchases",
    "transation": "transaction",
    "transactn": "transaction",
    "avgarge": "average",
    "avrage": "average",
    "higest": "highest",
    "lowset": "lowest",
    "maximun": "maximum",
    "minimun": "minimum",
    "outstandng": "outstanding",
    "outstading": "outstanding",
    "ledger": "ledger",
    "invoce": "invoice",
    "suplier": "supplier",
    "manfacturer": "manufacturer",
    "manufaturer": "manufacturer",
    "waight": "weight",
    "wieght": "weight",
    "weigt": "weight",
    "carat": "carat",
    "qty": "qty",
    "qunatity": "quantity",
    "quantitiy": "quantity",
}

# Known valid business terms (identity mappings) — prevents LLM from "correcting"
# terms like "netwt" into "net worth". If any of these are present, the sync
# corrector marks the question as already handled and skips the LLM fallback.
_KNOWN_VALID_TERMS: Dict[str, str] = {
    "netwt": "netwt",
    "grosswt": "grosswt",
    "groswt": "grosswt",
    "netwt_24k": "netwt_24k",
    "dctw": "dctw",
    "cspcs": "cspcs",
    "dpcs": "dpcs",
    "miscpcs": "miscpcs",
    "miscwt": "miscwt",
    "csctw": "csctw",
    "goldamt": "goldamt",
    "silveramt": "silveramt",
    "platinumamt": "platinumamt",
    "goldpurewt": "goldpurewt",
    "silverpurewt": "silverpurewt",
    "platinumpurewt": "platinumpurewt",
    "totalpurewt": "totalpurewt",
}


def _sync_spell_correct(question: str) -> Tuple[str, bool]:
    """Fast sync spell correction using a typo dictionary.

    Returns (corrected_question, was_corrected).
    """
    lowered = question.lower()
    corrected = lowered
    changed = False
    for typo, fix in _COMMON_TYPOS.items():
        if typo in corrected:
            corrected = corrected.replace(typo, fix)
            changed = True

    # Check for known valid terms — if present, skip LLM fallback to avoid
    # the LLM "correcting" valid terms like "netwt" into "net worth"
    for term, fix in _KNOWN_VALID_TERMS.items():
        if term in corrected:
            changed = True  # mark as handled so LLM is skipped

    if not changed:
        return question, False

    return corrected, True


async def correct_spelling(question: str) -> str:
    """Auto-correct spelling mistakes in a user question.

    Uses a fast sync typo dictionary first, then falls back to LLM for
    complex corrections. Returns the corrected question.
    """
    # Fast path: common typo dictionary
    corrected, changed = _sync_spell_correct(question)
    if changed:
        logger.info("Spell corrected (sync): '%s' -> '%s'", question[:80], corrected[:80])
        return corrected

    # LLM path for more complex typos
    prompt = (
        "Correct spelling mistakes in this business question. "
        "Only fix typos, do not change the meaning. "
        "Return ONLY the corrected question, nothing else.\n\n"
        f"Question: {question}\n"
        "Corrected:"
    )

    try:
        from app.services import llm_gateway
        result = await llm_gateway.chat(
            tier="cheap",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.0,
            max_tokens=200,
        )
        corrected = result.text.strip()
        if corrected and corrected.lower() != question.lower():
            logger.info("Spell corrected (LLM): '%s' -> '%s'", question[:80], corrected[:80])
            return corrected
    except Exception as e:
        logger.warning("Spell correction LLM failed: %s", e)

    return question
