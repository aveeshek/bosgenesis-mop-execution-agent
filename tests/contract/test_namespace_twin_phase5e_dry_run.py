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

def test_mechanical_dry_run_state_is_complete_without_duplicate_status(
    tmp_path, monkeypatch
) -> None:
    execution = _execution_service(tmp_path, monkeypatch)
    twin_service = NamespaceTwinService(
        NamespaceTwinRepository(
            f"sqlite+pysqlite:///{(tmp_path / 'phase5e-mechanical.db').as_posix()}"
        ),
        execution_service=execution,
    )
    created = twin_service.create(_twin_payload("phase5e-mechanical"), actor_id="operator")
    _seed_dry_run(execution, created, job_id="job-phase5e-mechanical")
    execution._repository.save_step(
        ExecutionStep(
            step_id="step-k8s-validate",
            job_id="job-phase5e-mechanical",
            phase_id="phase-apply",
            sequence_index=2,
            type=StepType.K8S_VALIDATE,
            commands=[{"command": "kubectl get namespace sample-target"}],
            command_fingerprint="sha256:mechanical-fingerprint",
            state=StepState.DRY_RUN_SUCCEEDED,
        )
    )

    attached = twin_service.attach_dry_run_evidence(
        created["twin_id"],
        {"dry_run_job_id": "job-phase5e-mechanical"},
    )

    evidence = attached["dry_run"]["data"]
    assert evidence["status"] == "passed"
    assert evidence["partial_steps"] == []
    assert next(
        item for item in evidence["validations"] if item["type"] == "kubernetes_server_dry_run"
    )["status"] == "passed"


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

class _OnDemandDryRunExecution:
    def __init__(self) -> None:
        self.job_payload: dict | None = None
        self.job_state = "missing"
        self.create_calls = 0
        self.start_calls = 0

    def get_job(self, job_id: str) -> dict:
        if self.job_payload is None:
            return {"ok": False}
        return {
            "ok": True,
            "data": {"job": {"job_id": job_id, "state": self.job_state}},
        }

    def register_bundle(self, payload: dict) -> dict:
        return {"ok": True, "bundle_id": payload["bundle_id"], "data": payload}

    def validate_bundle(self, bundle_id: str, payload: dict) -> dict:
        return {
            "ok": True,
            "bundle_id": bundle_id,
            "data": {"valid": True, "bundle": {"bundle_id": bundle_id}},
        }

    def create_job(self, payload: dict) -> dict:
        self.create_calls += 1
        self.job_payload = dict(payload)
        self.job_state = "created"
        return {
            "ok": True,
            "job_id": payload["job_id"],
            "data": {"job": {"job_id": payload["job_id"], "state": "created"}},
        }

    def start_job(self, job_id: str) -> dict:
        self.start_calls += 1
        self.job_state = "dry_running"
        return {
            "ok": True,
            "job_id": job_id,
            "data": {"job": {"job_id": job_id, "state": self.job_state}},
        }

    def namespace_twin_dry_run_evidence(self, job_id: str) -> dict:
        assert self.job_payload is not None
        now = datetime.now(UTC).isoformat()
        return {
            "ok": True,
            "data": {
                "dry_run_evidence": {
                    "dry_run_job_id": job_id,
                    "status": "passed" if self.job_state == "completed" else "running",
                    "target_namespace": self.job_payload["target_namespace"],
                    "bundle_hash": self.job_payload["bundle_hash"],
                    "input_hash": self.job_payload["namespace_twin_input_hash"],
                    "command_fingerprint_hash": "a" * 64,
                    "updated_at": now,
                    "completed_at": now,
                    "dry_run_satisfied": self.job_state == "completed",
                    "observations": [],
                    "validations": [],
                    "reports": [],
                    "evidence_refs": [],
                    "failed_steps": [],
                    "partial_steps": [],
                    "fidelity_limitations": [
                        "Authoritative dry-run does not predict runtime convergence."
                    ],
                }
            },
        }


def test_on_demand_full_simulation_is_restart_safe_and_idempotent(
    tmp_path,
) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'on-demand.db').as_posix()}"
    execution = _OnDemandDryRunExecution()
    first_service = NamespaceTwinService(
        NamespaceTwinRepository(database_url),
        execution_service=execution,
    )
    payload = _twin_payload("on-demand-full-simulation")
    payload["run_authoritative_dry_run"] = True

    created = first_service.create(payload, actor_id="operator")

    assert created["relationships"]["dry_run_job_id"].startswith("twinjob_")
    assert created["foundation_facts"]["simulation"]["mode"] == "full_on_demand"
    assert created["foundation_facts"]["simulation"]["mutation_performed"] is False
    assert execution.create_calls == 1
    assert execution.start_calls == 1

    pending = first_service.get(created["twin_id"])
    assert pending["decision_is_final"] is False
    assert pending["foundation_facts"]["simulation"]["state"] == "dry_running"

    execution.job_state = "completed"
    restored_service = NamespaceTwinService(
        NamespaceTwinRepository(database_url),
        execution_service=execution,
    )
    finalized = restored_service.get(created["twin_id"])

    assert finalized["decision_is_final"] is True
    assert finalized["decision"] in {"green", "amber", "red"}
    assert finalized["tab_states"]["dry-run"]["state"] == "available"
    assert finalized["foundation_facts"]["simulation"]["state"] == "completed"
    assert finalized["foundation_facts"]["simulation"]["mutation_performed"] is False

    event_types = {
        item["event_type"]
        for item in restored_service.events(created["twin_id"])["events"]
    }
    assert "authoritative_simulation_started" in event_types
    assert "authoritative_simulation_progress" in event_types
    assert "dry_run_evidence_verified" in event_types

    replay = restored_service.create(payload, actor_id="operator")
    assert replay["idempotent_replay"] is True
    assert replay["decision_is_final"] is True
    assert execution.create_calls == 1
    assert execution.start_calls == 1