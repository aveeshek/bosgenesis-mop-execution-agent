from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from bosgenesis_mop_execution_agent.models import (
    ActorType,
    ApprovalScope,
    AuditEvent,
    ExecutionJob,
    ExecutionMode,
    ExecutionPhase,
    ExecutionStep,
    ExternalInstruction,
    HumanApproval,
    InstructionType,
    JobState,
    MemoryLayer,
    MemoryQuery,
    MemoryRecord,
    Observation,
    ObservationSeverity,
    ObservationType,
    PhaseStatus,
    ReportArtifact,
    ReportType,
    StepState,
)
from bosgenesis_mop_execution_agent.persistence import (
    AppendOnlyAuditWriter,
    IdempotencyConflictError,
    IdempotencyStatus,
    IdempotencyStore,
    JsonExecutionRepository,
)


def test_postgres_migration_defines_phase2_tables() -> None:
    migration = (
        __import__("pathlib")
        .Path("migrations/postgres/0001_phase2_core.sql")
        .read_text(encoding="utf-8")
    )

    for table in [
        "mop_execution_jobs",
        "mop_execution_phases",
        "mop_execution_steps",
        "mop_execution_observations",
        "mop_execution_instructions",
        "mop_execution_approvals",
        "mop_execution_audit_events",
        "mop_execution_idempotency_keys",
        "mop_execution_namespace_locks",
        "mop_execution_report_artifacts",
    ]:
        assert table in migration


def test_json_repository_persists_and_rehydrates_execution_records(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo.json"
    repo = JsonExecutionRepository(repo_path)
    now = datetime(2026, 6, 16, tzinfo=UTC)
    job = ExecutionJob(
        job_id="job-1",
        bundle_id="bundle-1",
        target_namespace="target-ns",
        state=JobState.DRY_RUN_READY,
        execution_mode=ExecutionMode.DRY_RUN_ONLY,
        correlation_id="corr-1",
        trace_id="trace-1",
        created_at=now,
        updated_at=now,
    )
    phase = ExecutionPhase(
        job_id=job.job_id,
        phase_id="phase-1",
        sequence_index=0,
        status=PhaseStatus.READY,
    )
    step = ExecutionStep(
        job_id=job.job_id,
        phase_id=phase.phase_id,
        step_id="step-1",
        sequence_index=0,
        state=StepState.READY,
    )
    observation = Observation(
        observation_id="obs-1",
        job_id=job.job_id,
        severity=ObservationSeverity.INFO,
        observation_type=ObservationType.STATE_TRANSITION,
        summary="State changed.",
    )
    instruction = ExternalInstruction(
        instruction_id="instr-1",
        job_id=job.job_id,
        instruction_type=InstructionType.CONTINUE,
        controller_id="codex",
        issued_by="external_llm_controller",
    )
    approval = HumanApproval(
        approval_id="appr-1",
        job_id=job.job_id,
        approver_id="user@example.com",
        approval_scope=ApprovalScope.MUTATION,
        ticket_reference="CHG-1",
        statement="Approved.",
        expires_at=now + timedelta(hours=1),
    )
    report = ReportArtifact(
        report_id="report-1",
        job_id=job.job_id,
        report_type=ReportType.EXECUTION_SUMMARY,
        path="output/execution-report.md",
    )

    repo.save_job(job)
    repo.save_phase(phase)
    repo.save_step(step)
    repo.add_observation(observation)
    repo.save_instruction(instruction)
    repo.save_approval(approval)
    repo.save_report(report)
    repo.save_memory_record(
        MemoryRecord(
            layer=MemoryLayer.DURABLE_JOB,
            job_id=job.job_id,
            namespace="target-ns",
            summary="Job memory.",
            payload_redacted={"state": "dry_run_ready"},
        )
    )

    rehydrated = JsonExecutionRepository(repo_path)

    assert rehydrated.get_job(job.job_id) == job
    assert rehydrated.get_phases(job.job_id) == [phase]
    assert rehydrated.get_steps(job.job_id) == [step]
    assert rehydrated.get_observations(job.job_id)[0].observation_id == "obs-1"
    assert rehydrated.get_instruction("instr-1") == instruction
    assert rehydrated.get_approval("appr-1") == approval
    assert rehydrated.get_reports(job.job_id) == [report]
    memory = rehydrated.list_memory_records(MemoryQuery(job_id=job.job_id, namespace="target-ns"))
    assert memory[0].layer == MemoryLayer.DURABLE_JOB
    assert memory[0].authority == "context_only_not_decision_authority"


def test_append_only_audit_writer_rejects_duplicate_event_id(tmp_path: Path) -> None:
    repo = JsonExecutionRepository(tmp_path / "repo.json")
    writer = AppendOnlyAuditWriter(repo)
    event = AuditEvent(
        audit_event_id="audit-1",
        actor_type=ActorType.WORKER,
        action="job_created",
        job_id="job-1",
    )

    writer.write(event)

    with pytest.raises(ValueError, match="audit_event_already_exists"):
        writer.write(event)

    assert writer.list_for_job("job-1") == [event]


def test_idempotency_store_replays_same_request_and_blocks_conflict(tmp_path: Path) -> None:
    store = IdempotencyStore(tmp_path / "idempotency.json")

    first, created = store.begin(
        idempotency_key="key-1",
        scope="create-job",
        request_payload={"bundle_id": "bundle-1"},
        correlation_id="corr-1",
    )
    replay, replay_created = store.begin(
        idempotency_key="key-1",
        scope="create-job",
        request_payload={"bundle_id": "bundle-1"},
        correlation_id="corr-1",
    )
    completed = store.complete("key-1", {"job_id": "job-1", "state": "created"})

    assert created is True
    assert replay_created is False
    assert replay.idempotency_key == first.idempotency_key
    assert completed.state == IdempotencyStatus.COMPLETED
    assert completed.result_payload_redacted == {"job_id": "job-1", "state": "created"}

    with pytest.raises(IdempotencyConflictError):
        store.begin(
            idempotency_key="key-1",
            scope="create-job",
            request_payload={"bundle_id": "different"},
        )


def test_idempotency_store_rehydrates_completed_records(tmp_path: Path) -> None:
    path = tmp_path / "idempotency.json"
    store = IdempotencyStore(path)
    store.begin(
        idempotency_key="key-1",
        scope="create-job",
        request_payload={"bundle_id": "bundle-1"},
    )
    store.complete("key-1", {"job_id": "job-1"})

    rehydrated = IdempotencyStore(path)
    record = rehydrated.get("key-1")

    assert record is not None
    assert record.state == IdempotencyStatus.COMPLETED
    assert record.result_payload_redacted == {"job_id": "job-1"}
