import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from app.services.column_registry import _load_registry
from app.services.intent import _detect_explicit_intent
from app.services.query_plan import QueryPlan
from app.services.semantic_query_parser import ParseResult, parse_query

logger = logging.getLogger(__name__)


@dataclass
class PlanningResult:
    parsed: ParseResult
    plan: QueryPlan
    intent_spec: Any
    validated_filters: Dict[str, Any]
    ai_where: str
    routing_source: str = "llm"


_FIELD_VALUE_STOP_WORDS = {
    "a", "an", "and", "amount", "average", "by", "category", "compared",
    "count", "customer", "date", "discount", "for", "generates", "growth",
    "highest", "how", "invoice", "items", "last", "lowest", "many", "month",
    "most", "pieces", "pcs", "per", "range", "revenue", "sale", "sales",
    "segment", "the", "this", "today", "top", "total", "type", "units",
    "value", "wastage", "week", "what", "which", "who", "wise", "year", "yesterday",
}


def _apply_explicit_field_filters(parsed: ParseResult, question: str) -> None:
    """Extract explicit '<field> <value> <metric>' phrases into governed filters.

    This keeps values such as ``customer ThGems total sales`` and
    ``customer type Retailer total sales`` out of the ambiguous-entity path and
    sends them through the configured filter_key_map/SP filter contract instead.
    """
    report_cfg = _load_registry().get(parsed.report_key, {})
    filter_map = report_cfg.get("filter_key_map", {}) or {}
    candidates = []
    for alias, target in filter_map.items():
        if target in ("_date", "_name_filter"):
            continue
        phrase = alias.replace("_", " ").strip()
        if phrase:
            candidates.append((phrase, alias))
    candidates.sort(key=lambda item: len(item[0]), reverse=True)

    metric_start = (
        r"(?:total\s+)?(?:sales?|revenue|amount|value|tax|discount|"
        r"pieces|pcs|units|weight|wt)\b"
    )
    occupied: List[tuple[int, int]] = []
    extracted = False
    for phrase, alias in candidates:
        match = re.search(
            rf"\b{re.escape(phrase)}\s+(?:of\s+|is\s+)?"
            rf"([A-Za-z0-9._&/-]+(?:\s+[A-Za-z0-9._&/-]+){{0,3}}?)\s+{metric_start}",
            question,
            re.IGNORECASE,
        )
        if not match:
            continue
        value = match.group(1).strip(" ,.?")
        words = {word.lower() for word in re.findall(r"[A-Za-z0-9._&/-]+", value)}
        if not value or len(value) > 100 or words & _FIELD_VALUE_STOP_WORDS:
            continue
        if any(match.start(1) < end and match.end(1) > start for start, end in occupied):
            continue
        occupied.append(match.span(1))
        parsed.filters.setdefault(alias, value)
        extracted = True

    if extracted and not re.search(r"\b(?:by|wise|per|top|highest|lowest|most)\b", question, re.IGNORECASE):
        parsed.dimension = None
        parsed.intent = None


def _apply_deterministic_override(parsed: ParseResult, question: str) -> None:
    det_spec = _detect_explicit_intent(question, parsed.report_key)
    report_cfg = _load_registry().get(parsed.report_key, {})
    default_metric = report_cfg.get("default_metric", "Amount")
    if det_spec is not None:
        parsed.confidence = max(parsed.confidence, 0.95)
    if det_spec is not None and det_spec.metric_key and (parsed.metric == default_metric or det_spec.override_metric):
        parsed.metric = det_spec.metric_key
        parsed.aggregation = det_spec.aggregation or parsed.aggregation
        if det_spec.dimension:
            parsed.dimension = det_spec.dimension
            parsed.sort = det_spec.sort or parsed.sort
            parsed.limit = det_spec.limit or parsed.limit
    if det_spec is not None and parsed.metric == det_spec.metric_key:
        parsed.intent = det_spec.intent
    _apply_explicit_field_filters(parsed, question)


def finalize_query(
    parsed: ParseResult,
    question: str,
    previous_filters: Optional[Dict[str, Any]] = None,
    routing_source: str = "llm",
) -> PlanningResult:
    source_confidence = {"frontend": 1.0, "intent": 0.95, "keyword": 0.75, "context": 0.80, "llm": 0.60}
    parsed.confidence = source_confidence.get(routing_source, 0.60)
    _apply_deterministic_override(parsed, question)
    lowered = question.lower()
    if not parsed.date_filter and any(term in lowered for term in ("growth", "compared with the previous", "compare with the previous")):
        parsed.date_filter = {"preset": "this_month"}
    if parsed.dimension and parsed.aggregation in ("min", "max"):
        # String-typed metrics (designno, CustomerName...) can't be summed —
        # MAX() is the only valid aggregate for text, keep it.
        from app.services.column_registry import _REGISTRY as _COL_REG
        col_meta = _COL_REG.get(parsed.report_key, {}).get("columns", {}).get(parsed.metric, {})
        if str(col_meta.get("type", "")).lower() not in ("string", "text"):
            parsed.aggregation = "sum"
    plan = parsed.to_query_plan(question)
    plan.validate_against_registry()
    parsed.metric = plan.metric
    parsed.dimension = plan.dimension
    parsed.limit = plan.limit
    parsed.aggregation = plan.aggregation
    parsed.sort = plan.sort_by or "desc"
    validated_filters = plan.validated_filters()
    if not plan.date_range and previous_filters:
        for key in ("start_date", "end_date"):
            if previous_filters.get(key):
                validated_filters[key] = previous_filters[key]
    return PlanningResult(
        parsed=parsed,
        plan=plan,
        intent_spec=parsed.to_intent_spec(),
        validated_filters=validated_filters,
        ai_where=parsed.generate_where_clause(),
        routing_source=routing_source,
    )


async def plan_query(
    question: str,
    history: Optional[List[Dict[str, str]]] = None,
    token_usage: Optional[List[Dict[str, int]]] = None,
    report_name: str = "",
    previous_filters: Optional[Dict[str, Any]] = None,
    routing_source: str = "llm",
    registry: Optional[Dict[str, Any]] = None,
    fallback_report: str = "",
) -> PlanningResult:
    selected_report = report_name
    if selected_report:
        routing_source = "frontend"
    elif registry and len(registry) > 1:
        from app.services.intent import classify_question_by_intent
        from app.services.classifier import keyword_classify
        selected_report = classify_question_by_intent(question, registry) or ""
        if selected_report:
            routing_source = "intent"
        else:
            selected_report = keyword_classify(question, registry) or ""
            if selected_report:
                routing_source = "keyword"
    parsed = await parse_query(question, history=history, token_usage=token_usage, report_name=selected_report)
    report_aliases = {"sales_summary": "sales_report", "wip_summary": "wip_report"}
    parsed.report_key = report_aliases.get(parsed.report_key, parsed.report_key)
    if selected_report:
        if parsed.report_key and parsed.report_key != selected_report:
            parsed.alternatives = [{"intent": parsed.report_key, "confidence": 0.25}]
        parsed.report_key = selected_report
    elif (not parsed.report_key or not parsed.report_key.strip()) and fallback_report:
        parsed.report_key = fallback_report
        routing_source = "context"
    return finalize_query(parsed, question, previous_filters, routing_source)
