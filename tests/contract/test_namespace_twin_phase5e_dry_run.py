from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app
from bosgenesis_mop_execution_agent.api.service import MopExecutionApiService
from bosgenesis_mop_execution_agent.models import (
    ExecutionStep,
    JobState,
    Observation,
    ObservationSeverity,
    ObservationType,
    StepState,
)
from bosgenesis_mop_execution_agent.models.enums import StepType
from bosgenesis_mop_execution_agent.namespace_twin.persistence import NamespaceTwinRepository
from bosgenesis_mop_execution_agent.namespace_twin.service import (
    NamespaceTwinError,
    NamespaceTwinService,
)

FIXTURE = Path("tests/fixtures/sample_mop_bundle").resolve()


def _twin_payload(key: str) -> dict:
    return {
        "source": {"type": "local_path", "value": str(FIXTURE)},
        "target_namespace": "sample-target",
        "target_cluster": "contract-cluster",
        "idempotency_key": key,
    }


def _execution_service(tmp_path, monkeypatch) -> MopExecutionApiService:
    monkeypatch.setenv("MOP_EXECUTION_STATE_FILE", str(tmp_path / "execution-state.json"))
    return MopExecutionApiService()


def _seed_dry_run(
    execution: MopExecutionApiService,
    twin: dict,
    *,
    job_id: str,
    failed: bool = False,
    bundle_hash: str | None = None,
) -> None:
    execution.create_job(
        {
            "job_id": job_id,
            "bundle_id": "bundle-contract-1",
            "target_namespace": twin["target"]["namespace"],
            "source_namespace": "sample-source",
            "execution_mode": "dry_run_only",
            "namespace_twin_input_hash": twin["input_hash"],
            "bundle_hash": bundle_hash or twin["bundle_hash"],
        }
    )
    job = execution._repository.get_job(job_id)
    assert job is not None
    execution._repository.save_job(
        job.model_copy(
            update={
                "state": JobState.FAILED if failed else JobState.DRY_RUN_READY,
                "dry_run_satisfied": not failed,
                "updated_at": datetime.now(UTC),
                "completed_at": datetime.now(UTC),
            }
        )
    )
    execution._repository.save_step(
        ExecutionStep(
            step_id="step-k8s-apply",
            job_id=job_id,
            phase_id="phase-apply",
            sequence_index=1,
            type=StepType.K8S_APPLY,
            commands=[{"command": "kubectl apply --server-side --dry-run=server"}],
            command_fingerprint="sha256:contract-fingerprint",
            dry_run_status=(
                StepState.DRY_RUN_FAILED if failed else StepState.DRY_RUN_SUCCEEDED
            ),
            state=StepState.DRY_RUN_FAILED if failed else StepState.DRY_RUN_SUCCEEDED,
        )
    )
    execution._repository.add_observation(
        Observation(
            observation_id=f"obs-{job_id}",
            job_id=job_id,
            phase_id="phase-apply",
            step_id="step-k8s-apply",
            severity=ObservationSeverity.ERROR if failed else ObservationSeverity.INFO,
            observation_type=ObservationType.DRY_RUN_RESULT,
            summary=(
                "Server-side dry-run rejected the manifest."
                if failed
                else "Server-side dry-run accepted the manifest."
            ),
            mcp_server="bosgenesis-k8s-inspector-mcp",
            mcp_tool="k8s_apply_manifest",
            result={"status": "failed" if failed else "passed"},
            redaction_applied=True,
        )
    )


def test_authoritative_dry_run_attaches_filters_and_finalizes(tmp_path, monkeypatch) -> None:
    execution = _execution_service(tmp_path, monkeypatch)
    twin_service = NamespaceTwinService(
        NamespaceTwinRepository(
            f"sqlite+pysqlite:///{(tmp_path / 'phase5e.db').as_posix()}"
        ),
        execution_service=execution,
    )
    created = twin_service.create(_twin_payload("phase5e-pass"), actor_id="operator")
    _seed_dry_run(execution, created, job_id="job-phase5e-pass")

    attached = twin_service.attach_dry_run_evidence(
        created["twin_id"],
        {"dry_run_job_id": "job-phase5e-pass"},
    )
    final = attached["twin"]
    dry_run = attached["dry_run"]
    policy_projection = created["foundation_facts"]["policy_twin"]["decision_projection"]

    assert final["decision_is_final"] is True
    assert final["decision"] == policy_projection["level"]
    assert final["relationships"]["dry_run_job_id"] == "job-phase5e-pass"
    assert final["tab_states"]["dry-run"]["state"] == "available"
    assert "dry-run" not in final["optional_states"]
    assert dry_run["data"]["authoritative"] is True
    assert dry_run["data"]["status"] == "passed"
    assert dry_run["data"]["command_fingerprint_hash"]
    assert dry_run["data"]["snapshot"]["hash"]
    assert dry_run["data"]["automatic_instruction_submission"] is False
    assert dry_run["data"]["automatic_mutation_retry"] is False

    filtered = twin_service.dry_run(
        created["twin_id"],
        phase="phase-apply",
        tool="k8s_apply_manifest",
        outcome="accepted",
    )
    assert len(filtered["data"]["observations"]) == 1
    assert filtered["data"]["applied_filters"]["phase"] == "phase-apply"

    replay = twin_service.attach_dry_run_evidence(
        created["twin_id"],
        {"dry_run_job_id": "job-phase5e-pass"},
    )
    assert replay["idempotent_replay"] is True


