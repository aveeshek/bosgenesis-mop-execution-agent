"""Deterministic async execution worker runtime."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from bosgenesis_mop_execution_agent.common.time import utc_now
from bosgenesis_mop_execution_agent.models import (
    ActorType,
    ExecutionJob,
    ExecutionMode,
    ExecutionPhase,
    ExecutionProgress,
    ExecutionStep,
    ExternalInstruction,
    JobState,
    Observation,
    ObservationSeverity,
    ObservationType,
    PhaseStatus,
    StepState,
    StepType,
)
from bosgenesis_mop_execution_agent.persistence import (
    AppendOnlyAuditWriter,
    NamespaceLock,
    NamespaceLockService,
    NamespaceLockUnavailable,
    WorkerHeartbeatService,
)
from bosgenesis_mop_execution_agent.persistence.repositories import JsonExecutionRepository
from bosgenesis_mop_execution_agent.plans.models import MachineExecutionPlan
from bosgenesis_mop_execution_agent.runtime.decision_context import DecisionContextBuilder
from bosgenesis_mop_execution_agent.runtime.dry_run import DryRunExecutor
from bosgenesis_mop_execution_agent.runtime.instructions import InstructionDecision, InstructionGate
from bosgenesis_mop_execution_agent.runtime.mutation import MutationExecutor
from bosgenesis_mop_execution_agent.runtime.observations import ObservationBuilder
from bosgenesis_mop_execution_agent.runtime.queue import InMemoryJobQueue
from bosgenesis_mop_execution_agent.runtime.scheduler import PhaseStepScheduler
from bosgenesis_mop_execution_agent.runtime.waits import WaitExecutor
from bosgenesis_mop_execution_agent.state.machine import DEFAULT_STATE_MACHINE


@dataclass(frozen=True)
class RuntimeDecision:
    """Result of one worker tick."""

    job_id: str | None
    state: JobState | None
    action: str
    requeue: bool = False


class WorkerRuntime:
    """One-worker deterministic runtime for queued execution jobs."""

    def __init__(
        self,
        *,
        repository: JsonExecutionRepository,
        queue: InMemoryJobQueue,
        lock_service: NamespaceLockService,
        heartbeat_service: WorkerHeartbeatService,
        worker_id: str = "worker-1",
        scheduler: PhaseStepScheduler | None = None,
        observations: ObservationBuilder | None = None,
        decisions: DecisionContextBuilder | None = None,
        dry_runs: DryRunExecutor | None = None,
        mutations: MutationExecutor | None = None,
        waits: WaitExecutor | None = None,
    ) -> None:
        self._repository = repository
        self._queue = queue
        self._locks = lock_service
        self._heartbeats = heartbeat_service
        self._worker_id = worker_id
        self._scheduler = scheduler or PhaseStepScheduler()
        self._observations = observations or ObservationBuilder()
        self._decisions = decisions or DecisionContextBuilder()
        self._dry_runs = dry_runs
        self._mutations = mutations
        self._waits = waits or WaitExecutor()
        self._active_locks: dict[str, NamespaceLock] = {}
        self._pause_requested: set[str] = set()
        self._cancel_requested: set[str] = set()

    def enqueue(self, job_id: str) -> bool:
        return self._queue.enqueue(job_id)

    def recover_restartable_jobs(self) -> int:
        """Requeue persisted jobs that can safely resume after restart."""
        return self._queue.rehydrate()

    def seed_plan(self, job_id: str, plan: MachineExecutionPlan) -> None:
        """Persist phases and steps from a parsed machine execution plan."""
        for phase_index, plan_phase in enumerate(plan.phases):
            phase = ExecutionPhase(
                phase_id=plan_phase.phase_id,
                job_id=job_id,
                sequence_index=phase_index,
                status=PhaseStatus.PENDING,
                title=plan_phase.title,
                objective=plan_phase.objective,
                depends_on=plan_phase.depends_on,
            )
            self._repository.save_phase(phase)
            for step_index, plan_step in enumerate(plan_phase.steps):
                self._repository.save_step(
                    ExecutionStep(
                        step_id=plan_step.step_id,
                        job_id=job_id,
                        phase_id=plan_phase.phase_id,
                        sequence_index=step_index,
                        title=plan_step.title,
                        type=_step_type(plan_step.type),
                        state=StepState.PENDING,
                        depends_on=plan_step.depends_on,
                        manifest_refs=plan_step.manifest_refs,
                        values_refs=plan_step.values_refs,
                        commands=[
                            command.model_dump(mode="json") for command in plan_step.commands
                        ],
                    )
                )
        job = self._require_job(job_id)
        self._save_job(self._with_progress(job))

    def request_pause(self, job_id: str) -> None:
        self._pause_requested.add(job_id)
        self.enqueue(job_id)

    def request_cancel(self, job_id: str) -> None:
        self._cancel_requested.add(job_id)
        self.enqueue(job_id)

    def submit_instruction(self, instruction: ExternalInstruction) -> InstructionDecision:
        """Accept or reject an explicit instruction from an external controller."""
        decision = InstructionGate(self._repository).receive(instruction)
        if decision.accepted:
            self.enqueue(instruction.job_id)
        return decision

    def run_once(self) -> RuntimeDecision:
        queued = self._queue.dequeue()
        if queued is None:
            return RuntimeDecision(job_id=None, state=None, action="idle")
        return self.process_job(queued.job_id)

    def process_job(self, job_id: str) -> RuntimeDecision:
        job = self._require_job(job_id)
        self._heartbeats.heartbeat(self._worker_id, job_id)

        if job_id in self._cancel_requested:
            self._cancel_requested.remove(job_id)
            return self._cancel(job)
        if job_id in self._pause_requested:
            self._pause_requested.remove(job_id)
            return self._pause(job)

        if job.state == JobState.CREATED:
            updated = self._transition(
                job,
                JobState.VALIDATING_BUNDLE,
                "Worker accepted queued job.",
            )
            self.enqueue(job_id)
            return RuntimeDecision(job_id, updated.state, "accepted", requeue=True)
        if job.state == JobState.VALIDATING_BUNDLE:
            return self._validate_runtime_plan(job)
        if job.state == JobState.DRY_RUN_READY:
            return self._start_dry_run(job)
        if job.state == JobState.DRY_RUNNING:
            return self._run_next_step(job, dry_run=True)
        if job.state == JobState.AWAITING_HUMAN_APPROVAL:
            return self._await_human_approval(job)
        if job.state == JobState.EXECUTING:
            return self._run_next_step(job, dry_run=False)
        if job.state == JobState.WAIT_SCHEDULED:
            return self._poll_wait(job)
        if job.state == JobState.ROLLBACK_REQUESTED:
            return self._decision_required(
                job,
                reason_code="ROLLBACK_INSTRUCTION_REQUIRED",
                summary="Rollback requires explicit external instruction and human approval.",
            )
        return RuntimeDecision(job_id, job.state, "not_runnable")

    def _validate_runtime_plan(self, job: ExecutionJob) -> RuntimeDecision:
        phases = self._repository.get_phases(job.job_id)
        steps = self._repository.get_steps(job.job_id)
        if not phases or not steps:
            return self._decision_required(
                job,
                reason_code="MACHINE_PLAN_REQUIRED",
                summary="No persisted machine execution plan is available for this job.",
            )
        updated = self._transition(job, JobState.DRY_RUN_READY, "Machine plan loaded.")
        self.enqueue(job.job_id)
        return RuntimeDecision(updated.job_id, updated.state, "plan_ready", requeue=True)

    def _start_dry_run(self, job: ExecutionJob) -> RuntimeDecision:
        lock = self._acquire_lock_or_decision(job)
        if lock is None:
            return RuntimeDecision(job.job_id, JobState.DECISION_REQUIRED, "lock_unavailable")
        updated = self._transition(job, JobState.DRY_RUNNING, "Dry-run execution started.")
        self.enqueue(job.job_id)
        return RuntimeDecision(updated.job_id, updated.state, "dry_run_started", requeue=True)

    def _run_next_step(self, job: ExecutionJob, *, dry_run: bool) -> RuntimeDecision:
        phases = self._repository.get_phases(job.job_id)
        steps = self._repository.get_steps(job.job_id)
        if not dry_run:
            mutation_selection = self._scheduler.select_next_mutation_step(phases, steps)
            if mutation_selection is None:
                completed = self._transition(job, JobState.COMPLETED, "No mutations remain.")
                self._release_lock(completed.job_id)
                return RuntimeDecision(completed.job_id, completed.state, "completed")
            phase, step = mutation_selection
            return self._execute_mutation_step(job, phase, step)

        completed_empty_phase = self._complete_next_empty_ready_phase(job, phases, steps)
        if completed_empty_phase is not None:
            return completed_empty_phase
        phases = self._repository.get_phases(job.job_id)
        steps = self._repository.get_steps(job.job_id)
        selection = self._scheduler.select_next_step(phases, steps)
        if selection is None:
            if any(step.state in {StepState.PENDING, StepState.READY} for step in steps):
                return self._decision_required(
                    job,
                    reason_code="NO_RUNNABLE_STEP",
                    summary="Pending steps remain, but no dependency-ready step can run.",
                )
            if (
                dry_run
                and job.execution_mode == ExecutionMode.EXECUTE_AFTER_APPROVAL
                and self._scheduler.select_next_mutation_step(phases, steps) is not None
            ):
                updated = self._transition(
                    job.model_copy(update={"dry_run_satisfied": True}),
                    JobState.AWAITING_HUMAN_APPROVAL,
                    "Dry-run completed; mutation requires approval.",
                )
                self._release_lock(updated.job_id)
                self.enqueue(job.job_id)
                return RuntimeDecision(
                    updated.job_id,
                    updated.state,
                    "awaiting_human_approval",
                    requeue=True,
                )
            completed = self._transition(job, JobState.COMPLETED, "No runnable steps remain.")
            self._release_lock(completed.job_id)
            return RuntimeDecision(completed.job_id, completed.state, "completed")

        phase, step = selection
        if step.type == StepType.WAIT:
            return self._schedule_wait(job, phase, step)
        if step.type in {StepType.CONTEXT_CHECK, StepType.K8S_GET, StepType.K8S_VALIDATE}:
            return self._complete_mechanical_step(job, phase, step, dry_run=dry_run)
        if dry_run and step.type in {
            StepType.K8S_APPLY,
            StepType.HELM_INSTALL,
            StepType.HELM_UPGRADE,
            StepType.HELM_VALIDATE,
        }:
            return self._execute_dry_run_step(job, phase, step)

        return self._decision_required(
            job,
            reason_code="EXTERNAL_INSTRUCTION_REQUIRED",
            summary=f"Step {step.step_id} requires external instruction before execution.",
            step=step,
        )

    def _execute_dry_run_step(
        self,
        job: ExecutionJob,
        phase: ExecutionPhase,
        step: ExecutionStep,
    ) -> RuntimeDecision:
        if self._dry_runs is None:
            return self._decision_required(
                job,
                reason_code="DRY_RUN_EXECUTOR_UNAVAILABLE",
                summary=f"Step {step.step_id} requires a configured dry-run executor.",
                step=step,
            )

        now = utc_now()
        self._repository.save_step(
            step.model_copy(
                update={
                    "state": StepState.DRY_RUN_RUNNING,
                    "dry_run_status": StepState.DRY_RUN_RUNNING,
                    "started_at": step.started_at or now,
                    "attempt_number": step.attempt_number + 1,
                }
            )
        )
        result = self._dry_runs.execute(job=job, step=step)
        if not result.success:
            failed_step = step.model_copy(
                update={
                    "state": StepState.DRY_RUN_FAILED,
                    "dry_run_status": StepState.DRY_RUN_FAILED,
                    "completed_at": utc_now(),
                    "attempt_number": step.attempt_number + 1,
                }
            )
            self._repository.save_step(failed_step)
            self._add_observation(
                self._observations.build(
                    job_id=job.job_id,
                    observation_type=ObservationType.DRY_RUN_RESULT,
                    severity=ObservationSeverity.ERROR,
                    summary=result.message or f"Dry-run failed for step {step.step_id}.",
                    phase_id=phase.phase_id,
                    step_id=step.step_id,
                    result={
                        "action": result.action,
                        "error_code": result.error_code.value if result.error_code else None,
                        "outputs": result.outputs,
                        "worker_reasoning_triggered": False,
                    },
                )
            )
            return self._decision_required(
                job,
                reason_code=result.error_code.value if result.error_code else "DRY_RUN_FAILED",
                summary=result.message or f"Dry-run failed for step {step.step_id}.",
                step=failed_step,
            )

        completed_step = step.model_copy(
            update={
                "state": StepState.DRY_RUN_SUCCEEDED,
                "dry_run_status": StepState.DRY_RUN_SUCCEEDED,
                "completed_at": utc_now(),
                "attempt_number": step.attempt_number + 1,
            }
        )
        self._repository.save_step(completed_step)
        self._add_observation(
            self._observations.build(
                job_id=job.job_id,
                observation_type=ObservationType.DRY_RUN_RESULT,
                summary=f"Dry-run succeeded for step {step.step_id}.",
                phase_id=phase.phase_id,
                step_id=step.step_id,
                result={
                    "action": result.action,
                    "outputs": result.outputs,
                    "dry_run_only": job.execution_mode == ExecutionMode.DRY_RUN_ONLY,
                    "mutation_performed": False,
                    "worker_reasoning_triggered": False,
                },
            )
        )
        self._complete_phase_if_ready(phase)
        self._save_job(self._with_progress(job.model_copy(update={"dry_run_satisfied": True})))
        self.enqueue(job.job_id)
        return RuntimeDecision(job.job_id, job.state, "dry_run_step_completed", requeue=True)

    def _await_human_approval(self, job: ExecutionJob) -> RuntimeDecision:
        approvals = self._repository.get_approvals(job.job_id)
        if not approvals:
            return RuntimeDecision(job.job_id, job.state, "awaiting_human_approval")
        updated = self._transition(job, JobState.EXECUTING, "Human approval is available.")
        self.enqueue(job.job_id)
        return RuntimeDecision(updated.job_id, updated.state, "executing", requeue=True)

    def _execute_mutation_step(
        self,
        job: ExecutionJob,
        phase: ExecutionPhase,
        step: ExecutionStep,
    ) -> RuntimeDecision:
        if self._mutations is None:
            return self._decision_required(
                job,
                reason_code="MUTATION_EXECUTOR_UNAVAILABLE",
                summary=f"Step {step.step_id} requires a configured mutation executor.",
                step=step,
            )
        if self._acquire_lock_or_decision(job) is None:
            return RuntimeDecision(job.job_id, JobState.DECISION_REQUIRED, "lock_unavailable")

        now = utc_now()
        running_step = step.model_copy(
            update={
                "state": StepState.MUTATION_RUNNING,
                "mutation_status": StepState.MUTATION_RUNNING,
                "started_at": step.started_at or now,
                "attempt_number": step.attempt_number + 1,
            }
        )
        self._repository.save_step(running_step)
        result = self._mutations.execute(
            job=job,
            step=running_step,
            approvals=self._repository.get_approvals(job.job_id),
            instructions=self._repository.get_instructions(job.job_id),
        )
        if not result.success:
            failed_step = running_step.model_copy(
                update={
                    "state": StepState.MUTATION_FAILED,
                    "mutation_status": StepState.MUTATION_FAILED,
                    "completed_at": utc_now(),
                }
            )
            self._repository.save_step(failed_step)
            self._add_observation(
                self._observations.build(
                    job_id=job.job_id,
                    observation_type=ObservationType.MUTATION_RESULT,
                    severity=ObservationSeverity.CRITICAL
                    if result.unknown_mutation_outcome
                    else ObservationSeverity.ERROR,
                    summary=result.message or f"Mutation failed for step {step.step_id}.",
                    phase_id=phase.phase_id,
                    step_id=step.step_id,
                    result={
                        "action": result.action,
                        "error_code": result.error_code.value if result.error_code else None,
                        "outputs": result.outputs,
                        "resource_mutations": result.resource_mutations,
                        "unknown_mutation_outcome": result.unknown_mutation_outcome,
                        "worker_reasoning_triggered": False,
                    },
                    policy_blocks=result.policy_blocks,
                )
            )
            return self._decision_required(
                job,
                reason_code="UNKNOWN_MUTATION_OUTCOME"
                if result.unknown_mutation_outcome
                else result.error_code.value
                if result.error_code
                else "MUTATION_BLOCKED",
                summary=result.message or f"Mutation failed for step {step.step_id}.",
                step=failed_step,
            )

        completed_step = running_step.model_copy(
            update={
                "state": StepState.MUTATION_SUCCEEDED,
                "mutation_status": StepState.MUTATION_SUCCEEDED,
                "completed_at": utc_now(),
            }
        )
        self._repository.save_step(completed_step)
        self._add_observation(
            self._observations.build(
                job_id=job.job_id,
                observation_type=ObservationType.MUTATION_RESULT,
                summary=f"Mutation succeeded for step {step.step_id}.",
                phase_id=phase.phase_id,
                step_id=step.step_id,
                result={
                    "action": result.action,
                    "outputs": result.outputs,
                    "resource_mutations": result.resource_mutations,
                    "unknown_mutation_outcome": False,
                    "worker_reasoning_triggered": False,
                },
            )
        )
        self._complete_phase_if_ready(phase)
        self._save_job(self._with_progress(job))
        self.enqueue(job.job_id)
        return RuntimeDecision(job.job_id, job.state, "mutation_step_completed", requeue=True)

    def _complete_mechanical_step(
        self,
        job: ExecutionJob,
        phase: ExecutionPhase,
        step: ExecutionStep,
        *,
        dry_run: bool,
    ) -> RuntimeDecision:
        now = utc_now()
        completed_state = StepState.DRY_RUN_SUCCEEDED if dry_run else StepState.VALIDATION_SUCCEEDED
        updated_step = step.model_copy(
            update={
                "state": completed_state,
                "started_at": step.started_at or now,
                "completed_at": now,
                "attempt_number": step.attempt_number + 1,
            }
        )
        self._repository.save_step(updated_step)
        self._add_observation(
            self._observations.build(
                job_id=job.job_id,
                observation_type=ObservationType.DRY_RUN_RESULT
                if dry_run
                else ObservationType.VALIDATION_RESULT,
                summary=f"Step {step.step_id} completed mechanically.",
                phase_id=phase.phase_id,
                step_id=step.step_id,
                result={
                    "worker_reasoning_triggered": False,
                    "step_type": step.type.value,
                    "state": completed_state.value,
                },
            )
        )
        self._complete_phase_if_ready(phase)
        self._save_job(self._with_progress(job))
        self.enqueue(job.job_id)
        return RuntimeDecision(job.job_id, job.state, "step_completed", requeue=True)

    def _complete_next_empty_ready_phase(
        self,
        job: ExecutionJob,
        phases: list[ExecutionPhase],
        steps: list[ExecutionStep],
    ) -> RuntimeDecision | None:
        for phase in self._scheduler.ready_phases(phases):
            if any(step.phase_id == phase.phase_id for step in steps):
                continue
            self._repository.save_phase(
                phase.model_copy(
                    update={
                        "status": PhaseStatus.COMPLETED,
                        "started_at": phase.started_at or utc_now(),
                        "completed_at": utc_now(),
                    }
                )
            )
            self._add_observation(
                self._observations.build(
                    job_id=job.job_id,
                    observation_type=ObservationType.DRY_RUN_RESULT,
                    summary=f"Empty phase {phase.phase_id} completed mechanically.",
                    phase_id=phase.phase_id,
                    result={
                        "worker_reasoning_triggered": False,
                        "phase_id": phase.phase_id,
                        "state": PhaseStatus.COMPLETED.value,
                    },
                )
            )
            self._save_job(self._with_progress(job))
            self.enqueue(job.job_id)
            return RuntimeDecision(job.job_id, job.state, "empty_phase_completed", requeue=True)
        return None

    def _schedule_wait(
        self,
        job: ExecutionJob,
        phase: ExecutionPhase,
        step: ExecutionStep,
    ) -> RuntimeDecision:
        now = utc_now()
        self._repository.save_step(
            step.model_copy(
                update={
                    "state": StepState.WAITING,
                    "started_at": step.started_at or now,
                    "attempt_number": step.attempt_number + 1,
                }
            )
        )
        self._add_observation(
            self._observations.build(
                job_id=job.job_id,
                observation_type=ObservationType.WAIT_RESULT,
                summary=f"Wait scheduled for step {step.step_id}.",
                phase_id=phase.phase_id,
                step_id=step.step_id,
                result={"next_poll_after_seconds": self._waits.due_at().isoformat()},
            )
        )
        updated = self._transition(job, JobState.WAIT_SCHEDULED, "Wait scheduled.")
        self.enqueue(job.job_id)
        return RuntimeDecision(updated.job_id, updated.state, "wait_scheduled", requeue=True)

    def _poll_wait(self, job: ExecutionJob) -> RuntimeDecision:
        step = self._current_or_waiting_step(job)
        if step is None:
            return self._decision_required(
                job,
                reason_code="WAIT_STATE_WITHOUT_STEP",
                summary="Job is waiting but no waiting step is persisted.",
            )
        result = self._waits.evaluate(started_at=step.started_at, timeout_seconds=1)
        if result.timed_out:
            return self._decision_required(
                job,
                reason_code="TIMEOUT_EXCEEDED",
                summary=f"Wait step {step.step_id} exceeded its timeout.",
                step=step,
            )
        self._add_observation(
            self._observations.build(
                job_id=job.job_id,
                observation_type=ObservationType.WAIT_RESULT,
                summary=f"Wait step {step.step_id} is still pending.",
                phase_id=step.phase_id,
                step_id=step.step_id,
                result=result.__dict__,
            )
        )
        self.enqueue(job.job_id)
        return RuntimeDecision(job.job_id, job.state, "wait_pending", requeue=True)

    def _pause(self, job: ExecutionJob) -> RuntimeDecision:
        if job.state == JobState.PAUSED:
            return RuntimeDecision(job.job_id, job.state, "already_paused")
        paused = self._transition(job, JobState.PAUSED, "Pause requested.")
        self._release_lock(job.job_id)
        return RuntimeDecision(paused.job_id, paused.state, "paused")

    def _cancel(self, job: ExecutionJob) -> RuntimeDecision:
        cancelled = self._transition(job, JobState.CANCELLED, "Cancel requested.")
        for step in self._repository.get_steps(job.job_id):
            if step.state not in {
                StepState.DRY_RUN_SUCCEEDED,
                StepState.MUTATION_SUCCEEDED,
                StepState.VALIDATION_SUCCEEDED,
            }:
                self._repository.save_step(step.model_copy(update={"state": StepState.CANCELLED}))
        self._release_lock(job.job_id)
        return RuntimeDecision(cancelled.job_id, cancelled.state, "cancelled")

    def _decision_required(
        self,
        job: ExecutionJob,
        *,
        reason_code: str,
        summary: str,
        step: ExecutionStep | None = None,
    ) -> RuntimeDecision:
        recent = self._repository.get_observations(job.job_id)[-5:]
        context = self._decisions.build(
            job=job,
            reason_code=reason_code,
            summary=summary,
            step=step,
            observations=recent,
        )
        observation = self._observations.build(
            job_id=job.job_id,
            observation_type=ObservationType.DECISION_REQUEST,
            severity=ObservationSeverity.WARNING,
            summary=summary,
            phase_id=step.phase_id if step else job.current_phase_id,
            step_id=step.step_id if step else job.current_step_id,
            result={"reason_code": reason_code, "worker_reasoning_triggered": False},
            next_required_decision=context,
        )
        self._add_observation(observation)
        if step is not None:
            self._repository.save_step(
                step.model_copy(update={"state": StepState.DECISION_REQUIRED})
            )
        updated = self._transition(
            job.model_copy(update={"decision_required": True, "blocked": True}),
            JobState.DECISION_REQUIRED,
            summary,
            details={"reason_code": reason_code},
        )
        self._release_lock(job.job_id)
        return RuntimeDecision(updated.job_id, updated.state, "decision_required")

    def _acquire_lock_or_decision(self, job: ExecutionJob) -> NamespaceLock | None:
        if job.job_id in self._active_locks:
            return self._active_locks[job.job_id]
        try:
            lock = self._locks.acquire(job.target_namespace, job.job_id)
        except NamespaceLockUnavailable:
            self._decision_required(
                job,
                reason_code="NAMESPACE_LOCK_UNAVAILABLE",
                summary=f"Target namespace {job.target_namespace} is locked by another job.",
            )
            return None
        self._active_locks[job.job_id] = lock
        self._add_observation(
            self._observations.build(
                job_id=job.job_id,
                observation_type=ObservationType.POLICY_CHECK,
                summary="Target namespace lock acquired.",
                result={
                    "target_namespace": job.target_namespace,
                    "lease_seconds": lock.lease_seconds,
                },
            )
        )
        return lock

    def _release_lock(self, job_id: str) -> None:
        lock = self._active_locks.pop(job_id, None)
        if lock is not None:
            self._locks.release(lock)

    def _transition(
        self,
        job: ExecutionJob,
        target: JobState,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> ExecutionJob:
        result = DEFAULT_STATE_MACHINE.transition(
            job_id=job.job_id,
            from_state=job.state,
            to_state=target,
            actor_type=ActorType.WORKER,
            actor_id=self._worker_id,
            reason=reason,
            correlation_id=job.correlation_id,
            trace_id=job.trace_id,
            details=details,
        )
        updated = job.model_copy(
            update={
                "state": target,
                "updated_at": utc_now(),
                "decision_required": target == JobState.DECISION_REQUIRED,
                "blocked": target == JobState.DECISION_REQUIRED,
                "completed_at": utc_now()
                if target in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
                else job.completed_at,
            }
        )
        self._save_job(self._with_progress(updated))
        self._repository.add_observation(result.observation)
        AppendOnlyAuditWriter(self._repository).write(result.audit_event)
        return updated

    def _complete_phase_if_ready(self, phase: ExecutionPhase) -> None:
        steps = self._repository.get_steps(phase.job_id)
        if self._scheduler.phase_is_complete(phase, steps):
            self._repository.save_phase(
                phase.model_copy(
                    update={"status": PhaseStatus.COMPLETED, "completed_at": utc_now()}
                )
            )
        elif phase.status != PhaseStatus.RUNNING:
            self._repository.save_phase(
                phase.model_copy(update={"status": PhaseStatus.RUNNING, "started_at": utc_now()})
            )

    def _current_or_waiting_step(self, job: ExecutionJob) -> ExecutionStep | None:
        for step in self._repository.get_steps(job.job_id):
            if step.state == StepState.WAITING:
                return step
        return None

    def _with_progress(self, job: ExecutionJob) -> ExecutionJob:
        phases = self._repository.get_phases(job.job_id)
        steps = self._repository.get_steps(job.job_id)
        completed_steps = [
            step
            for step in steps
            if step.state
            in {
                StepState.DRY_RUN_SUCCEEDED,
                StepState.MUTATION_SUCCEEDED,
                StepState.VALIDATION_SUCCEEDED,
                StepState.SKIPPED_BY_INSTRUCTION,
            }
        ]
        failed_steps = [
            step
            for step in steps
            if step.state
            in {
                StepState.DRY_RUN_FAILED,
                StepState.MUTATION_FAILED,
                StepState.VALIDATION_FAILED,
                StepState.DECISION_REQUIRED,
            }
        ]
        return job.model_copy(
            update={
                "progress": ExecutionProgress(
                    total_phases=len(phases),
                    completed_phases=len(
                        [phase for phase in phases if phase.status == PhaseStatus.COMPLETED]
                    ),
                    total_steps=len(steps),
                    completed_steps=len(completed_steps),
                    failed_steps=len(failed_steps),
                )
            }
        )

    def _save_job(self, job: ExecutionJob) -> None:
        self._repository.save_job(job)

    def _add_observation(self, observation: Observation) -> None:
        self._repository.add_observation(observation)

    def _require_job(self, job_id: str) -> ExecutionJob:
        job = self._repository.get_job(job_id)
        if job is None:
            msg = f"job_not_found:{job_id}"
            raise KeyError(msg)
        return job


def _step_type(value: str) -> StepType:
    try:
        return StepType(value)
    except ValueError:
        return StepType.UNKNOWN
