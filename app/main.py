import time
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, FastAPI, Request, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.config import settings, validate_startup
from app.middleware.auth import AuthMiddleware
from app.middleware.logging import RequestIdMiddleware, setup_logging, get_logger
from app.middleware.metrics import REQUEST_COUNT, REQUEST_LATENCY
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.models import (
    ChatActionRequest, ChatRequest, ChatResponse, ExportResponse,
    FeedbackRequest, FeedbackResponse, ReportRegistryEntry,
)
from app.services.api_client import (
    ReportApiError,
    export_report_api,
    get_report_registry,
)
from app.services.cache import ChatCache
from app.services.classifier import (
    classify_question,
    list_available_reports,
    load_classifier_embeddings,
)
from app.services.filter_extractor import (
    FilterExtractError,
    extract_filters,
    merge_with_previous,
)
from app.services import conversation_store, usage_store
from app.services import chat_service
from app.services import feedback_store
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

# Reference chat UI + block renderer (public path per AuthMiddleware).
from pathlib import Path as _Path
from fastapi.staticfiles import StaticFiles as _StaticFiles
_STATIC_DIR = _Path(__file__).resolve().parent.parent / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", _StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")


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
async def health(request: Request) -> Dict[str, Any]:
    """Health check with dependency status. Public callers see status
    booleans only; provider/model details require the admin key."""
    deps = {}
    all_ok = True
    is_admin = bool(
        settings.admin_api_key
        and request.headers.get("x-admin-key", "") == settings.admin_api_key
    )

    # Check report registry
    deps["reports_loaded"] = len(_REPORT_REGISTRY) if is_admin else bool(len(_REPORT_REGISTRY))
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
        if is_admin:
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
        if is_admin:
            deps["embedding_model"] = settings.embedding_model
        deps["embedding_configured"] = True
    except Exception:
        deps["embedding_configured"] = False

    return {
        "status": "ok" if all_ok else "degraded",
        "version": "1.0.0",
        "reports_loaded": len(_REPORT_REGISTRY),
        "dependencies": deps,
    }


@v1_router.get("/health")
async def health_v1(request: Request) -> Dict[str, Any]:
    return await health(request)


@app.post("/cache/invalidate")
def cache_invalidate_all(request: Request) -> Dict[str, Any]:
    """Clear all cache entries."""
    _require_admin(request)
    if _CHAT_CACHE is None:
        return {"invalidated": 0}
    count = _CHAT_CACHE.invalidate_all()
    logger.info("Cache invalidated: %d entries removed", count)
    return {"invalidated": count}


@app.post("/cache/invalidate/{report_key}")
def cache_invalidate_report(report_key: str, request: Request) -> Dict[str, Any]:
    """Clear cache entries for a specific report."""
    _require_admin(request)
    if _CHAT_CACHE is None:
        return {"invalidated": 0}
    count = _CHAT_CACHE.invalidate_report(report_key)
    logger.info("Cache invalidated for %s: %d entries removed", report_key, count)
    return {"report_key": report_key, "invalidated": count}


@v1_router.post("/cache/invalidate")
def cache_invalidate_all_v1(request: Request) -> Dict[str, Any]:
    return cache_invalidate_all(request)


@v1_router.post("/cache/invalidate/{report_key}")
def cache_invalidate_report_v1(report_key: str, request: Request) -> Dict[str, Any]:
    return cache_invalidate_report(report_key, request)


@app.get("/reports")
def reports() -> Dict[str, Any]:
    return {"reports": list_available_reports(_REPORT_REGISTRY)}


@v1_router.get("/reports")
def reports_v1() -> Dict[str, Any]:
    return reports()


def _require_admin(request: Request) -> None:
    """Admin-gate for sensitive ops endpoints. When ADMIN_API_KEY is configured
    the X-Admin-Key header must match; when unset (dev), authentication alone
    is enough (AUTH_REQUIRED already blocks anonymous callers)."""
    if settings.admin_api_key:
        if request.headers.get("x-admin-key", "") != settings.admin_api_key:
            raise HTTPException(status_code=403, detail="Admin access required")
    elif not getattr(request.state, "user_id", None) and settings.auth_required:
        raise HTTPException(status_code=401, detail="Authentication required")


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

    # Same rule as chat_service._resolve_identity: query params are a dev-mode
    # convenience only — under AUTH_REQUIRED the verified cookie is the source.
    if settings.auth_required:
        company_code = getattr(request.state, "company_code", None) or "DEMO"
        user_id = getattr(request.state, "user_id", None) or "u123"
    else:
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