def test_failed_authoritative_dry_run_has_red_precedence(tmp_path, monkeypatch) -> None:
    execution = _execution_service(tmp_path, monkeypatch)
    twin_service = NamespaceTwinService(
        NamespaceTwinRepository(
            f"sqlite+pysqlite:///{(tmp_path / 'phase5e-failed.db').as_posix()}"
        ),
        execution_service=execution,
    )
    created = twin_service.create(_twin_payload("phase5e-failed"), actor_id="operator")
    _seed_dry_run(execution, created, job_id="job-phase5e-failed", failed=True)

    attached = twin_service.attach_dry_run_evidence(
        created["twin_id"],
        {"dry_run_job_id": "job-phase5e-failed"},
    )

    assert attached["twin"]["decision"] == "red"
    assert attached["dry_run"]["data"]["status"] == "failed"
    assert attached["dry_run"]["data"]["observation_counts"]["rejected"] == 1


def test_dry_run_attachment_rejects_mismatched_bundle_via_rest(tmp_path, monkeypatch) -> None:
    execution = _execution_service(tmp_path, monkeypatch)
    twin_service = NamespaceTwinService(
        NamespaceTwinRepository(
            f"sqlite+pysqlite:///{(tmp_path / 'phase5e-rest.db').as_posix()}"
        ),
        execution_service=execution,
    )
    app = create_app()
    app.state.mop_execution_service = execution
    app.state.namespace_twin_service = twin_service
    with TestClient(app) as client:
        created = client.post(
            "/v1/namespace-twins",
            json=_twin_payload("phase5e-rest"),
        ).json()["data"]
        _seed_dry_run(
            execution,
            created,
            job_id="job-phase5e-mismatch",
            bundle_hash="f" * 64,
        )
        response = client.post(
            f"/v1/namespace-twins/{created['twin_id']}/dry-run-evidence",
            json={"dry_run_job_id": "job-phase5e-mismatch"},
        )
        pending = client.get(f"/v1/namespace-twins/{created['twin_id']}/dry-run")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "dry_run_evidence_mismatch"
    assert pending.status_code == 200
    assert pending.json()["data"]["availability"]["state"] == "not_run"





