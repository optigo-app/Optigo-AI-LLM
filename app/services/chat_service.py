"""Chat pipeline — shared orchestration for /chat and /chat/stream.

One pipeline (run_chat) produces a ChatResponse; the streaming endpoint
translates the same result into SSE events, so the two paths can never drift.
"""

import json as _json
import re
import time
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from app.config import settings
from app.middleware.audit_log import log_chat_exchange, log_request_trace
from app.middleware.auth import validate_session_id
from app.middleware.logging import get_logger
from app.middleware.metrics import (
    CACHE_HITS, CACHE_MISSES, CACHE_SIZE, LLM_CALLS, LLM_LATENCY, LLM_TOKENS,
)
from app.middleware.prompt_guard import detect_injection, sanitize_question
from app.models import (
    Actions, AnswerData, ChatActionRequest, ChatRequest, ChatResponse,
    Metadata, PeriodInfo, ReportInfo,
)
from app.services import block_builder, conversation_store, usage_store
from app.services.answer_generator import generate_answer, _resolve_metric, _record_count
from app.services.api_client import ReportApiError, call_report_api, export_report_api
from app.services.classifier import get_greeting_response, keyword_classify
from app.services.context_resolver import (
    ambiguous_entity_value, is_probable_person_name, looks_like_followup,
    needs_date_clarification, resolve_context,
)
from app.services.real_api_client import RealApiError
from app.services.spelling import normalize_hinglish, normalize_spelling

logger = get_logger(__name__)


def _resolve_identity(request: Request, body: "ChatRequest") -> tuple:
    """(company_code, user_id) — cookie-authenticated identity wins when
    AUTH_REQUIRED; body fields are honored only in dev mode."""
    if settings.auth_required:
        return (
            getattr(request.state, "company_code", None) or "DEMO",
            getattr(request.state, "user_id", None) or "u123",
        )
    return (
        getattr(request.state, "company_code", None) or body.company_code or "DEMO",
        getattr(request.state, "user_id", None) or body.user_id or "u123",
    )


def _resolve_exec_scope(request: Request, body: "ChatRequest", report_meta: Dict[str, Any], user_id: str) -> tuple:
    """(sp_number, yearcode, appuserid) — execution scope for the ERP call.
    Under AUTH_REQUIRED the client cannot choose the SP, the yearcode, or the
    ERP user identity — those come from the verified session or config.
    Dev mode keeps the historical body overrides for testing."""
    if settings.auth_required:
        return (
            report_meta.get("sp") or settings.real_api_sp,
            getattr(request.state, "yearcode", None) or settings.real_api_yearcode,
            getattr(request.state, "appuserid", None) or user_id or "admin@orail.co.in",
        )
    return (
        body.sp or report_meta.get("sp") or settings.real_api_sp,
        body.yearcode or settings.real_api_yearcode,
        body.appuserid or user_id or "admin@orail.co.in",
    )


def _future_period_message(validated_filters: Dict[str, Any]) -> Optional[str]:
    """Honest guard: a resolved range that starts in the future can never
    return data — say so instead of executing and showing an empty result."""
    start = (validated_filters or {}).get("start_date", "")
    if not start:
        return None
    from datetime import date
    try:
        future = date.fromisoformat(start[:10]) > date.today()
    except (ValueError, TypeError):
        return None
    if not future:
        return None
    from app.services.block_builder import format_date_friendly
    return (
        f"That period ({format_date_friendly(start)}) is in the future — no "
        "data exists yet. Try 'today', 'yesterday', or a past date range."
    )


# Structured filters the API accepts inbound (body.filters / widget actions).
# Deliberately narrow — dates only. Entity filters go through the question-
# rewrite path so the deterministic field extractor and prompt both see them.
_INBOUND_FILTER_FIELDS = ("start_date", "end_date")
_INBOUND_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_inbound_filters(filters: Dict[str, Any]) -> Dict[str, Any]:
    """Whitelist-check inbound structured filters. Returns a clean dict."""
    out: Dict[str, Any] = {}
    for field in _INBOUND_FILTER_FIELDS:
        value = (filters or {}).get(field)
        if isinstance(value, str) and _INBOUND_DATE_RE.match(value):
            out[field] = value
    if out.get("start_date") and out.get("end_date") and out["start_date"] > out["end_date"]:
        out["start_date"], out["end_date"] = out["end_date"], out["start_date"]
    return out


def prepare_chat(body: ChatRequest, request: Request) -> tuple:
    """Shared preamble: identity resolution + session binding + response mode.

    Returns (company_code, user_id, session_id, response_mode). Idempotent —
    safe to call from both the SSE wrapper (for the session event) and run_chat.
    """
    company_code, user_id = _resolve_identity(request, body)

    # Sanitize session_id, then bind it to this identity — a session_id that
    # belongs to another (company, user) is refused and replaced with a fresh
    # one instead of leaking that conversation's history. A session already
    # bound by an earlier prepare_chat call (e.g. the SSE wrapper) is reused
    # so the stream and the pipeline agree on the rebound id.
    session_id = (
        getattr(request.state, "session_id", None)
        or validate_session_id(body.session_id)
        or conversation_store.new_session_id()
    )
    session_owner = f"{company_code}:{user_id}"
    if not conversation_store.claim_session(session_id, session_owner):
        logger.warning(
            "Session %s claimed by another identity — issuing fresh session", session_id
        )
        session_id = conversation_store.new_session_id()
        conversation_store.claim_session(session_id, session_owner)
    request.state.session_id = session_id

    response_mode = body.response_mode or "normal"
    if not body.response_mode or body.response_mode == "normal":
        wide_companies = settings.wide_response_companies
        if wide_companies and company_code in wide_companies.split(","):
            response_mode = "wide"
    return company_code, user_id, session_id, response_mode



