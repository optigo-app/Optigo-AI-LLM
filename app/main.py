import re
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, StreamingResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.config import settings, validate_startup
from app.middleware.auth import AuthMiddleware, validate_session_id
from app.middleware.logging import RequestIdMiddleware, setup_logging, get_logger
from app.middleware.metrics import (
    REQUEST_COUNT, REQUEST_LATENCY, CACHE_HITS, CACHE_MISSES,
    LLM_TOKENS, LLM_CALLS, LLM_LATENCY, CACHE_SIZE,
)
from app.middleware.prompt_guard import detect_injection, sanitize_question
from app.services.spelling import normalize_spelling
from app.middleware.audit_log import log_chat_exchange, log_request_trace
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.models import ChatRequest, ChatResponse, ExportResponse, ReportRegistryEntry, FeedbackRequest, FeedbackResponse, AnswerData, ReportInfo, PeriodInfo, Metadata, Actions
from app.services.answer_generator import generate_answer, _resolve_metric, _record_count
from app.services import block_builder
from app.services.api_client import (
    ReportApiError,
    call_report_api,
    export_report_api,
    get_report_registry,
)
from app.services.real_api_client import RealApiError
from app.services.cache import ChatCache
from app.services.classifier import (
    classify_question,
    get_greeting_response,
    keyword_classify,
    list_available_reports,
    load_classifier_embeddings,
)
from app.services.filter_extractor import (
    FilterExtractError,
    extract_filters,
    merge_with_previous,
)
from app.services import conversation_store, usage_store
from app.services import feedback_store
from app.services.context_resolver import ambiguous_entity_value, is_probable_person_name, looks_like_followup, needs_date_clarification, resolve_context
from app.services.validator import validate_and_fill
from app.services.column_registry import get_suggested_questions

logger = get_logger(__name__)

limiter = Limiter(key_func=get_remote_address, default_limits=[f"{settings.rate_limit_per_minute}/minute"])


# Registry is loaded once at startup and reused for every request.
_REPORT_REGISTRY: Dict[str, ReportRegistryEntry] = {}
_CHAT_CACHE: ChatCache | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _REPORT_REGISTRY, _CHAT_CACHE
    setup_logging()
    startup_warnings = validate_startup()
    for w in startup_warnings:
        logger.warning("Startup config warning: %s", w)
    _REPORT_REGISTRY = get_report_registry()
    _CHAT_CACHE = ChatCache()
    await load_classifier_embeddings(_REPORT_REGISTRY)
    logger.info("Application started successfully")
    yield
    _REPORT_REGISTRY = {}
    if _CHAT_CACHE is not None:
        _CHAT_CACHE.close()
        _CHAT_CACHE = None
    # Close shared httpx connection pool
    from app.services.api_client import _get_client
    client = _get_client()
    await client.aclose()
    logger.info("Application shutting down")


app = FastAPI(title="OptigoApps LLM Chatbot", version="1.0.0", lifespan=lifespan)
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(RequestIdMiddleware)
app.add_middleware(AuthMiddleware)
app.add_middleware(SecurityHeadersMiddleware)
_cors_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
_is_wildcard = _cors_origins == ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=not _is_wildcard,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# API versioning: all endpoints under /v1
v1_router = APIRouter(prefix="/v1")


