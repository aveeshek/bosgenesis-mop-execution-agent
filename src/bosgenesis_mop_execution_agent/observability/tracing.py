"""OpenTelemetry bootstrap for SigNoz-compatible OTLP export."""

from __future__ import annotations

import os
from typing import Any

from bosgenesis_mop_execution_agent.observability.logging import log_event

_configured = False
_status: dict[str, Any] = {"enabled": False, "configured": False}


def configure_tracing(app: Any | None = None) -> dict[str, Any]:
    """Configure OTEL tracing when enabled; degrade gracefully in local tests."""
    global _configured, _status
    enabled = _env_bool("OTEL_ENABLED", default=_env_bool("ENABLE_OTEL", default=False))
    endpoint = (
        os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.getenv("SIGNOZ_ENDPOINT")
        or "http://signoz-otel-collector.signoz.svc.cluster.local:4317"
    )
    service_name = os.getenv("OTEL_SERVICE_NAME") or os.getenv(
        "SERVICE_NAME",
        "bosgenesis-mop-execution-agent",
    )
    _status = {
        "enabled": enabled,
        "configured": False,
        "service_name": service_name,
        "endpoint": endpoint,
    }
    if not enabled:
        return dict(_status)
    if _configured:
        _status["configured"] = True
        return dict(_status)
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
    except Exception as exc:  # pragma: no cover - optional dependency guard
        _status["error"] = f"otel_import_failed:{type(exc).__name__}"
        log_event("otel_import_failed", error_type=type(exc).__name__)
        return dict(_status)

    resource = Resource.create(
        {
            "service.name": service_name,
            "deployment.environment": os.getenv("ENVIRONMENT", "local"),
        }
    )
    provider = TracerProvider(resource=resource)
    insecure = _env_bool("OTEL_EXPORTER_OTLP_INSECURE", default=True)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=insecure))
    )
    trace.set_tracer_provider(provider)
    if app is not None:
        FastAPIInstrumentor.instrument_app(app, tracer_provider=provider)
    _configured = True
    _status["configured"] = True
    log_event("otel_tracing_configured", endpoint=endpoint, service_name=service_name)
    return dict(_status)


def tracing_status() -> dict[str, Any]:
    return dict(_status)


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
