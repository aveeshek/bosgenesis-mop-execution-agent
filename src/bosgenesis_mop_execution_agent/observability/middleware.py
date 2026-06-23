"""FastAPI request observability middleware."""

from __future__ import annotations

import re
import time
from uuid import uuid4

from fastapi import Request, Response

from bosgenesis_mop_execution_agent.observability.logging import (
    clear_observability_context,
    log_event,
    set_observability_context,
)
from bosgenesis_mop_execution_agent.observability.metrics import METRICS

_JOB_ID_RE = re.compile(r"/execution-jobs/([^/]+)")


async def observability_middleware(request: Request, call_next) -> Response:  # type: ignore[no-untyped-def]
    started = time.perf_counter()
    request_id = request.headers.get("X-Request-ID") or f"req-{uuid4()}"
    correlation_id = request.headers.get("X-Correlation-ID") or request_id
    trace_id = request.headers.get("X-Trace-ID") or _trace_id_from_traceparent(
        request.headers.get("traceparent")
    )
    job_id = _job_id_from_path(request.url.path)
    set_observability_context(
        request_id=request_id,
        job_id=job_id,
        correlation_id=correlation_id,
        trace_id=trace_id,
    )
    try:
        response = await call_next(request)
    except Exception as exc:
        METRICS.increment(
            "bosgenesis_mop_execution_http_requests_total",
            {
                "method": request.method,
                "path": _route_template(request),
                "status": "500",
                "job_id": job_id,
                "correlation_id": correlation_id,
                "trace_id": trace_id,
            },
        )
        log_event(
            "http_request_failed",
            method=request.method,
            path=request.url.path,
            job_id=job_id,
            correlation_id=correlation_id,
            trace_id=trace_id,
            error_type=type(exc).__name__,
        )
        raise
    finally:
        duration = time.perf_counter() - started
        # If an exception was raised, the explicit except path records the request counter.
        if "response" in locals():
            labels = {
                "method": request.method,
                "path": _route_template(request),
                "status": str(response.status_code),
                "job_id": job_id,
                "correlation_id": correlation_id,
                "trace_id": trace_id,
            }
            METRICS.increment("bosgenesis_mop_execution_http_requests_total", labels)
            METRICS.observe("bosgenesis_mop_execution_http_request_seconds", duration, labels)
            response.headers["X-Request-ID"] = request_id
            response.headers["X-Correlation-ID"] = correlation_id
            if trace_id:
                response.headers["X-Trace-ID"] = trace_id
            log_event(
                "http_request_completed",
                method=request.method,
                path=request.url.path,
                status_code=response.status_code,
                duration_ms=round(duration * 1000, 3),
                job_id=job_id,
                correlation_id=correlation_id,
                trace_id=trace_id,
            )
        clear_observability_context()
    return response


def _job_id_from_path(path: str) -> str | None:
    match = _JOB_ID_RE.search(path)
    return match.group(1) if match else None


def _trace_id_from_traceparent(traceparent: str | None) -> str | None:
    if not traceparent:
        return None
    parts = traceparent.split("-")
    return parts[1] if len(parts) >= 2 and parts[1] else None


def _route_template(request: Request) -> str:
    route = request.scope.get("route")
    path = getattr(route, "path", None)
    return str(path or request.url.path)