@app.middleware("http")
async def metrics_middleware(request: Request, call_next):
    """Track request count and latency for all endpoints."""
    start = time.time()
    response = await call_next(request)
    duration = time.time() - start
    endpoint = request.url.path
    REQUEST_COUNT.labels(
        endpoint=endpoint,
        method=request.method,
        status=str(response.status_code),
    ).inc()
    REQUEST_LATENCY.labels(endpoint=endpoint).observe(duration)
    return response


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return PlainTextResponse(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
async def health() -> Dict[str, Any]:
    """Health check with dependency status."""
    deps = {}
    all_ok = True

    # Check report registry
    deps["reports_loaded"] = len(_REPORT_REGISTRY)
    if len(_REPORT_REGISTRY) == 0:
        all_ok = False
        deps["reports_status"] = "error"
    else:
        deps["reports_status"] = "ok"

    # Check cache
    deps["cache_active"] = _CHAT_CACHE is not None

    # Check LLM API key (without making a call)
    from app.services import llm_gateway
    try:
        provider, model, api_key, base_url = llm_gateway._get_provider_config("cheap")
        deps["llm_provider"] = provider
        deps["llm_model"] = model
        deps["llm_configured"] = bool(api_key)
        if not api_key:
            all_ok = False
            deps["llm_status"] = "error: no API key"
        else:
            deps["llm_status"] = "ok"
    except Exception as e:
        deps["llm_status"] = f"error: {str(e)[:100]}"
        all_ok = False

    # Check embedding model
    try:
        deps["embedding_model"] = settings.embedding_model
        deps["embedding_configured"] = True
    except Exception:
        deps["embedding_configured"] = False

    return {
        "status": "ok" if all_ok else "degraded",
        "version": "1.0.0",
        "dependencies": deps,
    }


@v1_router.get("/health")
async def health_v1() -> Dict[str, Any]:
    return await health()


@app.post("/cache/invalidate")
def cache_invalidate_all() -> Dict[str, Any]:
    """Clear all cache entries."""
    if _CHAT_CACHE is None:
        return {"invalidated": 0}
    count = _CHAT_CACHE.invalidate_all()
    logger.info("Cache invalidated: %d entries removed", count)
    return {"invalidated": count}


@app.post("/cache/invalidate/{report_key}")
def cache_invalidate_report(report_key: str) -> Dict[str, Any]:
    """Clear cache entries for a specific report."""
    if _CHAT_CACHE is None:
        return {"invalidated": 0}
    count = _CHAT_CACHE.invalidate_report(report_key)
    logger.info("Cache invalidated for %s: %d entries removed", report_key, count)
    return {"report_key": report_key, "invalidated": count}


@v1_router.post("/cache/invalidate")
def cache_invalidate_all_v1() -> Dict[str, Any]:
    return cache_invalidate_all()


@v1_router.post("/cache/invalidate/{report_key}")
def cache_invalidate_report_v1(report_key: str) -> Dict[str, Any]:
    return cache_invalidate_report(report_key)


@app.get("/reports")
def reports() -> Dict[str, Any]:
    return {"reports": list_available_reports(_REPORT_REGISTRY)}


@v1_router.get("/reports")
def reports_v1() -> Dict[str, Any]:
    return reports()


def _resolve_report_key(report_key: Optional[str], pid: Optional[int]) -> Optional[str]:
    """Resolve a report key from report_key or pid (frontend sends pid)."""
    if report_key:
        return report_key
    if pid is not None:
        for rk, entry in _REPORT_REGISTRY.items():
            if getattr(entry, "pid", None) == pid:
                return rk
    return None


@app.get("/suggested-questions")
def suggested_questions(
    report_key: Optional[str] = None,
    pid: Optional[int] = None,
) -> Dict[str, Any]:
    """Return suggested questions per report for the chatbot welcome screen.

    - No filter: returns questions grouped by report.
    - ?report_key=X or ?pid=N: returns questions for that single report.
    """
    rk = _resolve_report_key(report_key, pid)
    if rk:
        entry = _REPORT_REGISTRY.get(rk)
        if entry is None:
            raise HTTPException(status_code=404, detail=f"Unknown report: {report_key or pid}")
        return {
            "report_key": rk,
            "report_name": rk.replace("_", " ").title(),
            "questions": get_suggested_questions(rk),
        }
    if pid is not None:
        raise HTTPException(status_code=404, detail=f"Unknown report pid: {pid}")
    return {
        "reports": [
            {
                "report_key": rk,
                "report_name": rk.replace("_", " ").title(),
                "questions": get_suggested_questions(rk),
            }
            for rk in _REPORT_REGISTRY
        ]
    }


@v1_router.get("/suggested-questions")
def suggested_questions_v1(
    report_key: Optional[str] = None,
    pid: Optional[int] = None,
) -> Dict[str, Any]:
    return suggested_questions(report_key, pid)


@app.get("/data/{report_key}")
async def get_report_data(report_key: str, request: Request) -> Dict[str, Any]:
    """Return raw report data for frontend grids."""
    entry = _REPORT_REGISTRY.get(report_key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown report key: {report_key}")

    company_code = getattr(request.state, "company_code", None) or request.query_params.get("company_code") or "DEMO"
    user_id = getattr(request.state, "user_id", None) or request.query_params.get("user_id") or "u123"

    try:
        data = await call_report_api(
            report_key, {}, company_code, user_id, _REPORT_REGISTRY,
        )
    except ReportApiError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.body)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))

    return data


@v1_router.get("/data/{report_key}")
async def get_report_data_v1(report_key: str, request: Request) -> Dict[str, Any]:
    return await get_report_data(report_key, request)


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


_CHAT_RESPONSE_EXCLUDE = {
    "report_key", "answer_text", "assumptions",
    "download_url", "token_usage", "session_id",
}


@app.post("/chat", response_model=ChatResponse, response_model_exclude=_CHAT_RESPONSE_EXCLUDE)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    """Single-entry chat endpoint with classifier + cache + filter extraction + answer generation."""
    return await _chat_impl(body, request)


@v1_router.post("/chat", response_model=ChatResponse, response_model_exclude=_CHAT_RESPONSE_EXCLUDE)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def chat_v1(body: ChatRequest, request: Request) -> ChatResponse:
    return await _chat_impl(body, request)


