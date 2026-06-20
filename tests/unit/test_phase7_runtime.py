from __future__ import annotations

from datetime import timedelta
from pathlib import Path

from bosgenesis_mop_execution_agent.common.time import utc_now
from bosgenesis_mop_execution_agent.models import (
    ExecutionJob,
    ExecutionMode,
    ExecutionPhase,
    ExecutionStep,
    JobState,
    ObservationType,
    PhaseStatus,
    StepState,
    StepType,
)
from bosgenesis_mop_execution_agent.persistence import (
    InMemoryRedisLikeClient,
    NamespaceLockService,
    WorkerHeartbeatService,
)
from bosgenesis_mop_execution_agent.persistence.locks import namespace_lock_key
from bosgenesis_mop_execution_agent.persistence.repositories import JsonExecutionRepository
from bosgenesis_mop_execution_agent.runtime import InMemoryJobQueue, WorkerRuntime
from bosgenesis_mop_execution_agent.runtime.scheduler import PhaseStepScheduler


def test_worker_queue_scheduler_and_observations_complete_mechanical_step(
    tmp_path: Path,
) -> None:
    repo, runtime, redis_client = _runtime(tmp_path)
    job = _job()
    repo.save_job(job)
    repo.save_phase(_phase(job.job_id))
    repo.save_step(_step(job.job_id, step_type=StepType.CONTEXT_CHECK))

    runtime.enqueue(job.job_id)

    assert runtime.run_once().action == "accepted"
    assert runtime.run_once().action == "plan_ready"
    assert runtime.run_once().action == "dry_run_started"
    assert runtime.run_once().action == "step_completed"
    assert runtime.run_once().action == "completed"

    completed = repo.get_job(job.job_id)
    assert completed is not None
    assert completed.state == JobState.COMPLETED
    assert completed.progress.completed_steps == 1
    assert repo.get_steps(job.job_id)[0].state == StepState.DRY_RUN_SUCCEEDED
    assert repo.get_phases(job.job_id)[0].status == PhaseStatus.COMPLETED
    assert repo.list_audit_events(job.job_id)
    assert any(
        observation.observation_type == ObservationType.DRY_RUN_RESULT
        for observation in repo.get_observations(job.job_id)
    )
    assert redis_client.get(namespace_lock_key(job.target_namespace)) is None


def test_worker_restart_rehydrates_runnable_jobs(tmp_path: Path) -> None:
    repo_path = tmp_path / "repo.json"
    repo = JsonExecutionRepository(repo_path)
    repo.save_job(_job(state=JobState.DRY_RUN_READY))

    rehydrated_repo = JsonExecutionRepository(repo_path)
    queue = InMemoryJobQueue(rehydrated_repo)
    runtime = WorkerRuntime(
        repository=rehydrated_repo,
        queue=queue,
        lock_service=NamespaceLockService(InMemoryRedisLikeClient()),
        heartbeat_service=WorkerHeartbeatService(InMemoryRedisLikeClient()),
    )

    assert runtime.recover_restartable_jobs() == 1
    assert runtime.run_once().action in {"lock_unavailable", "dry_run_started"}


def test_namespace_lock_contention_pauses_for_decision(tmp_path: Path) -> None:
    repo, runtime, redis_client = _runtime(tmp_path)
    job = _job(state=JobState.DRY_RUN_READY)
    repo.save_job(job)
    NamespaceLockService(redis_client).acquire(job.target_namespace, "other-job")

    runtime.enqueue(job.job_id)
    decision = runtime.run_once()

    stored = repo.get_job(job.job_id)
    assert decision.action == "lock_unavailable"
    assert stored is not None
    assert stored.state == JobState.DECISION_REQUIRED
    assert any(
        observation.next_required_decision is not None
        for observation in repo.get_observations(job.job_id)
    )
    assert "other-job" in str(redis_client.get(namespace_lock_key(job.target_namespace)))


def test_wait_timeout_enters_decision_required_without_reasoning(tmp_path: Path) -> None:
    repo, runtime, _ = _runtime(tmp_path)
    job = _job(state=JobState.DRY_RUN_READY)
    repo.save_job(job)
    repo.save_phase(_phase(job.job_id))
    repo.save_step(_step(job.job_id, step_type=StepType.WAIT))
    runtime.enqueue(job.job_id)

    assert runtime.run_once().action == "dry_run_started"
    assert runtime.run_once().action == "wait_scheduled"
    waiting_step = repo.get_steps(job.job_id)[0]
    repo.save_step(
        waiting_step.model_copy(update={"started_at": utc_now() - timedelta(seconds=5)})
    )
    decision = runtime.run_once()

    stored = repo.get_job(job.job_id)
    assert decision.action == "decision_required"
    assert stored is not None
    assert stored.state == JobState.DECISION_REQUIRED
    latest = repo.get_observations(job.job_id)[-2]
    assert latest.next_required_decision is not None
    assert latest.result["worker_reasoning_triggered"] is False


def test_cancel_safe_stop_releases_lock_and_cancels_pending_steps(tmp_path: Path) -> None:
    repo, runtime, redis_client = _runtime(tmp_path)
    job = _job(state=JobState.DRY_RUN_READY)
    repo.save_job(job)
    repo.save_phase(_phase(job.job_id))
    repo.save_step(_step(job.job_id, step_type=StepType.CONTEXT_CHECK))
    runtime.enqueue(job.job_id)

    assert runtime.run_once().action == "dry_run_started"
    runtime.request_cancel(job.job_id)
    assert runtime.run_once().action == "cancelled"

    stored = repo.get_job(job.job_id)
    assert stored is not None
    assert stored.state == JobState.CANCELLED
    assert repo.get_steps(job.job_id)[0].state == StepState.CANCELLED
    assert redis_client.get(namespace_lock_key(job.target_namespace)) is None


