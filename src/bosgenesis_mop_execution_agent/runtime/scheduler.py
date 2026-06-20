"""Phase dependency scheduler and step selector."""

from __future__ import annotations

from bosgenesis_mop_execution_agent.models import (
    ExecutionPhase,
    ExecutionStep,
    PhaseStatus,
    StepState,
    StepType,
)


class PhaseStepScheduler:
    """Select the next runnable phase and step without inferring repairs."""

    def ready_phases(self, phases: list[ExecutionPhase]) -> list[ExecutionPhase]:
        completed = {
            phase.phase_id for phase in phases if phase.status == PhaseStatus.COMPLETED
        }
        return sorted(
            [
                phase
                for phase in phases
                if phase.status in {PhaseStatus.PENDING, PhaseStatus.READY, PhaseStatus.RUNNING}
                and all(dep in completed for dep in phase.depends_on)
            ],
            key=lambda item: item.sequence_index,
        )

    def select_next_step(
        self,
        phases: list[ExecutionPhase],
        steps: list[ExecutionStep],
    ) -> tuple[ExecutionPhase, ExecutionStep] | None:
        """Return the first dependency-ready step in the first dependency-ready phase."""
        completed_steps = {
            step.step_id
            for step in steps
            if step.state
            in {
                StepState.DRY_RUN_SUCCEEDED,
                StepState.MUTATION_SUCCEEDED,
                StepState.VALIDATION_SUCCEEDED,
                StepState.SKIPPED_BY_INSTRUCTION,
            }
        }
        completed_phases = {
            phase.phase_id for phase in phases if phase.status == PhaseStatus.COMPLETED
        }
        phase_by_id = {phase.phase_id: phase for phase in self.ready_phases(phases)}
        for phase in phase_by_id.values():
            phase_steps = sorted(
                [step for step in steps if step.phase_id == phase.phase_id],
                key=lambda item: item.sequence_index,
            )
            for step in phase_steps:
                if step.state not in {StepState.PENDING, StepState.READY, StepState.WAITING}:
                    continue
                if all(
                    dep in completed_steps or dep in completed_phases
                    for dep in step.depends_on
                ):
                    return phase, step
        return None

    def select_next_mutation_step(
        self,
        phases: list[ExecutionPhase],
        steps: list[ExecutionStep],
    ) -> tuple[ExecutionPhase, ExecutionStep] | None:
        """Return the first dry-run-succeeded mutating step ready for execution."""
        completed_steps = {
            step.step_id
            for step in steps
            if step.state
            in {
                StepState.MUTATION_SUCCEEDED,
                StepState.VALIDATION_SUCCEEDED,
                StepState.DRY_RUN_SUCCEEDED,
                StepState.SKIPPED_BY_INSTRUCTION,
            }
        }
        completed_phases = {
            phase.phase_id for phase in phases if phase.status == PhaseStatus.COMPLETED
        }
        mutating_types = {
            StepType.K8S_APPLY,
            StepType.K8S_DELETE,
            StepType.HELM_INSTALL,
            StepType.HELM_UPGRADE,
        }
        for phase in sorted(phases, key=lambda item: item.sequence_index):
            phase_steps = sorted(
                [step for step in steps if step.phase_id == phase.phase_id],
                key=lambda item: item.sequence_index,
            )
            for step in phase_steps:
                if step.type not in mutating_types:
                    continue
                if step.state in {
                    StepState.MUTATION_RUNNING,
                    StepState.MUTATION_SUCCEEDED,
                    StepState.MUTATION_FAILED,
                    StepState.DECISION_REQUIRED,
                    StepState.CANCELLED,
                }:
                    continue
                if (
                    step.state != StepState.DRY_RUN_SUCCEEDED
                    and step.dry_run_status != StepState.DRY_RUN_SUCCEEDED
                ):
                    continue
                if all(
                    dep in completed_steps or dep in completed_phases or dep == step.step_id
                    for dep in step.depends_on
                ):
                    return phase, step
        return None

    def phase_is_complete(self, phase: ExecutionPhase, steps: list[ExecutionStep]) -> bool:
        phase_steps = [step for step in steps if step.phase_id == phase.phase_id]
        terminal_step_states = {
            StepState.DRY_RUN_SUCCEEDED,
            StepState.MUTATION_SUCCEEDED,
            StepState.VALIDATION_SUCCEEDED,
            StepState.SKIPPED_BY_INSTRUCTION,
            StepState.CANCELLED,
        }
        return bool(phase_steps) and all(
            step.state in terminal_step_states for step in phase_steps
        )