async def _chat_impl(body: ChatRequest, request: Request) -> ChatResponse:
    start = time.time()

    # Input validation
    if not body.question or not body.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    if len(body.question) > settings.max_question_length:
        raise HTTPException(
            status_code=400,
            detail=f"Question too long (max {settings.max_question_length} characters)",
        )

    # Sanitize session_id
    session_id = validate_session_id(body.session_id) or conversation_store.new_session_id()

    # Detect response mode early (needed for error paths + cache)
    response_mode = body.response_mode or "normal"
    # Company-wide config: if no explicit mode, check allowlist
    company_code_early = getattr(request.state, "company_code", None) or body.company_code or "DEMO"
    if not body.response_mode or body.response_mode == "normal":
        wide_companies = settings.wide_response_companies
        if wide_companies and company_code_early in wide_companies.split(","):
            response_mode = "wide"

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

    # ── Resolve report from pid (frontend sends unique report ID) ──
    # Done before the greeting check so the greeting can be report-scoped.
    resolved_report_name = body.report_name or ""
    if body.pid and not resolved_report_name:
        for rk, entry in _REPORT_REGISTRY.items():
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
        body.question, registry=_REPORT_REGISTRY, report_key=resolved_report_name or None,
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
            msg = f"What does '{ambiguous_value}' refer to: a customer, salesperson, brand, branch, or category?"
            blocks = [block_builder.build_entity_choice_block(ambiguous_value, msg)] if response_mode == "wide" else None
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

    # Use authenticated identity from middleware (cookie) with body as fallback for dev mode
    company_code = getattr(request.state, "company_code", None) or body.company_code or "DEMO"
    user_id = getattr(request.state, "user_id", None) or body.user_id or "u123"

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
    if not cache_report_name and len(_REPORT_REGISTRY) > 1:
        from app.services.intent import classify_question_by_intent
        cache_report_name = (
            classify_question_by_intent(resolved_question, _REPORT_REGISTRY)
            or keyword_classify(resolved_question, _REPORT_REGISTRY)
            or conversation_store.get_last_report(session_id)
            or ""
        )

    # Semantic cache check (skip when the caller explicitly asks for an export link,
    # or when the frontend asks for a regenerated/fresh answer).
    # Uses resolved_question so follow-ups get the right cache key, not the raw short input.
    # The cache is scoped to the resolved report (from pid/report_name) so the same
    # question asked on a different report does not return another report's answer.
    if _CHAT_CACHE is not None and not body.export and not body.regenerate:
        cached = await _CHAT_CACHE.lookup(
            resolved_question, company_code, user_id, response_mode,
            report_key=cache_report_name or None,
        )
        if cached:
            CACHE_HITS.inc()
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
            registry=_REPORT_REGISTRY,
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

    entry = _REPORT_REGISTRY.get(report_key)
    if entry is None:
        if not report_key or not report_key.strip():
            msg = "I couldn't determine which report this question belongs to. Please specify a report type (e.g., sales, purchase, stock) or rephrase your question."
        else:
            msg = f"I couldn't find a report named '{report_key}'. Available reports: {', '.join(sorted(_REPORT_REGISTRY.keys()))}."
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
    if query_plan.confidence < 0.75 and not resolved_report_name and not market_ctx and not _complete_ranking:
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

    # Use authenticated identity from middleware (cookie) with body as fallback for dev mode
    company_code = getattr(request.state, "company_code", None) or body.company_code or "DEMO"
    user_id = getattr(request.state, "user_id", None) or body.user_id or "u123"

    # 5. Permission check is delegated to the Node API via company_code + user_id.
    # 6. Call report API (real API or dummy DB depending on config)
    tracer.start("execute")
    try:
        if settings.use_real_api:
            from app.services.real_api_client import call_real_report_api, get_report_sp_map

            sp_map = get_report_sp_map()
            report_meta = sp_map.get(report_key, {})
            sp_number = body.sp or report_meta.get("sp") or settings.real_api_sp
            yearcode = body.yearcode or settings.real_api_yearcode
            appuserid = body.appuserid or user_id or "admin@orail.co.in"
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
                _REPORT_REGISTRY,
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
                    report_key, validated_filters, company_code, user_id, _REPORT_REGISTRY,
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
        if _CHAT_CACHE is not None and not body.export:
            await _CHAT_CACHE.store(
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
                        report_key, step_filters, company_code, user_id, _REPORT_REGISTRY,
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
                if _CHAT_CACHE is not None and not body.export:
                    await _CHAT_CACHE.store(
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
                            report_key, step_filters, company_code, user_id, _REPORT_REGISTRY,
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
                    if _CHAT_CACHE is not None and not body.export:
                        await _CHAT_CACHE.store(
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
                _REPORT_REGISTRY,
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
    if _CHAT_CACHE is not None and not response.error and not body.export:
        await _CHAT_CACHE.store(
            resolved_question, company_code, user_id, response, response_mode,
            report_key=report_key,
        )
        CACHE_SIZE.set(len(_CHAT_CACHE.cache) if hasattr(_CHAT_CACHE, 'cache') else 0)

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


@app.post("/chat/stream")
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def chat_stream(body: ChatRequest, request: Request):
    """SSE streaming chat endpoint — streams the answer token-by-token from the LLM."""
    return await _chat_stream_impl(body, request)


@v1_router.post("/chat/stream")
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def chat_stream_v1(body: ChatRequest, request: Request):
    return await _chat_stream_impl(body, request)


async def _chat_stream_impl(body: ChatRequest, request: Request):

    if not body.question or not body.question.strip():
        raise HTTPException(status_code=400, detail="Question cannot be empty")
    if len(body.question) > settings.max_question_length:
        raise HTTPException(
            status_code=400,
            detail=f"Question too long (max {settings.max_question_length} characters)",
        )

    session_id = validate_session_id(body.session_id) or conversation_store.new_session_id()

    injection = detect_injection(body.question)
    if injection:
        logger.warning("Prompt injection blocked: session=%s pattern=%s", session_id, injection)

        async def _blocked():
            yield 'data: {"error": "Blocked"}\n\n'
        return StreamingResponse(_blocked(), media_type="text/event-stream")

    body.question = sanitize_question(body.question)
    body.question = normalize_spelling(body.question)

    # Resolve report for a report-scoped greeting
    stream_report_name = body.report_name or ""
    if body.pid and not stream_report_name:
        for rk, entry in _REPORT_REGISTRY.items():
            if getattr(entry, "pid", None) == body.pid:
                stream_report_name = rk
                break

    # Scope guard: return friendly static messages for greetings and out-of-scope questions
    greeting_msg = get_greeting_response(
        body.question, registry=_REPORT_REGISTRY, report_key=stream_report_name or None,
    )
    if greeting_msg:
        async def _out_of_scope():
            import json as _json
            yield f'data: {_json.dumps({"answer": greeting_msg})}\n\n'
            yield f'data: {_json.dumps({"done": True})}\n\n'
        return StreamingResponse(_out_of_scope(), media_type="text/event-stream")

    if needs_date_clarification(body.question):
        async def _date_clarification():
            import json as _json
            msg = "Which date range would you like? For example: today, this month, last month, this year, or specific start and end dates."
            block = block_builder.build_date_range_input_block(msg)
            yield f'data: {_json.dumps({"status": "clarify", "answer": msg, "blocks": [block], "done": True})}\n\n'
        return StreamingResponse(_date_clarification(), media_type="text/event-stream")

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
            async def _entity_clarification():
                import json as _json
                msg = f"What does '{ambiguous_value}' refer to: a customer, salesperson, brand, branch, or category?"
                block = block_builder.build_entity_choice_block(ambiguous_value, msg)
                yield f'data: {_json.dumps({"status": "clarify", "answer": msg, "blocks": [block], "done": True})}\n\n'
            return StreamingResponse(_entity_clarification(), media_type="text/event-stream")

    async def _stream():
        import json as _json
        from app.services.answer_generator import generate_answer

        token_usage_calls: List[Dict[str, int]] = []
        from app.services.pipeline_trace import StageTracer
        tracer = StageTracer(getattr(request.state, "request_id", ""))
        history = conversation_store.get_messages(session_id, limit=10)

        # Send session_id first
        yield f'data: {_json.dumps({"session_id": session_id})}\n\n'

        # ── Semantic query parser: ONE LLM call replaces spell correction,
        #    report classification, filter extraction, intent detection, and
        #    WHERE clause generation ──

        # Resolve follow-up questions using conversation history
        tracer.start("context")
        resolved_question = await resolve_context(
            body.question, history, token_usage=token_usage_calls,
        )
        tracer.end("context", detail=resolved_question if resolved_question != body.question else "pass-through")

        from app.services.market_context import (
            build_market_note, detect_market_context, load_market_snapshot,
        )
        market_ctx = detect_market_context(resolved_question)
        market_snapshot = load_market_snapshot(market_ctx["topic"]) if market_ctx else []

        from app.services.query_planner import plan_query
        last_intent = conversation_store.get_last_intent(session_id)
        tracer.start("semantic_parse")
        try:
            planning = await plan_query(
                resolved_question, history=history, token_usage=token_usage_calls,
                report_name=stream_report_name,
                # Same rule as /chat: session filters only carry into
                # genuine follow-ups, never standalone questions.
                previous_filters=(
                    conversation_store.get_last_filters(session_id)
                    if looks_like_followup(body.question, history) else {}
                ) or {},
                registry=_REPORT_REGISTRY,
                fallback_report=last_intent.get("report_key", ""),
            )
            tracer.end("semantic_parse",
                       detail=f"{planning.parsed.report_key}/{planning.parsed.metric} conf={planning.parsed.confidence}")
        except ValueError as exc:
            tracer.end("semantic_parse", outcome="error", detail=str(exc))
            tracer.emit(question=body.question, status="error")
            logger.warning("Stream query planning failed: %s", exc)
            yield f'data: {_json.dumps({"error": "Unable to validate that query safely."})}\n\n'
            return
        parsed = planning.parsed
        report_key = parsed.report_key
        query_plan = planning.plan
        intent_spec = planning.intent_spec
        validated_filters = planning.validated_filters
        ai_where = planning.ai_where

        _future_msg = _future_period_message(validated_filters)
        if _future_msg:
            yield f'data: {_json.dumps({"answer": _future_msg, "report_key": report_key})}\n\n'
            yield f'data: {_json.dumps({"done": True})}\n\n'
            return

        # "details / list / breakup" on a count-style metric — show entities.
        if (
            not parsed.dimension
            and re.search(r"\b(details?|breakup|breakdown|list)\b", resolved_question.lower())
        ):
            from app.services.column_registry import get_detail_dimension, get_default_metric
            _detail_dim = get_detail_dimension(report_key, parsed.metric)
            if _detail_dim:
                parsed.dimension = _detail_dim
                intent_spec.dimension = _detail_dim
                # "Detail" of a unique-count metric should show a real value
                # per entity (e.g. sales), not just the row count again.
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

        entry = _REPORT_REGISTRY.get(report_key)
        if entry is None:
            yield f'data: {_json.dumps({"error": f"Unknown report: {report_key}"})}\n\n'
            return
        _complete_ranking = bool(parsed.metric and parsed.dimension)
        if parsed.clarify or (query_plan.confidence < 0.75 and not stream_report_name and not market_ctx and not _complete_ranking):
            message = parsed.clarify or "I’m not confident which report or metric you mean. Please specify the report and metric."
            log_request_trace(request_id=getattr(request.state, "request_id", ""), intent=query_plan.intent, confidence=query_plan.confidence, query_plan=query_plan.model_dump(mode="json"), failure_stage="clarification")
            yield f'data: {_json.dumps({"status": "clarify", "report_key": report_key, "answer": message, "done": True})}\n\n'
            return
        conversation_store.set_last_report(session_id, report_key)
        conversation_store.set_last_intent(
            session_id, report_key=report_key, metric=parsed.metric,
            dimension=parsed.dimension or "", resolved_question=resolved_question,
        )

        if validated_filters:
            conversation_store.set_last_filters(session_id, validated_filters)

        # Call report API
        tracer.start("execute")
        try:
            company_code = getattr(request.state, "company_code", None) or body.company_code or "DEMO"
            user_id = getattr(request.state, "user_id", None) or body.user_id or "u123"
            if settings.use_real_api:
                from app.services.real_api_client import (
                    call_real_report_api, get_report_sp_map,
                )
                sp_map = get_report_sp_map()
                report_meta = sp_map.get(report_key, {})
                sp_number = body.sp or report_meta.get("sp") or settings.real_api_sp
                yearcode = body.yearcode or settings.real_api_yearcode
                appuserid = body.appuserid or user_id or "admin@orail.co.in"
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
                    report_key, validated_filters, company_code, user_id, _REPORT_REGISTRY,
                )
            tracer.end("execute", detail=f"rows={_record_count(data) if isinstance(data, dict) else '?'}")
        except Exception as exc:
            tracer.end("execute", outcome="error", detail=str(exc)[:150])
            tracer.emit(question=resolved_question, status="error", report_key=report_key)
            log_request_trace(request_id=getattr(request.state, "request_id", ""), intent=query_plan.intent, confidence=query_plan.confidence, query_plan=query_plan.model_dump(mode="json"), failure_stage="execution")
            logger.exception("Stream: report API call failed: %s", exc)
            yield f'data: {_json.dumps({"error": "Unable to fetch report data. Please try again."})}\n\n'
            return

        record_count = _record_count(data)
        returned_count = len(data.get("rows", [])) if isinstance(data, dict) and isinstance(data.get("rows"), list) else record_count
        query_plan.results_limited = query_plan.results_limited or record_count > returned_count
        # Send metadata
        yield f'data: {_json.dumps({"report_key": report_key, "filters": validated_filters, "request_id": getattr(request.state, "request_id", ""), "intent_confidence": query_plan.confidence, "complexity": query_plan.complexity.value, "record_count": record_count, "results_limited": query_plan.results_limited})}\n\n'

        # Multi-metric support: companion metrics fetch via extra SP calls —
        # mirrors the /chat path so streamed answers show count + amount too.
        if parsed.extra_metrics and not parsed.dimension:
            from app.services.semantic_query_parser import get_metric_unit, get_metric_unit_label
            from app.services.orchestrator import execute_plan
            from app.services.query_plan import QueryStep
            from app.services.block_builder import _display_value

            metric_results = []
            primary_resolved = _resolve_metric(data, intent_spec, body.question)
            metric_results.append({
                "metric_key": parsed.metric,
                "value": primary_resolved.value,
                "unit": intent_spec.unit,
                "unit_label": getattr(intent_spec, "unit_label", ""),
                "label": primary_resolved.label,
            })
            query_plan.steps = [QueryStep(type="metric_fetch", metric=m) for m in parsed.extra_metrics]

            async def _fetch_extra(step):
                extra_spec = parsed.to_intent_spec()
                extra_spec.metric_key = step.metric
                extra_spec.intent = f"semantic_{step.metric}"
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
                        report_key, validated_filters, company_code, user_id, _REPORT_REGISTRY,
                    )
                resolved = _resolve_metric(extra_data, extra_spec, body.question)
                return {
                    "metric_key": step.metric, "value": resolved.value,
                    "unit": extra_spec.unit, "unit_label": getattr(extra_spec, "unit_label", ""),
                    "label": resolved.label,
                }

            for outcome in await execute_plan(query_plan, _fetch_extra):
                if outcome.error:
                    metric_results.append({"metric_key": outcome.step.metric, "value": None,
                                           "unit": get_metric_unit(outcome.step.metric),
                                           "unit_label": get_metric_unit_label(outcome.step.metric, report_key),
                                           "label": outcome.step.metric})
                else:
                    metric_results.append(outcome.data)

            lines = []
            for r in metric_results:
                if r.get("value") is not None:
                    lines.append(f"{r.get('label', r['metric_key'])}: {_display_value(r['value'], r.get('unit', 'currency'), 'INR', r.get('unit_label', ''))}")
                else:
                    lines.append(f"{r.get('label', r['metric_key'])}: N/A")
            if record_count > 0:
                lines.append(f"Transactions: {record_count}")
            lines.append(f"Sources: {report_key.replace('_', ' ').title()}")
            answer = "\n".join(lines)
            if market_ctx:
                answer = f"{build_market_note(market_ctx, market_snapshot)}\n\n{answer}"
            chunk_size = 3
            for i in range(0, len(answer), chunk_size):
                yield f'data: {_json.dumps({"chunk": answer[i:i + chunk_size]})}\n\n'
            yield f'data: {_json.dumps({"done": True})}\n\n'
            conversation_store.add_message(session_id, "user", resolved_question)
            conversation_store.add_message(session_id, "assistant", answer)
            tracer.emit(question=resolved_question, status="success", report_key=report_key,
                        extra={"path": "multi_metric"})
            log_request_trace(request_id=getattr(request.state, "request_id", ""), intent=query_plan.intent, confidence=query_plan.confidence, query_plan=query_plan.model_dump(mode="json"), result_count=record_count)
            return

        # Growth/comparison support: fetch previous period and emit delta answer
        _sq_lower = resolved_question.lower()
        _is_growth = any(kw in _sq_lower for kw in (
            "growth", "compared", "compare", "previous period", "vs last", "versus last",
        ))
        if _is_growth and not parsed.dimension:
            try:
                from app.services.formatters import format_percentage
                from app.services.orchestrator import compute_previous_period
                from app.services.block_builder import _display_value

                prev_result = compute_previous_period(validated_filters, parsed.date_filter)
                if prev_result is None:
                    raise ValueError("no computable previous period")
                prev_filters, cur_start, cur_end = prev_result
                if not validated_filters.get("start_date") and cur_start:
                    validated_filters["start_date"] = cur_start
                    validated_filters["end_date"] = cur_end

                if settings.use_real_api:
                    prev_data = await call_real_report_api(
                        report_key=report_key, intent_spec=intent_spec,
                        validated_filters=prev_filters, appuserid=appuserid,
                        ip_address=ip_address, yearcode=yearcode, sp_number=sp_number,
                        ai_where_clause=ai_where,
                    )
                else:
                    prev_data = await call_report_api(
                        report_key, prev_filters, company_code, user_id, _REPORT_REGISTRY,
                    )

                cur_v = (_resolve_metric(data, intent_spec, body.question).value or 0)
                prev_v = (_resolve_metric(prev_data, intent_spec, body.question).value or 0)
                growth_pct = ((cur_v - prev_v) / abs(prev_v)) * 100 if prev_v else None
                _u = getattr(intent_spec, "unit", "currency") or "currency"
                _ul = getattr(intent_spec, "unit_label", "") or ""

                lines = [
                    f"Current period: {_display_value(cur_v, _u, 'INR', _ul)}",
                    f"Previous period: {_display_value(prev_v, _u, 'INR', _ul)}",
                    f"Growth: {format_percentage(growth_pct)}" if growth_pct is not None
                    else "Growth: N/A (previous period was zero)",
                    f"Previous period range: {block_builder.format_date_friendly(prev_filters['start_date'])}"
                    f" to {block_builder.format_date_friendly(prev_filters['end_date'])}",
                    f"Transactions: {record_count}",
                    f"Sources: {report_key.replace('_', ' ').title()}",
                ]
                answer = "\n".join(lines)
                if market_ctx:
                    answer = f"{build_market_note(market_ctx, market_snapshot)}\n\n{answer}"
                chunk_size = 3
                for i in range(0, len(answer), chunk_size):
                    yield f'data: {_json.dumps({"chunk": answer[i:i + chunk_size]})}\n\n'
                yield f'data: {_json.dumps({"done": True})}\n\n'
                conversation_store.add_message(session_id, "user", resolved_question)
                conversation_store.add_message(session_id, "assistant", answer)
                return
            except Exception as exc:
                logger.warning("Stream growth comparison failed: %s", exc)
                # fall through to normal single-period answer

        # Multi-period support: questions naming 2+ distinct periods
        if not _is_growth and (not parsed.dimension or is_time_dimension(parsed.dimension)):
            from app.services.orchestrator import (
                detect_periods, is_time_dimension, resolve_preset_dates, period_label,
            )
            _period_mentions = detect_periods(resolved_question)
            if len(_period_mentions) >= 2:
                try:
                    from app.services.block_builder import _display_value
                    from dataclasses import replace as _dc_replace
                    period_spec = _dc_replace(intent_spec, dimension="")
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
                                report_key, step_filters, company_code, user_id, _REPORT_REGISTRY,
                            )
                        p_resolved = _resolve_metric(pdata, period_spec, body.question)
                        period_rows.append((period_label(preset), p_resolved.value, _record_count(pdata)))

                    if len(period_rows) >= 2:
                        _u = getattr(intent_spec, "unit", "currency") or "currency"
                        _ul = getattr(intent_spec, "unit_label", "") or ""
                        lines = [
                            f"{lbl}: {_display_value(v or 0, _u, 'INR', _ul)} ({rc} transactions)"
                            for lbl, v, rc in period_rows
                        ]
                        lines.append(f"Sources: {report_key.replace('_', ' ').title()}")
                        answer = "\n".join(lines)
                        if market_ctx:
                            answer = f"{build_market_note(market_ctx, market_snapshot)}\n\n{answer}"
                        chunk_size = 3
                        for i in range(0, len(answer), chunk_size):
                            yield f'data: {_json.dumps({"chunk": answer[i:i + chunk_size]})}\n\n'
                        yield f'data: {_json.dumps({"done": True})}\n\n'
                        conversation_store.add_message(session_id, "user", resolved_question)
                        conversation_store.add_message(session_id, "assistant", answer)
                        tracer.emit(question=resolved_question, status="success", report_key=report_key,
                                    extra={"path": "multi_period"})
                        return
                except Exception as exc:
                    logger.warning("Stream multi-period query failed: %s", exc)

        # Generate answer via the deterministic pipeline
        tracer.start("answer")
        try:
            answer = await generate_answer(
                data, entry, validated_filters, [],
                body.question, history=history, intent_spec=intent_spec,
            )
            tracer.end("answer")
        except Exception as exc:
            tracer.end("answer", outcome="error", detail=str(exc)[:150])
            tracer.emit(question=resolved_question, status="error", report_key=report_key)
            log_request_trace(request_id=getattr(request.state, "request_id", ""), intent=query_plan.intent, confidence=query_plan.confidence, query_plan=query_plan.model_dump(mode="json"), result_count=record_count, failure_stage="answer_generation")
            logger.exception("Stream: answer generation failed: %s", exc)
            yield f'data: {_json.dumps({"error": "Something went wrong generating this response. Please try again."})}\n\n'
            return

        # Market-context note: honest external/internal boundary
        if market_ctx:
            answer = f"{build_market_note(market_ctx, market_snapshot)}\n\n{answer}"

        # Stream the deterministic answer in small chunks for UX
        chunk_size = 3  # characters per chunk for smooth streaming feel
        for i in range(0, len(answer), chunk_size):
            chunk = answer[i:i + chunk_size]
            yield f'data: {_json.dumps({"chunk": chunk})}\n\n'

        # Send done signal
        yield f'data: {_json.dumps({"done": True})}\n\n'

        # Store conversation
        conversation_store.add_message(session_id, "user", resolved_question)
        conversation_store.add_message(session_id, "assistant", answer)
        log_request_trace(
            request_id=getattr(request.state, "request_id", ""), intent=query_plan.intent,
            confidence=query_plan.confidence, query_plan=query_plan.model_dump(mode="json"),
            stage_latencies=tracer.latencies,
            result_count=_record_count(data), cache_hit=False,
            failure_stage=tracer.failure_stage,
        )
        tracer.emit(question=resolved_question, status="success", report_key=report_key)

    return StreamingResponse(_stream(), media_type="text/event-stream")


