from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app
from bosgenesis_mop_execution_agent.namespace_twin.delta import LiveSnapshot
from bosgenesis_mop_execution_agent.namespace_twin.persistence import NamespaceTwinRepository
from bosgenesis_mop_execution_agent.namespace_twin.service import (
    NamespaceTwinError,
    NamespaceTwinService,
)

FIXTURE = Path("tests/fixtures/sample_mop_bundle").resolve()


class EmptyCollector:
    def collect(self, namespace: str, *, correlation_id: str) -> LiveSnapshot:
        del namespace, correlation_id
        return LiveSnapshot(resources=[], available=True, complete_kinds={"ConfigMap"})


def _service(tmp_path: Path) -> NamespaceTwinService:
    return NamespaceTwinService(
        NamespaceTwinRepository(f"sqlite+pysqlite:///{(tmp_path / 'phase5k.db').as_posix()}"),
        live_collector=EmptyCollector(),
    )


def _create(service: NamespaceTwinService, key: str) -> dict:
    return service.create(
        {
            "source": {"type": "local_path", "value": str(FIXTURE)},
            "target_namespace": "sample-target",
            "target_cluster": "contract-cluster",
            "idempotency_key": key,
        },
        actor_id="operator",
    )


def _payload(**overrides: object) -> dict:
    now = datetime.now(UTC).isoformat()
    payload = {
        "replay_id": "replay_contract_001",
        "infrastructure_approved": True,
        "approval_id": "approval_replay_001",
        "mode": "mimic_namespace",
        "isolation_target": "esda-twin-sample-001",
        "synthetic_secret_strategy": "Synthetic placeholders with redacted references only.",
        "production_secret_values_copied": False,
        "production_data_copied": False,
        "retention_seconds": 0,
        "timeline": [
            {
                "sequence": 1,
                "phase": "prepare",
                "status": "passed",
                "summary": "Mimic namespace prepared.",
                "created_at": now,
            },
            {
                "sequence": 2,
                "phase": "apply",
                "status": "passed",
                "summary": "Rendered resources applied in isolation.",
                "created_at": now,
            },
            {
                "sequence": 3,
                "phase": "cleanup",
                "status": "passed",
                "summary": "Mimic namespace removed.",
                "created_at": now,
            },
        ],
        "checks": [
            {"type": "readiness", "status": "passed", "summary": "Workloads became ready."},
            {"type": "smoke_test", "status": "passed", "summary": "Bounded smoke tests passed."},
            {"type": "cleanup", "status": "passed", "summary": "Cleanup completed."},
        ],
        "cleanup_status": "completed",
        "evidence_refs": [
            {
                "evidence_id": "evidence_replay_report_001",
                "source_type": "report",
                "source_id": "replay-report.json",
                "summary": "Redacted isolated replay report.",
                "captured_at": now,
                "redacted": True,
                "href": None,
            }
        ],
        "limitations": ["External production dependencies were not contacted."],
    }
    payload.update(overrides)
    return payload


def test_replay_stays_not_run_without_explicit_approved_result(tmp_path) -> None:
    service = _service(tmp_path)
    created = _create(service, "phase5k-not-run")

    tab = service.mop_replay(created["twin_id"])
    twin = service.get(created["twin_id"])

    assert tab["availability"]["state"] == "not_run"
    assert tab["data"] is None
    assert twin["tab_states"]["mop-replay"]["state"] == "not_run"
    assert twin["optional_states"] == {}


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"infrastructure_approved": False}, "replay_infrastructure_not_approved"),
        ({"isolation_target": "sample-target"}, "unsafe_replay_namespace"),
        ({"production_secret_values_copied": True}, "production_secret_copy_forbidden"),
        ({"production_data_copied": True}, "production_data_copy_forbidden"),
        ({"cleanup_status": "running"}, "replay_cleanup_terminal_required"),
    ],
)
def test_replay_rejects_unsafe_or_non_terminal_evidence(
    tmp_path, overrides: dict, code: str
) -> None:
    service = _service(tmp_path)
    created = _create(service, "phase5k-reject-" + code)

    with pytest.raises(NamespaceTwinError) as exc_info:
        service.record_mop_replay(created["twin_id"], _payload(**overrides), actor_id="operator")

    assert exc_info.value.code == code
    assert service.mop_replay(created["twin_id"])["availability"]["state"] == "not_run"


def test_replay_records_redacted_additional_evidence_without_decision_authority(tmp_path) -> None:
    service = _service(tmp_path)
    created = _create(service, "phase5k-record")
    before = service.get(created["twin_id"])

    result = service.record_mop_replay(created["twin_id"], _payload(), actor_id="replay-operator")
    data = result["data"]
    after = service.get(created["twin_id"])
    events = service.events(created["twin_id"], limit=100, offset=0)["events"]

    assert result["availability"]["state"] == "available"
    assert data["status"] == "passed"
    assert data["cleanup_status"] == "completed"
    assert data["additional_evidence_only"] is True
    assert data["production_secret_values_copied"] is False
    assert data["production_data_copied"] is False
    assert data["model_authority"] is False
    assert data["execution_eligibility_effect"] == "none"
    assert len(data["replay_hash"]) == 64
    assert any("does not prove production success" in item for item in data["limitations"])
    assert after["decision"] == before["decision"]
    assert after["decision_version"] == before["decision_version"]
    assert events[-1]["event_type"] == "mop_replay_recorded"
    assert events[-1]["payload"]["actor_id"] == "replay-operator"


def test_replay_is_exposed_by_authenticated_rest(tmp_path) -> None:
    service = _service(tmp_path)
    app = create_app()
    app.state.namespace_twin_service = service
    with TestClient(app) as client:
        created = client.post(
            "/v1/namespace-twins",
            json={
                "source": {"type": "local_path", "value": str(FIXTURE)},
                "target_namespace": "sample-target",
                "target_cluster": "contract-cluster",
                "idempotency_key": "phase5k-rest",
            },
        ).json()["data"]
        initial = client.get(f"/v1/namespace-twins/{created['twin_id']}/mop-replay")
        recorded = client.post(
            f"/v1/namespace-twins/{created['twin_id']}/mop-replay",
            json=_payload(),
            headers={"x-esda-actor": "admin"},
        )

    assert initial.status_code == 200
    assert initial.json()["data"]["availability"]["state"] == "not_run"
    assert recorded.status_code == 200
    assert recorded.json()["data"]["data"]["rules_version"] == ("namespace-twin-mop-replay-1.0.0")
