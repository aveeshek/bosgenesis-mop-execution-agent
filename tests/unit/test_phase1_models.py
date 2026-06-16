from datetime import UTC, datetime, timedelta

from bosgenesis_mop_execution_agent.models import (
    ActorType,
    ApprovalScope,
    AuditEvent,
    ErrorCode,
    ExecutionJob,
    ExecutionMode,
    ExecutionPhase,
    ExecutionStep,
    ExternalInstruction,
    HumanApproval,
    InstructionType,
    JobState,
    Observation,
    ObservationSeverity,
    ObservationType,
    PhaseStatus,
    PolicyBlock,
    PolicySeverity,
    ReportArtifact,
    ReportType,
    ResourceRef,
    StepState,
    StepType,
)
from bosgenesis_mop_execution_agent.models.errors import problem_details_for_error


def test_job_state_enum_matches_openapi_contract() -> None:
    assert [state.value for state in JobState] == [
        "created",
        "validating_bundle",
        "invalid_bundle",
        "awaiting_human_approval",
        "dry_run_ready",
        "dry_running",
        "awaiting_llm_instruction",
        "executing",
        "decision_required",
        "paused",
        "wait_scheduled",
        "validation_running",
        "rollback_requested",
        "rolling_back",
        "completed",
        "failed",
        "cancelled",
    ]


def test_step_state_enum_matches_specs_contract() -> None:
    assert [state.value for state in StepState] == [
        "pending",
        "blocked",
        "ready",
        "dry_run_running",
        "dry_run_succeeded",
        "dry_run_failed",
        "approval_required",
        "approved",
        "mutation_running",
        "mutation_succeeded",
        "mutation_failed",
        "validation_running",
        "validation_succeeded",
        "validation_failed",
        "waiting",
        "decision_required",
        "skipped_by_instruction",
        "cancelled",
    ]


def test_execution_models_serialize_for_restart_rehydration() -> None:
    created_at = datetime(2026, 6, 16, tzinfo=UTC)
    job = ExecutionJob(
        job_id="job-1",
        bundle_id="bundle-1",
        target_namespace="target-ns",
        state=JobState.DRY_RUN_READY,
        execution_mode=ExecutionMode.DRY_RUN_ONLY,
        correlation_id="corr-1",
        trace_id="trace-1",
        created_at=created_at,
        updated_at=created_at,
    )

    rehydrated = ExecutionJob.model_validate(job.model_dump(mode="json"))

    assert rehydrated.job_id == "job-1"
    assert rehydrated.state == JobState.DRY_RUN_READY
    assert rehydrated.correlation_id == "corr-1"
    assert rehydrated.trace_id == "trace-1"


def test_phase_step_and_resource_models_capture_execution_context() -> None:
    resource = ResourceRef(
        api_version="v1",
        kind="ConfigMap",
        namespace="target-ns",
        name="sample-app-config",
        file_path="generated/configmap-sample-app.yaml",
    )
    phase = ExecutionPhase(
        job_id="job-1",
        phase_id="apply_configmaps",
        sequence_index=0,
        status=PhaseStatus.READY,
        correlation_id="corr-1",
        trace_id="trace-1",
    )
    step = ExecutionStep(
        job_id="job-1",
        phase_id=phase.phase_id,
        step_id="apply-sample-configmap",
        sequence_index=0,
        type=StepType.K8S_APPLY,
        state=StepState.READY,
        resource_refs=[resource],
        command_fingerprint="sha256:abc",
        correlation_id="corr-1",
        trace_id="trace-1",
    )

    assert step.resource_refs[0].name == "sample-app-config"
    assert step.command_fingerprint == "sha256:abc"
    assert phase.status == PhaseStatus.READY


def test_instruction_approval_observation_audit_report_and_policy_models() -> None:
    expires_at = datetime.now(UTC) + timedelta(hours=1)
    resource = ResourceRef(kind="ConfigMap", namespace="target-ns", name="sample")
    instruction = ExternalInstruction(
        instruction_id="instr-1",
        job_id="job-1",
        instruction_type=InstructionType.CONTINUE,
        controller_id="codex",
        issued_by="external_llm_controller",
        target_resource=resource,
        correlation_id="corr-1",
        trace_id="trace-1",
    )
    approval = HumanApproval(
        approval_id="appr-1",
        job_id="job-1",
        approver_id="user@example.com",
        approval_scope=ApprovalScope.MUTATION,
        ticket_reference="CHG-1",
        statement="Approved for target namespace only.",
        expires_at=expires_at,
        approved_resource_refs=[resource],
        correlation_id="corr-1",
        trace_id="trace-1",
    )
    policy_block = PolicyBlock(
        code="DRY_RUN_REQUIRED",
        message="Mutation requires dry-run.",
        severity=PolicySeverity.BLOCK,
        guardrail="dry_run_before_mutation",
    )
    observation = Observation(
        observation_id="obs-1",
        job_id="job-1",
        severity=ObservationSeverity.INFO,
        observation_type=ObservationType.POLICY_CHECK,
        summary="Policy evaluated.",
        policy_blocks=[policy_block],
        correlation_id="corr-1",
        trace_id="trace-1",
    )
    audit = AuditEvent(
        audit_event_id="audit-1",
        actor_type=ActorType.WORKER,
        action="policy_check",
        job_id="job-1",
        correlation_id="corr-1",
        trace_id="trace-1",
    )
    report = ReportArtifact(
        report_id="report-1",
        report_type=ReportType.EXECUTION_SUMMARY,
        path="output/execution-report.md",
        job_id="job-1",
    )

    assert instruction.instruction_type == InstructionType.CONTINUE
    assert approval.approval_scope == ApprovalScope.MUTATION
    assert observation.policy_blocks[0].code == "DRY_RUN_REQUIRED"
    assert audit.redacted is True
    assert report.report_type == ReportType.EXECUTION_SUMMARY


def test_problem_details_mapping_uses_deterministic_error_code() -> None:
    problem = problem_details_for_error(
        error_code=ErrorCode.INVALID_STATE_TRANSITION,
        title="Invalid state transition",
        status=409,
        detail="created cannot transition to executing",
        correlation_id="corr-1",
    )

    assert problem.error_code == ErrorCode.INVALID_STATE_TRANSITION
    assert problem.type == "urn:bosgenesis:mop-execution:error:INVALID_STATE_TRANSITION"
    assert problem.status == 409