def test_scheduler_respects_phase_and_step_dependencies() -> None:
    scheduler = PhaseStepScheduler()
    phases = [
        _phase("job-1", phase_id="phase-a", status=PhaseStatus.COMPLETED),
        _phase("job-1", phase_id="phase-b", depends_on=["phase-a"]),
    ]
    steps = [
        _step(
            "job-1",
            phase_id="phase-a",
            step_id="step-a",
            state=StepState.DRY_RUN_SUCCEEDED,
        ),
        _step("job-1", phase_id="phase-b", step_id="step-b", depends_on=["step-a"]),
    ]
    blocked_steps = [
        _step("job-1", phase_id="phase-a", step_id="other", state=StepState.PENDING),
        _step("job-1", phase_id="phase-b", step_id="step-b", depends_on=["missing"]),
    ]

    assert scheduler.select_next_step(phases, steps) is not None
    assert scheduler.select_next_step(phases, blocked_steps) is None


def test_worker_completes_empty_dependency_phase(tmp_path: Path) -> None:
    repo, runtime, _ = _runtime(tmp_path)
    job = _job(state=JobState.DRY_RUN_READY)
    repo.save_job(job)
    repo.save_phase(_phase(job.job_id, phase_id="phase-a", status=PhaseStatus.COMPLETED))
    repo.save_phase(_phase(job.job_id, phase_id="phase-empty", depends_on=["phase-a"]))
    repo.save_phase(_phase(job.job_id, phase_id="phase-b", depends_on=["phase-empty"]))
    repo.save_step(
        _step(
            job.job_id,
            phase_id="phase-b",
            step_id="step-b",
            step_type=StepType.CONTEXT_CHECK,
            depends_on=["phase-empty"],
        )
    )
    runtime.enqueue(job.job_id)

    assert runtime.run_once().action == "dry_run_started"
    assert runtime.run_once().action == "empty_phase_completed"
    assert runtime.run_once().action == "step_completed"

    phases = {phase.phase_id: phase for phase in repo.get_phases(job.job_id)}
    assert phases["phase-empty"].status == PhaseStatus.COMPLETED
    assert repo.get_steps(job.job_id)[0].state == StepState.DRY_RUN_SUCCEEDED


def test_worker_releases_lock_while_awaiting_approval(tmp_path: Path) -> None:
    repo, runtime, redis_client = _runtime(tmp_path)
    job = _job(state=JobState.DRY_RUN_READY).model_copy(
        update={"execution_mode": ExecutionMode.EXECUTE_AFTER_APPROVAL}
    )
    repo.save_job(job)
    repo.save_phase(_phase(job.job_id))
    repo.save_step(_step(job.job_id, step_type=StepType.K8S_APPLY))
    runtime.enqueue(job.job_id)

    assert runtime.run_once().action == "dry_run_started"
    step = repo.get_steps(job.job_id)[0]
    repo.save_step(
        step.model_copy(
            update={
                "state": StepState.DRY_RUN_SUCCEEDED,
                "dry_run_status": StepState.DRY_RUN_SUCCEEDED,
            }
        )
    )
    decision = runtime.run_once()

    stored = repo.get_job(job.job_id)
    assert decision.action == "awaiting_human_approval"
    assert stored is not None
    assert stored.state == JobState.AWAITING_HUMAN_APPROVAL
    assert redis_client.get(namespace_lock_key(job.target_namespace)) is None


def _runtime(
    tmp_path: Path,
) -> tuple[JsonExecutionRepository, WorkerRuntime, InMemoryRedisLikeClient]:
    repo = JsonExecutionRepository(tmp_path / "repo.json")
    redis_client = InMemoryRedisLikeClient()
    runtime = WorkerRuntime(
        repository=repo,
        queue=InMemoryJobQueue(repo),
        lock_service=NamespaceLockService(redis_client),
        heartbeat_service=WorkerHeartbeatService(redis_client),
    )
    return repo, runtime, redis_client


def _job(state: JobState = JobState.CREATED) -> ExecutionJob:
    return ExecutionJob(
        job_id="job-1",
        bundle_id="bundle-1",
        target_namespace="target-ns",
        state=state,
    )


def _phase(
    job_id: str,
    *,
    phase_id: str = "phase-1",
    status: PhaseStatus = PhaseStatus.PENDING,
    depends_on: list[str] | None = None,
) -> ExecutionPhase:
    return ExecutionPhase(
        phase_id=phase_id,
        job_id=job_id,
        sequence_index=0,
        status=status,
        objective="test objective",
        depends_on=depends_on or [],
    )


def _step(
    job_id: str,
    *,
    phase_id: str = "phase-1",
    step_id: str = "step-1",
    step_type: StepType = StepType.CONTEXT_CHECK,
    state: StepState = StepState.PENDING,
    depends_on: list[str] | None = None,
) -> ExecutionStep:
    return ExecutionStep(
        step_id=step_id,
        job_id=job_id,
        phase_id=phase_id,
        sequence_index=0,
        title="test step",
        type=step_type,
        state=state,
        depends_on=depends_on or [],
    )
