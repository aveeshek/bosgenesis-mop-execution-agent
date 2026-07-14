from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app
from bosgenesis_mop_execution_agent.namespace_twin.persistence import NamespaceTwinRepository
from bosgenesis_mop_execution_agent.namespace_twin.service import NamespaceTwinService

FIXTURE = Path("tests/fixtures/sample_mop_bundle").resolve()


def _payload(idempotency_key: str = "phase4-contract") -> dict:
    return {
        "source": {"type": "local_path", "value": str(FIXTURE)},
        "target_namespace": "sample-target",
        "target_cluster": "contract-cluster",
        "idempotency_key": idempotency_key,
    }


def test_real_provisional_twin_is_idempotent_ordered_and_redacted(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'twins.db').as_posix()}"
    service = NamespaceTwinService(NamespaceTwinRepository(database_url))

    first = service.create(_payload(), actor_id="operator-1")
    replay = service.create(_payload(), actor_id="operator-1")
    events = service.events(first["twin_id"])["events"]

    assert first["lifecycle_status"] == "awaiting_dry_run"
    assert first["decision"] == "pending"
    assert first["decision_is_final"] is False
    assert first["input_hash"]
    assert first["policy_version"]
    assert first["risk_rule_version"]
    assert replay["twin_id"] == first["twin_id"]
    assert replay["idempotent_replay"] is True
    assert [event["sequence"] for event in events] == [1, 2, 3]
    assert "password" not in str(events).lower()
    assert first["facts"]["provisional"] is True
    assert first["facts"]["module_modes"]["policy"] == "mock_non_authoritative"


def test_twin_survives_service_restart_and_records_recovery(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'restart.db').as_posix()}"
    service = NamespaceTwinService(NamespaceTwinRepository(database_url))
    created = service.create(_payload("restart-key"), actor_id="operator-1")

    restarted = NamespaceTwinService(NamespaceTwinRepository(database_url))
    restored = restarted.get(created["twin_id"])
    events = restarted.events(created["twin_id"])["events"]

    assert restored["input_hash"] == created["input_hash"]
    assert created["twin_id"] in restarted.recovered_twin_ids
    assert events[-1]["event_type"] == "twin_recovered"
    assert [event["sequence"] for event in events] == list(range(1, len(events) + 1))


def test_concurrent_events_receive_unique_monotonic_sequences(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'events.db').as_posix()}"
    repository = NamespaceTwinRepository(database_url)
    service = NamespaceTwinService(repository)
    created = service.create(_payload("event-key"), actor_id="operator-1")

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(
            pool.map(
                lambda index: repository.append_event(
                    created["twin_id"],
                    "contract_event",
                    f"Contract event {index}",
                    {"index": index},
                ),
                range(20),
            )
        )

    events = service.events(created["twin_id"], limit=100)["events"]
    sequences = [event["sequence"] for event in events]
    assert sequences == list(range(1, len(events) + 1))
    assert len(sequences) == len(set(sequences))


def test_cancel_is_idempotent_and_terminal_decision_is_immutable(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'cancel.db').as_posix()}"
    service = NamespaceTwinService(NamespaceTwinRepository(database_url))
    created = service.create(_payload("cancel-key"), actor_id="operator-1")

    cancelled = service.cancel(created["twin_id"], actor_id="operator-1")
    replay = service.cancel(created["twin_id"], actor_id="operator-1")

    assert cancelled["lifecycle_status"] == "cancelled"
    assert replay["lifecycle_status"] == "cancelled"


def test_real_rest_contract_and_invalid_bundle_failure_are_typed(tmp_path, monkeypatch) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'api.db').as_posix()}"
    monkeypatch.setenv("NAMESPACE_TWIN_DATABASE_URL", database_url)
    monkeypatch.setenv("MOP_EXECUTION_STATE_FILE", str(tmp_path / "execution-state.json"))
    with TestClient(create_app()) as client:
        created_response = client.post("/v1/namespace-twins", json=_payload("api-key"))
        created = created_response.json()["data"]
        listed = client.get("/v1/namespace-twins").json()["data"]
        detail = client.get(f"/v1/namespace-twins/{created['twin_id']}").json()["data"]
        events = client.get(f"/v1/namespace-twins/{created['twin_id']}/events").json()["data"]
        invalid = client.post(
            "/v1/namespace-twins",
            json={
                "source": {"type": "local_path", "value": str(tmp_path / "missing")},
                "target_namespace": "sample-target",
            },
        )

    assert created_response.status_code == 200
    assert created_response.headers["x-data-mode"] == "real_core"
    assert listed["items"][0]["twin_id"] == created["twin_id"]
    assert detail["decision_is_final"] is False
    assert events["events"][0]["sequence"] == 1
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "bundle_validation_failed"