@app.post("/export", response_model=ExportResponse)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def export_report(body: ChatRequest, request: Request) -> ExportResponse:
    return await _export_impl(body, request)


@v1_router.post("/export", response_model=ExportResponse)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def export_report_v1(body: ChatRequest, request: Request) -> ExportResponse:
    return await _export_impl(body, request)


async def _export_impl(body: ChatRequest, request: Request) -> ExportResponse:
    """Dedicated export endpoint: classify, extract/validate filters, and ask the Node API for a download URL."""

    token_usage_calls: List[Dict[str, int]] = []
    report_key = await classify_question(body.question, _REPORT_REGISTRY, token_usage=token_usage_calls)
    if report_key is None:
        return ExportResponse(
            error="I couldn't determine which report to export. Try mentioning one of these: "
            + ", ".join(r["report_key"] for r in list_available_reports(_REPORT_REGISTRY))
        )

    entry = _REPORT_REGISTRY[report_key]

    previous_filters = conversation_store.get_last_filters(body.session_id) or {}
    if body.filters is not None:
        raw_filters = body.filters
    else:
        try:
            llm_filters, mentioned_fields = await extract_filters(
                body.question,
                report_key,
                entry.filter_schema,
                token_usage=token_usage_calls,
                previous_filters=previous_filters,
            )
        except FilterExtractError as exc:
            return ExportResponse(
                report_key=report_key,
                error=f"Could not extract filters from your question: {exc}",
            )
        raw_filters = merge_with_previous(previous_filters, llm_filters, mentioned_fields)

    validation = validate_and_fill(entry.filter_schema, raw_filters)
    if validation.errors:
        return ExportResponse(
            report_key=report_key,
            error="Filter validation failed: " + "; ".join(validation.errors),
        )

    try:
        download_url = await export_report_api(
            report_key,
            validation.cleaned,
            body.company_code,
            body.user_id,
            _REPORT_REGISTRY,
        )
    except ReportApiError as exc:
        body_snippet = exc.body[:200] + "..." if len(exc.body) > 200 else exc.body
        return ExportResponse(
            report_key=report_key,
            assumptions=validation.assumptions,
            error=f"Export API error {exc.status_code}: {body_snippet}",
        )
    except Exception as exc:
        return ExportResponse(
            report_key=report_key,
            assumptions=validation.assumptions,
            error=f"Could not reach the export API: {exc}",
        )

    token_usage = usage_store.build_token_usage(token_usage_calls)
    response = ExportResponse(
        report_key=report_key,
        download_url=download_url,
        assumptions=validation.assumptions,
        token_usage=token_usage,
    )
    usage_store.log_token_usage(
        endpoint="/export",
        company_code=body.company_code,
        user_id=body.user_id,
        question=body.question,
        report_key=report_key,
        token_usage=token_usage,
    )
    return response