async def run_chat(
    body: ChatRequest, request: Request, registry: Dict[str, Any], cache: Any,
) -> ChatResponse:
    start = time.time()

    # Input validation
    if not body.question or not body.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    if len(body.question) > settings.max_question_length:
        raise HTTPException(
            status_code=400,
            detail=f"Question too long (max {settings.max_question_length} characters)",
        )

    # Identity + session binding + response mode (shared with /chat/stream).
    company_code, user_id, session_id, response_mode = prepare_chat(body, request)

    # A fresh user-typed question supersedes any pending clarify widget —
    # drop it so a stale click can't resurrect dead context. Requests spawned
    # by /chat/action keep it alive (they consume it).
    if not body.from_action:
        conversation_store.clear_pending(session_id)

    # Prompt injection guard
    injection = detect_injection(body.question)
    if injection:
        logger.warning("Prompt injection blocked: session=%s pattern=%s", session_id, injection)
        if response_mode == "wide":
            return ChatResponse(
                error="I can only answer questions about your ERP reports. Please ask a relevant question.",
                session_id=session_id,
                blocks=[{"type": "error", "content": "I can only answer questions about your ERP reports. Please ask a relevant question."}],
            )
        return ChatResponse(
            error="I can only answer questions about your ERP reports. Please ask a relevant question.",
            session_id=session_id,
        )
    body.question = sanitize_question(body.question)
    body.question = normalize_spelling(body.question)
    body.question = normalize_hinglish(body.question)

    # ── Resolve report from pid (frontend sends unique report ID) ──
    # Done before the greeting check so the greeting can be report-scoped.
    resolved_report_name = body.report_name or ""
    if body.pid and not resolved_report_name:
        for rk, entry in registry.items():
            if getattr(entry, "pid", None) == body.pid:
                resolved_report_name = rk
                break
        if not resolved_report_name:
            if response_mode == "wide":
                return ChatResponse(
                    error=f"Unknown report pid: {body.pid}",
                    session_id=session_id,
                    blocks=[{"type": "error", "content": f"Unknown report pid: {body.pid}"}],
                )
            return ChatResponse(
                error=f"Unknown report pid: {body.pid}",
                session_id=session_id,
            )

    # Scope guard: return friendly static messages for greetings and out-of-scope questions
    greeting_msg = get_greeting_response(
        body.question, registry=registry, report_key=resolved_report_name or None,
    )
    if greeting_msg:
        greeting_answer = AnswerData(type="text", value=greeting_msg)
        if response_mode == "wide":
            return ChatResponse(
                answer=greeting_answer,
                answer_text=greeting_msg,
                session_id=session_id,
                blocks=[{"type": "text", "content": greeting_msg}],
            )
        return ChatResponse(
            answer=greeting_answer,
            answer_text=greeting_msg,
            session_id=session_id,
        )

    if needs_date_clarification(body.question):
        msg = "Which date range would you like? For example: today, this month, last month, this year, or specific start and end dates."
        blocks = [block_builder.build_date_range_input_block(msg)] if response_mode == "wide" else None
        # The question being re-dated is the previous user turn, not this one.
        prior = [m for m in conversation_store.get_messages(session_id, limit=10)
                 if m.get("role") == "user"]
        conversation_store.set_pending(session_id, {
            "kind": "date_range",
            "question": prior[-1]["content"] if prior else "",
            "report_key": resolved_report_name or "",
        })
        conversation_store.add_message(session_id, "user", body.question)
        conversation_store.add_message(session_id, "assistant", msg)
        return ChatResponse(
            status="clarify", answer=AnswerData(type="text", value=msg),
            answer_text=msg, session_id=session_id, blocks=blocks,
        )

    ambiguous_value = ambiguous_entity_value(body.question)
    if ambiguous_value:
        if is_probable_person_name(ambiguous_value):
            # Person-like names almost always mean a customer — apply the
            # customer filter directly instead of a clarification round-trip.
            body.question = re.sub(
                re.escape(ambiguous_value),
                f"customer {ambiguous_value}",
                body.question, count=1, flags=re.IGNORECASE,
            )
        else:
            msg = f"What does '{ambiguous_value}' refer to: a design, SKU, customer, invoice, salesperson, brand, category, or branch?"
            blocks = [block_builder.build_entity_choice_block(ambiguous_value, msg)] if response_mode == "wide" else None
            # Server-trusted pending state: /chat/action validates a
            # select_option click against these option ids and reconstructs
            # the question deterministically ("customer VIDSY total sales").
            conversation_store.set_pending(session_id, {
                "kind": "entity_disambiguation",
                "question": body.question,
                "entity_value": ambiguous_value,
                "report_key": resolved_report_name or "",
                "options": {
                    oid: {
                        "label": block_builder.ENTITY_OPTION_LABELS.get(oid, oid.title()),
                        "inject": inject,
                    }
                    for oid, inject in block_builder.ENTITY_OPTION_INJECT.items()
                },
            })
            conversation_store.add_message(session_id, "user", body.question)
            conversation_store.add_message(session_id, "assistant", msg)
            return ChatResponse(
                status="clarify", answer=AnswerData(type="text", value=msg),
                answer_text=msg, session_id=session_id, blocks=blocks,
            )

    token_usage_calls: List[Dict[str, int]] = []
    from app.services.pipeline_trace import StageTracer
    tracer = StageTracer(getattr(request.state, "request_id", ""))
    history = conversation_store.get_messages(session_id, limit=10)

    logger.info("Chat request: session=%s question=%s", session_id, body.question[:100])

    # ── Semantic query parser: ONE LLM call replaces spell correction,
    #    report classification, filter extraction, intent detection, and
    #    WHERE clause generation ──

    # Resolve follow-up questions using conversation history.
    # Fresh questions pass through unchanged (zero latency). Follow-ups are
    # rewritten into standalone questions so the parser sees full context.
    tracer.start("context")
    resolved_question = await resolve_context(
        body.question, history, token_usage=token_usage_calls,
    )
    tracer.end("context", detail=resolved_question if resolved_question != body.question else "pass-through")

    # External-market detection: when the question references market/industry
    # information, the governed pipeline still answers the internal side and we
    # attach an explicit boundary note (+ curated external snapshot if configured).
    from app.services.market_context import (
        build_market_note, detect_market_context, load_market_snapshot,
    )
    market_ctx = detect_market_context(resolved_question)
    market_snapshot = load_market_snapshot(market_ctx["topic"]) if market_ctx else []

    cache_report_name = resolved_report_name
    # Scope strength: fuzzy (semantic) cache hits are only safe when the
    # report scope is high-precision — a frontend pin or a deterministic
    # intent rule. Keyword/session hints can disagree with the report the
    # planner would actually execute (the LLM may overrule a keyword pick),
    # so under weak scope we only trust exact-match hits.
    _cache_scope_strong = bool(cache_report_name) or len(registry) <= 1
    if not cache_report_name and len(registry) > 1:
        from app.services.intent import classify_question_by_intent
        cache_report_name = classify_question_by_intent(resolved_question, registry)
        if cache_report_name:
            _cache_scope_strong = True
        else:
            cache_report_name = (
                keyword_classify(resolved_question, registry)
                or conversation_store.get_last_report(session_id)
                or ""
            )

    # Semantic cache check (skip when the caller explicitly asks for an export link,
    # or when the frontend asks for a regenerated/fresh answer).
    # Uses resolved_question so follow-ups get the right cache key, not the raw short input.
    # The cache is scoped to the resolved report (from pid/report_name) so the same
    # question asked on a different report does not return another report's answer.
    if cache is not None and not body.export and not body.regenerate:
        cached = await cache.lookup(
            resolved_question, company_code, user_id, response_mode,
            report_key=cache_report_name or None,
            allow_semantic=_cache_scope_strong,
        )
        if cached:
            CACHE_HITS.inc()
            if cached.metadata is not None:
                cached.metadata.cache_hit = True
            conversation_store.set_last_report(session_id, cached.report_key)
            log_chat_exchange(
                question=resolved_question,
                answer=cached.answer_text or "",
                report_key=cached.report_key,
                filters=cached.filters or {},
                session_id=session_id,
                latency_ms=round((time.time() - start) * 1000, 1),
            )
            log_request_trace(
                request_id=getattr(request.state, "request_id", ""),
                intent=cached.report_key or "", confidence=(cached.metadata.intent_confidence if cached.metadata else 0.0) or 0.0,
                result_count=(cached.metadata.record_count if cached.metadata else 0), cache_hit=True,
            )
            return cached
    CACHE_MISSES.inc()

    from app.services.query_planner import plan_query
    last_intent = conversation_store.get_last_intent(session_id)
    tracer.start("semantic_parse")
    try:
        planning = await plan_query(
            resolved_question, history=history, token_usage=token_usage_calls,
            report_name=resolved_report_name,
            # Session filters (date range etc.) only carry into genuine
            # follow-ups — a standalone question or suggestion click must not
            # silently inherit yesterday's window.
            previous_filters=(
                conversation_store.get_last_filters(session_id)
                if looks_like_followup(body.question, history) else {}
            ) or {},
            registry=registry,
            fallback_report=last_intent.get("report_key", ""),
        )
        tracer.end("semantic_parse",
                   detail=f"{planning.parsed.report_key}/{planning.parsed.metric} conf={planning.parsed.confidence}")
    except ValueError as exc:
        tracer.end("semantic_parse", outcome="error", detail=str(exc))
        logger.warning("Query planning failed: %s", exc)
        msg = "I couldn't safely validate that query. Please specify the report and metric."
        return ChatResponse(error=msg, session_id=session_id)
    parsed = planning.parsed
    report_key = parsed.report_key
    query_plan = planning.plan
    intent_spec = planning.intent_spec
    validated_filters = planning.validated_filters
    ai_where = planning.ai_where
    routing_source = planning.routing_source

    # Structured inbound filters (set_date_range actions, or a frontend that
    # already knows the dates) override whatever the parser extracted — a
    # widget click is authoritative data, not NL to be re-interpreted.
    _inbound = _validate_inbound_filters(body.filters or {})
    if _inbound:
        validated_filters = {**validated_filters, **_inbound}

    _future_msg = _future_period_message(validated_filters)
    if _future_msg:
        if response_mode == "wide":
            return ChatResponse(
                report_key=report_key,
                answer=AnswerData(type="text", value=_future_msg),
                answer_text=_future_msg,
                session_id=session_id,
                blocks=[{"type": "text", "content": _future_msg}],
            )
        return ChatResponse(
            report_key=report_key,
            answer=AnswerData(type="text", value=_future_msg),
            answer_text=_future_msg,
            session_id=session_id,
        )

    # "details / list / breakup" on a count-style metric — the user wants the
    # entities themselves (which customers/designs/bills), not the count again.
    if (
        not parsed.dimension
        and re.search(r"\b(details?|breakup|breakdown|list)\b", resolved_question.lower())
    ):
        from app.services.column_registry import get_detail_dimension, get_default_metric
        _detail_dim = get_detail_dimension(report_key, parsed.metric)
        if _detail_dim:
            parsed.dimension = _detail_dim
            intent_spec.dimension = _detail_dim
            # "Detail" of a unique-count metric should show a real value per
            # entity (e.g. sales) — a bare row count adds no information.
            _default_metric = get_default_metric(report_key)
            if _default_metric:
                parsed.metric = _default_metric
                intent_spec.metric_key = _default_metric
                parsed.aggregation = "sum"
                intent_spec.aggregation = "sum"
            else:
                # count_distinct with a dimension is not supported by the SP.
                parsed.aggregation = "count"
                intent_spec.aggregation = "count"
            parsed.limit = max(parsed.limit or 0, 50)
            intent_spec.limit = parsed.limit

    entry = registry.get(report_key)
    if entry is None:
        if not report_key or not report_key.strip():
            msg = "I couldn't determine which report this question belongs to. Please specify a report type (e.g., sales, purchase, stock) or rephrase your question."
        else:
            msg = f"I couldn't find a report named '{report_key}'. Available reports: {', '.join(sorted(registry.keys()))}."
        if response_mode == "wide":
            return ChatResponse(
                error=msg,
                session_id=session_id,
                blocks=[{"type": "error", "content": msg}],
            )
        return ChatResponse(
            error=msg,
            session_id=session_id,
        )
    conversation_store.set_last_report(session_id, report_key)
    conversation_store.set_last_intent(
        session_id,
        report_key=report_key,
        metric=parsed.metric,
        dimension=parsed.dimension or "",
        resolved_question=resolved_question,
    )

    # ── Handle clarification: LLM detected query is too broad ──
    if parsed.clarify:
        clarify_msg = parsed.clarify
        # Store in conversation history so follow-up questions have context
        conversation_store.add_message(session_id, "user", resolved_question)
        conversation_store.add_message(session_id, "assistant", clarify_msg)
        log_request_trace(
            request_id=getattr(request.state, "request_id", ""), intent=query_plan.intent,
            confidence=query_plan.confidence, query_plan=query_plan.model_dump(mode="json"),
            failure_stage="clarification",
        )
        conversation_store.set_pending(session_id, {
            "kind": "generic", "question": resolved_question, "report_key": report_key,
        })
        if response_mode == "wide":
            from app.services.block_builder import build_clarify_blocks
            blocks = build_clarify_blocks(report_key)
            # Replace the generic content with the LLM's clarify message
            if blocks and blocks[0].get("type") == "clarify":
                blocks[0]["content"] = clarify_msg
            return ChatResponse(
                report_key=report_key,
                answer=AnswerData(type="text", value=clarify_msg),
                answer_text=clarify_msg,
                session_id=session_id,
                blocks=blocks,
            )
        return ChatResponse(
            report_key=report_key,
            answer=AnswerData(type="text", value=clarify_msg),
            answer_text=clarify_msg,
            session_id=session_id,
        )

    from app.services.column_registry import _load_registry as _load_col_reg
    _rcfg = _load_col_reg().get(report_key, {})
    # A structurally complete ranking parse (metric + dimension) is safe to
    # execute even when the LLM under-reports confidence — e.g. "least/worst"
    # questions reliably parse to sort=asc but score below the gate.
    _complete_ranking = bool(parsed.metric and parsed.dimension)
    # A pinned report resolves the ROUTE, not the parse — a confused metric/
    # filter extraction on a frontend-selected report is still a wrong answer.
    # Keep a lower floor (0.5) for that path so borderline parses don't spam
    # clarifications when the user already scoped the report.
    _model_conf = getattr(parsed, "model_confidence", query_plan.confidence)
    # On a pinned report an informative parse (the model made real choices —
    # dimension/filters/ai_where, including rescued ones) is a valid plan even
    # when the model under-reports confidence; the floor only guards empty,
    # guessy parses.
    _pinned_informative = bool(
        resolved_report_name
        and (parsed.dimension or parsed.filters or parsed.ai_where
             or (parsed.metric and parsed.metric != "Amount"))
    )
    _low_conf = (_model_conf < 0.50 and not _pinned_informative) if resolved_report_name else (query_plan.confidence < 0.75)
    if _low_conf and not market_ctx and not _complete_ranking:
        msg = "I’m not confident which report or metric you mean. Please specify the report and metric."
        log_request_trace(
            request_id=getattr(request.state, "request_id", ""), intent=query_plan.intent,
            confidence=query_plan.confidence, query_plan=query_plan.model_dump(mode="json"),
            failure_stage="low_confidence",
        )
        return ChatResponse(
            status="clarify", report_key=report_key,
            answer=AnswerData(type="text", value=msg), answer_text=msg,
            session_id=session_id, blocks=[{"type": "clarify", "content": msg}],
        )

    # If the LLM requested a dimension that was dropped (filter-only / not a
    # real groupable column e.g. SalesRep, CustomerType), clarify instead of
    # returning a misleading grand total.
    if parsed.dimension and not intent_spec.dimension:
        from app.services.semantic_query_parser import _build_dimension_catalog
        _dims = _build_dimension_catalog(report_key, _rcfg)
        _avail = [d.get("label") or d["name"] for d in _dims]
        # Keep the suggestion list readable — drop internal/technical columns.
        _skip = ("image", "identifier", "internal code", "caching", "random")
        _avail = [a for a in _avail if not any(s in a.lower() for s in _skip)][:10]
        clarify_msg = (
            f"This report doesn't have a '{parsed.dimension}' breakdown."
            + (f" You can group by: {', '.join(_avail)}." if _avail else "")
        )
        conversation_store.add_message(session_id, "user", resolved_question)
        conversation_store.add_message(session_id, "assistant", clarify_msg)
        log_request_trace(
            request_id=getattr(request.state, "request_id", ""), intent=query_plan.intent,
            confidence=query_plan.confidence, query_plan=query_plan.model_dump(mode="json"),
            failure_stage="clarification",
        )
        if response_mode == "wide":
            return ChatResponse(
                report_key=report_key,
                answer=AnswerData(type="text", value=clarify_msg),
                answer_text=clarify_msg,
                session_id=session_id,
                blocks=[{"type": "clarify", "content": clarify_msg}],
            )
        return ChatResponse(
            report_key=report_key,
            answer=AnswerData(type="text", value=clarify_msg),
            answer_text=clarify_msg,
            session_id=session_id,
        )

    if validated_filters:
        conversation_store.set_last_filters(session_id, validated_filters)

    # 5. Permission check is delegated to the Node API via company_code + user_id.
    # 6. Call report API (real API or dummy DB depending on config)
    tracer.start("execute")
    try:
        if settings.use_real_api:
            from app.services.real_api_client import call_real_report_api, get_report_sp_map

            sp_map = get_report_sp_map()
            report_meta = sp_map.get(report_key, {})
            # Under AUTH_REQUIRED the client may not choose SP/yearcode/ERP user.
            sp_number, yearcode, appuserid = _resolve_exec_scope(
                request, body, report_meta, user_id
            )
            ip_address = body.ip_address or getattr(request, "client", None)
            if hasattr(ip_address, "host"):
                ip_address = ip_address.host
            elif ip_address is None:
                ip_address = "127.0.0.1"

            data = await call_real_report_api(
                report_key=report_key,
                intent_spec=intent_spec,
                validated_filters=validated_filters,
                appuserid=appuserid,
                ip_address=ip_address,
                yearcode=yearcode,
                sp_number=sp_number,
                ai_where_clause=ai_where,
            )
        else:
            data = await call_report_api(
                report_key,
                validated_filters,
                company_code,
                user_id,
                registry,
            )
        tracer.end("execute", detail=f"rows={_record_count(data) if isinstance(data, dict) else '?'}")
    except ReportApiError as exc:
        tracer.end("execute", outcome="error", detail=f"api_status={exc.status_code}")
        log_request_trace(request_id=getattr(request.state, "request_id", ""), intent=query_plan.intent, confidence=query_plan.confidence, query_plan=query_plan.model_dump(mode="json"), failure_stage="execution")
        logger.error("Report API error: status=%s body=%s", exc.status_code, exc.body[:300])
        msg = "Unable to fetch report data. Please try again."
        if response_mode == "wide":
            return ChatResponse(report_key=report_key, error=msg,
                blocks=[{"type": "error", "content": msg}])
        return ChatResponse(report_key=report_key, error=msg)
    except RealApiError as exc:
        tracer.end("execute", outcome="error", detail=f"api_status={exc.status_code}")
        log_request_trace(request_id=getattr(request.state, "request_id", ""), intent=query_plan.intent, confidence=query_plan.confidence, query_plan=query_plan.model_dump(mode="json"), failure_stage="execution")
        logger.error("Real API error: status=%s body=%s", exc.status_code, exc.body[:300])
        msg = "Unable to fetch report data. Please try again."
        if response_mode == "wide":
            return ChatResponse(report_key=report_key, error=msg,
                blocks=[{"type": "error", "content": msg}])
        return ChatResponse(report_key=report_key, error=msg)
    except Exception as exc:
        tracer.end("execute", outcome="error", detail=str(exc)[:150])
        log_request_trace(request_id=getattr(request.state, "request_id", ""), intent=query_plan.intent, confidence=query_plan.confidence, query_plan=query_plan.model_dump(mode="json"), failure_stage="execution")
        logger.exception("Report API call failed: %s", exc)
        msg = "Unable to reach the report service. Please try again."
        if response_mode == "wide":
            return ChatResponse(report_key=report_key, error=msg,
                blocks=[{"type": "error", "content": msg}])
        return ChatResponse(report_key=report_key, error=msg)

    if isinstance(data, dict) and isinstance(data.get("rows"), list):
        returned_count = len(data["rows"])
        total_count = int(data.get("total_count", returned_count) or returned_count)
        query_plan.results_limited = query_plan.results_limited or total_count > returned_count

    # 7. Large-result handling -- handled by the answer generator (caps rows sent to the LLM).
    # 8. Answer generation via deterministic pipeline
    answer_start = time.time()
    # Build assumptions from validated filters (e.g. default date range).
    # Skip when the date was explicit in the question (parsed.date_filter set)
    # — an explicit "today"/"this month" is not an assumption, and the period
    # block already shows the range.
    assumptions: list = []
    date_was_explicit = bool(getattr(parsed, "date_filter", None))
    if not date_was_explicit and (
        validated_filters.get("start_date") or validated_filters.get("end_date")
    ):
        sd = block_builder.format_date_friendly(validated_filters.get("start_date", ""))
        ed = block_builder.format_date_friendly(validated_filters.get("end_date", ""))
        if sd and ed:
            assumptions.append(f"Date range {sd} to {ed}")
        elif sd:
            assumptions.append(f"From {sd}")
        elif ed:
            assumptions.append(f"Up to {ed}")

    # Fail-safe visibility: an LLM-supplied filter that failed validation was
    # dropped — the result is unfiltered. Say so instead of presenting grand
    # totals as if the filter applied.
    if getattr(parsed, "ai_where_dropped", False) or (
        isinstance(data, dict) and data.get("ai_where_dropped")
    ) or getattr(query_plan, "dropped_filters", None):
        assumptions.append(
            "Some filters in your question couldn't be applied — results shown are unfiltered."
        )

    # ── Period wording detection (shared by multi-period & comparison paths) ──
    _q_lower = body.question.lower()
    _is_growth_query = any(kw in _q_lower for kw in (
        "growth", "compared", "compare", "previous period", "vs last", "versus last",
    ))
    from app.services.orchestrator import detect_periods, is_time_dimension
    _period_mentions = (
        detect_periods(resolved_question)
        if not _is_growth_query
           and (not parsed.dimension or is_time_dimension(parsed.dimension))
        else []
    )

    # ── Multi-metric support: fetch extra metrics via additional SP calls ──
    if parsed.extra_metrics and not parsed.dimension and len(_period_mentions) < 2:
        from app.services.semantic_query_parser import get_metric_unit, get_metric_unit_label

        # Build ordered results list: primary metric first, then extras
        metric_results = []

        # Primary metric from the already-fetched data
        primary_resolved = _resolve_metric(data, intent_spec, body.question)
        metric_results.append({
            "metric_key": parsed.metric,
            "value": primary_resolved.value,
            "unit": intent_spec.unit,
            "unit_label": getattr(intent_spec, "unit_label", ""),
            "label": primary_resolved.label,
        })

        from app.services.orchestrator import execute_plan
        from app.services.query_plan import QueryStep
        query_plan.steps = [QueryStep(type="metric_fetch", metric=metric) for metric in parsed.extra_metrics]

        async def _fetch_metric_step(step):
            extra_spec = parsed.to_intent_spec()
            extra_spec.metric_key = step.metric
            extra_spec.intent = f"semantic_{step.metric}"
            # Companions keep their own natural aggregation — a count
            # question's amount companion must SUM, otherwise Layer 3
            # degrades it to total_count and shows the count as rupees.
            from app.services.column_registry import _REGISTRY as _CREG
            cat = (_CREG.get(report_key, {}).get("metric_catalog") or {}).get(step.metric, {})
            extra_spec.aggregation = "count" if str(cat.get("type", "")).lower() == "count" else "sum"
            extra_spec.unit = get_metric_unit(step.metric)
            extra_spec.unit_label = get_metric_unit_label(step.metric, report_key)
            if settings.use_real_api:
                extra_data = await call_real_report_api(
                    report_key=report_key, intent_spec=extra_spec,
                    validated_filters=validated_filters, appuserid=appuserid,
                    ip_address=ip_address, yearcode=yearcode, sp_number=sp_number,
                    ai_where_clause=ai_where,
                )
            else:
                extra_data = await call_report_api(
                    report_key, validated_filters, company_code, user_id, registry,
                )
            resolved = _resolve_metric(extra_data, extra_spec, body.question)
            return {
                "metric_key": step.metric, "value": resolved.value,
                "unit": extra_spec.unit, "unit_label": getattr(extra_spec, "unit_label", ""),
                "label": resolved.label,
            }

        for outcome in await execute_plan(query_plan, _fetch_metric_step):
            if outcome.error:
                logger.warning("Failed to fetch extra metric %s: %s", outcome.step.metric, outcome.error)
                metric_results.append({
                    "metric_key": outcome.step.metric, "value": None,
                    "unit": get_metric_unit(outcome.step.metric),
                    "unit_label": get_metric_unit_label(outcome.step.metric, report_key),
                    "label": outcome.step.metric,
                })
            else:
                metric_results.append(outcome.data)

        record_count = _record_count(data)
        source_report = report_key.replace("_", " ").title()

        if response_mode == "wide":
            blocks = block_builder.build_multi_metric_blocks(
                metric_results, body.question, record_count, source_report, assumptions,
                filters=validated_filters,
            )
            answer_text = block_builder.blocks_to_text(blocks)
        else:
            # Normal mode: build text answer with all metrics listed
            lines = []
            for r in metric_results:
                val = r.get("value")
                unit = r.get("unit", "currency")
                ulabel = r.get("unit_label", "")
                label = r.get("label", r.get("metric_key", ""))
                if val is not None:
                    from app.services.block_builder import _display_value
                    lines.append(f"{label}: {_display_value(val, unit, 'INR', ulabel)}")
                else:
                    lines.append(f"{label}: N/A")
            if record_count > 0:
                lines.append(f"Transactions: {record_count}")
            lines.append(f"Sources: {source_report}")
            answer_text = "\n".join(lines)
            blocks = None

        response = ChatResponse(
            report_key=report_key,
            answer=AnswerData(type="text", value=answer_text or ""),
            answer_text=answer_text,
            filters=validated_filters,
            session_id=session_id,
            blocks=blocks,
            metadata=Metadata(
                session_id=session_id, request_id=getattr(request.state, "request_id", None),
                record_count=record_count, intent_confidence=query_plan.confidence,
                complexity=query_plan.complexity.value, results_limited=query_plan.results_limited,
            ),
        )

        # Cache the multi-metric response
        if cache is not None and not body.export:
            await cache.store(
                resolved_question, company_code, user_id, response, response_mode,
                report_key=report_key,
            )

        # Store conversation history for follow-up resolution
        conversation_store.add_message(session_id, "user", resolved_question)
        conversation_store.add_message(session_id, "assistant", answer_text or "")

        # Log the chat exchange
        log_chat_exchange(
            question=resolved_question,
            answer=answer_text or "",
            report_key=report_key,
            filters=validated_filters,
            session_id=session_id,
            latency_ms=round((time.time() - start) * 1000, 1),
        )
        log_request_trace(
            request_id=getattr(request.state, "request_id", ""), intent=query_plan.intent,
            confidence=query_plan.confidence, query_plan=query_plan.model_dump(mode="json"),
            stage_latencies=tracer.latencies,
            result_count=record_count, cache_hit=False,
            failure_stage=tracer.failure_stage,
        )
        tracer.emit(question=resolved_question, status="success", report_key=report_key,
                    extra={"path": "multi_metric"})
        return response

    # ── Growth/comparison support: detect "growth %" or "compared with previous" ──
    if _is_growth_query and not parsed.dimension:
        from app.services.formatters import format_percentage
        from app.services.orchestrator import compute_previous_period

        prev_result = compute_previous_period(validated_filters, parsed.date_filter)
        if prev_result is None:
            _is_growth_query = False  # can't compute without a date range
        else:
            prev_filters, cur_start, cur_end = prev_result
            if not validated_filters.get("start_date") and cur_start:
                validated_filters["start_date"] = cur_start
                validated_filters["end_date"] = cur_end
                assumptions.append(
                    f"Date range {block_builder.format_date_friendly(cur_start)} to {block_builder.format_date_friendly(cur_end)}"
                )

        if _is_growth_query:
            try:
                from app.services.orchestrator import execute_plan
                from app.services.query_plan import DateRange, QueryStep
                query_plan.steps = [QueryStep(
                    type="period_comparison", metric=query_plan.metric,
                    date_range=DateRange(start=prev_filters["start_date"], end=prev_filters["end_date"]),
                )]

                async def _fetch_period_step(step):
                    step_filters = dict(validated_filters)
                    step_filters.update(step.date_range.resolved())
                    if settings.use_real_api:
                        return await call_real_report_api(
                            report_key=report_key, intent_spec=intent_spec,
                            validated_filters=step_filters, appuserid=appuserid,
                            ip_address=ip_address, yearcode=yearcode, sp_number=sp_number,
                            ai_where_clause=ai_where,
                        )
                    return await call_report_api(
                        report_key, step_filters, company_code, user_id, registry,
                    )

                period_results = await execute_plan(query_plan, _fetch_period_step)
                if period_results[0].error:
                    raise RuntimeError(period_results[0].error)
                prev_data = period_results[0].data
                current_val = _resolve_metric(data, intent_spec, body.question)
                prev_val = _resolve_metric(prev_data, intent_spec, body.question)
                cur_v = current_val.value if current_val.value else 0
                prev_v = prev_val.value if prev_val.value else 0
                if prev_v != 0:
                    growth_pct = ((cur_v - prev_v) / abs(prev_v)) * 100
                else:
                    growth_pct = None
                record_count = _record_count(data)
                source_report = report_key.replace("_", " ").title()

                # Build assumptions for both periods
                growth_assumptions = list(assumptions)
                growth_assumptions.append(
                    f"Previous period: {block_builder.format_date_friendly(prev_filters['start_date'])}"
                    f" to {block_builder.format_date_friendly(prev_filters['end_date'])}"
                )

                from app.services.block_builder import _display_value
                _u = getattr(intent_spec, "unit", "currency") or "currency"
                _ul = getattr(intent_spec, "unit_label", "") or ""
                if response_mode == "wide":
                    blocks = [
                        {"type": "heading", "content": body.question},
                        {"type": "table", "columns": ["Period", "Value", "Growth %"],
                         "rows": [
                            ["Current", _display_value(cur_v, _u, "INR", _ul), ""],
                            ["Previous", _display_value(prev_v, _u, "INR", _ul), ""],
                            ["Growth", "", format_percentage(growth_pct) if growth_pct is not None else "N/A"],
                         ]},
                        {"type": "text", "content": f"Transactions: {record_count}"},
                        {"type": "text", "content": f"Sources: {source_report}"},
                    ]
                    for a in growth_assumptions:
                        blocks.append({"type": "assumption", "content": a})
                    answer_text = block_builder.blocks_to_text(blocks)
                else:
                    lines = [
                        f"Current period: {_display_value(cur_v, _u, 'INR', _ul)}",
                        f"Previous period: {_display_value(prev_v, _u, 'INR', _ul)}",
                    ]
                    if growth_pct is not None:
                        lines.append(f"Growth: {format_percentage(growth_pct)}")
                    else:
                        lines.append("Growth: N/A (previous period was zero)")
                    if record_count > 0:
                        lines.append(f"Transactions: {record_count}")
                    lines.append(f"Sources: {source_report}")
                    answer_text = "\n".join(lines)
                    blocks = None

                response = ChatResponse(
                    report_key=report_key,
                    answer=AnswerData(type="text", value=answer_text or ""),
                    answer_text=answer_text,
                    filters=validated_filters,
                    assumptions=growth_assumptions,
                    session_id=session_id,
                    blocks=blocks,
                    metadata=Metadata(
                        session_id=session_id, request_id=getattr(request.state, "request_id", None),
                        record_count=record_count, intent_confidence=query_plan.confidence,
                        complexity=query_plan.complexity.value, results_limited=query_plan.results_limited,
                    ),
                )
                if cache is not None and not body.export:
                    await cache.store(
                        resolved_question, company_code, user_id, response, response_mode,
                        report_key=report_key,
                    )
                # Store conversation history for follow-up resolution
                conversation_store.add_message(session_id, "user", resolved_question)
                conversation_store.add_message(session_id, "assistant", answer_text or "")
                log_chat_exchange(
                    question=resolved_question,
                    answer=answer_text or "",
                    report_key=report_key,
                    filters=validated_filters,
                    session_id=session_id,
                    latency_ms=round((time.time() - start) * 1000, 1),
                )
                log_request_trace(
                    request_id=getattr(request.state, "request_id", ""), intent=query_plan.intent,
                    confidence=query_plan.confidence, query_plan=query_plan.model_dump(mode="json"),
                    stage_latencies=tracer.latencies,
                    result_count=record_count, cache_hit=False,
                    failure_stage=tracer.failure_stage,
                )
                tracer.emit(question=resolved_question, status="success", report_key=report_key,
                            extra={"path": "comparison"})
                return response
            except Exception as exc:
                logger.warning("Growth comparison failed: %s", exc)
                assumptions.append("Previous-period comparison was unavailable; showing the current period only")

    # ── Multi-period support: "gross wt for this year, this month and today" ──
    # When a question names 2+ distinct periods (and is not a growth/comparison
    # question), fetch each period and present them side by side.
    if len(_period_mentions) >= 2:
        from app.services.orchestrator import resolve_preset_dates, period_label
        try:
                from dataclasses import replace as _dc_replace
                period_spec = _dc_replace(intent_spec, dimension="")
                record_count = _record_count(data)
                period_rows = []
                for preset in _period_mentions:
                    p_start, p_end = resolve_preset_dates(preset)
                    if not p_start:
                        continue
                    step_filters = dict(validated_filters)
                    step_filters["start_date"] = p_start
                    step_filters["end_date"] = p_end
                    if settings.use_real_api:
                        pdata = await call_real_report_api(
                            report_key=report_key, intent_spec=period_spec,
                            validated_filters=step_filters, appuserid=appuserid,
                            ip_address=ip_address, yearcode=yearcode, sp_number=sp_number,
                            ai_where_clause=ai_where,
                        )
                    else:
                        pdata = await call_report_api(
                            report_key, step_filters, company_code, user_id, registry,
                        )
                    p_resolved = _resolve_metric(pdata, period_spec, body.question)
                    period_rows.append((period_label(preset), p_resolved.value, _record_count(pdata)))

                if len(period_rows) >= 2:
                    from app.services.block_builder import _display_value
                    _u = getattr(intent_spec, "unit", "currency") or "currency"
                    _ul = getattr(intent_spec, "unit_label", "") or ""
                    metric_label = getattr(intent_spec, "label", "") or parsed.metric
                    src = report_key.replace("_", " ").title()

                    if response_mode == "wide":
                        blocks = [
                            {"type": "heading", "content": body.question},
                            {"type": "table", "columns": ["Period", metric_label, "Rows"],
                             "rows": [[lbl, _display_value(v or 0, _u, "INR", _ul), str(rc)]
                                      for lbl, v, rc in period_rows]},
                            {"type": "text", "content": f"Sources: {src}"},
                        ]
                        for a in assumptions:
                            blocks.append({"type": "assumption", "content": a})
                        answer_text = block_builder.blocks_to_text(blocks)
                    else:
                        lines = [
                            f"{lbl}: {_display_value(v or 0, _u, 'INR', _ul)} ({rc} transactions)"
                            for lbl, v, rc in period_rows
                        ]
                        lines.append(f"Sources: {src}")
                        answer_text = "\n".join(lines)
                        blocks = None

                    response = ChatResponse(
                        report_key=report_key,
                        answer=AnswerData(type="text", value=answer_text or ""),
                        answer_text=answer_text,
                        filters=validated_filters,
                        assumptions=assumptions,
                        session_id=session_id,
                        blocks=blocks,
                        metadata=Metadata(
                            session_id=session_id, request_id=getattr(request.state, "request_id", None),
                            record_count=record_count, intent_confidence=query_plan.confidence,
                            complexity=query_plan.complexity.value, results_limited=query_plan.results_limited,
                        ),
                    )
                    if cache is not None and not body.export:
                        await cache.store(
                            resolved_question, company_code, user_id, response, response_mode,
                            report_key=report_key,
                        )
                    conversation_store.add_message(session_id, "user", resolved_question)
                    conversation_store.add_message(session_id, "assistant", answer_text or "")
                    log_chat_exchange(
                        question=resolved_question,
                        answer=answer_text or "",
                        report_key=report_key,
                        filters=validated_filters,
                        session_id=session_id,
                        latency_ms=round((time.time() - start) * 1000, 1),
                    )
                    log_request_trace(
                        request_id=getattr(request.state, "request_id", ""), intent=query_plan.intent,
                        confidence=query_plan.confidence, query_plan=query_plan.model_dump(mode="json"),
                        stage_latencies=tracer.latencies,
                        result_count=record_count, cache_hit=False,
                    )
                    tracer.emit(question=resolved_question, status="success", report_key=report_key,
                                extra={"path": "multi_period"})
                    return response
        except Exception as exc:
            logger.warning("Multi-period query failed: %s", exc)
            # fall through to normal single-period answer

    tracer.start("answer")
    try:
        answer = await generate_answer(
            data,
            entry,
            validated_filters,
            assumptions,
            body.question,
            history=history,
            intent_spec=intent_spec,
            response_mode=response_mode,
        )
        tracer.end("answer")
    except Exception as exc:
        tracer.end("answer", outcome="error", detail=str(exc)[:150])
        tracer.emit(question=resolved_question, status="error", report_key=report_key)
        log_request_trace(request_id=getattr(request.state, "request_id", ""), intent=query_plan.intent, confidence=query_plan.confidence, query_plan=query_plan.model_dump(mode="json"), stage_latencies=tracer.latencies, result_count=_record_count(data), failure_stage="answer_generation")
        LLM_CALLS.labels(provider="unknown", tier="cheap", status="error").inc()
        logger.exception("Answer generation failed: %s", exc)
        msg = "Something went wrong generating this response. Please try again."
        if response_mode == "wide":
            return ChatResponse(
                report_key=report_key, filters=validated_filters, error=msg,
                blocks=[{"type": "error", "content": msg}],
            )
        return ChatResponse(
            report_key=report_key,
            filters=validated_filters,
            error=msg,
        )

    answer_latency = time.time() - answer_start
    # Record LLM metrics for the answer generation call
    for call in token_usage_calls:
        provider = call.get("provider", "unknown")
        LLM_LATENCY.labels(provider=provider, tier="cheap").observe(answer_latency)

    # 9. Optional export link for large list results
    download_url: Optional[str] = None
    if body.export and entry.response_mode == "list":
        try:
            download_url = await export_report_api(
                report_key,
                validated_filters,
                company_code,
                user_id,
                registry,
            )
        except ReportApiError:
            # Export is best-effort inside /chat; the answer still explains how to get the full list.
            pass

    # 10. Cache + return answer (do not cache export responses; download URLs may expire)
    token_usage = usage_store.build_token_usage(token_usage_calls)
    # Handle wide mode: answer is a list of block dicts
    blocks = None
    answer_text = answer
    if response_mode == "wide" and isinstance(answer, list):
        blocks = answer
        answer_text = block_builder.blocks_to_text(blocks)

    # Dropped-filter notice: never present unfiltered totals as if the
    # requested filter applied — surface it inline AND as an assumption block.
    if getattr(parsed, "ai_where_dropped", False) or (
        isinstance(data, dict) and data.get("ai_where_dropped")
    ) or getattr(query_plan, "dropped_filters", None):
        notice = "Note: some filters couldn't be applied — results shown are unfiltered."
        if answer_text and notice not in answer_text:
            answer_text = f"{answer_text}\n{notice}"
        if blocks is not None:
            blocks = blocks + [{"type": "assumption",
                                "content": "Some filters in your question couldn't be applied — results shown are unfiltered."}]

    # Market-context note: honest boundary between external market info and
    # internal ERP facts. External claims (when a curated snapshot exists) are
    # always attributed with source + retrieval date.
    if market_ctx:
        market_note = build_market_note(market_ctx, market_snapshot)
        answer_text = f"{market_note}\n\n{answer_text}" if answer_text else market_note
        market_block = {
            "type": "market_context",
            "note": market_note,
            "sources": [
                {
                    "name": e.get("source_name"),
                    "url": e.get("source_url"),
                    "retrieved_at": e.get("retrieved_at"),
                }
                for e in market_snapshot
            ],
        }
        blocks = (blocks or []) + [market_block]

    # Build structured response for frontend
    report_name = report_key.replace("_", " ").title() if report_key else ""
    record_count = _record_count(data) if 'data' in dir() else 0

    # Extract primary metric from blocks for structured answer
    answer_data = None
    if blocks:
        for b in blocks:
            if b.get("type") == "metric_card":
                answer_data = AnswerData(
                    type="metric",
                    title=b.get("label", ""),
                    value=b.get("value", ""),
                    raw_value=b.get("raw_value"),
                    unit=b.get("unit", ""),
                    unit_label=b.get("unit_label", ""),
                    currency=b.get("currency"),
                    subtext=b.get("subtext", ""),
                )
                break
    # Normal mode has no blocks — carry the text answer in the modern `answer`
    # field (legacy `answer_text` is excluded from the API response).
    if answer_data is None and answer_text:
        answer_data = AnswerData(type="text", value=answer_text)

    # Build period info from filters
    period_info = None
    if validated_filters:
        start = validated_filters.get("start_date")
        end = validated_filters.get("end_date")
        if start or end:
            from datetime import date as _date, timedelta as _td
            _today = _date.today().isoformat()
            _yesterday = (_date.today() - _td(days=1)).isoformat()
            if start == end == _today:
                _label = "Today"
            elif start == end == _yesterday:
                _label = "Yesterday"
            elif start == end:
                _label = block_builder.format_date_friendly(start)
            elif start and end:
                try:
                    _s = _date.fromisoformat(start)
                    _e = _date.fromisoformat(end)
                    if _s.day == 1 and (_e + _td(days=1)).day == 1:
                        # Whole-month span: 'Aug 2026' or 'Aug 2026 – Sep 2026'
                        _fm = block_builder.format_date_friendly(start[:7])
                        _tm = block_builder.format_date_friendly(end[:7])
                        _label = _fm if _fm == _tm else f"{_fm} – {_tm}"
                    else:
                        _label = (
                            f"{block_builder.format_date_friendly(start)}"
                            f" to {block_builder.format_date_friendly(end)}"
                        )
                except ValueError:
                    _label = f"{start} to {end}"
            else:
                _label = block_builder.format_date_friendly(start or end)
            period_info = PeriodInfo(start=start, end=end, label=_label)

    response = ChatResponse(
        status="success",
        report=ReportInfo(key=report_key, name=report_name) if report_key else None,
        question=body.question,
        answer=answer_data,
        period=period_info,
        blocks=blocks,
        filters=validated_filters,
        metadata=Metadata(
            session_id=session_id,
            request_id=getattr(request.state, "request_id", None),
            record_count=record_count,
            token_usage=token_usage,
            intent_confidence=query_plan.confidence,
            complexity=query_plan.complexity.value,
            results_limited=query_plan.results_limited,
        ),
        actions=Actions(download_url=download_url),
        # Legacy fields for backward compatibility
        report_key=report_key,
        answer_text=answer_text,
        assumptions=assumptions,
        download_url=download_url,
        token_usage=token_usage,
        session_id=session_id,
    )
    # Track token usage metrics
    total_cost = 0.0
    for call in token_usage_calls:
        provider = call.get("provider", "unknown")
        LLM_TOKENS.labels(provider=provider, type="prompt").inc(call.get("prompt_tokens", 0))
        LLM_TOKENS.labels(provider=provider, type="completion").inc(call.get("completion_tokens", 0))
        LLM_CALLS.labels(provider=provider, tier="cheap", status="success").inc()
        total_cost += call.get("estimated_cost_usd", 0)
    if total_cost > 0:
        logger.info("LLM cost for this request: $%.6f", total_cost)

    usage_store.log_token_usage(
        endpoint="/chat",
        company_code=company_code,
        user_id=user_id,
        question=resolved_question,
        report_key=report_key,
        token_usage=token_usage,
    )
    conversation_store.add_message(session_id, "user", resolved_question)
    conversation_store.add_message(session_id, "assistant", answer_text)
    if cache is not None and not response.error and not body.export:
        await cache.store(
            resolved_question, company_code, user_id, response, response_mode,
            report_key=report_key,
        )
        CACHE_SIZE.set(len(cache.cache) if hasattr(cache, 'cache') else 0)

    # Log the full chat exchange (question + answer + context)
    log_chat_exchange(
        question=resolved_question,
        answer=answer_text or "",
        report_key=report_key,
        filters=validated_filters,
        session_id=session_id,
        error=response.error or "",
        latency_ms=round((time.time() - answer_start) * 1000, 1),
    )

    log_request_trace(
        request_id=getattr(request.state, "request_id", ""),
        intent=query_plan.intent,
        confidence=query_plan.confidence,
        query_plan=query_plan.model_dump(mode="json"),
        stage_latencies=tracer.latencies,
        result_count=record_count,
        cache_hit=False,
        failure_stage=tracer.failure_stage,
    )
    tracer.emit(question=resolved_question, status="success", report_key=report_key)
    return response


