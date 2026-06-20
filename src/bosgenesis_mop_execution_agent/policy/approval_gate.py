"""Human approval policy guards."""

from __future__ import annotations

from datetime import datetime

from bosgenesis_mop_execution_agent.models import (
    ApprovalScope,
    HumanApproval,
    PolicyBlock,
    PolicySeverity,
    ResourceRef,
)

MUTATION_SCOPES = {
    ApprovalScope.MUTATION,
    ApprovalScope.NAMESPACE_CREATION,
    ApprovalScope.ROLLBACK,
    ApprovalScope.DESTRUCTIVE_ROLLBACK,
}


def approval_blocks(
    *,
    mutating: bool,
    approvals: list[HumanApproval],
    job_id: str,
    target_namespace: str,
    phase_id: str | None,
    step_id: str | None,
    resource_refs: list[ResourceRef],
    fingerprint: str | None,
    now: datetime,
) -> list[PolicyBlock]:
    """Block mutations without an active approval whose scope matches the action."""
    if not mutating:
        return []
    matching = [
        approval
        for approval in approvals
        if _approval_matches(
            approval=approval,
            job_id=job_id,
            target_namespace=target_namespace,
            phase_id=phase_id,
            step_id=step_id,
            resource_refs=resource_refs,
            fingerprint=fingerprint,
            now=now,
        )
    ]
    if matching:
        return []
    has_expired_approval = any(
        approval.expires_at is not None and approval.expires_at <= now for approval in approvals
    )
    if has_expired_approval:
        return [_block("APPROVAL_EXPIRED", "Approval expired before mutation.")]
    if approvals:
        return [_block("APPROVAL_SCOPE_MISMATCH", "Approval does not match this mutation.")]
    return [_block("APPROVAL_REQUIRED", "Mutating action requires human approval.")]


def _approval_matches(
    *,
    approval: HumanApproval,
    job_id: str,
    target_namespace: str,
    phase_id: str | None,
    step_id: str | None,
    resource_refs: list[ResourceRef],
    fingerprint: str | None,
    now: datetime,
) -> bool:
    if approval.job_id != job_id or approval.approval_scope not in MUTATION_SCOPES:
        return False
    if approval.expires_at is not None and approval.expires_at <= now:
        return False
    if approval.approved_phase_ids and phase_id not in approval.approved_phase_ids:
        return False
    if approval.approved_step_ids and step_id not in approval.approved_step_ids:
        return False
    if approval.command_fingerprint and approval.command_fingerprint != fingerprint:
        return False
    if approval.approved_resource_refs:
        return _resources_covered(approval.approved_resource_refs, resource_refs, target_namespace)
    return True


def _resources_covered(
    approved_refs: list[ResourceRef],
    requested_refs: list[ResourceRef],
    target_namespace: str,
) -> bool:
    if not requested_refs:
        return True
    approved_keys = {_resource_key(ref, target_namespace) for ref in approved_refs}
    requested_keys = {_resource_key(ref, target_namespace) for ref in requested_refs}
    return requested_keys.issubset(approved_keys)


def _resource_key(
    ref: ResourceRef,
    target_namespace: str,
) -> tuple[str | None, str | None, str | None]:
    return (ref.kind, ref.namespace or target_namespace, ref.name)


def _block(code: str, message: str) -> PolicyBlock:
    return PolicyBlock(
        code=code,
        message=message,
        severity=PolicySeverity.BLOCK,
        guardrail="human_approval",
    )
