"""Audit log for AI API calls and SQL writes.

Writes a persistent log file (logs/audit.log) recording:
  - Every LLM/AI API call (provider, model, tier, tokens, cost, latency)
  - Every SQL write / dynamic SQL execution (WHERE clause, SP mode, metric, dimension)
  - Every WHERE-clause generation attempt (input, output, validation result)

Each entry is a JSON line for easy parsing with jq, Python, or log shippers.
Uses RotatingFileHandler to prevent unbounded growth and a background thread
to avoid blocking the async event loop.
"""

import json
import logging
import logging.handlers
import os
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Log directory next to the app
_LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "logs")
_LOG_PATH = os.path.join(_LOG_DIR, "audit.log")

# Ensure the directory exists
os.makedirs(_LOG_DIR, exist_ok=True)

# ── Async-safe writer: queue + background thread ──
_write_queue: queue.Queue = queue.Queue()
_writer_thread: Optional[threading.Thread] = None
_writer_lock = threading.Lock()


def _get_rotating_handler() -> logging.handlers.RotatingFileHandler:
    """Create a rotating file handler for audit.log (10MB x 5 backups)."""
    handler = logging.handlers.RotatingFileHandler(
        _LOG_PATH, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


_handler = _get_rotating_handler()


def _writer_loop() -> None:
    """Background thread that drains the write queue to the rotating file."""
    while True:
        try:
            line = _write_queue.get(timeout=5)
            if line is None:  # poison pill
                break
            _handler.emit(logging.LogRecord(
                name="audit", level=logging.INFO, pathname="", lineno=0,
                msg=line, args=(), exc_info=None,
            ))
            _write_queue.task_done()
        except queue.Empty:
            continue
        except Exception:
            pass  # never crash the writer thread


def _ensure_writer() -> None:
    """Start the background writer thread if not already running."""
    global _writer_thread
    with _writer_lock:
        if _writer_thread is None or not _writer_thread.is_alive():
            _writer_thread = threading.Thread(target=_writer_loop, daemon=True)
            _writer_thread.start()


def _write_entry(entry: Dict[str, Any]) -> None:
    """Queue a JSON line for the audit log (non-blocking)."""
    entry["timestamp"] = datetime.now(timezone.utc).isoformat()
    line = json.dumps(entry, ensure_ascii=False, default=str)
    _ensure_writer()
    _write_queue.put(line)


# ── AI / LLM API call logging ──

def log_llm_call(
    *,
    caller: str,
    tier: str,
    provider: str,
    model: str,
    messages_summary: str,
    temperature: float,
    max_tokens: int,
    response_text: str = "",
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    estimated_cost_usd: float = 0.0,
    latency_ms: float = 0.0,
    success: bool = True,
    error: str = "",
    request_id: str = "",
    full_messages: list = None,
) -> None:
    """Log an LLM API call with full request body."""
    entry = {
        "event": "llm_call",
        "caller": caller,
        "tier": tier,
        "provider": provider,
        "model": model,
        "messages_summary": messages_summary[:500],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_preview": response_text[:300],
        "response_full": response_text[:2000],
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost_usd": round(estimated_cost_usd, 6),
        "latency_ms": round(latency_ms, 1),
        "success": success,
        "error": error[:300],
        "request_id": request_id or "-",
    }
    if full_messages is not None:
        entry["request_body"] = json.dumps(full_messages, ensure_ascii=False, default=str)[:5000]
    _write_entry(entry)


def log_embedding_call(
    *,
    caller: str,
    provider: str,
    model: str,
    text_preview: str,
    tokens: int = 0,
    latency_ms: float = 0.0,
    success: bool = True,
    error: str = "",
    request_id: str = "",
) -> None:
    """Log an embedding API call."""
    _write_entry({
        "event": "embedding_call",
        "caller": caller,
        "provider": provider,
        "model": model,
        "text_preview": text_preview[:200],
        "tokens": tokens,
        "latency_ms": round(latency_ms, 1),
        "success": success,
        "error": error[:300],
        "request_id": request_id or "-",
    })


# ── SQL / WHERE-clause logging ──

def log_where_clause_generation(
    *,
    question: str,
    generated_clause: str,
    validated_clause: str,
    validation_passed: bool,
    validation_errors: Optional[list] = None,
    latency_ms: float = 0.0,
    request_id: str = "",
) -> None:
    """Log an AI WHERE-clause generation + validation attempt."""
    _write_entry({
        "event": "where_clause_generation",
        "question": question[:300],
        "generated_clause": generated_clause[:500],
        "validated_clause": validated_clause[:500],
        "validation_passed": validation_passed,
        "validation_errors": validation_errors or [],
        "latency_ms": round(latency_ms, 1),
        "request_id": request_id or "-",
    })


def log_sql_execution(
    *,
    caller: str,
    sp_mode: str,
    metric_key: str,
    aggregation: str,
    dimension: str,
    limit: int,
    ai_where_clause: str = "",
    filters: Optional[Dict[str, Any]] = None,
    row_count: int = 0,
    stat_code: Optional[int] = None,
    latency_ms: float = 0.0,
    success: bool = True,
    error: str = "",
    request_id: str = "",
) -> None:
    """Log a SQL execution via the report API / stored procedure."""
    _write_entry({
        "event": "sql_execution",
        "caller": caller,
        "sp_mode": sp_mode,
        "metric_key": metric_key,
        "aggregation": aggregation,
        "dimension": dimension,
        "limit": limit,
        "ai_where_clause": ai_where_clause[:500],
        "filters": filters or {},
        "row_count": row_count,
        "stat_code": stat_code,
        "latency_ms": round(latency_ms, 1),
        "success": success,
        "error": error[:300],
        "request_id": request_id or "-",
    })


# ── Convenience context manager for timing ──

class AuditTimer:
    """Simple timer for measuring latency in audit log entries."""

    def __init__(self):
        self._start = 0.0

    def __enter__(self):
        self._start = time.perf_counter()
        return self

    def __exit__(self, *args):
        self.elapsed_ms = (time.perf_counter() - self._start) * 1000
        return False

    @property
    def ms(self) -> float:
        return getattr(self, "elapsed_ms", 0.0)


# ── Chat exchange logging (question + answer + context) ──

def log_chat_exchange(
    *,
    question: str,
    answer: str = "",
    report_key: str = "",
    filters: dict = None,
    assumptions: list = None,
    session_id: str = "",
    error: str = "",
    latency_ms: float = 0.0,
) -> None:
    """Log a full chat exchange: user question + AI answer + context."""
    _write_entry({
        "event": "chat_exchange",
        "question": question,
        "answer": answer[:2000],
        "report_key": report_key,
        "filters": filters or {},
        "assumptions": assumptions or [],
        "session_id": session_id,
        "error": error[:500],
        "latency_ms": round(latency_ms, 1),
    })


# ── Report API request logging (what we send to the real API) ──

def log_report_api_request(
    *,
    report_key: str,
    api_url: str,
    request_body: dict = None,
    ai_where_clause: str = "",
    response_status: int = 0,
    response_preview: str = "",
    latency_ms: float = 0.0,
    success: bool = True,
    error: str = "",
) -> None:
    """Log the full request body sent to the real report API."""
    _write_entry({
        "event": "report_api_request",
        "report_key": report_key,
        "api_url": api_url,
        "request_body": json.dumps(request_body or {}, ensure_ascii=False, default=str)[:5000],
        "ai_where_clause": ai_where_clause[:1000],
        "response_status": response_status,
        "response_preview": response_preview[:1000],
        "latency_ms": round(latency_ms, 1),
        "success": success,
        "error": error[:500],
    })


# ── Metric validation audit trail (3-layer architecture) ──

def log_metric_validation(
    *,
    layer: int,
    question: str = "",
    report_key: str = "",
    original_metric: str = "",
    corrected_metric: str = "",
    original_type: str = "",
    corrected_type: str = "",
    intent_type: str = "",
    action: str = "",
    reason: str = "",
    extra_metrics_dropped: list = None,
    request_id: str = "",
) -> None:
    """Log a metric validation decision from any layer of the 3-layer architecture.

    Args:
        layer: 1 (prompt), 2 (post-LLM override), 3 (SP guard)
        question: user's original question
        report_key: report key
        original_metric: metric the LLM chose
        corrected_metric: metric after validation (same as original if no override)
        original_type: semantic type of original metric (amount/weight/count/rate)
        corrected_type: semantic type of corrected metric
        intent_type: detected intent type from question
        action: "pass", "override", "drop_extra", "fallback", "warn"
        reason: human-readable explanation
        extra_metrics_dropped: list of extra metrics that were dropped
        request_id: request ID for correlation
    """
    _write_entry({
        "event": "metric_validation",
        "layer": layer,
        "question": question[:300],
        "report_key": report_key,
        "original_metric": original_metric,
        "corrected_metric": corrected_metric,
        "original_type": original_type,
        "corrected_type": corrected_type,
        "intent_type": intent_type,
        "action": action,
        "reason": reason[:500],
        "extra_metrics_dropped": extra_metrics_dropped or [],
        "request_id": request_id or "-",
    })