async def run_action(
    body: ChatActionRequest, request: Request, registry: Dict[str, Any], cache: Any,
) -> ChatResponse:
    """Resolve a structured widget action into a governed re-run of run_chat.

    The click payload is validated against the pending clarify state the
    server recorded when it emitted the widget — the client can only choose
    among the options it was actually offered, and the user's intent is
    reconstructed deterministically instead of re-parsing display text.
    """
    company_code, user_id, session_id, response_mode = prepare_chat(body, request)
    action = body.action or {}
    action_type = str(action.get("type", ""))
    payload = action.get("payload") if isinstance(action.get("payload"), dict) else {}
    display = str(action.get("display") or "")

    def _clarify(msg: str) -> ChatResponse:
        return ChatResponse(
            status="clarify", session_id=session_id,
            answer=AnswerData(type="text", value=msg), answer_text=msg,
            blocks=[{"type": "clarify", "content": msg, "blocking": False}]
            if response_mode == "wide" else None,
        )

    def _request(question: str, filters: Optional[Dict[str, Any]] = None) -> ChatRequest:
        return ChatRequest(
            question=question, session_id=session_id,
            company_code=company_code, user_id=user_id,
            response_mode=response_mode,
            report_name=body.report_name, pid=body.pid,
            filters=filters, from_action=True,
        )

    # Suggestion chips need no pending state — a verbatim question.
    if action_type == "send_message":
        message = str(payload.get("message") or "").strip()
        if not message:
            return _clarify("That option didn't include a question — please type it instead.")
        if len(message) > settings.max_question_length:
            message = message[: settings.max_question_length]
        if display:
            conversation_store.add_message(session_id, "user", display)
        conversation_store.clear_pending(session_id)
        return await run_chat(_request(message), request, registry, cache)

    pending = conversation_store.get_pending(session_id)
    if not pending:
        return _clarify(
            "That option has expired — please retype or re-ask your question."
        )
    kind = pending.get("kind")

    if action_type == "select_option":
        if kind != "entity_disambiguation":
            return _clarify("That option doesn't match the pending question — please retype it.")
        option_id = str(payload.get("option_id") or "")
        option = (pending.get("options") or {}).get(option_id)
        base_q = str(pending.get("question") or "")
        entity = str(pending.get("entity_value") or "")
        if not option or not base_q or not entity:
            return _clarify("That option is no longer valid — please retype your question.")
        # Deterministic rewrite: "VIDSY total sales" → "customer VIDSY total
        # sales". The explicit-field extractor lands the filter without the
        # LLM having to infer it. Server-side entity value, not the payload's.
        inject = str(option.get("inject") or option_id)
        question, n = re.subn(
            re.escape(entity), f"{inject} {entity}", base_q,
            count=1, flags=re.IGNORECASE,
        )
        if not n:
            question = f"{base_q} for {inject} {entity}"
        conversation_store.clear_pending(session_id)
        conversation_store.add_message(
            session_id, "user", display or f"{option.get('label', option_id)}: {entity}"
        )
        return await run_chat(_request(question), request, registry, cache)

    if action_type == "set_date_range":
        if kind != "date_range":
            return _clarify("That action doesn't match the pending question — please retype it.")
        start = str(payload.get("start_date") or "")
        end = str(payload.get("end_date") or "")
        preset = str(payload.get("preset") or "")
        if preset and not (start or end):
            from app.services.orchestrator import resolve_preset_dates
            start, end = resolve_preset_dates(preset)
        base_q = str(pending.get("question") or "")
        if not base_q:
            return _clarify("Please re-ask your question with the new date range.")
        filters = _validate_inbound_filters({"start_date": start, "end_date": end})
        if not filters.get("start_date") or not filters.get("end_date"):
            return _clarify("I couldn't read that date range — please pick a valid start and end date.")
        conversation_store.clear_pending(session_id)
        conversation_store.add_message(
            session_id, "user",
            display or f"Date range: {filters['start_date']} to {filters['end_date']}",
        )
        return await run_chat(_request(base_q, filters=filters), request, registry, cache)

    return _clarify("That action isn't supported — please type your question instead.")


