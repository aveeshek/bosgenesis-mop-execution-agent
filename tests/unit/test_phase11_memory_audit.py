from __future__ import annotations

from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app
from bosgenesis_mop_execution_agent.memory import ExecutionMemoryStore
from bosgenesis_mop_execution_agent.models import MEMORY_AUTHORITY, MemoryLayer, MemoryQuery
from bosgenesis_mop_execution_agent.security import contains_sensitive_content


def test_postgres_migration_defines_phase11_memory_table() -> None:
    migration = __import__("pathlib").Path(
        "migrations/postgres/0002_phase11_memory.sql"
    ).read_text(encoding="utf-8")

    for token in [
        "mop_execution_memory_records",
        "context_only_not_decision_authority",
        "namespace",
        "chart",
        "kind",
        "error_code",
        "mcp_source",
        "tenant",
        "environment",
        "idx_mop_execution_memory_filters",
    ]:
        assert token in migration


def test_memory_store_supports_phase11_layers_filters_and_authority() -> None:
    store = ExecutionMemoryStore()

    for layer in MemoryLayer:
        store.write(
            layer=layer,
            job_id="job-1",
            summary=f"{layer.value} memory",
            payload={"kind": "ConfigMap"},
            namespace="signoz",
            chart="signoz",
            kind="ConfigMap",
            error_code="DRY_RUN_FAILED",
            mcp_source="bosgenesis_k8s",
            tenant="tenant-a",
            environment="lab",
        )

    records = store.query(
        MemoryQuery(
            job_id="job-1",
            namespace="signoz",
            chart="signoz",
            kind="ConfigMap",
            error_code="DRY_RUN_FAILED",
            mcp_source="bosgenesis_k8s",
            tenant="tenant-a",
            environment="lab",
        )
    )

    assert {record.layer for record in records} == set(MemoryLayer)
    assert {record.authority for record in records} == {MEMORY_AUTHORITY}
    assert all(record.redaction_applied for record in records)


def test_memory_writes_are_redacted() -> None:
    store = ExecutionMemoryStore()

    record = store.write(
        layer=MemoryLayer.SEMANTIC_FAILURE,
        job_id="job-1",
        summary="dry-run failed with password=fake-password-value",
        payload={
            "databasePassword": "fake-password-value",
            "log": "Authorization: Bearer fakebearertoken1234567890",
        },
        namespace="signoz",
        error_code="DRY_RUN_FAILED",
    )

    assert not contains_sensitive_content(record.model_dump(mode="json"))
    assert "fake-password-value" not in str(record.model_dump(mode="json"))
    assert "fakebearertoken" not in str(record.model_dump(mode="json"))


def test_memory_context_is_context_only_and_cannot_transition_job_state() -> None:
    client = TestClient(create_app())
    created = client.post(
        "/v1/execution-jobs",
        json={
            "bundle_id": "bundle-1",
            "target_namespace": "signoz",
            "tenant": "tenant-a",
            "environment": "lab",
        },
    ).json()
    job_id = created["job_id"]
    before = client.get(f"/v1/execution-jobs/{job_id}").json()["data"]["job"]["state"]

    memory = client.get(
        f"/v1/execution-jobs/{job_id}/memory-context",
        params={"namespace": "signoz", "tenant": "tenant-a", "environment": "lab"},
    ).json()
    after = client.get(f"/v1/execution-jobs/{job_id}").json()["data"]["job"]["state"]
    context = memory["data"]["memory_context"]

    assert before == "created"
    assert after == "created"
    assert context["authority"] == MEMORY_AUTHORITY
    assert context["filters"] == {
        "job_id": job_id,
        "namespace": "signoz",
        "tenant": "tenant-a",
        "environment": "lab",
    }
    assert context["records"]
    assert {record["authority"] for record in context["records"]} == {MEMORY_AUTHORITY}


def test_memory_context_filters_by_error_code_and_policy_source() -> None:
    client = TestClient(create_app())
    job_id = client.post(
        "/v1/execution-jobs",
        json={"bundle_id": "bundle-1", "target_namespace": "signoz"},
    ).json()["job_id"]
    client.post(
        "/v1/policy/evaluate",
        json={
            "job_id": job_id,
            "target_namespace": "signoz",
            "mutating": True,
            "command": "kubectl apply -f generated -n signoz",
            "dry_run_satisfied": False,
            "audit_written": False,
        },
    )

    memory = client.get(
        f"/v1/execution-jobs/{job_id}/memory-context",
        params={"namespace": "signoz", "error_code": "DRY_RUN_REQUIRED"},
    ).json()["data"]["memory_context"]

    assert len(memory["records"]) == 1
    assert memory["records"][0]["layer"] == "policy"
    assert memory["records"][0]["error_code"] == "DRY_RUN_REQUIRED"


def test_representative_job_audit_is_complete() -> None:
    client = TestClient(create_app())
    job_id = client.post(
        "/v1/execution-jobs",
        json={"bundle_id": "bundle-1", "target_namespace": "signoz"},
    ).json()["job_id"]

    client.post(f"/v1/execution-jobs/{job_id}/start")
    client.post(
        f"/v1/execution-jobs/{job_id}/instructions",
        json={"instruction_type": "continue", "rationale": "external controller said continue"},
    )
    client.post(
        f"/v1/execution-jobs/{job_id}/instructions",
        json={"instruction_type": "invent_repair"},
    )
    client.post(
        f"/v1/execution-jobs/{job_id}/instructions",
        json={"instruction_type": "patch_manifest", "manifest_patch": {"data": "unsafe"}},
    )
    client.post(
        f"/v1/execution-jobs/{job_id}/approvals",
        json={
            "approval_scope": "mutation",
            "ticket_reference": "CHG-1",
            "statement": "Approved for signoz only.",
        },
    )
    client.post(f"/v1/execution-jobs/{job_id}/reports/release-notes")

    actions = [
        event["action"]
        for event in client.get(f"/v1/execution-jobs/{job_id}/audit-events").json()["data"][
            "audit_events"
        ]
    ]

    assert {
        "job_created",
        "job_state_transition",
        "instruction_received",
        "instruction_accepted",
        "instruction_rejected",
        "instruction_policy_blocked",
        "approval_submitted",
        "release_notes_requested",
    }.issubset(actions)
