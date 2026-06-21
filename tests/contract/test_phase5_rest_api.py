from __future__ import annotations

from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app


def test_health_ready_capabilities_and_redacted_config() -> None:
    client = TestClient(create_app())

    assert client.get("/healthz").status_code == 200
    ready = client.get("/readyz").json()
    capabilities = client.get("/v1/capabilities").json()
    config = client.get("/v1/config/effective").json()

    assert ready["ok"] is True
    assert ready["data"]["status"] == "ready"
    assert capabilities["ok"] is True
    assert "mop_execution_health" in capabilities["data"]["tools"]
    assert config["ok"] is True
    assert config["data"]["memory"]["enabled"] is True
    assert config["data"]["memory"]["postgres_enabled"] is True
    assert config["data"]["memory"]["schema"] == "mop_execution"
    assert config["data"]["memory"]["authority"] == "context_only_not_decision_authority"
    assert config["data"]["postgres"]["enabled"] is True
    assert config["data"]["secrets"]["database_url"] == "[REDACTED]"
    assert config["data"]["secrets"]["postgres_dsn"] == "[REDACTED]"
    assert "postgres://" not in str(config)


def test_artifact_bundle_job_control_and_retrieval_endpoints() -> None:
    client = TestClient(create_app())

    bundle = client.post(
        "/v1/artifact-bundles",
        json={
            "source": {"type": "local_path", "value": "tests/fixtures/sample_mop_bundle"},
            "target_namespace": "sample-target",
        },
    ).json()
    bundle_id = bundle["bundle_id"]
    validation = client.post(f"/v1/artifact-bundles/{bundle_id}/validate", json={}).json()

    created = client.post(
        "/v1/execution-jobs",
        json={
            "bundle_id": bundle_id,
            "target_namespace": "sample-target",
            "job_name": "phase5-smoke",
            "plan": {"phase_ids": ["apply_configmaps"]},
        },
    ).json()
    job_id = created["job_id"]
    started = client.post(f"/v1/execution-jobs/{job_id}/start").json()

    assert bundle["ok"] is True
    assert validation["data"]["valid"] is True
    assert client.get("/v1/artifact-bundles").json()["data"]["bundles"][0]["bundle_id"] == bundle_id
    assert created["state"] == "created"
    assert started["state"] == "validating_bundle"
    assert client.get(f"/v1/artifact-bundles/{bundle_id}").json()["bundle_id"] == bundle_id
    assert client.get("/v1/execution-jobs").json()["data"]["jobs"][0]["job_id"] == job_id
    job_state = client.get(f"/v1/execution-jobs/{job_id}").json()["data"]["job"]["state"]
    assert job_state == "validating_bundle"
    plan = client.get(f"/v1/execution-jobs/{job_id}/plan").json()["data"]["plan"]
    assert plan["phases"][0]["phase_id"] == "apply_configmaps"
    assert client.get(f"/v1/execution-jobs/{job_id}/observations").json()["ok"] is True
    assert client.get(f"/v1/execution-jobs/{job_id}/events").json()["ok"] is True
    assert client.get(f"/v1/execution-jobs/{job_id}/audit-events").json()["ok"] is True
    assert client.get(f"/v1/execution-jobs/{job_id}/memory-context").json()["ok"] is True
    stream = client.get(f"/v1/execution-jobs/{job_id}/stream")
    assert stream.status_code == 200
    assert "event: snapshot" in stream.text


def test_start_endpoint_queues_runtime_work_without_inline_execution(monkeypatch) -> None:
    monkeypatch.setenv("MOP_EXECUTION_API_BACKGROUND_WORKER_ENABLED", "false")
    client = TestClient(create_app())

    bundle_id = client.post(
        "/v1/artifact-bundles",
        json={
            "source": {"type": "local_path", "value": "tests/fixtures/sample_mop_bundle"},
            "target_namespace": "sample-target",
        },
    ).json()["bundle_id"]
    client.post(f"/v1/artifact-bundles/{bundle_id}/validate", json={})
    created = client.post(
        "/v1/execution-jobs",
        json={
            "bundle_id": bundle_id,
            "target_namespace": "sample-target",
            "execution_mode": "dry_run_only",
        },
    ).json()

    started = client.post(f"/v1/execution-jobs/{created['job_id']}/start").json()
    stored = client.get(f"/v1/execution-jobs/{created['job_id']}").json()

    assert started["message"] == "Job queued for asynchronous execution."
    assert started["data"]["runtime_action"] == "queued"
    assert stored["data"]["job"]["state"] == "created"


