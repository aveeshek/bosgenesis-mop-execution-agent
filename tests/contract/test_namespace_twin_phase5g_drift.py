from __future__ import annotations

import shutil
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app
from bosgenesis_mop_execution_agent.namespace_twin.delta import LiveSnapshot
from bosgenesis_mop_execution_agent.namespace_twin.persistence import NamespaceTwinRepository
from bosgenesis_mop_execution_agent.namespace_twin.service import NamespaceTwinService

FIXTURE = Path("tests/fixtures/sample_mop_bundle").resolve()


def _deployment(image: str = "nginx:1.0", *, namespace: str = "sample-target") -> dict:
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": "sample-app",
            "namespace": namespace,
            "labels": {"app.kubernetes.io/instance": "sample"},
        },
        "spec": {
            "replicas": 1,
            "selector": {"matchLabels": {"app": "sample"}},
            "template": {
                "metadata": {"labels": {"app": "sample"}},
                "spec": {"containers": [{"name": "app", "image": image}]},
            },
        },
        "status": {"readyReplicas": 1},
    }


class SequenceCollector:
    def __init__(self, snapshots: list[list[dict]]) -> None:
        self.snapshots = snapshots
        self.index = 0

    def collect(self, namespace: str, *, correlation_id: str) -> LiveSnapshot:
        del namespace, correlation_id
        resources = self.snapshots[min(self.index, len(self.snapshots) - 1)]
        self.index += 1
        return LiveSnapshot(
            resources=deepcopy(resources),
            available=True,
            complete_kinds={"Deployment"},
            evidence_refs=[f"snapshot:{self.index}"],
        )


def _service(tmp_path: Path, collector: SequenceCollector) -> NamespaceTwinService:
    return NamespaceTwinService(
        NamespaceTwinRepository(f"sqlite+pysqlite:///{(tmp_path / 'phase5g.db').as_posix()}"),
        live_collector=collector,
    )


def _payload(tmp_path: Path, key: str) -> dict:
    bundle = tmp_path / "bundle"
    if not bundle.exists():
        shutil.copytree(FIXTURE, bundle)
    return {
        "source": {"type": "local_path", "value": str(bundle)},
        "target_namespace": "sample-target",
        "target_cluster": "contract-cluster",
        "idempotency_key": key,
    }


def test_no_drift_preserves_lifecycle_and_execution_eligibility(tmp_path) -> None:
    collector = SequenceCollector([[_deployment()], [_deployment()]])
    service = _service(tmp_path, collector)
    created = service.create(_payload(tmp_path, "phase5g-none"), actor_id="operator")

    refreshed = service.refresh_drift(created["twin_id"], actor_id="operator")
    twin = service.get(created["twin_id"])

    assert refreshed["data"]["status"] == "none"
    assert refreshed["data"]["changes"] == []
    assert refreshed["data"]["execution_disabled"] is False
    assert twin["lifecycle_status"] == "awaiting_dry_run"
    assert twin["foundation_facts"]["drift_twin"]["rules_version"] == ("namespace-twin-drift-1.0.0")


def test_workload_spec_drift_supersedes_prior_execution_eligibility(tmp_path) -> None:
    collector = SequenceCollector([[_deployment()], [_deployment("nginx:2.0")]])
    service = _service(tmp_path, collector)
    created = service.create(_payload(tmp_path, "phase5g-major"), actor_id="operator")

    refreshed = service.refresh_drift(created["twin_id"], actor_id="operator")
    twin = service.get(created["twin_id"])

    assert refreshed["data"]["status"] == "major"
    assert refreshed["data"]["material"] is True
    assert refreshed["data"]["decision_invalidated"] is True
    assert refreshed["data"]["changes"][0]["evidence_refs"][0]["redacted"] is True
    assert twin["lifecycle_status"] == "superseded"
    assert twin["autonomy_eligibility"] == "ineligible"
    assert twin["freshness"]["status"] == "superseded"


def test_material_drift_preserves_and_supersedes_a_final_decision(tmp_path) -> None:
    collector = SequenceCollector([[_deployment()], [_deployment("nginx:2.0")]])
    service = _service(tmp_path, collector)
    created = service.create(_payload(tmp_path, "phase5g-final"), actor_id="operator")
    current = service.get(created["twin_id"])
    service.repository.transition(
        created["twin_id"],
        "decision_calculating",
        message="Authoritative dry-run evidence attached.",
    )
    service.repository.persist_terminal_decision(
        created["twin_id"],
        decision="green",
        report_hash="a" * 64,
        facts={**current["foundation_facts"], "dry_run_job_id": "dry-run-1"},
    )

    refreshed = service.refresh_drift(created["twin_id"], actor_id="operator")
    twin = service.get(created["twin_id"])

    assert refreshed["data"]["status"] == "major"
    assert twin["lifecycle_status"] == "superseded"
    assert twin["decision"] == "green"
    assert twin["decision_is_final"] is True
    assert twin["prior_decision"]["decision"] == "green"
    assert twin["prior_decision"]["report_hash"] == "a" * 64
    actions = {item["code"]: item for item in twin["actions"]}
    assert actions["start_execution"]["enabled"] is False

def test_policy_boundary_drift_is_critical(tmp_path) -> None:
    collector = SequenceCollector([[_deployment()], [_deployment(namespace="outside-boundary")]])
    service = _service(tmp_path, collector)
    created = service.create(_payload(tmp_path, "phase5g-critical"), actor_id="operator")

    data = service.refresh_drift(created["twin_id"], actor_id="operator")["data"]

    assert data["status"] == "critical"
    assert any(change["axes"]["target"] for change in data["changes"])
    assert data["execution_disabled"] is True


def test_drift_read_and_authorized_refresh_are_exposed_by_rest(tmp_path) -> None:
    collector = SequenceCollector([[_deployment()], [_deployment()]])
    service = _service(tmp_path, collector)
    app = create_app()
    app.state.namespace_twin_service = service
    with TestClient(app) as client:
        created = client.post(
            "/v1/namespace-twins",
            json=_payload(tmp_path, "phase5g-rest"),
        ).json()["data"]
        initial = client.get(f"/v1/namespace-twins/{created['twin_id']}/drift")
        refreshed = client.post(f"/v1/namespace-twins/{created['twin_id']}/drift/refresh")

    assert initial.status_code == 200
    assert initial.json()["data"]["data"]["status"] == "none"
    assert refreshed.status_code == 200
    assert refreshed.json()["data"]["data"]["rules_version"] == ("namespace-twin-drift-1.0.0")
    assert refreshed.json()["data"]["data"]["model_authority"] is False