# ── Feedback endpoints (thumbs up/down) ──────────────────────────────────────

def _feedback_impl(body: FeedbackRequest, request: Request) -> FeedbackResponse:
    """Store user feedback (up/down) for a chat answer."""
    if body.rating not in ("up", "down"):
        return FeedbackResponse(status="error", message="rating must be 'up' or 'down'")
    if not body.question or not body.question.strip():
        return FeedbackResponse(status="error", message="question is required")
    if not body.session_id or not body.session_id.strip():
        return FeedbackResponse(status="error", message="session_id is required")

    # Use authenticated identity from middleware (more secure than body values)
    company_code = getattr(request.state, "company_code", None) or body.company_code or ""
    user_id = getattr(request.state, "user_id", None) or body.user_id or ""

    try:
        fid = feedback_store.add_feedback(
            session_id=body.session_id,
            question=body.question,
            answer=body.answer,
            report_key=body.report_key,
            metric=body.metric,
            rating=body.rating,
            comment=body.comment,
            company_code=company_code,
            user_id=user_id,
            corrected_metric=body.corrected_metric,
            failure_reason=body.failure_reason,
            query_plan=body.query_plan,
            confidence=body.confidence,
            latency_ms=body.latency_ms,
        )
        logger.info(
            "Feedback: session=%s rating=%s report=%s question=%s",
            body.session_id, body.rating, body.report_key, body.question[:60],
        )
        return FeedbackResponse(status="ok", message="Feedback recorded", feedback_id=fid)
    except Exception as exc:
        logger.error("Feedback store error: %s", exc)
        return FeedbackResponse(status="error", message="Failed to record feedback")


