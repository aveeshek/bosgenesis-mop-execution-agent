"""External LLM instruction validation and acceptance flow."""

from __future__ import annotations

from dataclasses import dataclass, field

from bosgenesis_mop_execution_agent.common.ids import new_id
from bosgenesis_mop_execution_agent.models import (
    ActorType,
    AuditEvent,
    ExecutionJob,
    ExecutionMode,
    ExternalInstruction,
    InstructionType,
    JobState,
    PolicyBlock,
    PolicySeverity,
    StepState,
)
from bosgenesis_mop_execution_agent.persistence.audit import AppendOnlyAuditWriter
from bosgenesis_mop_execution_agent.persistence.repositories import JsonExecutionRepository

SAFE_RESUME_INSTRUCTIONS = {
    InstructionType.CONTINUE,
    InstructionType.RETRY,
    InstructionType.WAIT,
    InstructionType.SKIP,
    InstructionType.ABORT,
    InstructionType.REFRESH_OBSERVATION,
}

UNSAFE_WITHOUT_DEDICATED_POLICY = {
    InstructionType.PATCH_MANIFEST,
    InstructionType.REPLACE_MANIFEST,
    InstructionType.INVOKE_MCP_TOOL,
}


@dataclass(frozen=True)
class InstructionDecision:
    """Outcome of validating an external controller instruction."""

    accepted: bool
    status: str
    reason_code: str | None = None
    policy_blocks: list[PolicyBlock] = field(default_factory=list)


