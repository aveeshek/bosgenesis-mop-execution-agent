from __future__ import annotations

import pytest
from tests.contract.test_namespace_twin_phase5e_dry_run import (
    _execution_service,
    _seed_dry_run,
    _twin_payload,
)

from bosgenesis_mop_execution_agent.namespace_twin.persistence import NamespaceTwinRepository
from bosgenesis_mop_execution_agent.namespace_twin.service import (
    NamespaceTwinError,
    NamespaceTwinService,
)


def _final_twin(tmp_path, monkeypatch):
    execution = _execution_service(tmp_path, monkeypatch)
    service = NamespaceTwinService(
        NamespaceTwinRepository(
            f"sqlite+pysqlite:///{(tmp_path / 'phase6.db').as_posix()}"
        ),
        execution_service=execution,
    )
    created = service.create(_twin_payload("phase6-gate"), actor_id="operator")
    _seed_dry_run(execution, created, job_id="job-phase6-authoritative")
    attached = service.attach_dry_run_evidence(
        created["twin_id"],
        {"dry_run_job_id": "job-phase6-authoritative"},
    )
    return service, attached["twin"]


def _link_payload(twin: dict) -> dict:
    return {
        "decision_version": twin["decision_version"],
        "gate_hash": "gate-phase6-contract",
        "bundle_hash": twin["bundle_hash"],
        "input_hash": twin["input_hash"],
        "target_namespace": twin["target_namespace"],
        "dry_run_job_id": "job-bundle-execution-dry-run",
        "authoritative_dry_run_job_id": "job-phase6-authoritative",
        "command_fingerprint_hash": twin["foundation_facts"][
            "command_fingerprint_hash"
        ],
        "approval_id": "approval-phase6",
        "approval_status": "accepted",
        "execution_id": "job-phase6-mutation",
        "execution_status": "succeeded",
        "validation_status": "passed",
        "report_status": "published",
        "rollback_cleanup_status": "not_required",
        "outcome_comparison": {
            "planned": "approved_mutation",
            "observed": "succeeded",
            "matches_plan": True,
        },
    }


def test_execution_link_preserves_final_decision_and_is_idempotent(
    tmp_path, monkeypatch
) -> None:
    service, twin = _final_twin(tmp_path, monkeypatch)
    before = (
        twin["decision"],
        twin["decision_version"],
        twin["decision_is_final"],
    )

    first = service.record_execution_link(
        twin["twin_id"], _link_payload(twin), actor_id="operator"
    )
    second = service.record_execution_link(
        twin["twin_id"], _link_payload(twin), actor_id="operator"
    )
    current = service.get(twin["twin_id"])
    events, _ = service.repository.list_events(twin["twin_id"], limit=100)

    assert (
        current["decision"],
        current["decision_version"],
        current["decision_is_final"],
    ) == before
    assert first["relationships"]["execution_id"] == "job-phase6-mutation"
    assert second["relationships"]["execution_status"] == "succeeded"
    assert len(current["foundation_facts"]["execution_links"]) == 1
    assert current["foundation_facts"]["execution_links"][0][
        "pre_execution_decision"
    ] == before[0]
    assert [item["event_type"] for item in events].count("execution_linked") == 2


def test_execution_link_rejects_changed_canonical_identity(tmp_path, monkeypatch) -> None:
    service, twin = _final_twin(tmp_path, monkeypatch)
    payload = _link_payload(twin)
    payload["bundle_hash"] = "f" * 64
    payload["decision_version"] += 1

    with pytest.raises(NamespaceTwinError) as raised:
        service.record_execution_link(twin["twin_id"], payload, actor_id="operator")

    assert raised.value.code == "execution_link_identity_mismatch"
    assert "bundle hash" in " ".join(raised.value.details["errors"]).lower()
    assert "decision version" in " ".join(raised.value.details["errors"]).lower()
    assert service.get(twin["twin_id"])["relationships"]["execution_id"] is None