@app.get("/reports/{report_key}/masters")
async def get_report_masters(report_key: str, request: Request) -> Dict[str, Any]:
    """One bootstrap call: all master vocabularies for a report.

    Returns live distinct values for each master column (grouped count through
    the governed SP path — same tables/base_filter/tenant resolution as chat),
    merged with static canonical literals. Frontend should call once on report
    load; values are cached per tenant for a few minutes.
    """
    from app.services.master_data import get_report_masters as fetch_masters
    from app.services.real_api_client import get_report_sp_map

    entry = _REPORT_REGISTRY.get(report_key)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Unknown report key: {report_key}")

    if settings.auth_required:
        company_code = getattr(request.state, "company_code", None) or "DEMO"
        user_id = getattr(request.state, "user_id", None) or "u123"
        appuserid = getattr(request.state, "appuserid", None) or user_id or "admin@orail.co.in"
        yearcode = getattr(request.state, "yearcode", None) or settings.real_api_yearcode
        sp_number = None
    else:
        company_code = getattr(request.state, "company_code", None) or request.query_params.get("company_code") or "DEMO"
        user_id = getattr(request.state, "user_id", None) or request.query_params.get("user_id") or "u123"
        appuserid = request.query_params.get("appuserid") or user_id or "admin@orail.co.in"
        yearcode = request.query_params.get("yearcode") or settings.real_api_yearcode
        sp_param = request.query_params.get("sp")
        sp_number = int(sp_param) if sp_param and sp_param.isdigit() else None

    report_meta = get_report_sp_map().get(report_key, {})
    ip = getattr(getattr(request, "client", None), "host", None) or "127.0.0.1"

    try:
        return await fetch_masters(
            report_key,
            company_code=company_code,
            appuserid=appuserid,
            ip_address=ip,
            yearcode=yearcode,
            sp_number=sp_number or report_meta.get("sp"),
            refresh=request.query_params.get("refresh", "").lower() in ("1", "true", "yes"),
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@v1_router.get("/reports/{report_key}/masters")
async def get_report_masters_v1(report_key: str, request: Request) -> Dict[str, Any]:
    return await get_report_masters(report_key, request)




_CHAT_RESPONSE_EXCLUDE = {
    "report_key", "answer_text", "assumptions",
    "download_url", "token_usage",
}


@app.post("/chat", response_model=ChatResponse, response_model_exclude=_CHAT_RESPONSE_EXCLUDE)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def chat(body: ChatRequest, request: Request) -> ChatResponse:
    """Single-entry chat endpoint with classifier + cache + filter extraction + answer generation."""
    resp = await chat_service.run_chat(body, request, _REPORT_REGISTRY, _CHAT_CACHE)
    # Echo the effective session so the client adopts a rebound session_id.
    resp.session_id = getattr(request.state, "session_id", None) or resp.session_id
    return resp


@v1_router.post("/chat", response_model=ChatResponse, response_model_exclude=_CHAT_RESPONSE_EXCLUDE)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def chat_v1(body: ChatRequest, request: Request) -> ChatResponse:
    resp = await chat_service.run_chat(body, request, _REPORT_REGISTRY, _CHAT_CACHE)
    resp.session_id = getattr(request.state, "session_id", None) or resp.session_id
    return resp


@app.post("/chat/stream")
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def chat_stream(body: ChatRequest, request: Request):
    """SSE streaming endpoint — same pipeline as /chat, events instead of JSON."""
    return await chat_service.stream_chat(body, request, _REPORT_REGISTRY, _CHAT_CACHE)


@v1_router.post("/chat/stream")
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def chat_stream_v1(body: ChatRequest, request: Request):
    return await chat_service.stream_chat(body, request, _REPORT_REGISTRY, _CHAT_CACHE)


@app.post("/chat/action", response_model=ChatResponse, response_model_exclude=_CHAT_RESPONSE_EXCLUDE)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def chat_action(body: ChatActionRequest, request: Request) -> ChatResponse:
    """Structured widget action — a block's `action` payload posted back here
    resolves against the session's pending clarify state and re-runs the
    governed pipeline. Clicks never re-enter NL parsing."""
    resp = await chat_service.run_action(body, request, _REPORT_REGISTRY, _CHAT_CACHE)
    resp.session_id = getattr(request.state, "session_id", None) or resp.session_id
    return resp


@v1_router.post("/chat/action", response_model=ChatResponse, response_model_exclude=_CHAT_RESPONSE_EXCLUDE)
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def chat_action_v1(body: ChatActionRequest, request: Request) -> ChatResponse:
    resp = await chat_service.run_action(body, request, _REPORT_REGISTRY, _CHAT_CACHE)
    resp.session_id = getattr(request.state, "session_id", None) or resp.session_id
    return resp


@app.post("/chat/action/stream")
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def chat_action_stream(body: ChatActionRequest, request: Request):
    """SSE streaming variant of /chat/action — same event contract."""
    return await chat_service.stream_action(body, request, _REPORT_REGISTRY, _CHAT_CACHE)


@v1_router.post("/chat/action/stream")
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def chat_action_stream_v1(body: ChatActionRequest, request: Request):
    return await chat_service.stream_action(body, request, _REPORT_REGISTRY, _CHAT_CACHE)




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

    # Identity must come from the verified session — body-supplied
    # company_code/user_id are only honored in dev mode (AUTH_REQUIRED=false).
    exp_company, exp_user = chat_service._resolve_identity(request, body)

    try:
        download_url = await export_report_api(
            report_key,
            validation.cleaned,
            exp_company,
            exp_user,
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
        company_code=exp_company,
        user_id=exp_user,
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

    # Use authenticated identity from middleware (more secure than body values).
    # Body identity is dev-mode only; under AUTH_REQUIRED the cookie is trusted.
    if settings.auth_required:
        company_code = getattr(request.state, "company_code", None) or ""
        user_id = getattr(request.state, "user_id", None) or ""
    else:
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
    _require_admin(request)
    return feedback_store.get_feedback_stats(report_key=report_key, days=days)


@v1_router.get("/feedback/stats")
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def feedback_stats_v1(request: Request, report_key: str = "", days: int = 30) -> dict:
    """Get feedback statistics (v1)."""
    _require_admin(request)
    return feedback_store.get_feedback_stats(report_key=report_key, days=days)


@app.get("/feedback/down")
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def feedback_down(request: Request, report_key: str = "", limit: int = 100) -> list:
    """Get all down-voted feedback for prompt tuning / retraining analysis."""
    _require_admin(request)
    return feedback_store.get_down_feedback(report_key=report_key, limit=limit)


@v1_router.get("/feedback/down")
@limiter.limit(f"{settings.rate_limit_per_minute}/minute")
async def feedback_down_v1(request: Request, report_key: str = "", limit: int = 100) -> list:
    """Get all down-voted feedback (v1)."""
    _require_admin(request)
    return feedback_store.get_down_feedback(report_key=report_key, limit=limit)


app.include_router(v1_router)
