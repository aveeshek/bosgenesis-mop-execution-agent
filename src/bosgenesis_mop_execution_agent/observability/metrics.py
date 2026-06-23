"""Small in-process Prometheus text metrics registry."""

from __future__ import annotations

import math
import threading
from collections import defaultdict
from datetime import datetime
from typing import Any

from bosgenesis_mop_execution_agent.models import ExecutionJob, JobState

Labels = tuple[tuple[str, str], ...]


class MetricsRegistry:
    """Minimal deterministic metrics registry for unit tests and POC deployments."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._counters: dict[tuple[str, Labels], float] = defaultdict(float)
        self._gauges: dict[tuple[str, Labels], float] = {}
        self._histograms: dict[tuple[str, Labels], list[float]] = defaultdict(list)

    def increment(self, name: str, labels: dict[str, Any] | None = None, amount: float = 1) -> None:
        with self._lock:
            self._counters[(name, _labels(labels))] += amount

    def set_gauge(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._gauges[(name, _labels(labels))] = value

    def observe(self, name: str, value: float, labels: dict[str, Any] | None = None) -> None:
        if math.isnan(value) or math.isinf(value):
            return
        with self._lock:
            self._histograms[(name, _labels(labels))].append(max(0.0, float(value)))

    def render_prometheus(self) -> str:
        with self._lock:
            lines: list[str] = []
            for (name, labels), value in sorted(self._counters.items()):
                lines.append(f"# TYPE {name} counter")
                lines.append(f"{name}{_format_labels(labels)} {value:g}")
            for (name, labels), value in sorted(self._gauges.items()):
                lines.append(f"# TYPE {name} gauge")
                lines.append(f"{name}{_format_labels(labels)} {value:g}")
            for (name, labels), values in sorted(self._histograms.items()):
                count = len(values)
                total = sum(values)
                lines.append(f"# TYPE {name} summary")
                lines.append(f"{name}_count{_format_labels(labels)} {count:g}")
                lines.append(f"{name}_sum{_format_labels(labels)} {total:g}")
            return "\n".join(lines) + "\n"

    def reset(self) -> None:
        with self._lock:
            self._counters.clear()
            self._gauges.clear()
            self._histograms.clear()


METRICS = MetricsRegistry()


def record_job_state(job: ExecutionJob) -> None:
    labels = _job_labels(job)
    METRICS.increment("bosgenesis_mop_execution_job_state_transitions_total", labels)
    for state in JobState:
        METRICS.set_gauge(
            "bosgenesis_mop_execution_job_state",
            1 if job.state == state else 0,
            {**labels, "state": state.value},
        )
    if job.completed_at is not None:
        METRICS.observe(
            "bosgenesis_mop_execution_job_duration_seconds",
            _seconds_between(job.created_at, job.completed_at),
            labels,
        )


def record_policy_blocks(
    blocks: list[dict[str, Any]],
    *,
    job_id: str | None = None,
    correlation_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    for block in blocks:
        METRICS.increment(
            "bosgenesis_mop_execution_policy_blocks_total",
            {
                "code": block.get("code") or "UNKNOWN",
                "job_id": job_id,
                "correlation_id": correlation_id,
                "trace_id": trace_id,
            },
        )


def record_decision_required(
    job: ExecutionJob,
    *,
    reason_code: str,
) -> None:
    METRICS.increment(
        "bosgenesis_mop_execution_decision_required_total",
        {**_job_labels(job), "reason_code": reason_code},
    )


def record_approval_wait(job: ExecutionJob, approved_at: datetime) -> None:
    METRICS.observe(
        "bosgenesis_mop_execution_approval_wait_seconds",
        _seconds_between(job.created_at, approved_at),
        _job_labels(job),
    )


def record_redaction(
    surface: str,
    *,
    job_id: str | None = None,
    correlation_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    METRICS.increment(
        "bosgenesis_mop_execution_redactions_total",
        {
            "surface": surface,
            "job_id": job_id,
            "correlation_id": correlation_id,
            "trace_id": trace_id,
        },
    )


def record_audit_failure(
    *,
    action: str,
    job_id: str | None = None,
    correlation_id: str | None = None,
    trace_id: str | None = None,
) -> None:
    METRICS.increment(
        "bosgenesis_mop_execution_audit_failures_total",
        {
            "action": action,
            "job_id": job_id,
            "correlation_id": correlation_id,
            "trace_id": trace_id,
        },
    )


def record_lock_contention(job: ExecutionJob) -> None:
    METRICS.increment("bosgenesis_mop_execution_lock_contention_total", _job_labels(job))


def record_mcp_call(
    *,
    server_name: str,
    tool_name: str,
    success: bool,
    duration_seconds: float,
    job_id: str | None,
    correlation_id: str | None,
    trace_id: str | None,
    error_code: str | None = None,
) -> None:
    labels = {
        "server": server_name,
        "tool": tool_name,
        "success": str(success).lower(),
        "error_code": error_code or "",
        "job_id": job_id,
        "correlation_id": correlation_id,
        "trace_id": trace_id,
    }
    METRICS.increment("bosgenesis_mop_execution_mcp_calls_total", labels)
    METRICS.observe("bosgenesis_mop_execution_mcp_latency_seconds", duration_seconds, labels)


def _job_labels(job: ExecutionJob) -> dict[str, Any]:
    return {
        "job_id": job.job_id,
        "target_namespace": job.target_namespace,
        "state": job.state.value,
        "correlation_id": job.correlation_id,
        "trace_id": job.trace_id,
    }


def _seconds_between(start: datetime, end: datetime) -> float:
    return max(0.0, (end - start).total_seconds())


def _labels(labels: dict[str, Any] | None) -> Labels:
    return tuple(
        sorted((key, "" if value is None else str(value)) for key, value in (labels or {}).items())
    )


def _format_labels(labels: Labels) -> str:
    if not labels:
        return ""
    encoded = ",".join(f'{key}="{_escape(value)}"' for key, value in labels)
    return f"{{{encoded}}}"


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
