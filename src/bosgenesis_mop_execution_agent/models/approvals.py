"""Human approval models."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from bosgenesis_mop_execution_agent.models.base import StrictBaseModel
from bosgenesis_mop_execution_agent.models.resources import ResourceRef


class ApprovalScope(StrEnum):
    """Human approval scopes from the OpenAPI contract."""

    NON_MUTATING_VALIDATION = "non_mutating_validation"
    DRY_RUN = "dry_run"
    NAMESPACE_CREATION = "namespace_creation"
    MUTATION = "mutation"
    ROLLBACK = "rollback"
    DESTRUCTIVE_ROLLBACK = "destructive_rollback"
    POLICY_EXCEPTION = "policy_exception"


class HumanApproval(StrictBaseModel):
    """Bounded human approval record."""

    approval_id: str
    job_id: str
    approver_id: str
    approval_scope: ApprovalScope
    ticket_reference: str
    statement: str
    correlation_id: str | None = None
    trace_id: str | None = None
    approver_role: str | None = None
    expires_at: datetime | None = None
    approved_resource_refs: list[ResourceRef] = []
    approved_phase_ids: list[str] = []
    approved_step_ids: list[str] = []
    policy_exception_id: str | None = None
