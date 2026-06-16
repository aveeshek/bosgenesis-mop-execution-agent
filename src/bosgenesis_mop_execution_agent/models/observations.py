"""Observation models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from bosgenesis_mop_execution_agent.common.time import utc_now
from bosgenesis_mop_execution_agent.models.base import StrictBaseModel
from bosgenesis_mop_execution_agent.models.enums import ObservationSeverity, ObservationType
from bosgenesis_mop_execution_agent.models.policies import PolicyBlock
from bosgenesis_mop_execution_agent.models.resources import ResourceRef


class Observation(StrictBaseModel):
    """Structured factual observation."""

    observation_id: str
    job_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    severity: ObservationSeverity
    observation_type: ObservationType
    summary: str
    phase_id: str | None = None
    step_id: str | None = None
    command_id: str | None = None
    correlation_id: str | None = None
    trace_id: str | None = None
    mcp_server: str | None = None
    mcp_tool: str | None = None
    resource_refs: list[ResourceRef] = []
    stdout_redacted: str | None = None
    stderr_redacted: str | None = None
    result: dict[str, Any] = {}
    redaction_applied: bool = True
    policy_blocks: list[PolicyBlock] = []
    next_required_decision: dict[str, Any] | None = None
