from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app
from bosgenesis_mop_execution_agent.namespace_twin.persistence import NamespaceTwinRepository
from bosgenesis_mop_execution_agent.namespace_twin.service import NamespaceTwinService

FIXTURE = Path("tests/fixtures/sample_mop_bundle").resolve()


def _payload(key: str, namespace: str) -> dict:
    return {
        "source": {"type": "local_path", "value": str(FIXTURE)},
        "target_namespace": namespace,
        "target_cluster": "contract-cluster",
        "idempotency_key": key,
    }


def _green(service: NamespaceTwinService, twin_id: str) -> dict:
    current = service.get(twin_id)
    service.repository.transition(
        twin_id,
        "decision_calculating",
        message="Authoritative dry-run evidence attached.",
    )
    service.repository.persist_terminal_decision(
        twin_id,
        decision="green",
        report_hash="a" * 64,
        facts={
            **current["foundation_facts"],
            "dry_run_job_id": "dry-run-1",
            "execution_status": "unlinked",
        },
    )
    return service.get(twin_id)


def test_phase5a_list_filters_cursor_metrics_and_restores_active_and_terminal(tmp_path) -> None:
    repository = NamespaceTwinRepository(
        f"sqlite+pysqlite:///{(tmp_path / 'phase5a.db').as_posix()}"
    )
    service = NamespaceTwinService(repository)
    active = service.create(_payload("active", "sample-target"), actor_id="operator-a")
    terminal = service.create(_payload("terminal", "sample-target"), actor_id="operator-b")
    green = _green(service, terminal["twin_id"])

    first_page = service.list({"limit": 1, "sort": "created_at", "direction": "asc"})
    second_page = service.list(
        {
            "limit": 1,
            "sort": "created_at",
            "direction": "asc",
            "cursor": first_page["page"]["next_cursor"],
        }
    )
    filtered = service.list({"decision": "green", "actor": "operator-b"})

    assert first_page["page"]["offset"] == 0
    assert first_page["items"][0]["twin_id"] == active["twin_id"]
    assert second_page["page"]["offset"] == 1
    assert second_page["items"][0]["twin_id"] == terminal["twin_id"]
    assert first_page["metrics"]["total"] == 2
    assert first_page["metrics"]["green"] == 1
    assert filtered["page"]["result_count"] == 1
    assert filtered["items"][0]["decision"] == "green"
    assert service.get(active["twin_id"])["visible_lifecycle"] == "awaiting_dry_run"
    assert service.get(terminal["twin_id"])["final_summary"]["decision"] == "green"
    assert green["decision_is_final"] is True

    assert service.get(terminal["twin_id"])["visible_lifecycle"] == "ready"
    assert service.get(terminal["twin_id"])["risk"]["level"] == "low"


def test_phase5a_overview_summaries_and_actions_are_authoritative(tmp_path) -> None:
    service = NamespaceTwinService(
        NamespaceTwinRepository(f"sqlite+pysqlite:///{(tmp_path / 'overview.db').as_posix()}")
    )
    active = service.create(_payload("overview", "sample-target"), actor_id="operator")
    active_actions = {row["code"]: row for row in service.actions(active["twin_id"])}

    assert active["preliminary_summary"]["deterministic"] is True
    assert active["final_summary"] is None
    assert active_actions["cancel_generation"]["enabled"] is True
    assert active["risk"]["level"] == "medium"
    assert active["tab_states"]["overview"]["state"] == "available"
    assert active_actions["start_execution"]["enabled"] is False

    terminal = _green(service, active["twin_id"])
    overview = service.overview(active["twin_id"])
    terminal_actions = {row["code"]: row for row in terminal["actions"]}

    assert overview["kind"] == "overview"
    assert overview["state"] == "available"
    assert overview["decision_version"] == terminal["decision_version"]
    assert overview["final_summary"]["decision"] == "green"
    assert "resources" not in overview and "artifacts" not in overview
    assert terminal_actions["start_execution"]["enabled"] is True
    assert terminal_actions["cancel_generation"]["enabled"] is False
    assert terminal_actions["request_approval"]["enabled"] is False
    assert overview["actions"] == terminal["actions"]


def test_phase5a_rest_overview_and_actions_contract(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'api-phase5a.db').as_posix()}"
    monkeypatch.setenv("NAMESPACE_TWIN_DATABASE_URL", database_url)
    monkeypatch.setenv("MOP_EXECUTION_STATE_FILE", str(tmp_path / "execution-state.json"))
    with TestClient(create_app()) as client:
        created = client.post(
            "/v1/namespace-twins", json=_payload("api-phase5a", "sample-target")
        ).json()["data"]
        overview = client.get(f"/v1/namespace-twins/{created['twin_id']}/overview")
        actions = client.get(f"/v1/namespace-twins/{created['twin_id']}/actions")

    assert overview.status_code == 200
    assert overview.json()["data"]["kind"] == "overview"
    assert overview.json()["data"]["fact_envelope"]["decision"] == "pending"
    assert actions.status_code == 200
    action_data = actions.json()["data"]
    assert action_data["schema_version"] == "1.0.0"
    assert action_data["decision_version"] == created["decision_version"]
    assert action_data["lifecycle_status"] == created["lifecycle_status"]
    assert action_data["freshness"]["status"] == "fresh"
    assert isinstance(action_data["actions"], list)
    assert {action["code"] for action in action_data["actions"]} >= {
        "open_twin",
        "cancel_generation",
        "start_execution",
        "regenerate_twin",
    }


def test_phase5a_accepts_generated_bundle_nested_artifact_index(tmp_path) -> None:
    bundle = tmp_path / "generated-bundle"
    shutil.copytree(FIXTURE, bundle)
    (bundle / "artifact-index.json").unlink()

    artifacts = bundle / "deployment-artifacts"
    manifest = artifacts / "kubernetes-manifests" / "configmap-sample-app.yaml"
    manifest.parent.mkdir(parents=True)
    shutil.copy2(
        bundle / "generated" / "configmap-sample-app.yaml",
        manifest,
    )
    (artifacts / "artifact-index.json").write_text(
        json.dumps(
            {
                "artifact_type": "bosgenesis_deployment_artifacts",
                "source_namespace": "sample-source",
                "target_namespace_placeholder": "sample-target",
                "values": [],
                "kubernetes_manifests": ["kubernetes-manifests/configmap-sample-app.yaml"],
                "raw_configmaps": [],
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    service = NamespaceTwinService(
        NamespaceTwinRepository(
            f"sqlite+pysqlite:///{(tmp_path / 'generated-layout.db').as_posix()}"
        )
    )
    payload = _payload("generated-layout", "sample-target")
    payload["source"] = {"type": "local_path", "value": str(bundle)}
    created = service.create(payload, actor_id="operator")

    provenance = created["foundation_facts"]["provenance"]
    assert provenance["artifact_index_present"] is True
    assert provenance["referenced_files_verified"] == 1