async def _chat_response_events(
    resp: ChatResponse, request: Request,
) -> AsyncIterator[Dict[str, Any]]:
    """Translate a ChatResponse into SSE-shaped event dicts.

    Shared by /chat/stream and /chat/action/stream — emitted shape:
      {"error"} | {"status":"clarify",...,"done":true} | metadata + chunks + done.
    """
    if resp.error:
        yield {"error": resp.error}
        return

    text = resp.answer_text or (resp.answer.value if resp.answer else "") or ""
    if resp.status == "clarify":
        ev: Dict[str, Any] = {
            "status": "clarify", "report_key": resp.report_key or "",
            "answer": text, "done": True,
        }
        if resp.blocks:
            ev["blocks"] = resp.blocks
        yield ev
        return

    meta = resp.metadata
    yield {
        "report_key": resp.report_key or "",
        "filters": resp.filters or {},
        "request_id": getattr(request.state, "request_id", ""),
        "intent_confidence": getattr(meta, "intent_confidence", None),
        "complexity": getattr(meta, "complexity", None),
        "record_count": getattr(meta, "record_count", 0),
        "results_limited": getattr(meta, "results_limited", False),
        "cache_hit": getattr(meta, "cache_hit", False),
    }
    if resp.blocks:
        yield {"blocks": resp.blocks}
    for i in range(0, len(text), 3):
        yield {"chunk": text[i:i + 3]}
    yield {"done": True}