class InstructionGate:
    """Validate and persist external LLM instructions without granting authority."""

    def __init__(self, repository: JsonExecutionRepository) -> None:
        self._repository = repository
        self._audit = AppendOnlyAuditWriter(repository)

    def receive(self, instruction: ExternalInstruction) -> InstructionDecision:
        """Record, validate, and accept/reject one instruction."""
        job = self._repository.get_job(instruction.job_id)
        self._write_audit(instruction, "instruction_received")
        if job is None:
            self._write_audit(instruction, "instruction_rejected", {"reason_code": "JOB_NOT_FOUND"})
            return InstructionDecision(False, "rejected", "JOB_NOT_FOUND")

        blocked = self._policy_blocks(job, instruction)
        if blocked:
            self._write_audit(
                instruction,
                "instruction_policy_blocked",
                {"reason_code": blocked[0].code},
            )
            return InstructionDecision(
                accepted=False,
                status="policy_blocked",
                reason_code=blocked[0].code,
                policy_blocks=blocked,
            )

        if instruction.instruction_type not in SAFE_RESUME_INSTRUCTIONS:
            self._write_audit(
                instruction,
                "instruction_rejected",
                {"reason_code": "UNSUPPORTED_INSTRUCTION_TYPE"},
            )
            return InstructionDecision(False, "rejected", "UNSUPPORTED_INSTRUCTION_TYPE")

        self._repository.save_instruction(instruction)
        self._apply_safe_instruction(job, instruction)
        self._write_audit(instruction, "instruction_accepted")
        return InstructionDecision(True, "accepted")

    def _policy_blocks(
        self,
        job: ExecutionJob,
        instruction: ExternalInstruction,
    ) -> list[PolicyBlock]:
        blocks: list[PolicyBlock] = []
        allowed_non_decision = {
            InstructionType.ABORT,
            InstructionType.REFRESH_OBSERVATION,
        }
        if job.state in {JobState.AWAITING_HUMAN_APPROVAL, JobState.EXECUTING}:
            allowed_non_decision.add(InstructionType.CONTINUE)
        if job.state == JobState.ROLLBACK_REQUESTED:
            allowed_non_decision.add(InstructionType.ROLLBACK)
        if (
            job.state != JobState.DECISION_REQUIRED
            and instruction.instruction_type not in allowed_non_decision
        ):
            blocks.append(
                _block(
                    "INSTRUCTION_STATE_MISMATCH",
                    "Instruction can only resume a decision-required job.",
                )
            )
        if (
            instruction.instruction_type == InstructionType.ROLLBACK
            and job.state != JobState.ROLLBACK_REQUESTED
        ):
            blocks.append(
                _block(
                    "ROLLBACK_STATE_REQUIRED",
                    "Rollback instructions require a rollback-requested job.",
                )
            )
        if instruction.instruction_type in UNSAFE_WITHOUT_DEDICATED_POLICY:
            blocks.append(
                _block(
                    "UNSAFE_INSTRUCTION_BLOCKED",
                    f"{instruction.instruction_type.value} requires a dedicated policy path.",
                )
            )
        if instruction.replacement_manifest and not instruction.dry_run_required:
            blocks.append(
                _block(
                    "INSTRUCTION_DRY_RUN_REQUIRED",
                    "Manifest replacement instructions must require dry-run.",
                )
            )
        return blocks

    def _apply_safe_instruction(
        self,
        job: ExecutionJob,
        instruction: ExternalInstruction,
    ) -> None:
        if instruction.instruction_type == InstructionType.ABORT:
            self._repository.save_job(
                job.model_copy(update={"state": JobState.CANCELLED, "blocked": False})
            )
            return
        if instruction.instruction_type == InstructionType.ROLLBACK:
            self._repository.save_job(
                job.model_copy(
                    update={
                        "decision_required": False,
                        "blocked": False,
                    }
                )
            )
            return
        if instruction.instruction_type in {InstructionType.CONTINUE, InstructionType.RETRY}:
            if job.state in {JobState.AWAITING_HUMAN_APPROVAL, JobState.EXECUTING}:
                return
            resume_mutation = (
                job.execution_mode == ExecutionMode.EXECUTE_AFTER_APPROVAL
                and job.dry_run_satisfied
            )
            resumed_step_state = (
                StepState.DRY_RUN_SUCCEEDED if resume_mutation else StepState.PENDING
            )
            for step in self._repository.get_steps(job.job_id):
                if instruction.target_step_id and step.step_id != instruction.target_step_id:
                    continue
                if step.state == StepState.DECISION_REQUIRED:
                    update = {
                        "state": resumed_step_state,
                        "mutation_status": None,
                        "validation_status": None,
                    }
                    if resume_mutation:
                        update["dry_run_status"] = StepState.DRY_RUN_SUCCEEDED
                    else:
                        update["dry_run_status"] = None
                    self._repository.save_step(
                        step.model_copy(update=update)
                    )
                    break
            self._repository.save_job(
                job.model_copy(
                    update={
                        "state": (
                            JobState.EXECUTING if resume_mutation else JobState.DRY_RUN_READY
                        ),
                        "decision_required": False,
                        "blocked": False,
                    }
                )
            )
        elif instruction.instruction_type == InstructionType.SKIP:
            for step in self._repository.get_steps(job.job_id):
                if instruction.target_step_id and step.step_id == instruction.target_step_id:
                    self._repository.save_step(
                        step.model_copy(update={"state": StepState.SKIPPED_BY_INSTRUCTION})
                    )
                    break
            self._repository.save_job(
                job.model_copy(
                    update={
                        "state": JobState.DRY_RUN_READY,
                        "decision_required": False,
                        "blocked": False,
                    }
                )
            )

    def _write_audit(
        self,
        instruction: ExternalInstruction,
        action: str,
        details: dict[str, str] | None = None,
    ) -> None:
        self._audit.write(
            AuditEvent(
                audit_event_id=new_id("audit"),
                actor_type=ActorType.EXTERNAL_LLM,
                actor_id=instruction.controller_id,
                action=action,
                job_id=instruction.job_id,
                correlation_id=instruction.correlation_id,
                trace_id=instruction.trace_id,
                details={
                    "instruction_id": instruction.instruction_id,
                    "instruction_type": instruction.instruction_type.value,
                    "issued_by": instruction.issued_by,
                    **(details or {}),
                },
            )
        )


def _block(code: str, message: str) -> PolicyBlock:
    return PolicyBlock(
        code=code,
        message=message,
        severity=PolicySeverity.BLOCK,
        guardrail="external_instruction",
    )
