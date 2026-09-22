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
from app.middleware.audit_log import log_chat_exchange
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
from app.services.context_resolver import resolve_context
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


# Base terms that indicate a filterable field or date range is being mentioned.
# Value-specific terms (branch names, categories, brands, etc.) are derived
# dynamically from each report's canonical_values + location_aliases below.
_BASE_FILTER_TERMS = {
    "branch", "location", "head office", "warehouse",
    "customer", "supplier", "party", "vendor",
    "brand", "metal", "gold", "silver", "platinum", "diamond", "22k", "18k", "14k",
    "category", "subcategory", "retail", "distributor", "wholesale",
    "sales rep", "salesperson", "representative", "employee",
    "2024", "2025", "2026", "january", "february", "march", "april",
    "may", "june", "july", "august", "september", "october", "november", "december",
    "this month", "last month", "this year", "last year", "this week", "last week",
    "today", "yesterday",
}


def _build_filter_keywords() -> set:
    """Aggregate filter-indicating terms from all report configs.

    Pulls every canonical_values alias and location_aliases key across all
    registered reports so new branches/categories/brands are recognised as
    filter mentions without editing this list. Falls back to the static base
    set if the registry is unavailable.
    """
    terms = set(_BASE_FILTER_TERMS)
    try:
        from app.services.column_registry import _load_registry
        for report_cfg in _load_registry().values():
            for field_map in (report_cfg.get("canonical_values") or {}).values():
                if isinstance(field_map, dict):
                    terms.update(k.lower() for k in field_map.keys() if k)
            for alias in (report_cfg.get("location_aliases") or {}).keys():
                if alias:
                    terms.add(alias.lower())
    except Exception:
        pass
    return terms


# Keywords that indicate the user is mentioning a specific filter value.
# If none of these appear, we can skip the LLM filter extraction call.
_FILTER_KEYWORDS = _build_filter_keywords()


