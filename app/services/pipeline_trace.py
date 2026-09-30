"""Stage-level pipeline tracing for chat requests.

Each chat request runs a fixed governed pipeline:

    spell/route -> semantic_parse -> plan_validate -> sql_guard
    -> execute -> answer

``StageTracer`` records per-stage latency, outcome, and a short detail
string so a single request can be replayed from the audit log:

    {"event": "pipeline_trace", "request_id": "...",
     "stages": [{"name": "execute", "ms": 412.3, "outcome": "ok", ...}]}

It also feeds ``stage_latencies`` + ``failure_stage`` into the existing
``log_request_trace`` entry, which were previously always empty.

This is deliberately dependency-free: if LangGraph is ever adopted, each
stage maps 1:1 to a graph node and this module becomes the checkpointer's
data source.
"""

import logging
import time
from contextlib import contextmanager
from typing import Any, Dict, List, Optional

from app.middleware.audit_log import _write_entry

logger = logging.getLogger(__name__)

_DETAIL_LIMIT = 200


def _truncate(detail: Any) -> Any:
    if isinstance(detail, str) and len(detail) > _DETAIL_LIMIT:
        return detail[:_DETAIL_LIMIT] + "..."
    return detail


class StageTracer:
    """Accumulates per-stage timing/outcome for one request."""

    def __init__(self, request_id: str = ""):
        self.request_id = request_id
        self.stages: List[Dict[str, Any]] = []
        self._open: Dict[str, float] = {}

    def start(self, stage: str) -> None:
        self._open[stage] = time.perf_counter()

    def end(self, stage: str, outcome: str = "ok", detail: Any = None) -> float:
        """Close a started stage. Returns elapsed ms (0 if never started)."""
        started = self._open.pop(stage, None)
        elapsed = (time.perf_counter() - started) * 1000 if started else 0.0
        self.stages.append({
            "name": stage,
            "ms": round(elapsed, 1),
            "outcome": outcome,
            "detail": _truncate(detail) if detail is not None else None,
        })
        return elapsed

    def record(self, stage: str, outcome: str = "ok", detail: Any = None,
               elapsed_ms: float = 0.0) -> None:
        """One-shot record for a stage timed elsewhere."""
        self.stages.append({
            "name": stage,
            "ms": round(elapsed_ms, 1),
            "outcome": outcome,
            "detail": _truncate(detail) if detail is not None else None,
        })

    @contextmanager
    def stage(self, name: str, detail: Any = None):
        """``with tracer.stage("parse")`` — records outcome=ok or error."""
        self.start(name)
        try:
            yield
        except Exception as exc:
            self.end(name, outcome="error", detail=str(exc))
            raise
        else:
            self.end(name, detail=detail)

    def skip(self, stage: str, reason: str = "") -> None:
        self.record(stage, outcome="skipped", detail=reason)

    @property
    def failure_stage(self) -> str:
        for s in self.stages:
            if s["outcome"] == "error":
                return s["name"]
        return ""

    @property
    def latencies(self) -> Dict[str, float]:
        """``{stage: ms}`` for ``log_request_trace(stage_latencies=...)``."""
        return {s["name"]: s["ms"] for s in self.stages}

    def outcome_of(self, stage: str) -> str:
        for s in reversed(self.stages):
            if s["name"] == stage:
                return s["outcome"]
        return ""

    def emit(self, *, question: str = "", status: str = "",
             report_key: str = "", extra: Optional[Dict[str, Any]] = None) -> None:
        """Write the accumulated trace as one JSONL event in the audit log."""
        try:
            _write_entry({
                "event": "pipeline_trace",
                "request_id": self.request_id,
                "question": _truncate(question),
                "report_key": report_key,
                "status": status,
                "total_ms": round(sum(s["ms"] for s in self.stages), 1),
                "stages": self.stages,
                "extra": extra or {},
            })
        except Exception:
            logger.debug("pipeline_trace emit failed", exc_info=True)
