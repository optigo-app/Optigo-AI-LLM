"""Semantic query parser — one LLM call replaces spell correction, report
classification, filter extraction, intent detection, and WHERE clause generation.

The LLM receives a compact semantic catalog (built from report_columns.json)
and returns structured JSON that Python resolves to SP parameters.

This module is now a thin orchestrator. Implementation lives in focused modules:
  - catalog_builder.py   — metric/dimension/filter catalog + semantic catalog text
  - prompt_builder.py    — system/user prompt construction
  - metric_validator.py  — Layer-2 metric-type guard + unit classification
  - parse_result.py      — ParseResult, ai_where validation, SP-param resolution

All public names are re-exported here so existing imports keep working.
"""
import json
import logging
from typing import Any, Dict, List, Optional

from app.services import llm_gateway
from app.middleware.audit_log import log_metric_validation

# ── Re-exports (backward compatibility — existing imports unchanged) ──────────
from app.services.catalog_builder import (  # noqa: F401
    _get_valid_base_columns,
    _get_cached_valid_columns,
    _load_report_columns,
    _get_metric_catalog,
    _build_metric_catalog,
    _build_dimension_catalog,
    _build_filter_catalog,
    _build_prompt_column_sections,
    build_semantic_catalog,
    _get_catalog,
)
from app.services.metric_validator import (  # noqa: F401
    _detect_intent_type,
    validate_metric_intent,
    get_metric_unit,
    get_metric_unit_label,
    CURRENCY_METRICS,
    WEIGHT_METRICS,
    COUNT_METRICS,
    RATE_METRICS,
)
from app.services.parse_result import (  # noqa: F401
    validate_ai_where,
    ParseResult,
)
from app.services.prompt_builder import (  # noqa: F401
    _build_system_prompt,
    _build_user_prompt,
    _build_report_rules,
    _build_ai_where_examples,
    _get_column_filter_expr,
)

logger = logging.getLogger(__name__)

_PARSE_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "erp_query_plan",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "report_key": {"type": "string"},
                "metric": {"type": "string"},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "alternatives": {"type": "array", "items": {"type": "object", "properties": {"intent": {"type": "string"}, "confidence": {"type": "number"}}, "required": ["intent", "confidence"], "additionalProperties": False}},
                "extra_metrics": {"type": "array", "items": {"type": "string"}},
                "dimension": {"type": ["string", "null"]},
                "aggregation": {"type": "string", "enum": ["sum", "avg", "max", "min", "count", "count_distinct"]},
                "limit": {"type": "integer", "minimum": 1},
                "filters": {"type": "null"},
                "date_filter": {
                    "anyOf": [
                        {"type": "null"},
                        {"type": "object", "properties": {"preset": {"type": "string"}}, "required": ["preset"], "additionalProperties": False},
                        {"type": "object", "properties": {"start": {"type": "string"}, "end": {"type": "string"}}, "required": ["start", "end"], "additionalProperties": False},
                    ]
                },
                "sort": {"type": "string", "enum": ["asc", "desc"]},
                "ai_where": {"type": ["string", "null"]},
                "clarify": {"type": ["string", "null"]},
            },
            "required": ["report_key", "metric", "confidence", "alternatives", "extra_metrics", "dimension", "aggregation", "limit", "filters", "date_filter", "sort", "ai_where", "clarify"],
            "additionalProperties": False,
        },
    },
}


def _parse_failure_result(report_name: str = "") -> ParseResult:
    """Fail-safe result when the LLM call errors out entirely.

    A fabricated ``confidence=0.8 + Amount/sum`` default would sail through the
    clarify gate and return a grand total for a question that was never
    understood. ``confidence=0`` + ``clarify`` makes the failure explicit
    instead of answering with made-up numbers.
    """
    return ParseResult({
        "report_key": report_name or "sales_report",
        "metric": "Amount",
        "aggregation": "sum",
        "confidence": 0.0,
        "clarify": "I couldn't understand that question — please rephrase it.",
    })


