import pytest

from bosgenesis_mop_execution_agent.models import ActorType, JobState, ObservationType
from bosgenesis_mop_execution_agent.state import (
    InvalidTransitionError,
    StateMachine,
    StateTransition,
)


def test_allowed_transition_returns_auditable_records() -> None:
    machine = StateMachine()

    result = machine.transition(
        job_id="job-1",
        from_state=JobState.CREATED,
        to_state=JobState.VALIDATING_BUNDLE,
        actor_type=ActorType.WORKER,
        actor_id="worker-1",
        reason="bundle validation starts",
        correlation_id="corr-1",
        trace_id="trace-1",
    )

    assert result.transition.from_state == JobState.CREATED
    assert result.transition.to_state == JobState.VALIDATING_BUNDLE
    assert result.observation.observation_type == ObservationType.STATE_TRANSITION
    assert result.observation.correlation_id == "corr-1"
    assert result.audit_event.action == "job_state_transition"
    assert result.audit_event.details["to_state"] == "validating_bundle"


def test_invalid_transition_is_rejected() -> None:
    machine = StateMachine()

    with pytest.raises(InvalidTransitionError) as exc_info:
        machine.assert_transition_allowed(JobState.CREATED, JobState.EXECUTING)

    assert exc_info.value.from_state == JobState.CREATED
    assert exc_info.value.to_state == JobState.EXECUTING


def test_terminal_states_have_no_outbound_transitions() -> None:
    machine = StateMachine()

    assert machine.allowed_targets(JobState.COMPLETED) == frozenset()
    assert machine.allowed_targets(JobState.FAILED) == frozenset()
    assert machine.allowed_targets(JobState.CANCELLED) == frozenset()


def test_decision_required_can_continue_only_through_controlled_states() -> None:
    machine = StateMachine()

    assert JobState.EXECUTING in machine.allowed_targets(JobState.DECISION_REQUIRED)
    assert JobState.AWAITING_LLM_INSTRUCTION in machine.allowed_targets(JobState.DECISION_REQUIRED)
    assert JobState.COMPLETED not in machine.allowed_targets(JobState.DECISION_REQUIRED)


def test_transition_guard_hooks_run_before_records_are_returned() -> None:
    seen: list[StateTransition] = []

    def collect_guard(transition: StateTransition) -> None:
        seen.append(transition)

    machine = StateMachine(guards=(collect_guard,))
    result = machine.transition(
        job_id="job-1",
        from_state=JobState.DRY_RUN_READY,
        to_state=JobState.DRY_RUNNING,
    )

    assert seen == [result.transition]


def test_transition_guard_can_block_transition() -> None:
    def blocking_guard(_: StateTransition) -> None:
        msg = "guard blocked"
        raise RuntimeError(msg)

    machine = StateMachine(guards=(blocking_guard,))

    with pytest.raises(RuntimeError, match="guard blocked"):
        machine.transition(
            job_id="job-1",
            from_state=JobState.DRY_RUN_READY,
            to_state=JobState.DRY_RUNNING,
        )