def test_stale_authoritative_dry_run_is_rejected(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NAMESPACE_TWIN_DRY_RUN_MAX_AGE_SECONDS", "60")
    execution = _execution_service(tmp_path, monkeypatch)
    twin_service = NamespaceTwinService(
        NamespaceTwinRepository(
            f"sqlite+pysqlite:///{(tmp_path / 'phase5e-stale.db').as_posix()}"
        ),
        execution_service=execution,
    )
    created = twin_service.create(_twin_payload("phase5e-stale"), actor_id="operator")
    _seed_dry_run(execution, created, job_id="job-phase5e-stale")
    job = execution._repository.get_job("job-phase5e-stale")
    assert job is not None
    execution._repository.save_job(
        job.model_copy(
            update={
                "updated_at": datetime.now(UTC) - timedelta(minutes=2),
                "completed_at": datetime.now(UTC) - timedelta(minutes=2),
            }
        )
    )

    with pytest.raises(NamespaceTwinError) as raised:
        twin_service.attach_dry_run_evidence(
            created["twin_id"],
            {"dry_run_job_id": "job-phase5e-stale"},
        )

    assert raised.value.code == "stale_dry_run_evidence"
    assert twin_service.get(created["twin_id"])["decision_is_final"] is False


def test_superseded_twin_rejects_authoritative_dry_run_attachment(
    tmp_path, monkeypatch
) -> None:
    execution = _execution_service(tmp_path, monkeypatch)
    twin_service = NamespaceTwinService(
        NamespaceTwinRepository(
            f"sqlite+pysqlite:///{(tmp_path / 'phase5e-superseded.db').as_posix()}"
        ),
        execution_service=execution,
    )
    first = twin_service.create(_twin_payload("phase5e-original"), actor_id="operator")
    replacement_payload = _twin_payload("phase5e-replacement")
    replacement_payload["supersedes_twin_id"] = first["twin_id"]
    twin_service.create(replacement_payload, actor_id="operator")
    _seed_dry_run(execution, first, job_id="job-phase5e-superseded")

    with pytest.raises(NamespaceTwinError) as raised:
        twin_service.attach_dry_run_evidence(
            first["twin_id"],
            {"dry_run_job_id": "job-phase5e-superseded"},
        )

    assert raised.value.code == "twin_not_attachable"
    assert twin_service.get(first["twin_id"])["lifecycle_status"] == "superseded"


def test_missing_command_fingerprint_is_partial_and_red(tmp_path, monkeypatch) -> None:
    execution = _execution_service(tmp_path, monkeypatch)
    twin_service = NamespaceTwinService(
        NamespaceTwinRepository(
            f"sqlite+pysqlite:///{(tmp_path / 'phase5e-partial.db').as_posix()}"
        ),
        execution_service=execution,
    )
    created = twin_service.create(_twin_payload("phase5e-partial"), actor_id="operator")
    execution.create_job(
        {
            "job_id": "job-phase5e-partial",
            "bundle_id": "bundle-contract-1",
            "target_namespace": created["target"]["namespace"],
            "source_namespace": "sample-source",
            "execution_mode": "dry_run_only",
            "namespace_twin_input_hash": created["input_hash"],
            "bundle_hash": created["bundle_hash"],
        }
    )
    job = execution._repository.get_job("job-phase5e-partial")
    assert job is not None
    execution._repository.save_job(
        job.model_copy(
            update={
                "state": JobState.DRY_RUN_READY,
                "dry_run_satisfied": True,
                "updated_at": datetime.now(UTC),
                "completed_at": datetime.now(UTC),
            }
        )
    )

    attached = twin_service.attach_dry_run_evidence(
        created["twin_id"],
        {"dry_run_job_id": "job-phase5e-partial"},
    )

    assert attached["dry_run"]["data"]["qualification_status"] == "partial"
    assert attached["dry_run"]["data"]["command_fingerprint_hash"] is None
    assert attached["twin"]["decision"] == "red"
    assert attached["twin"]["decision_is_final"] is True

def test_dry_run_attachment_polls_existing_job_until_terminal(tmp_path, monkeypatch) -> None:
    execution = _execution_service(tmp_path, monkeypatch)
    twin_service = NamespaceTwinService(
        NamespaceTwinRepository(
            f"sqlite+pysqlite:///{(tmp_path / 'phase5e-poll.db').as_posix()}"
        ),
        execution_service=execution,
    )
    created = twin_service.create(_twin_payload("phase5e-poll"), actor_id="operator")
    _seed_dry_run(execution, created, job_id="job-phase5e-poll")
    job = execution._repository.get_job("job-phase5e-poll")
    assert job is not None
    execution._repository.save_job(
        job.model_copy(
            update={
                "state": JobState.DRY_RUNNING,
                "dry_run_satisfied": False,
                "completed_at": None,
            }
        )
    )
    original = execution.namespace_twin_dry_run_evidence
    calls = {"count": 0}

    def delayed_terminal(job_id: str) -> dict:
        calls["count"] += 1
        if calls["count"] == 2:
            current = execution._repository.get_job(job_id)
            assert current is not None
            execution._repository.save_job(
                current.model_copy(
                    update={
                        "state": JobState.DRY_RUN_READY,
                        "dry_run_satisfied": True,
                        "updated_at": datetime.now(UTC),
                        "completed_at": datetime.now(UTC),
                    }
                )
            )
        return original(job_id)

    monkeypatch.setattr(execution, "namespace_twin_dry_run_evidence", delayed_terminal)
    attached = twin_service.attach_dry_run_evidence(
        created["twin_id"],
        {
            "dry_run_job_id": "job-phase5e-poll",
            "wait_seconds": 2,
            "poll_interval_ms": 100,
        },
    )

    assert calls["count"] >= 2
    assert attached["dry_run"]["data"]["status"] == "passed"
    assert attached["twin"]["decision_is_final"] is True