async def parse_query(
    question: str,
    history: Optional[List[Dict[str, str]]] = None,
    token_usage: Optional[List[Dict[str, int]]] = None,
    report_name: str = "",
) -> ParseResult:
    """One LLM call to extract structured query intent.

    This replaces: spell correction, report classification, filter extraction,
    intent detection, and WHERE clause generation.

    If report_name is provided (from frontend), only that report's catalog is used.
    """
    catalog = _get_catalog(report_name)

    from app.services.retrieval import format_examples, retrieve_examples
    examples = await retrieve_examples(question, report_name)
    example_context = format_examples(examples)
    user_prompt = _build_user_prompt(question, catalog)
    if example_context:
        user_prompt = f"{example_context}\n\n{user_prompt}"
    messages = [
        {"role": "system", "content": _build_system_prompt(report_name, question)},
        {"role": "user", "content": user_prompt},
    ]

    # Add recent history for follow-up questions
    if history:
        for msg in history[-4:]:
            if msg.get("role") in ("user", "assistant"):
                messages.insert(-1, {"role": msg["role"], "content": msg.get("content", "")[:200]})

    try:
        try:
            result = await llm_gateway.chat(
                tier="cheap",
                messages=messages,
                temperature=0.0,
                max_tokens=600,
                response_format=_PARSE_RESPONSE_FORMAT,
            )
        except llm_gateway.LLMGatewayError:
            result = await llm_gateway.chat(
                tier="cheap",
                messages=messages,
                temperature=0.0,
                max_tokens=600,
                response_format={"type": "json_object"},
            )
        if token_usage is not None:
            token_usage.append({
                "provider": result.usage.get("provider", "unknown"),
                "prompt_tokens": result.usage.get("prompt_tokens", 0),
                "completion_tokens": result.usage.get("completion_tokens", 0),
                "estimated_cost_usd": result.usage.get("estimated_cost_usd", 0),
            })
        try:
            data = json.loads(result.text)
        except json.JSONDecodeError:
            # Truncated/ malformed JSON — retry once with a larger budget
            # before giving up, otherwise filters/ai_where silently drop and
            # the user gets an unfiltered grand total.
            retry = await llm_gateway.chat(
                tier="cheap", messages=messages, temperature=0.0,
                max_tokens=1200, response_format={"type": "json_object"},
            )
            if token_usage is not None:
                token_usage.append({
                    "provider": retry.usage.get("provider", "unknown"),
                    "prompt_tokens": retry.usage.get("prompt_tokens", 0),
                    "completion_tokens": retry.usage.get("completion_tokens", 0),
                    "estimated_cost_usd": retry.usage.get("estimated_cost_usd", 0),
                })
            result = retry
            data = json.loads(result.text)
        parsed = ParseResult(data)
        if parsed.confidence < 0.75:
            try:
                stronger = await llm_gateway.chat(
                    tier="strong", messages=messages, temperature=0.0, max_tokens=600,
                    response_format=_PARSE_RESPONSE_FORMAT,
                )
                stronger_data = json.loads(stronger.text)
                stronger_parsed = ParseResult(stronger_data)
                if stronger_parsed.confidence >= parsed.confidence:
                    data, parsed = stronger_data, stronger_parsed
            except (llm_gateway.LLMGatewayError, json.JSONDecodeError):
                pass
        # Layer 2: validate metric type vs question intent
        # e.g. "material" (weight) should not return MetalAmount (amount)
        original_metric = parsed.metric
        corrected = validate_metric_intent(parsed.metric, question, parsed.report_key)
        if corrected != parsed.metric:
            parsed.metric = corrected
            data["metric"] = corrected
            parsed.raw["metric"] = corrected
            # Align aggregation with the corrected metric's catalog type —
            # e.g. Amount→total_count must also become agg=count, otherwise
            # the SP receives SUM(DI.id) and fails with stat_code errors.
            catalog = _get_metric_catalog(parsed.report_key)
            corrected_type = catalog.get(corrected, {}).get("type", "amount")
            _type_agg = {"count": "count", "rate": "avg", "text": "max"}
            new_agg = _type_agg.get(corrected_type, "sum")
            if parsed.aggregation != new_agg:
                parsed.aggregation = new_agg
                data["aggregation"] = new_agg
                parsed.raw["aggregation"] = new_agg
            # Structured audit trail
            log_metric_validation(
                layer=2,
                question=question,
                report_key=parsed.report_key,
                original_metric=original_metric,
                corrected_metric=corrected,
                original_type=catalog.get(original_metric, {}).get("type", "amount"),
                corrected_type=catalog.get(corrected, {}).get("type", "amount"),
                intent_type=_detect_intent_type(question),
                action="override",
                reason=f"LLM returned {original_metric} but question intent is {_detect_intent_type(question)}",
            )
        # Layer 2b: validate extra_metrics — filter out type mismatches
        if parsed.extra_metrics:
            catalog = _get_metric_catalog(parsed.report_key)
            primary_type = catalog.get(parsed.metric, {}).get("type", "amount")
            valid_extras = []
            dropped_extras = []
            for em in parsed.extra_metrics:
                em_type = catalog.get(em, {}).get("type", primary_type)
                if em_type == primary_type:
                    valid_extras.append(em)
                else:
                    dropped_extras.append(em)
                    logger.warning(
                        "Layer 2: dropping extra_metric %r (type=%s) — "
                        "primary metric %r is type=%s",
                        em, em_type, parsed.metric, primary_type
                    )
            if dropped_extras:
                log_metric_validation(
                    layer=2,
                    question=question,
                    report_key=parsed.report_key,
                    original_metric=parsed.metric,
                    corrected_metric=parsed.metric,
                    original_type=primary_type,
                    corrected_type=primary_type,
                    intent_type=primary_type,
                    action="drop_extra",
                    reason=f"Dropped extra_metrics with mismatched type: {dropped_extras}",
                    extra_metrics_dropped=dropped_extras,
                )
            parsed.extra_metrics = valid_extras
        logger.info("Semantic parse: '%s' -> %s", question[:80], parsed)
        return parsed
    except json.JSONDecodeError as exc:
        logger.error("Semantic parse JSON decode error: %s | raw: %s", exc, result.text[:200])
        return _parse_failure_result(report_name)
    except Exception as exc:
        logger.error("Semantic parse error: %s", exc)
        return _parse_failure_result(report_name)
