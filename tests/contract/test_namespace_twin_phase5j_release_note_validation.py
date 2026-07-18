from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app
from bosgenesis_mop_execution_agent.namespace_twin.delta import LiveSnapshot
from bosgenesis_mop_execution_agent.namespace_twin.persistence import NamespaceTwinRepository
from bosgenesis_mop_execution_agent.namespace_twin.service import NamespaceTwinService

FIXTURE = Path("tests/fixtures/sample_mop_bundle").resolve()
HASH = "a" * 64
PROMPT_HASH = "b" * 64
INPUT_HASH = "c" * 64


class EmptyCollector:
    def collect(self, namespace: str, *, correlation_id: str) -> LiveSnapshot:
        del namespace, correlation_id
        return LiveSnapshot(resources=[], available=True, complete_kinds={"ConfigMap"})


def _service(tmp_path: Path) -> NamespaceTwinService:
    return NamespaceTwinService(
        NamespaceTwinRepository(f"sqlite+pysqlite:///{(tmp_path / 'phase5j.db').as_posix()}"),
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


def _payload(claims: list[dict]) -> dict:
    return {
        "release_note_artifact_id": "art_release_notes_001",
        "release_note_artifact_hash": HASH,
        "claims": claims,
        "extraction": {
            "method": "bounded_model_with_deterministic_fallback",
            "model_profile": "azure_gpt5_pro",
            "prompt_version": "namespace_twin_release_note_claim_extraction_v1",
            "prompt_hash": PROMPT_HASH,
            "input_hash": INPUT_HASH,
            "fallback_used": False,
            "safe_summary": "Extracted bounded claims; deterministic evidence retains authority.",
        },
    }


def test_tab_stays_not_run_until_artifact_is_linked(tmp_path) -> None:
    service = _service(tmp_path)
    created = _create(service, "phase5j-not-run")

    tab = service.release_note_validation(created["twin_id"])
    twin = service.get(created["twin_id"])

    assert tab["availability"]["state"] == "not_run"
    assert tab["data"] is None
    assert twin["tab_states"]["release-note-validation"]["state"] == "not_run"
    assert "release-note-validation" not in twin["optional_states"]


def test_claims_are_deterministically_supported_contradicted_and_unsupported(tmp_path) -> None:
    service = _service(tmp_path)
    created = _create(service, "phase5j-claims")
    before = service.get(created["twin_id"])

    result = service.validate_release_note(
        created["twin_id"],
        _payload(
            [
                {"category": "configuration", "claim": "Configuration was updated."},
                {"category": "configuration", "claim": "No configuration changed."},
                {"category": "route", "claim": "A public route was added."},
            ]
        ),
        actor_id="operator",
    )
    data = result["data"]
    after = service.get(created["twin_id"])

    assert result["availability"]["state"] == "available"
    assert data["status"] == "failed"
    assert data["claim_counts"]["supported"] >= 1
    assert data["claim_counts"]["contradicted"] >= 1
    assert data["claim_counts"]["unsupported"] >= 1
    assert data["automatic_overwrite_allowed"] is False
    assert data["execution_eligibility_effect"] == "none"
    assert data["editorial_only"] is True
    assert data["extraction"]["prompt_hash"] == PROMPT_HASH
    assert data["extraction"]["input_hash"] == INPUT_HASH
    assert after["decision"] == before["decision"]
    assert after["decision_version"] == before["decision_version"]
    events = service.events(created["twin_id"], limit=100, offset=0)["events"]
    assert events[-1]["event_type"] == "release_note_validation_completed"


def test_missing_operational_claims_are_explicit_and_editorial_only(tmp_path) -> None:
    service = _service(tmp_path)
    created = _create(service, "phase5j-missing")

    data = service.validate_release_note(
        created["twin_id"],
        _payload([]),
        actor_id="operator",
    )["data"]

    assert data["status"] == "warning"
    assert data["claim_counts"]["missing"] >= 1
    assert any(item["status"] == "missing" for item in data["claims"])
    assert data["missing_operational_notes"]
    assert data["suggested_corrections"]


def test_release_note_validation_is_exposed_by_rest(tmp_path) -> None:
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
                "idempotency_key": "phase5j-rest",
            },
        ).json()["data"]
        initial = client.get(
            f"/v1/namespace-twins/{created['twin_id']}/release-note-validation"
        )
        validated = client.post(
            f"/v1/namespace-twins/{created['twin_id']}/release-note-validation",
            json=_payload([{"category": "configuration", "claim": "Configuration changed."}]),
            headers={"x-esda-actor": "admin"},
        )
        events = client.get(
            f"/v1/namespace-twins/{created['twin_id']}/events", params={"limit": 100}
        ).json()["data"]["events"]

    assert initial.status_code == 200
    assert initial.json()["data"]["availability"]["state"] == "not_run"
    assert validated.status_code == 200
    assert validated.json()["data"]["data"]["rules_version"] == (
        "namespace-twin-release-note-validation-1.0.0"
    )
    assert validated.json()["data"]["data"]["extraction"]["model_authority"] is False
    assert events[-1]["payload"]["actor_id"] == "admin"

def test_validation_hash_excludes_volatile_timestamps(tmp_path) -> None:
    service = _service(tmp_path)
    created = _create(service, "phase5j-repeatable-hash")
    payload = _payload(
        [{"category": "configuration", "claim": "Configuration was updated."}]
    )

    first = service.validate_release_note(
        created["twin_id"], payload, actor_id="operator"
    )["data"]
    second = service.validate_release_note(
        created["twin_id"], payload, actor_id="operator"
    )["data"]

    assert first["validation_hash"] == second["validation_hash"]
    assert first["release_note_artifact_hash"] == second["release_note_artifact_hash"]
    assert first["claim_counts"] == second["claim_counts"]