def test_policy_evaluate_and_redaction_preview_endpoints() -> None:
    client = TestClient(create_app())

    policy = client.post(
        "/v1/policy/evaluate",
        json={"job_id": "job-1", "target_namespace": "target-ns", "mutating": False},
    ).json()
    redaction = client.post(
        "/v1/redaction/preview",
        json={"content": "password=fake-password-value"},
    ).json()

    assert policy["ok"] is True
    assert policy["data"]["allowed"] is True
    assert redaction["ok"] is True
    assert redaction["data"]["redacted_content"] == "[REDACTED]"


def test_validating_bundle_job_can_be_paused_and_cancelled() -> None:
    client = TestClient(create_app())

    pause_job_id = client.post(
        "/v1/execution-jobs",
        json={"bundle_id": "bundle-1", "target_namespace": "target-ns"},
    ).json()["job_id"]
    client.post(f"/v1/execution-jobs/{pause_job_id}/start")
    paused = client.post(f"/v1/execution-jobs/{pause_job_id}/pause")

    cancel_job_id = client.post(
        "/v1/execution-jobs",
        json={"bundle_id": "bundle-1", "target_namespace": "target-ns"},
    ).json()["job_id"]
    client.post(f"/v1/execution-jobs/{cancel_job_id}/start")
    cancelled = client.post(f"/v1/execution-jobs/{cancel_job_id}/cancel")

    assert paused.status_code == 200
    assert paused.json()["state"] == "paused"
    assert cancelled.status_code == 200
    assert cancelled.json()["state"] == "cancelled"


def test_instruction_approval_reports_cancel_and_mcp_mirror() -> None:
    client = TestClient(create_app())
    job_id = client.post(
        "/v1/execution-jobs",
        json={"bundle_id": "bundle-1", "target_namespace": "target-ns"},
    ).json()["job_id"]

    instruction = client.post(
        f"/v1/execution-jobs/{job_id}/instructions",
        json={"instruction_type": "continue", "rationale": "explicit test instruction"},
    ).json()
    rejected_instruction = client.post(
        f"/v1/execution-jobs/{job_id}/instructions",
        json={"instruction_type": "invent_repair"},
    )
    blocked_instruction = client.post(
        f"/v1/execution-jobs/{job_id}/instructions",
        json={"instruction_type": "patch_manifest", "manifest_patch": {"data": "unsafe"}},
    )
    approval = client.post(
        f"/v1/execution-jobs/{job_id}/approvals",
        json={
            "approval_scope": "mutation",
            "ticket_reference": "CHG-1",
            "statement": "Approved for test namespace only.",
        },
    ).json()
    report = client.post(f"/v1/execution-jobs/{job_id}/reports/release-notes").json()
    reports = client.get(f"/v1/execution-jobs/{job_id}/reports").json()
    report_id = report["data"]["report"]["report_id"]
    report_metadata = client.get(f"/v1/execution-jobs/{job_id}/reports/{report_id}").json()
    cancelled = client.post(f"/v1/execution-jobs/{job_id}/cancel").json()
    audit_actions = [
        event["action"]
        for event in client.get(f"/v1/execution-jobs/{job_id}/audit-events").json()["data"][
            "audit_events"
        ]
    ]
    mcp_get = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "mop_execution_get_job", "arguments": {"job_id": job_id}},
        },
    ).json()["result"]["structuredContent"]

    assert instruction["ok"] is True
    assert rejected_instruction.status_code == 409
    assert rejected_instruction.json()["policy_blocks"][0]["code"] == "INSTRUCTION_SCHEMA_INVALID"
    assert blocked_instruction.status_code == 409
    assert blocked_instruction.json()["policy_blocks"][0]["code"] == "UNSAFE_INSTRUCTION_BLOCKED"
    assert "instruction_received" in audit_actions
    assert "instruction_accepted" in audit_actions
    assert "instruction_rejected" in audit_actions
    assert "instruction_policy_blocked" in audit_actions
    assert approval["data"]["job"]["approval_status"] == "active"
    assert report["data"]["report"]["report_type"] == "release_notes"
    assert reports["data"]["reports"][0]["report_id"] == report_id
    assert report_metadata["data"]["report"]["report_id"] == report_id
    assert cancelled["state"] == "cancelled"
    assert mcp_get["data"]["job"]["state"] == "cancelled"


def test_rollback_and_invalid_transition_return_standard_error_envelope() -> None:
    client = TestClient(create_app())
    job_id = client.post(
        "/v1/execution-jobs",
        json={"bundle_id": "bundle-1", "target_namespace": "target-ns"},
    ).json()["job_id"]

    rollback = client.post(
        f"/v1/execution-jobs/{job_id}/rollback",
        json={"requested_by": "pytest", "reason": "contract test"},
    )
    pause = client.post(f"/v1/execution-jobs/{job_id}/pause")

    assert rollback.status_code == 409
    assert rollback.json()["ok"] is False
    assert rollback.json()["policy_blocks"][0]["code"] == "INVALID_STATE_TRANSITION"
    assert pause.status_code == 409
    assert pause.json()["ok"] is False
