"""Deterministic error model."""

from __future__ import annotations

from enum import StrEnum

from bosgenesis_mop_execution_agent.models.base import StrictBaseModel
from bosgenesis_mop_execution_agent.models.policies import PolicyBlock


class ErrorCode(StrEnum):
    """Deterministic error codes from the specification."""

    BUNDLE_MISSING_REQUIRED_FILE = "BUNDLE_MISSING_REQUIRED_FILE"
    PLAN_SCHEMA_INVALID = "PLAN_SCHEMA_INVALID"
    PLAN_GRAPH_CYCLE = "PLAN_GRAPH_CYCLE"
    NAMESPACE_MISMATCH = "NAMESPACE_MISMATCH"
    CLUSTER_SCOPE_BLOCKED = "CLUSTER_SCOPE_BLOCKED"
    SECRET_VALUE_DETECTED = "SECRET_VALUE_DETECTED"
    PRODUCTION_DATA_COPY_DETECTED = "PRODUCTION_DATA_COPY_DETECTED"
    DRY_RUN_FAILED = "DRY_RUN_FAILED"
    APPROVAL_REQUIRED = "APPROVAL_REQUIRED"
    APPROVAL_SCOPE_MISMATCH = "APPROVAL_SCOPE_MISMATCH"
    RESOURCE_ALREADY_EXISTS = "RESOURCE_ALREADY_EXISTS"
    IMMUTABLE_FIELD_CONFLICT = "IMMUTABLE_FIELD_CONFLICT"
    HELM_RENDER_FAILED = "HELM_RENDER_FAILED"
    POD_UNSCHEDULABLE = "POD_UNSCHEDULABLE"
    NODE_UNAVAILABLE = "NODE_UNAVAILABLE"
    PVC_PENDING = "PVC_PENDING"
    INGRESS_CONFLICT = "INGRESS_CONFLICT"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    MCP_UNAVAILABLE = "MCP_UNAVAILABLE"
    TIMEOUT_EXCEEDED = "TIMEOUT_EXCEEDED"
    AUDIT_WRITE_FAILED = "AUDIT_WRITE_FAILED"
    INVALID_STATE_TRANSITION = "INVALID_STATE_TRANSITION"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


class ProblemDetails(StrictBaseModel):
    """Problem details response matching the OpenAPI shape."""

    type: str
    title: str
    status: int
    detail: str | None = None
    instance: str | None = None
    correlation_id: str | None = None
    error_code: ErrorCode | None = None
    policy_blocks: list[PolicyBlock] = []


def problem_details_for_error(
    *,
    error_code: ErrorCode,
    title: str,
    status: int,
    detail: str | None = None,
    correlation_id: str | None = None,
    policy_blocks: list[PolicyBlock] | None = None,
) -> ProblemDetails:
    """Build a deterministic problem-details payload."""
    return ProblemDetails(
        type=f"urn:bosgenesis:mop-execution:error:{error_code.value}",
        title=title,
        status=status,
        detail=detail,
        correlation_id=correlation_id,
        error_code=error_code,
        policy_blocks=policy_blocks or [],
    )