def _can_skip_filter_extraction(question: str, report_key: str) -> bool:
    """Check if the LLM filter extraction can be skipped.

    Returns True when the question doesn't mention any filter-related keywords,
    meaning the LLM call would return empty filters anyway.
    """
    lowered = question.lower()
    # If any filter keyword is mentioned, we need the LLM to extract the value
    for kw in _FILTER_KEYWORDS:
        if kw in lowered:
            return False
    # No filter keywords found — skip the LLM call
    return True


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

    token_usage_calls: List[Dict[str, int]] = []
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
    resolved_question = await resolve_context(
        body.question, history, token_usage=token_usage_calls,
    )

    # Semantic cache check (skip when the caller explicitly asks for an export link,
    # or when the frontend asks for a regenerated/fresh answer).
    # Uses resolved_question so follow-ups get the right cache key, not the raw short input.
    # The cache is scoped to the resolved report (from pid/report_name) so the same
    # question asked on a different report does not return another report's answer.
    if _CHAT_CACHE is not None and not body.export and not body.regenerate:
        cached = await _CHAT_CACHE.lookup(
            resolved_question, company_code, user_id, response_mode,
            report_key=resolved_report_name or None,
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
            return cached
    CACHE_MISSES.inc()

    # Scope the semantic catalog to a single report when possible. If the
    # frontend did not send report_name, use the fast keyword classifier to
    # pick the best report. This keeps the LLM prompt small as the number of
    # reports grows into the hundreds.
    semantic_report_name = resolved_report_name
    if not semantic_report_name and len(_REPORT_REGISTRY) > 1:
        # Stage 0: deterministic intent patterns (before keyword classifier)
        from app.services.intent import classify_question_by_intent
        intent_report = classify_question_by_intent(resolved_question, _REPORT_REGISTRY)
        if intent_report:
            semantic_report_name = intent_report
            logger.info("Intent classifier scoped semantic parser to: %s", semantic_report_name)
        else:
            semantic_report_name = keyword_classify(resolved_question, _REPORT_REGISTRY) or ""
            if semantic_report_name:
                logger.info("Keyword classifier scoped semantic parser to: %s", semantic_report_name)

    from app.services.semantic_query_parser import parse_query
    parsed = await parse_query(
        resolved_question, history=history, token_usage=token_usage_calls,
        report_name=semantic_report_name,
    )
    report_key = parsed.report_key

    # Fallback: if parser returned empty report but we have a last known
    # report from session context (follow-up scenario), use it.
    if not report_key or not report_key.strip():
        last_intent = conversation_store.get_last_intent(session_id)
        fallback_report = last_intent.get("report_key", "")
        if fallback_report and fallback_report in _REPORT_REGISTRY:
            logger.info(
                "Parser returned empty report, falling back to last: %s",
                fallback_report,
            )
            report_key = fallback_report

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

    intent_spec = parsed.to_intent_spec()

    # Deterministic intent override: if an explicit regex intent matched a
    # different metric than the LLM chose, prefer the deterministic one (e.g.
    # "total order" → order_count/total_count, not order_total_amount/Amount).
    # Only explicit regex intents may override — the keyword/column-name
    # fallbacks in detect_intent are weaker than the LLM and clobbered valid
    # metrics (e.g. "total wastage" → fallback 'total' → Amount).
    from app.services.intent import _detect_explicit_intent
    from app.services.column_registry import _load_registry as _load_col_reg
    det_spec = _detect_explicit_intent(resolved_question, report_key)
    # Trust the LLM whenever it picked a non-default metric — generic regex
    # intents like total_sales must not clobber a specific LLM choice
    # (DiamondAmount/ColorStoneAmount/GoldPureWt/etc.). The deterministic
    # override only refines the metric when the LLM fell back to the default.
    _rcfg = _load_col_reg().get(report_key, {})
    _default_metric = _rcfg.get("default_metric", "Amount")
    _llm_metric_is_default = intent_spec.metric_key == _default_metric
    if (det_spec is not None and det_spec.metric_key
            and det_spec.metric_key != intent_spec.metric_key
            and _llm_metric_is_default):
        logger.info(
            "Deterministic intent override: %s → %s (was %s → %s)",
            det_spec.intent, det_spec.metric_key,
            intent_spec.intent, intent_spec.metric_key,
        )
        intent_spec.metric_key = det_spec.metric_key
        intent_spec.aggregation = det_spec.aggregation or intent_spec.aggregation
        intent_spec.unit = det_spec.unit or intent_spec.unit
        intent_spec.unit_label = getattr(det_spec, "unit_label", "")
        if det_spec.dimension:
            intent_spec.dimension = det_spec.dimension
            intent_spec.sort = det_spec.sort or intent_spec.sort
            intent_spec.limit = det_spec.limit or intent_spec.limit

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

    validated_filters = parsed.to_validated_filters()
    ai_where = parsed.generate_where_clause()

    # Follow-up context: if this question didn't specify a date range, inherit
    # the previous turn's date filter so "which month had highest?" stays scoped
    # to the period the user was already asking about (not all-time).
    if not getattr(parsed, "date_filter", None) and not (
        validated_filters.get("start_date") or validated_filters.get("end_date")
    ):
        prev_filters = conversation_store.get_last_filters(session_id) or {}
        for dk in ("start_date", "end_date"):
            if prev_filters.get(dk):
                validated_filters[dk] = prev_filters[dk]
        if prev_filters.get("start_date") or prev_filters.get("end_date"):
            logger.info(
                "Inherited previous date filter for follow-up: %s to %s",
                prev_filters.get("start_date"), prev_filters.get("end_date"),
            )

    if validated_filters:
        conversation_store.set_last_filters(session_id, validated_filters)

    # Use authenticated identity from middleware (cookie) with body as fallback for dev mode
    company_code = getattr(request.state, "company_code", None) or body.company_code or "DEMO"
    user_id = getattr(request.state, "user_id", None) or body.user_id or "u123"

    # 5. Permission check is delegated to the Node API via company_code + user_id.
    # 6. Call report API (real API or dummy DB depending on config)
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
    except ReportApiError as exc:
        logger.error("Report API error: status=%s body=%s", exc.status_code, exc.body[:300])
        msg = "Unable to fetch report data. Please try again."
        if response_mode == "wide":
            return ChatResponse(report_key=report_key, error=msg,
                blocks=[{"type": "error", "content": msg}])
        return ChatResponse(report_key=report_key, error=msg)
    except RealApiError as exc:
        logger.error("Real API error: status=%s body=%s", exc.status_code, exc.body[:300])
        msg = "Unable to fetch report data. Please try again."
        if response_mode == "wide":
            return ChatResponse(report_key=report_key, error=msg,
                blocks=[{"type": "error", "content": msg}])
        return ChatResponse(report_key=report_key, error=msg)
    except Exception as exc:
        logger.exception("Report API call failed: %s", exc)
        msg = "Unable to reach the report service. Please try again."
        if response_mode == "wide":
            return ChatResponse(report_key=report_key, error=msg,
                blocks=[{"type": "error", "content": msg}])
        return ChatResponse(report_key=report_key, error=msg)

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
        sd = validated_filters.get("start_date", "")
        ed = validated_filters.get("end_date", "")
        if sd and ed:
            assumptions.append(f"Date range {sd} to {ed}")
        elif sd:
            assumptions.append(f"From {sd}")
        elif ed:
            assumptions.append(f"Up to {ed}")

    # ── Multi-metric support: fetch extra metrics via additional SP calls ──
    if parsed.extra_metrics and not parsed.dimension:
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

        # Fetch each extra metric via additional SP call
        for extra_metric in parsed.extra_metrics:
            extra_spec = parsed.to_intent_spec()
            extra_spec.metric_key = extra_metric
            extra_spec.intent = f"semantic_{extra_metric}"
            extra_spec.unit = get_metric_unit(extra_metric)
            extra_spec.unit_label = get_metric_unit_label(extra_metric, report_key)
            try:
                if settings.use_real_api:
                    extra_data = await call_real_report_api(
                        report_key=report_key,
                        intent_spec=extra_spec,
                        validated_filters=validated_filters,
                        appuserid=appuserid,
                        ip_address=ip_address,
                        yearcode=yearcode,
                        sp_number=sp_number,
                        ai_where_clause=ai_where,
                    )
                else:
                    extra_data = await call_report_api(
                        report_key, validated_filters, company_code, user_id, _REPORT_REGISTRY,
                    )
                extra_resolved = _resolve_metric(extra_data, extra_spec, body.question)
                metric_results.append({
                    "metric_key": extra_metric,
                    "value": extra_resolved.value,
                    "unit": extra_spec.unit,
                    "unit_label": getattr(extra_spec, "unit_label", ""),
                    "label": extra_resolved.label,
                })
            except Exception as exc:
                logger.warning("Failed to fetch extra metric %s: %s", extra_metric, exc)
                metric_results.append({
                    "metric_key": extra_metric,
                    "value": None,
                    "unit": extra_spec.unit,
                    "unit_label": getattr(extra_spec, "unit_label", ""),
                    "label": extra_metric,
                })

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
        return response

    # ── Growth/comparison support: detect "growth %" or "compared with previous" ──
    _q_lower = body.question.lower()
    _is_growth_query = any(kw in _q_lower for kw in (
        "growth", "compared", "compare", "previous period", "vs last", "versus last",
    ))
    if _is_growth_query and not parsed.dimension:
        from datetime import date, timedelta
        from app.services.formatters import format_currency, format_percentage

        # If no date_filter was set, default to this_month for growth comparison
        if not validated_filters.get("start_date"):
            today = date.today()
            validated_filters["start_date"] = today.replace(day=1).isoformat()
            validated_filters["end_date"] = today.isoformat()
            assumptions.append(f"Date range {validated_filters['start_date']} to {validated_filters['end_date']}")

        cur_start = validated_filters.get("start_date", "")
        cur_end = validated_filters.get("end_date", "")
        prev_filters = dict(validated_filters)

        # Compute previous period based on preset or explicit range
        preset = (parsed.date_filter or {}).get("preset", "")
        if not preset and not (parsed.date_filter or {}).get("start"):
            # No explicit date_filter — we defaulted to this_month above
            preset = "this_month"
        if preset == "this_month":
            today = date.today()
            first_of_month = today.replace(day=1)
            prev_end = first_of_month - timedelta(days=1)
            prev_start = prev_end.replace(day=1)
            prev_filters["start_date"] = prev_start.isoformat()
            prev_filters["end_date"] = prev_end.isoformat()
        elif preset == "this_year":
            today = date.today()
            prev_filters["start_date"] = f"{today.year - 1}-01-01"
            prev_filters["end_date"] = f"{today.year - 1}-12-31"
        elif preset == "this_week":
            today = date.today()
            monday = today - timedelta(days=today.weekday())
            prev_monday = monday - timedelta(days=7)
            prev_sunday = prev_monday + timedelta(days=6)
            prev_filters["start_date"] = prev_monday.isoformat()
            prev_filters["end_date"] = prev_sunday.isoformat()
        elif preset == "today":
            today = date.today()
            yesterday = today - timedelta(days=1)
            prev_filters["start_date"] = yesterday.isoformat()
            prev_filters["end_date"] = yesterday.isoformat()
        elif cur_start and cur_end:
            # Explicit range: shift by the range duration
            from datetime import date as _date
            s = _date.fromisoformat(cur_start)
            e = _date.fromisoformat(cur_end)
            duration = (e - s).days + 1
            prev_end = s - timedelta(days=1)
            prev_start = prev_end - timedelta(days=duration - 1)
            prev_filters["start_date"] = prev_start.isoformat()
            prev_filters["end_date"] = prev_end.isoformat()
        else:
            _is_growth_query = False  # can't compute without a date range

        if _is_growth_query:
            try:
                if settings.use_real_api:
                    prev_data = await call_real_report_api(
                        report_key=report_key,
                        intent_spec=intent_spec,
                        validated_filters=prev_filters,
                        appuserid=appuserid,
                        ip_address=ip_address,
                        yearcode=yearcode,
                        sp_number=sp_number,
                        ai_where_clause=ai_where,
                    )
                else:
                    prev_data = await call_report_api(
                        report_key, prev_filters, company_code, user_id, _REPORT_REGISTRY,
                    )
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
                    f"Previous period: {prev_filters['start_date']} to {prev_filters['end_date']}"
                )

                if response_mode == "wide":
                    blocks = [
                        {"type": "heading", "content": body.question},
                        {"type": "table", "columns": ["Period", "Value", "Growth %"],
                         "rows": [
                            ["Current", format_currency(cur_v), ""],
                            ["Previous", format_currency(prev_v), ""],
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
                        f"Current period: {format_currency(cur_v)}",
                        f"Previous period: {format_currency(prev_v)}",
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
                return response
            except Exception as exc:
                logger.warning("Growth comparison failed: %s", exc)

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
    except Exception as exc:
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
                _label = start
            elif start and end:
                _label = f"{start} to {end}"
            else:
                _label = start or end
            period_info = PeriodInfo(start=start, end=end, label=_label)

    response = ChatResponse(
        status="success",
        report=ReportInfo(key=report_key, name=report_name) if report_key else None,
        question=body.question,
        answer=answer_data,
        period=period_info,
        blocks=blocks,
        filters=validated_filters,
        metadata=Metadata(session_id=session_id, record_count=record_count, token_usage=token_usage),
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

    async def _stream():
        import json as _json
        from app.services.answer_generator import generate_answer

        token_usage_calls: List[Dict[str, int]] = []
        history = conversation_store.get_messages(session_id, limit=10)

        # Send session_id first
        yield f'data: {_json.dumps({"session_id": session_id})}\n\n'

        # ── Semantic query parser: ONE LLM call replaces spell correction,
        #    report classification, filter extraction, intent detection, and
        #    WHERE clause generation ──

        # Resolve follow-up questions using conversation history
        resolved_question = await resolve_context(
            body.question, history, token_usage=token_usage_calls,
        )

        from app.services.semantic_query_parser import parse_query
        parsed = await parse_query(
            resolved_question, history=history, token_usage=token_usage_calls,
            report_name=body.report_name or "",
        )
        report_key = parsed.report_key

        # Fallback: if parser returned empty report but we have a last known
        # report from session context (follow-up scenario), use it.
        if not report_key or not report_key.strip():
            last_intent = conversation_store.get_last_intent(session_id)
            fallback_report = last_intent.get("report_key", "")
            if fallback_report and fallback_report in _REPORT_REGISTRY:
                logger.info(
                    "Parser returned empty report, falling back to last: %s",
                    fallback_report,
                )
                report_key = fallback_report

        entry = _REPORT_REGISTRY.get(report_key)
        if entry is None:
            yield f'data: {_json.dumps({"error": f"Unknown report: {report_key}"})}\n\n'
            return
        conversation_store.set_last_report(session_id, report_key)
        conversation_store.set_last_intent(
            session_id,
            report_key=report_key,
            metric=parsed.metric,
            dimension=parsed.dimension or "",
            resolved_question=resolved_question,
        )

        intent_spec = parsed.to_intent_spec()
        validated_filters = parsed.to_validated_filters()
        ai_where = parsed.generate_where_clause()

        if validated_filters:
            conversation_store.set_last_filters(session_id, validated_filters)

        # Call report API
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
        except Exception as exc:
            logger.exception("Stream: report API call failed: %s", exc)
            yield f'data: {_json.dumps({"error": "Unable to fetch report data. Please try again."})}\n\n'
            return

        # Send metadata
        yield f'data: {_json.dumps({"report_key": report_key, "filters": validated_filters})}\n\n'

        # Generate answer via the deterministic pipeline
        try:
            answer = await generate_answer(
                data, entry, validated_filters, [],
                body.question, history=history, intent_spec=intent_spec,
            )
        except Exception as exc:
            logger.exception("Stream: answer generation failed: %s", exc)
            yield f'data: {_json.dumps({"error": "Something went wrong generating this response. Please try again."})}\n\n'
            return

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
