"""Common MCP client base with retries, redaction, observations, and audit hooks."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any, Protocol

from bosgenesis_mop_execution_agent.common.ids import new_id
from bosgenesis_mop_execution_agent.mcp_clients.models import (
    McpCallResult,
    McpStructuredError,
    McpTransportError,
)
from bosgenesis_mop_execution_agent.models import (
    ActorType,
    AuditEvent,
    ErrorCode,
    Observation,
    ObservationSeverity,
    ObservationType,
)
from bosgenesis_mop_execution_agent.security import redact_value


class McpTransport(Protocol):
    """Synchronous transport interface for MCP tool invocation."""

    def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
        correlation_id: str | None,
        trace_id: str | None,
    ) -> dict[str, Any]:
        """Call a named MCP tool and return its raw result."""


McpAuditHook = Callable[[AuditEvent], None]


class McpClientBase:
    """Base class for governed MCP clients."""

    def __init__(
        self,
        *,
        server_name: str,
        transport: McpTransport,
        job_id: str,
        timeout_seconds: float = 30.0,
        correlation_id: str | None = None,
        trace_id: str | None = None,
        audit_hook: McpAuditHook | None = None,
        max_safe_retries: int = 1,
    ) -> None:
        self.server_name = server_name
        self._transport = transport
        self._job_id = job_id
        self._timeout_seconds = timeout_seconds
        self._correlation_id = correlation_id
        self._trace_id = trace_id
        self._audit_hook = audit_hook
        self._max_safe_retries = max_safe_retries

    def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        safe_retry: bool = False,
        mutating: bool = False,
    ) -> McpCallResult:
        """Call an MCP tool and normalize success/failure without worker reasoning."""
        audit_event = self._build_audit_event(tool_name, arguments)
        try:
            if self._audit_hook is not None:
                self._audit_hook(audit_event)
        except Exception as exc:
            return self._failure_result(
                tool_name=tool_name,
                error=McpStructuredError(
                    error_code=ErrorCode.AUDIT_WRITE_FAILED,
                    message=f"audit_hook_failed:{type(exc).__name__}",
                    retryable=False,
                    raw_type=type(exc).__name__,
                ),
                attempts=1,
                audit_event=audit_event,
            )

        attempts = self._max_safe_retries + 1 if safe_retry and not mutating else 1
        last_error: McpStructuredError | None = None
        for attempt in range(1, attempts + 1):
            try:
                raw_result = self._transport.call_tool(
                    server_name=self.server_name,
                    tool_name=tool_name,
                    arguments=arguments,
                    timeout_seconds=self._timeout_seconds,
                    correlation_id=self._correlation_id,
                    trace_id=self._trace_id,
                )
            except TimeoutError as exc:
                last_error = McpStructuredError(
                    error_code=ErrorCode.TIMEOUT_EXCEEDED,
                    message=f"mcp_timeout:{tool_name}",
                    retryable=safe_retry and attempt < attempts,
                    raw_type=type(exc).__name__,
                )
                if attempt < attempts:
                    continue
                return self._failure_result(tool_name, last_error, attempt, audit_event)
            except McpTransportError as exc:
                last_error = McpStructuredError(
                    error_code=ErrorCode.MCP_UNAVAILABLE,
                    message=f"mcp_unavailable:{tool_name}:{exc}",
                    retryable=safe_retry and attempt < attempts,
                    raw_type=type(exc).__name__,
                )
                if attempt < attempts:
                    continue
                return self._failure_result(tool_name, last_error, attempt, audit_event)

            parsed = self._parse_raw_result(raw_result)
            if parsed.error is not None:
                return self._failure_result(tool_name, parsed.error, attempt, audit_event)
            return self._success_result(tool_name, parsed.data or {}, attempt, audit_event)

        fallback = last_error or McpStructuredError(
            error_code=ErrorCode.UNKNOWN_ERROR,
            message=f"mcp_unknown_failure:{tool_name}",
        )
        return self._failure_result(tool_name, fallback, attempts, audit_event)

    def _parse_raw_result(self, raw_result: dict[str, Any]) -> _ParsedMcpResult:
        if not isinstance(raw_result, dict):
            return _ParsedMcpResult(
                error=McpStructuredError(
                    error_code=ErrorCode.UNKNOWN_ERROR,
                    message="mcp_malformed_response:not_object",
                    raw_type=type(raw_result).__name__,
                )
            )
        if "ok" not in raw_result:
            return _ParsedMcpResult(
                error=McpStructuredError(
                    error_code=ErrorCode.UNKNOWN_ERROR,
                    message="mcp_malformed_response:missing_ok",
                )
            )
        ok = raw_result.get("ok")
        if ok is True:
            data = raw_result.get("data", {})
            if not isinstance(data, dict):
                return _ParsedMcpResult(
                    error=McpStructuredError(
                        error_code=ErrorCode.UNKNOWN_ERROR,
                        message="mcp_malformed_response:data_not_object",
                    )
                )
            return _ParsedMcpResult(data=redact_value(data))
        if ok is False:
            error = raw_result.get("error")
            if not isinstance(error, dict):
                return _ParsedMcpResult(
                    error=McpStructuredError(
                        error_code=ErrorCode.MCP_UNAVAILABLE,
                        message="mcp_error_response:missing_error",
                    )
                )
            return _ParsedMcpResult(error=self._structured_error(error))
        return _ParsedMcpResult(
            error=McpStructuredError(
                error_code=ErrorCode.UNKNOWN_ERROR,
                message="mcp_malformed_response:ok_not_boolean",
            )
        )

    def _structured_error(self, error: dict[str, Any]) -> McpStructuredError:
        raw_code = error.get("error_code")
        message = error.get("message", "mcp_error")
        retryable = error.get("retryable", False)
        try:
            error_code = ErrorCode(str(raw_code))
        except ValueError:
            error_code = ErrorCode.MCP_UNAVAILABLE
        return McpStructuredError(
            error_code=error_code,
            message=str(redact_value(message)),
            retryable=bool(retryable),
            raw_type=str(raw_code) if raw_code is not None else None,
        )

    def _success_result(
        self,
        tool_name: str,
        data: dict[str, Any],
        attempts: int,
        audit_event: AuditEvent,
    ) -> McpCallResult:
        observation = self._observation(
            tool_name=tool_name,
            severity=ObservationSeverity.INFO,
            summary=f"MCP call succeeded: {self.server_name}.{tool_name}",
            result={"success": True, "data": data, "attempts": attempts},
        )
        return McpCallResult(
            server_name=self.server_name,
            tool_name=tool_name,
            success=True,
            data=data,
            correlation_id=self._correlation_id,
            trace_id=self._trace_id,
            observation=observation,
            audit_event=audit_event,
            attempts=attempts,
        )

    def _failure_result(
        self,
        tool_name: str,
        error: McpStructuredError,
        attempts: int,
        audit_event: AuditEvent,
    ) -> McpCallResult:
        observation = self._observation(
            tool_name=tool_name,
            severity=ObservationSeverity.ERROR,
            summary=f"MCP call failed: {self.server_name}.{tool_name}",
            result={
                "success": False,
                "error": error.model_dump(mode="json"),
                "attempts": attempts,
                "worker_reasoning_triggered": False,
            },
        )
        return McpCallResult(
            server_name=self.server_name,
            tool_name=tool_name,
            success=False,
            error=error,
            correlation_id=self._correlation_id,
            trace_id=self._trace_id,
            observation=observation,
            audit_event=audit_event,
            attempts=attempts,
        )

    def _observation(
        self,
        *,
        tool_name: str,
        severity: ObservationSeverity,
        summary: str,
        result: dict[str, Any],
    ) -> Observation:
        return Observation(
            observation_id=new_id("obs"),
            job_id=self._job_id,
            severity=severity,
            observation_type=ObservationType.MCP_CALL_RESULT,
            summary=summary,
            correlation_id=self._correlation_id,
            trace_id=self._trace_id,
            mcp_server=self.server_name,
            mcp_tool=tool_name,
            result=redact_value(result),
            redaction_applied=True,
        )

    def _build_audit_event(self, tool_name: str, arguments: dict[str, Any]) -> AuditEvent:
        redacted_arguments = redact_value(arguments)
        return AuditEvent(
            audit_event_id=new_id("audit"),
            actor_type=ActorType.WORKER,
            action=f"mcp_call:{self.server_name}.{tool_name}",
            job_id=self._job_id,
            correlation_id=self._correlation_id,
            trace_id=self._trace_id,
            input_hash=_stable_hash(redacted_arguments),
            details={
                "mcp_server": self.server_name,
                "mcp_tool": tool_name,
                "arguments_redacted": redacted_arguments,
            },
            redacted=True,
        )


class _ParsedMcpResult:
    def __init__(
        self,
        *,
        data: dict[str, Any] | None = None,
        error: McpStructuredError | None = None,
    ) -> None:
        self.data = data
        self.error = error


def _stable_hash(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
