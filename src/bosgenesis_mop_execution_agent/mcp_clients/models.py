"""Structured MCP client result models."""

from __future__ import annotations

from typing import Any

from pydantic import Field

from bosgenesis_mop_execution_agent.models import AuditEvent, ErrorCode, Observation
from bosgenesis_mop_execution_agent.models.base import StrictBaseModel


class McpTransportError(RuntimeError):
    """Raised by transports for transient MCP connectivity failures."""


class McpStructuredError(StrictBaseModel):
    """Deterministic MCP error details."""

    error_code: ErrorCode
    message: str
    retryable: bool = False
    raw_type: str | None = None


class McpCallResult(StrictBaseModel):
    """Normalized MCP call result with observation and optional audit event."""

    server_name: str
    tool_name: str
    success: bool
    data: dict[str, Any] | None = None
    error: McpStructuredError | None = None
    correlation_id: str | None = None
    trace_id: str | None = None
    observation: Observation
    audit_event: AuditEvent | None = None
    attempts: int = Field(default=1, ge=1)
