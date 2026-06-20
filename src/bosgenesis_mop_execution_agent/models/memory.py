"""Execution memory models.

Memory is factual execution context only. It is never decision authority and
must not trigger state transitions by itself.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import Field

from bosgenesis_mop_execution_agent.common.ids import new_id
from bosgenesis_mop_execution_agent.common.time import utc_now
from bosgenesis_mop_execution_agent.models.base import StrictBaseModel

MEMORY_AUTHORITY = "context_only_not_decision_authority"


class MemoryLayer(StrEnum):
    """Phase 11 memory layers."""

    IN_RUN = "in_run"
    DURABLE_JOB = "durable_job"
    RESOURCE_STATE = "resource_state"
    EPISODIC_EXECUTION = "episodic_execution"
    SEMANTIC_FAILURE = "semantic_failure"
    POLICY = "policy"
    APPROVAL = "approval"
    AUDIT = "audit"
    OBSERVABILITY = "observability"


class MemoryRecord(StrictBaseModel):
    """Redacted execution-context memory record."""

    memory_id: str = Field(default_factory=lambda: new_id("mem"))
    layer: MemoryLayer
    job_id: str
    namespace: str | None = None
    chart: str | None = None
    kind: str | None = None
    resource_name: str | None = None
    error_code: str | None = None
    mcp_source: str | None = None
    tenant: str | None = None
    environment: str | None = None
    summary: str
    payload_redacted: dict[str, Any] = Field(default_factory=dict)
    authority: str = MEMORY_AUTHORITY
    redaction_applied: bool = True
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)


class MemoryQuery(StrictBaseModel):
    """Supported memory retrieval filters."""

    job_id: str | None = None
    namespace: str | None = None
    chart: str | None = None
    kind: str | None = None
    error_code: str | None = None
    mcp_source: str | None = None
    tenant: str | None = None
    environment: str | None = None