@app.post("/feedback", response_model=FeedbackResponse)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def submit_feedback(body: FeedbackRequest, request: Request) -> FeedbackResponse:
    """Submit thumbs up/down feedback for a chat answer."""
    return _feedback_impl(body, request)


@v1_router.post("/feedback", response_model=FeedbackResponse)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def submit_feedback_v1(body: FeedbackRequest, request: Request) -> FeedbackResponse:
    """Submit thumbs up/down feedback for a chat answer (v1)."""
    return _feedback_impl(body, request)


@app.get("/feedback/stats")
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def feedback_stats(request: Request, report_key: str = "", days: int = 30) -> dict:
    """Get feedback statistics for analytics / prompt tuning."""
    return feedback_store.get_feedback_stats(report_key=report_key, days=days)


@v1_router.get("/feedback/stats")
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def feedback_stats_v1(request: Request, report_key: str = "", days: int = 30) -> dict:
    """Get feedback statistics (v1)."""
    return feedback_store.get_feedback_stats(report_key=report_key, days=days)


@app.get("/feedback/down")
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def feedback_down(request: Request, report_key: str = "", limit: int = 100) -> list:
    """Get all down-voted feedback for prompt tuning / retraining analysis."""
    return feedback_store.get_down_feedback(report_key=report_key, limit=limit)


@v1_router.get("/feedback/down")
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def feedback_down_v1(request: Request, report_key: str = "", limit: int = 100) -> list:
    """Get all down-voted feedback (v1)."""
    return feedback_store.get_down_feedback(report_key=report_key, limit=limit)


app.include_router(v1_router)
