"""Observation builder for deterministic runtime events."""

from __future__ import annotations

from typing import Any, cast

from bosgenesis_mop_execution_agent.common.ids import new_id
from bosgenesis_mop_execution_agent.models import (
    Observation,
    ObservationSeverity,
    ObservationType,
    PolicyBlock,
    ResourceRef,
)
from bosgenesis_mop_execution_agent.security import redact_value


class ObservationBuilder:
    """Build redacted observations for every runtime event class."""

    def build(
        self,
        *,
        job_id: str,
        observation_type: ObservationType,
        summary: str,
        severity: ObservationSeverity = ObservationSeverity.INFO,
        phase_id: str | None = None,
        step_id: str | None = None,
        command_id: str | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
        mcp_server: str | None = None,
        mcp_tool: str | None = None,
        resource_refs: list[ResourceRef] | None = None,
        result: dict[str, Any] | None = None,
        policy_blocks: list[PolicyBlock] | None = None,
        next_required_decision: dict[str, Any] | None = None,
    ) -> Observation:
        return Observation(
            observation_id=new_id("obs"),
            job_id=job_id,
            severity=severity,
            observation_type=observation_type,
            summary=summary,
            phase_id=phase_id,
            step_id=step_id,
            command_id=command_id,
            correlation_id=correlation_id,
            trace_id=trace_id,
            mcp_server=mcp_server,
            mcp_tool=mcp_tool,
            resource_refs=resource_refs or [],
            result=cast("dict[str, Any]", redact_value(result or {})),
            policy_blocks=policy_blocks or [],
            next_required_decision=cast(
                "dict[str, Any] | None",
                redact_value(next_required_decision) if next_required_decision else None,
            ),
        )

    def error(
        self,
        *,
        job_id: str,
        summary: str,
        code: str,
        phase_id: str | None = None,
        step_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> Observation:
        return self.build(
            job_id=job_id,
            observation_type=ObservationType.ERROR,
            severity=ObservationSeverity.ERROR,
            summary=summary,
            phase_id=phase_id,
            step_id=step_id,
            result={"code": code, **(details or {})},
        )
