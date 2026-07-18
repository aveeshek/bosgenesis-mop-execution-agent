from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app
from bosgenesis_mop_execution_agent.namespace_twin.persistence import NamespaceTwinRepository
from bosgenesis_mop_execution_agent.namespace_twin.service import NamespaceTwinService

FIXTURE = Path("tests/fixtures/sample_mop_bundle").resolve()


def _payload(key: str) -> dict:
    return {
        "source": {"type": "local_path", "value": str(FIXTURE)},
        "target_namespace": "sample-target",
        "target_cluster": "contract-cluster",
        "idempotency_key": key,
    }


def test_audit_is_cursor_paginated_redacted_and_actor_aware(tmp_path) -> None:
    service = NamespaceTwinService(
        NamespaceTwinRepository(f"sqlite+pysqlite:///{(tmp_path / 'audit.db').as_posix()}")
    )
    created = service.create(_payload("phase5i-audit"), actor_id="operator-1")
    service.repository.append_event(
        created["twin_id"],
        "operator_review_recorded",
        "Operator reviewed safe evidence.",
        {
            "actor_id": "reviewer-1",
            "password": "must-not-appear",
            "evidence_refs": ["dry-run-observation-1"],
        },
    )

    first = service.audit(created["twin_id"], limit=2)
    second = service.audit(
        created["twin_id"], cursor=first["page"]["next_cursor"], limit=2
    )
    serialized = json.dumps([first, second])

    assert first["page"]["limit"] == 2
    assert first["page"]["has_more"] is True
    assert second["events"]
    assert first["events"][0]["sequence"] == 1
    assert second["events"][-1]["actor"]["id"] == "reviewer-1"
    assert second["events"][-1]["evidence_refs"][0]["redacted"] is True
    assert second["events"][-1]["hashes"]["input_hash"] == created["input_hash"]
    assert second["events"][-1]["versions"]["decision_version"] == 1
    assert "must-not-appear" not in serialized


def test_json_and_markdown_reports_are_deterministic_and_decision_identical(tmp_path) -> None:
    service = NamespaceTwinService(
        NamespaceTwinRepository(f"sqlite+pysqlite:///{(tmp_path / 'report.db').as_posix()}")
    )
    created = service.create(_payload("phase5i-report"), actor_id="operator-1")
    service.repository.append_event(
        created["twin_id"],
        "operator_note_recorded",
        "Safe note.",
        {"token": "must-not-appear"},
    )

    first = service.report(created["twin_id"])
    second = service.report(created["twin_id"])
    markdown = service.report_markdown(created["twin_id"])
    serialized = json.dumps(first)

    assert first == second
    assert first["report_hash"] == second["report_hash"]
    assert first["decision"]["value"] == "pending"
    assert "- Decision: `pending`" in markdown
    assert first["safety"]["secret_values_included"] is False
    assert first["safety"]["chain_of_thought_included"] is False
    assert "must-not-appear" not in serialized
    assert "must-not-appear" not in markdown


def test_real_audit_and_report_download_endpoints(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(
        "NAMESPACE_TWIN_DATABASE_URL",
        f"sqlite+pysqlite:///{(tmp_path / 'api.db').as_posix()}",
    )
    monkeypatch.setenv("MOP_EXECUTION_STATE_FILE", str(tmp_path / "execution-state.json"))
    with TestClient(create_app()) as client:
        created = client.post("/v1/namespace-twins", json=_payload("phase5i-api")).json()["data"]
        audit = client.get(
            f"/v1/namespace-twins/{created['twin_id']}/audit", params={"limit": 25}
        )
        json_report = client.get(
            f"/v1/namespace-twins/{created['twin_id']}/reports/json"
        )
        markdown_report = client.get(
            f"/v1/namespace-twins/{created['twin_id']}/reports/markdown"
        )

    assert audit.status_code == 200
    assert audit.json()["data"]["availability"]["state"] == "available"
    assert json_report.status_code == 200
    assert json_report.headers["content-disposition"].endswith("-audit-report.json\"")
    assert markdown_report.status_code == 200
    assert markdown_report.headers["content-disposition"].endswith("-audit-report.md\"")
    assert (
        f"- Decision: `{json_report.json()['decision']['value']}`"
        in markdown_report.text
    )