async def stream_chat_events(
    body: ChatRequest, request: Request, registry: Dict[str, Any], cache: Any,
) -> AsyncIterator[Dict[str, Any]]:
    """Yield SSE-shaped event dicts for the SAME pipeline /chat uses."""
    _, _, session_id, _ = prepare_chat(body, request)
    yield {"session_id": session_id}
    try:
        resp = await run_chat(body, request, registry, cache)
    except HTTPException as exc:
        yield {"error": str(exc.detail)}
        return
    except Exception:
        logger.exception("Stream pipeline failed")
        yield {"error": "Something went wrong. Please try again."}
        return
    async for ev in _chat_response_events(resp, request):
        yield ev


async def stream_action_events(
    body: ChatActionRequest, request: Request, registry: Dict[str, Any], cache: Any,
) -> AsyncIterator[Dict[str, Any]]:
    """SSE counterpart of /chat/action — same event contract."""
    _, _, session_id, _ = prepare_chat(body, request)
    yield {"session_id": session_id}
    try:
        resp = await run_action(body, request, registry, cache)
    except HTTPException as exc:
        yield {"error": str(exc.detail)}
        return
    except Exception:
        logger.exception("Stream action pipeline failed")
        yield {"error": "Something went wrong. Please try again."}
        return
    async for ev in _chat_response_events(resp, request):
        yield ev


async def stream_chat(
    body: ChatRequest, request: Request, registry: Dict[str, Any], cache: Any,
) -> StreamingResponse:
    """/chat/stream — SSE presentation of the shared pipeline result."""
    # Validate before the SSE response starts so bad input still gets 400
    # (inside the generator it could only be an error event with HTTP 200).
    if not body.question or not body.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    if len(body.question) > settings.max_question_length:
        raise HTTPException(
            status_code=400,
            detail=f"Question too long (max {settings.max_question_length} characters)",
        )

    async def _gen():
        async for ev in stream_chat_events(body, request, registry, cache):
            yield f"data: {_json.dumps(ev)}" + "\n\n"
    return StreamingResponse(_gen(), media_type="text/event-stream")


async def stream_action(
    body: ChatActionRequest, request: Request, registry: Dict[str, Any], cache: Any,
) -> StreamingResponse:
    """/chat/action/stream — SSE presentation of a resolved widget action."""
    async def _gen():
        async for ev in stream_action_events(body, request, registry, cache):
            yield f"data: {_json.dumps(ev)}" + "\n\n"
    return StreamingResponse(_gen(), media_type="text/event-stream")
