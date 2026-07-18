from __future__ import annotations

import json
import shutil
from pathlib import Path

from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app
from bosgenesis_mop_execution_agent.namespace_twin.persistence import NamespaceTwinRepository
from bosgenesis_mop_execution_agent.namespace_twin.rollback_twin import enrich_rollback_proof
from bosgenesis_mop_execution_agent.namespace_twin.service import NamespaceTwinService

FIXTURE = Path("tests/fixtures/sample_mop_bundle").resolve()


def _bundle(tmp_path: Path, *, rollback: bool = True) -> Path:
    root = tmp_path / "bundle"
    shutil.copytree(FIXTURE, root)
    if not rollback:
        return root
    previous = root / "previous" / "configmap-sample-app.yaml"
    previous.parent.mkdir()
    shutil.copy2(root / "generated" / "configmap-sample-app.yaml", previous)
    with (root / "machine_execution_plan.yaml").open("a", encoding="utf-8") as handle:
        handle.write(
            """
  - phase_id: rollback_configmaps
    title: Rollback sample ConfigMaps
    objective: Restore the previous sample ConfigMap.
    depends_on: [apply_configmaps]
    steps:
      - step_id: rollback-sample-configmap
        title: Restore previous sample ConfigMap
        type: rollback
        depends_on: [apply-sample-configmap]
        metadata:
          rollback_for: apply-sample-configmap
        manifest_refs:
          - previous/configmap-sample-app.yaml
        commands:
          - kind: dry_run
            command: k8s.server_side_dry_run_apply previous/configmap-sample-app.yaml
            dry_run: true
            mutating: false
          - kind: apply
            command: k8s.apply previous/configmap-sample-app.yaml
            dry_run: false
            mutating: true
        expected_outcomes:
          - ConfigMap sample-app-config returns to its previous state.
"""
        )
    index_path = root / "artifact-index.json"
    index = json.loads(index_path.read_text(encoding="utf-8"))
    index["files"].append(
        {"path": "previous/configmap-sample-app.yaml", "role": "previous_manifest"}
    )
    index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")
    return root


def _payload(bundle: Path, key: str) -> dict:
    return {
        "source": {"type": "local_path", "value": str(bundle)},
        "target_namespace": "sample-target",
        "target_cluster": "contract-cluster",
        "idempotency_key": key,
    }


def test_defined_rollback_is_medium_until_proven(tmp_path) -> None:
    service = NamespaceTwinService(
        NamespaceTwinRepository(f"sqlite+pysqlite:///{(tmp_path / 'phase5f.db').as_posix()}")
    )
    created = service.create(_payload(_bundle(tmp_path), "phase5f-defined"), actor_id="operator")
    rollback = service.rollback(created["twin_id"])
    data = rollback["data"]

    assert rollback["availability"]["state"] == "available"
    assert data["rollback_defined"] is True
    assert data["rollback_proven"] is False
    assert data["confidence"] == "medium"
    assert data["coverage"]["coverage_percent"] == 100
    assert data["previous_artifacts"]["manifests_available"] is True
    assert data["machine_plan_steps"][0]["forward_step_ids"] == ["apply-sample-configmap"]


def test_missing_rollback_is_unavailable(tmp_path) -> None:
    service = NamespaceTwinService(
        NamespaceTwinRepository(
            f"sqlite+pysqlite:///{(tmp_path / 'phase5f-missing.db').as_posix()}"
        )
    )
    created = service.create(
        _payload(_bundle(tmp_path, rollback=False), "phase5f-missing"),
        actor_id="operator",
    )
    data = service.rollback(created["twin_id"])["data"]

    assert data["confidence"] == "unavailable"
    assert data["rollback_defined"] is False
    assert "ROLLBACK_STEP_MISSING" in {item["code"] for item in data["gaps"]}


def test_rollback_specific_dry_run_evidence_can_prove_defined_rollback(tmp_path) -> None:
    service = NamespaceTwinService(
        NamespaceTwinRepository(f"sqlite+pysqlite:///{(tmp_path / 'phase5f-proof.db').as_posix()}")
    )
    created = service.create(
        _payload(_bundle(tmp_path), "phase5f-proof"),
        actor_id="operator",
    )
    initial = service.rollback(created["twin_id"])["data"]

    proven = enrich_rollback_proof(
        initial,
        {
            "dry_run_job_id": "job-phase5f-proof",
            "observations": [
                {
                    "step": "rollback-sample-configmap",
                    "outcome": "accepted",
                    "evidence_refs": ["obs-phase5f-rollback"],
                }
            ],
        },
    )

    assert initial["rollback_defined"] is True
    assert initial["rollback_proven"] is False
    assert proven["rollback_proven"] is True
    assert proven["proof"]["status"] == "passed"
    assert proven["confidence"] == "high"
    assert proven["model_authority"] is False
    assert "ROLLBACK_NOT_PROVEN" not in {item["code"] for item in proven["gaps"]}


def test_rollback_projection_is_exposed_by_rest(tmp_path) -> None:
    service = NamespaceTwinService(
        NamespaceTwinRepository(f"sqlite+pysqlite:///{(tmp_path / 'phase5f-rest.db').as_posix()}")
    )
    app = create_app()
    app.state.namespace_twin_service = service
    with TestClient(app) as client:
        created = client.post(
            "/v1/namespace-twins",
            json=_payload(_bundle(tmp_path), "phase5f-rest"),
        ).json()["data"]
        response = client.get(f"/v1/namespace-twins/{created['twin_id']}/rollback")

    assert response.status_code == 200
    assert response.json()["data"]["data"]["rule_version"] == ("namespace-twin-rollback-1.0.0")
    assert response.json()["data"]["data"]["model_authority"] is False
