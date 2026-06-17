from __future__ import annotations

from datetime import UTC, datetime, timedelta

from bosgenesis_mop_execution_agent.models import ApprovalScope, HumanApproval, ResourceRef
from bosgenesis_mop_execution_agent.persistence.idempotency import IdempotencyRecord, stable_hash
from bosgenesis_mop_execution_agent.policy import (
    PolicyEvaluationContext,
    PolicyLimits,
    command_fingerprint,
    evaluate_policy,
)

NOW = datetime(2026, 6, 16, tzinfo=UTC)


def test_namespace_scope_and_cluster_scope_are_blocked() -> None:
    decision = evaluate_policy(
        PolicyEvaluationContext(
            job_id="job-1",
            target_namespace="target-ns",
            resource_refs=[
                ResourceRef(kind="ConfigMap", namespace="other-ns", name="cfg"),
                ResourceRef(kind="ClusterRole", name="admin"),
            ],
            manifests=[
                {
                    "apiVersion": "v1",
                    "kind": "Namespace",
                    "metadata": {"name": "blocked"},
                }
            ],
        )
    )

    assert not decision.allowed
    assert _codes(decision) >= {
        "RESOURCE_NAMESPACE_OUT_OF_SCOPE",
        "CLUSTER_SCOPED_RESOURCE_BLOCKED",
    }


def test_mutating_action_requires_dry_run_approval_idempotency_and_audit() -> None:
    decision = evaluate_policy(
        PolicyEvaluationContext(
            job_id="job-1",
            target_namespace="target-ns",
            mutating=True,
            command="kubectl apply -f generated/configmap.yaml",
            now=NOW,
        )
    )

    assert not decision.allowed
    assert _codes(decision) >= {
        "DRY_RUN_REQUIRED",
        "APPROVAL_REQUIRED",
        "IDEMPOTENCY_REQUIRED",
        "AUDIT_REQUIRED_BEFORE_MUTATION",
    }


def test_matching_approval_scope_fingerprint_idempotency_and_audit_allows_mutation() -> None:
    command = "kubectl apply -f generated/configmap.yaml"
    request_payload = {"command": command, "step_id": "step-1"}
    fingerprint = command_fingerprint(command)
    approval = HumanApproval(
        approval_id="approval-1",
        job_id="job-1",
        approver_id="operator@example.com",
        approval_scope=ApprovalScope.MUTATION,
        ticket_reference="CHG-1",
        statement="Approved for target namespace only.",
        expires_at=NOW + timedelta(hours=1),
        approved_step_ids=["step-1"],
        command_fingerprint=fingerprint,
    )
    idempotency = IdempotencyRecord(
        idempotency_key="idem-1",
        scope="mutation",
        request_hash=stable_hash(request_payload),
    )

    decision = evaluate_policy(
        PolicyEvaluationContext(
            job_id="job-1",
            target_namespace="target-ns",
            mutating=True,
            step_id="step-1",
            command=command,
            approvals=[approval],
            dry_run_satisfied=True,
            idempotency_record=idempotency,
            request_payload=request_payload,
            audit_written=True,
            now=NOW,
        )
    )

    assert decision.allowed
    assert decision.blocks == []
    assert decision.command_fingerprint == fingerprint


def test_approval_scope_mismatch_and_expiration_are_blocked() -> None:
    command = "kubectl apply -f generated/configmap.yaml"
    approval = HumanApproval(
        approval_id="approval-1",
        job_id="job-1",
        approver_id="operator@example.com",
        approval_scope=ApprovalScope.DRY_RUN,
        ticket_reference="CHG-1",
        statement="Dry-run only.",
        expires_at=NOW - timedelta(minutes=1),
        command_fingerprint=command_fingerprint(command),
    )

    decision = evaluate_policy(
        PolicyEvaluationContext(
            job_id="job-1",
            target_namespace="target-ns",
            mutating=True,
            command=command,
            approvals=[approval],
            dry_run_satisfied=True,
            idempotency_record=IdempotencyRecord(
                idempotency_key="idem-1",
                scope="mutation",
                request_hash=stable_hash({"command": command}),
            ),
            request_payload={"command": command},
            audit_written=True,
            now=NOW,
        )
    )

    assert not decision.allowed
    assert "APPROVAL_EXPIRED" in _codes(decision)


def test_secret_values_are_detected_across_payload_types() -> None:
    secret_manifest = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": "unsafe", "namespace": "target-ns"},
        "stringData": {"password": "fake-password-value"},
    }
    values = {"image": {"pullSecret": "fake-token-value"}}
    instruction = {"env": {"API_KEY": "fake-api-key"}}
    log = "Bearer fakebearertoken1234567890"
    output = "cGFzc3dvcmQ9ZmFrZS1wYXNzd29yZC12YWx1ZQ=="

    decision = evaluate_policy(
        PolicyEvaluationContext(
            job_id="job-1",
            target_namespace="target-ns",
            manifests=[secret_manifest],
            values_files=[values],
            instructions=[instruction],
            logs=[log],
            outputs=[output],
        )
    )

    assert not decision.allowed
    assert "SECRET_VALUES_BLOCKED" in _codes(decision)


def test_production_data_and_pvc_copy_are_blocked() -> None:
    decision = evaluate_policy(
        PolicyEvaluationContext(
            job_id="job-1",
            target_namespace="target-ns",
            command="kubectl cp prod/app:/data ./backup",
            manifests=[
                {
                    "apiVersion": "v1",
                    "kind": "PersistentVolumeClaim",
                    "metadata": {"name": "data", "namespace": "target-ns"},
                }
            ],
        )
    )

    assert not decision.allowed
    assert _codes(decision) >= {"PRODUCTION_DATA_COPY_BLOCKED", "PVC_DATA_COPY_BLOCKED"}


def test_timeout_retry_and_idempotency_mismatch_are_blocked() -> None:
    decision = evaluate_policy(
        PolicyEvaluationContext(
            job_id="job-1",
            target_namespace="target-ns",
            mutating=True,
            command="kubectl apply -f generated/configmap.yaml",
            dry_run_satisfied=True,
            idempotency_record=IdempotencyRecord(
                idempotency_key="idem-1",
                scope="mutation",
                request_hash=stable_hash({"command": "different"}),
            ),
            request_payload={"command": "requested"},
            timeout_seconds=60,
            retry_attempts=4,
            audit_written=True,
            now=NOW,
        ),
        limits=PolicyLimits(max_step_timeout_seconds=30, max_retry_attempts=2),
    )

    assert not decision.allowed
    assert _codes(decision) >= {
        "IDEMPOTENCY_REQUEST_MISMATCH",
        "TIMEOUT_LIMIT_EXCEEDED",
        "RETRY_LIMIT_EXCEEDED",
    }


def _codes(decision: object) -> set[str]:
    return {block.code for block in decision.blocks}  # type: ignore[attr-defined]
