"""Deterministic execution job state machine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import Field

from bosgenesis_mop_execution_agent.common.ids import new_id
from bosgenesis_mop_execution_agent.common.time import utc_now
from bosgenesis_mop_execution_agent.models.audit import ActorType, AuditEvent
from bosgenesis_mop_execution_agent.models.base import StrictBaseModel
from bosgenesis_mop_execution_agent.models.enums import (
    JobState,
    ObservationSeverity,
    ObservationType,
)
from bosgenesis_mop_execution_agent.models.errors import ErrorCode
from bosgenesis_mop_execution_agent.models.observations import Observation

TERMINAL_STATES: frozenset[JobState] = frozenset(
    {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
)

ALLOWED_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.CREATED: frozenset({JobState.VALIDATING_BUNDLE, JobState.CANCELLED}),
    JobState.VALIDATING_BUNDLE: frozenset(
        {
            JobState.INVALID_BUNDLE,
            JobState.AWAITING_HUMAN_APPROVAL,
            JobState.AWAITING_LLM_INSTRUCTION,
            JobState.DECISION_REQUIRED,
            JobState.DRY_RUN_READY,
            JobState.FAILED,
        }
    ),
    JobState.INVALID_BUNDLE: frozenset({JobState.CANCELLED}),
    JobState.AWAITING_HUMAN_APPROVAL: frozenset(
        {
            JobState.DRY_RUN_READY,
            JobState.EXECUTING,
            JobState.ROLLING_BACK,
            JobState.CANCELLED,
            JobState.FAILED,
        }
    ),
    JobState.DRY_RUN_READY: frozenset(
        {JobState.DRY_RUNNING, JobState.DECISION_REQUIRED, JobState.PAUSED, JobState.CANCELLED}
    ),
    JobState.DRY_RUNNING: frozenset(
        {
            JobState.DRY_RUN_READY,
            JobState.AWAITING_LLM_INSTRUCTION,
            JobState.DECISION_REQUIRED,
            JobState.WAIT_SCHEDULED,
            JobState.AWAITING_HUMAN_APPROVAL,
            JobState.VALIDATION_RUNNING,
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.CANCELLED,
        }
    ),
    JobState.AWAITING_LLM_INSTRUCTION: frozenset(
        {
            JobState.DRY_RUN_READY,
            JobState.EXECUTING,
            JobState.ROLLING_BACK,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.PAUSED,
        }
    ),
    JobState.EXECUTING: frozenset(
        {
            JobState.WAIT_SCHEDULED,
            JobState.DECISION_REQUIRED,
            JobState.PAUSED,
            JobState.VALIDATION_RUNNING,
            JobState.COMPLETED,
            JobState.FAILED,
            JobState.ROLLBACK_REQUESTED,
            JobState.CANCELLED,
            JobState.AWAITING_HUMAN_APPROVAL,
        }
    ),
    JobState.DECISION_REQUIRED: frozenset(
        {
            JobState.AWAITING_LLM_INSTRUCTION,
            JobState.AWAITING_HUMAN_APPROVAL,
            JobState.DRY_RUN_READY,
            JobState.EXECUTING,
            JobState.ROLLING_BACK,
            JobState.FAILED,
            JobState.CANCELLED,
            JobState.PAUSED,
        }
    ),
    JobState.PAUSED: frozenset(
        {JobState.DRY_RUN_READY, JobState.EXECUTING, JobState.CANCELLED}
    ),
    JobState.WAIT_SCHEDULED: frozenset(
        {JobState.EXECUTING, JobState.DECISION_REQUIRED, JobState.CANCELLED, JobState.PAUSED}
    ),
    JobState.VALIDATION_RUNNING: frozenset(
        {JobState.COMPLETED, JobState.DECISION_REQUIRED, JobState.FAILED, JobState.CANCELLED}
    ),
    JobState.ROLLBACK_REQUESTED: frozenset(
        {
            JobState.AWAITING_HUMAN_APPROVAL,
            JobState.ROLLING_BACK,
            JobState.DECISION_REQUIRED,
            JobState.CANCELLED,
        }
    ),
    JobState.ROLLING_BACK: frozenset(
        {JobState.COMPLETED, JobState.FAILED, JobState.DECISION_REQUIRED, JobState.CANCELLED}
    ),
    JobState.COMPLETED: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}


class InvalidTransitionError(ValueError):
    """Raised when a requested state transition is not allowed."""

    def __init__(self, from_state: JobState, to_state: JobState) -> None:
        super().__init__(f"Invalid transition from {from_state.value} to {to_state.value}")
        self.error_code = ErrorCode.INVALID_STATE_TRANSITION
        self.from_state = from_state
        self.to_state = to_state


class StateTransition(StrictBaseModel):
    """Auditable state transition record."""

    job_id: str
    from_state: JobState
    to_state: JobState
    actor_type: ActorType = ActorType.WORKER
    actor_id: str | None = None
    reason: str | None = None
    correlation_id: str | None = None
    trace_id: str | None = None
    occurred_at: datetime = Field(default_factory=utc_now)


class TransitionResult(StrictBaseModel):
    """Records emitted for an accepted state transition."""

    transition: StateTransition
    observation: Observation
    audit_event: AuditEvent


TransitionGuard = Callable[[StateTransition], None]


@dataclass(frozen=True)
class StateMachine:
    """Deterministic state transition validator with optional guard hooks."""

    guards: tuple[TransitionGuard, ...] = ()

    def allowed_targets(self, state: JobState) -> frozenset[JobState]:
        """Return allowed target states for a source state."""
        return ALLOWED_TRANSITIONS[state]

    def assert_transition_allowed(self, from_state: JobState, to_state: JobState) -> None:
        """Raise if a transition is not allowed."""
        if to_state not in self.allowed_targets(from_state):
            raise InvalidTransitionError(from_state, to_state)

    def transition(
        self,
        *,
        job_id: str,
        from_state: JobState,
        to_state: JobState,
        actor_type: ActorType = ActorType.WORKER,
        actor_id: str | None = None,
        reason: str | None = None,
        correlation_id: str | None = None,
        trace_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> TransitionResult:
        """Validate a transition and build its observation and audit records."""
        self.assert_transition_allowed(from_state, to_state)
        transition = StateTransition(
            job_id=job_id,
            from_state=from_state,
            to_state=to_state,
            actor_type=actor_type,
            actor_id=actor_id,
            reason=reason,
            correlation_id=correlation_id,
            trace_id=trace_id,
        )
        for guard in self.guards:
            guard(transition)

        summary = f"Job transitioned from {from_state.value} to {to_state.value}."
        observation = Observation(
            observation_id=new_id("obs"),
            job_id=job_id,
            severity=ObservationSeverity.INFO,
            observation_type=ObservationType.STATE_TRANSITION,
            summary=summary,
            correlation_id=correlation_id,
            trace_id=trace_id,
            result={
                "from_state": from_state.value,
                "to_state": to_state.value,
                "reason": reason,
            },
        )
        audit_event = AuditEvent(
            audit_event_id=new_id("audit"),
            actor_type=actor_type,
            actor_id=actor_id,
            action="job_state_transition",
            job_id=job_id,
            correlation_id=correlation_id,
            trace_id=trace_id,
            details={
                "from_state": from_state.value,
                "to_state": to_state.value,
                "reason": reason,
                **(details or {}),
            },
        )
        return TransitionResult(
            transition=transition,
            observation=observation,
            audit_event=audit_event,
        )


DEFAULT_STATE_MACHINE = StateMachine()
