"""Structured JSON logging with request and job context."""

from __future__ import annotations

import contextvars
import json
import logging
import os
import sys
from datetime import UTC, datetime
from typing import Any

from bosgenesis_mop_execution_agent.security import redact_value

_request_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id",
    default=None,
)
_job_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("job_id", default=None)
_correlation_id: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id",
    default=None,
)
_trace_id: contextvars.ContextVar[str | None] = contextvars.ContextVar("trace_id", default=None)
_configured = False


class JsonLogFormatter(logging.Formatter):
    """Format log records as one JSON object per line."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "service": os.getenv("SERVICE_NAME", "bosgenesis-mop-execution-agent"),
            "environment": os.getenv("ENVIRONMENT", "local"),
            "request_id": _request_id.get(),
            "job_id": _job_id.get(),
            "correlation_id": _correlation_id.get(),
            "trace_id": _trace_id.get(),
        }
        extra = getattr(record, "bosgenesis", None)
        if isinstance(extra, dict):
            payload.update(redact_value(extra))
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(redact_value(payload), sort_keys=True, default=str)


def configure_logging() -> None:
    """Configure root logging once for API, worker, and reconciler processes."""
    global _configured
    if _configured:
        return
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    if os.getenv("LOG_FORMAT", "json").strip().lower() == "json":
        handler.setFormatter(JsonLogFormatter())
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)
    logging.getLogger("uvicorn.access").setLevel(level)
    _configured = True


def set_observability_context(
    *,
    request_id: str | None = None,
    job_id: str | None = None,
    correlation_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    if request_id is not None:
        _request_id.set(request_id)
    if job_id is not None:
        _job_id.set(job_id)
    if correlation_id is not None:
        _correlation_id.set(correlation_id)
    if trace_id is not None:
        _trace_id.set(trace_id)


def clear_observability_context() -> None:
    _request_id.set(None)
    _job_id.set(None)
    _correlation_id.set(None)
    _trace_id.set(None)


def log_event(
    event: str,
    *,
    level: int = logging.INFO,
    logger_name: str = "bosgenesis.mop_execution",
    **fields: Any,
) -> None:
    logging.getLogger(logger_name).log(
        level,
        event,
        extra={"bosgenesis": {"event": event, **fields}},
    )
