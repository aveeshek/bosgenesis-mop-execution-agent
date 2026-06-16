"""External instruction models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from bosgenesis_mop_execution_agent.models.base import StrictBaseModel
from bosgenesis_mop_execution_agent.models.resources import ResourceRef


class InstructionType(StrEnum):
    """Allowed external instruction types from OpenAPI/MCP contract."""

    CONTINUE = "continue"
    RETRY = "retry"
    WAIT = "wait"
    SKIP = "skip"
    PATCH_MANIFEST = "patch_manifest"
    REPLACE_MANIFEST = "replace_manifest"
    RUN_VALIDATION = "run_validation"
    REQUEST_HUMAN_APPROVAL = "request_human_approval"
    ROLLBACK = "rollback"
    ABORT = "abort"
    REFRESH_OBSERVATION = "refresh_observation"
    INVOKE_MCP_TOOL = "invoke_mcp_tool"


class RetryPolicy(StrictBaseModel):
    """Explicit retry bounds from an external instruction."""

    max_attempts: int = 1
    backoff_seconds: int = 0


class ExternalInstruction(StrictBaseModel):
    """Explicit bounded instruction from the external LLM controller."""

    instruction_id: str
    job_id: str
    instruction_type: InstructionType
    controller_id: str
    issued_by: str
    issued_at: datetime | None = None
    correlation_id: str | None = None
    trace_id: str | None = None
    target_phase_id: str | None = None
    target_step_id: str | None = None
    target_resource: ResourceRef | None = None
    rationale: str | None = None
    wait_seconds: int | None = None
    retry_policy: RetryPolicy | None = None
    manifest_patch: dict[str, Any] | None = None
    replacement_manifest: str | None = None
    validation_selector: str | None = None
    approval_token: str | None = None
    dry_run_required: bool = True
    destructive_action: bool = False
    safety_acknowledgements: list[str] = []
    metadata: dict[str, Any] = {}
