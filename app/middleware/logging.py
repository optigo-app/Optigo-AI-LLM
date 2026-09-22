"""Structured logging with request IDs for production observability."""

import logging
import sys
import uuid
from contextvars import ContextVar
from typing import Optional

from pythonjsonlogger import jsonlogger
from starlette.middleware.base import BaseHTTPMiddleware
from fastapi import Request


request_id_var: ContextVar[Optional[str]] = ContextVar("request_id", default=None)


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a unique request ID to every request and store it in context vars."""

    async def dispatch(self, request: Request, call_next):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
        token = request_id_var.set(rid)
        request.state.request_id = rid

        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        request_id_var.reset(token)
        return response


class RequestIdFilter(logging.Filter):
    """Inject the current request_id into log records."""

    def filter(self, record):
        record.request_id = request_id_var.get() or "-"
        return True


def setup_logging(level: str = "INFO") -> None:
    """Configure structured JSON logging for the application."""
    handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        "%(asctime)s %(name)s %(levelname)s %(request_id)s %(message)s",
        rename_fields={"asctime": "timestamp"},
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()
    root.addHandler(handler)

    # Add the request ID filter so every record gets it
    for h in root.handlers:
        h.addFilter(RequestIdFilter())

    # Quiet noisy libraries
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("openai").setLevel(logging.WARNING)
    logging.getLogger("anthropic").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
