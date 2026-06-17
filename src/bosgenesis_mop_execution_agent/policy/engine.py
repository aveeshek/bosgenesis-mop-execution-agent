"""Deterministic safety policy engine."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import Field

from bosgenesis_mop_execution_agent.common.time import utc_now
from bosgenesis_mop_execution_agent.models import HumanApproval, PolicyBlock, ResourceRef
from bosgenesis_mop_execution_agent.models.base import StrictBaseModel
from bosgenesis_mop_execution_agent.persistence.idempotency import IdempotencyRecord
from bosgenesis_mop_execution_agent.policy.approval_gate import approval_blocks
from bosgenesis_mop_execution_agent.policy.audit_gate import audit_blocks
from bosgenesis_mop_execution_agent.policy.command_fingerprint import command_fingerprint
from bosgenesis_mop_execution_agent.policy.dry_run_gate import dry_run_blocks
from bosgenesis_mop_execution_agent.policy.idempotency import idempotency_blocks
from bosgenesis_mop_execution_agent.policy.limits import limit_blocks
from bosgenesis_mop_execution_agent.policy.namespace_scope import namespace_scope_blocks
from bosgenesis_mop_execution_agent.policy.production_data_guard import production_data_blocks
from bosgenesis_mop_execution_agent.policy.secret_guard import secret_blocks


class PolicyLimits(StrictBaseModel):
    """Configurable policy limits."""

    max_step_timeout_seconds: int = 1800
    max_retry_attempts: int = 3


class PolicyEvaluationContext(StrictBaseModel):
    """Inputs required to evaluate a bounded action."""

    job_id: str
    target_namespace: str
    mutating: bool = False
    phase_id: str | None = None
    step_id: str | None = None
    command: str | None = None
    command_metadata: dict[str, Any] = Field(default_factory=dict)
    resource_refs: list[ResourceRef] = Field(default_factory=list)
    manifests: list[dict[str, Any]] = Field(default_factory=list)
    values_files: list[dict[str, Any]] = Field(default_factory=list)
    instructions: list[dict[str, Any] | str] = Field(default_factory=list)
    logs: list[str] = Field(default_factory=list)
    outputs: list[str | dict[str, Any]] = Field(default_factory=list)
    approvals: list[HumanApproval] = Field(default_factory=list)
    dry_run_satisfied: bool = False
    idempotency_record: IdempotencyRecord | None = None
    request_payload: dict[str, Any] | None = None
    timeout_seconds: int | None = None
    retry_attempts: int = 0
    audit_written: bool = False
    now: datetime = Field(default_factory=utc_now)


class PolicyDecision(StrictBaseModel):
    """Policy evaluation result."""

    allowed: bool
    blocks: list[PolicyBlock] = Field(default_factory=list)
    command_fingerprint: str | None = None


def evaluate_policy(
    context: PolicyEvaluationContext,
    limits: PolicyLimits | None = None,
) -> PolicyDecision:
    """Evaluate all Phase 4 safety guards and return deterministic blocks."""
    effective_limits = limits or PolicyLimits()
    fingerprint = (
        command_fingerprint(context.command, context.command_metadata)
        if context.command is not None
        else None
    )
    payloads: list[Any] = [
        *context.manifests,
        *context.values_files,
        *context.instructions,
        *context.logs,
        *context.outputs,
    ]
    blocks: list[PolicyBlock] = []
    blocks.extend(
        namespace_scope_blocks(
            target_namespace=context.target_namespace,
            resource_refs=context.resource_refs,
            manifests=context.manifests,
        )
    )
    blocks.extend(secret_blocks(payloads))
    blocks.extend(production_data_blocks(context.command, context.manifests))
    blocks.extend(
        dry_run_blocks(mutating=context.mutating, dry_run_satisfied=context.dry_run_satisfied)
    )
    blocks.extend(
        approval_blocks(
            mutating=context.mutating,
            approvals=context.approvals,
            job_id=context.job_id,
            target_namespace=context.target_namespace,
            phase_id=context.phase_id,
            step_id=context.step_id,
            resource_refs=context.resource_refs,
            fingerprint=fingerprint,
            now=context.now,
        )
    )
    blocks.extend(
        idempotency_blocks(
            mutating=context.mutating,
            idempotency_record=context.idempotency_record,
            request_payload=context.request_payload,
        )
    )
    blocks.extend(
        limit_blocks(
            timeout_seconds=context.timeout_seconds,
            retry_attempts=context.retry_attempts,
            max_timeout_seconds=effective_limits.max_step_timeout_seconds,
            max_retry_attempts=effective_limits.max_retry_attempts,
        )
    )
    blocks.extend(audit_blocks(mutating=context.mutating, audit_written=context.audit_written))
    return PolicyDecision(allowed=not blocks, blocks=blocks, command_fingerprint=fingerprint)
