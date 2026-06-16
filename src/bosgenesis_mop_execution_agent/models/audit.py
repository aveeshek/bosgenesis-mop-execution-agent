"""Audit models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from bosgenesis_mop_execution_agent.common.time import utc_now
from bosgenesis_mop_execution_agent.models.base import StrictBaseModel


class ActorType(StrEnum):
    """Actor types used in event and audit records."""

    WORKER = "worker"
    EXTERNAL_LLM = "external_llm"
    HUMAN = "human"
    SYSTEM = "system"
    MCP_SERVER = "mcp_server"


class AuditEvent(StrictBaseModel):
    """Append-only audit event."""

    audit_event_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    actor_type: ActorType
    action: str
    redacted: bool = True
    job_id: str | None = None
    actor_id: str | None = None
    correlation_id: str | None = None
    trace_id: str | None = None
    input_hash: str | None = None
    output_hash: str | None = None
    details: dict[str, Any] = {}
