from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from bosgenesis_mop_execution_agent.mcp_clients.models import (
    McpCallResult,
    McpStructuredError,
)
from bosgenesis_mop_execution_agent.models import (
    ErrorCode,
    ExecutionJob,
    ExecutionMode,
    ExternalInstruction,
    InstructionType,
    JobState,
    Observation,
    ObservationSeverity,
    ObservationType,
    StepState,
)
from bosgenesis_mop_execution_agent.persistence import (
    InMemoryRedisLikeClient,
    NamespaceLockService,
    WorkerHeartbeatService,
)
from bosgenesis_mop_execution_agent.persistence.repositories import JsonExecutionRepository
from bosgenesis_mop_execution_agent.plans.machine_plan_parser import parse_machine_plan
from bosgenesis_mop_execution_agent.runtime import (
    DryRunExecutor,
    InMemoryJobQueue,
    WorkerRuntime,
)

FAILURE_CASES = [
    pytest.param(
        Path("tests/fixtures/phase8_yaml_error"),
        None,
        ErrorCode.PLAN_SCHEMA_INVALID,
        id="yaml_syntax_error",
    ),
    pytest.param(
        Path("tests/fixtures/phase10_failure_loop"),
        ErrorCode.DRY_RUN_FAILED,
        ErrorCode.DRY_RUN_FAILED,
        id="dry_run_failure",
    ),
    pytest.param(
        Path("tests/fixtures/phase10_failure_loop"),
        ErrorCode.RESOURCE_ALREADY_EXISTS,
        ErrorCode.RESOURCE_ALREADY_EXISTS,
        id="resource_exists",
    ),
    pytest.param(
        Path("tests/fixtures/phase10_failure_loop"),
        ErrorCode.IMMUTABLE_FIELD_CONFLICT,
        ErrorCode.IMMUTABLE_FIELD_CONFLICT,
        id="immutable_conflict",
    ),
    pytest.param(
        Path("tests/fixtures/phase8_helm_failure"),
        ErrorCode.HELM_RENDER_FAILED,
        ErrorCode.HELM_RENDER_FAILED,
        id="helm_render_failure",
    ),
    pytest.param(
        Path("tests/fixtures/phase10_failure_loop"),
        ErrorCode.PVC_PENDING,
        ErrorCode.PVC_PENDING,
        id="pvc_pending",
    ),
    pytest.param(
        Path("tests/fixtures/phase10_failure_loop"),
        ErrorCode.POD_UNSCHEDULABLE,
        ErrorCode.POD_UNSCHEDULABLE,
        id="pod_unschedulable",
    ),
    pytest.param(
        Path("tests/fixtures/phase10_failure_loop"),
        ErrorCode.NODE_UNAVAILABLE,
        ErrorCode.NODE_UNAVAILABLE,
        id="node_unavailable",
    ),
    pytest.param(
        Path("tests/fixtures/phase10_failure_loop"),
        ErrorCode.VALIDATION_FAILED,
        ErrorCode.VALIDATION_FAILED,
        id="validation_failure",
    ),
    pytest.param(
        Path("tests/fixtures/phase10_failure_loop"),
        ErrorCode.INGRESS_CONFLICT,
        ErrorCode.INGRESS_CONFLICT,
        id="ingress_conflict",
    ),
    pytest.param(
        Path("tests/fixtures/phase10_failure_loop"),
        ErrorCode.MCP_UNAVAILABLE,
        ErrorCode.MCP_UNAVAILABLE,
        id="mcp_outage",
    ),
    pytest.param(
        Path("tests/fixtures/phase10_failure_loop"),
        ErrorCode.TIMEOUT_EXCEEDED,
        ErrorCode.TIMEOUT_EXCEEDED,
        id="timeout",
    ),
]


@pytest.mark.parametrize(("bundle_root", "injected_error", "expected_code"), FAILURE_CASES)
def test_failure_fixtures_pause_for_external_llm_without_repair(
    tmp_path: Path,
    bundle_root: Path,
    injected_error: ErrorCode | None,
    expected_code: ErrorCode,
) -> None:
    repo, runtime, fake_k8s = _runtime_for_failure(
        tmp_path,
        bundle_root=bundle_root,
        injected_error=injected_error,
    )
    job = _job(target_namespace="phase10-target" if "phase10" in str(bundle_root) else "signoz")
    repo.save_job(job)
    runtime.seed_plan(job.job_id, parse_machine_plan(bundle_root / "machine_execution_plan.yaml"))
    runtime.enqueue(job.job_id)

    for _ in range(4):
        decision = runtime.run_once()
        if decision.action == "decision_required":
            break

    stored = repo.get_job(job.job_id)
    assert stored is not None
    assert stored.state == JobState.DECISION_REQUIRED
    dry_run = _latest_observation(repo, job.job_id, ObservationType.DRY_RUN_RESULT)
    assert dry_run.result["error_code"] == expected_code.value
    assert dry_run.result["worker_reasoning_triggered"] is False
    decision_observation = _latest_observation(repo, job.job_id, ObservationType.DECISION_REQUEST)
    context = decision_observation.next_required_decision
    assert context is not None
    assert context["required_from"] == "external_llm"
    assert context["memory"]["authority"] == "context_only_not_decision_authority"
    assert context["reason_code"] == expected_code.value
    assert fake_k8s.mutation_calls == []


def test_valid_external_instruction_is_accepted_audited_and_resumes_safely(tmp_path: Path) -> None:
    bundle_root = Path("tests/fixtures/phase10_failure_loop")
    repo, runtime, _ = _runtime_for_failure(
        tmp_path,
        bundle_root=bundle_root,
        injected_error=ErrorCode.DRY_RUN_FAILED,
    )
    job = _job(target_namespace="phase10-target")
    repo.save_job(job)
    runtime.seed_plan(job.job_id, parse_machine_plan(bundle_root / "machine_execution_plan.yaml"))
    runtime.enqueue(job.job_id)
    for _ in range(4):
        if runtime.run_once().action == "decision_required":
            break

    result = runtime.submit_instruction(
        _instruction(
            instruction_id="kiro-continue-1",
            instruction_type=InstructionType.CONTINUE,
            target_step_id="apply-failure-fixture",
        )
    )

    stored = repo.get_job(job.job_id)
    step = repo.get_steps(job.job_id)[0]
    actions = [event.action for event in repo.list_audit_events(job.job_id)]
    assert result.accepted
    assert stored is not None
    assert stored.state == JobState.DRY_RUN_READY
    assert step.state == StepState.PENDING
    assert actions[-3:] == [
        "instruction_received",
        "instruction_accepted",
        "job_state_transition",
    ] or actions[-2:] == ["instruction_received", "instruction_accepted"]


def test_unsafe_external_instruction_is_policy_blocked_and_not_applied(tmp_path: Path) -> None:
    bundle_root = Path("tests/fixtures/phase10_failure_loop")
    repo, runtime, _ = _runtime_for_failure(
        tmp_path,
        bundle_root=bundle_root,
        injected_error=ErrorCode.DRY_RUN_FAILED,
    )
    job = _job(target_namespace="phase10-target")
    repo.save_job(job)
    runtime.seed_plan(job.job_id, parse_machine_plan(bundle_root / "machine_execution_plan.yaml"))
    runtime.enqueue(job.job_id)
    for _ in range(4):
        if runtime.run_once().action == "decision_required":
            break

    result = runtime.submit_instruction(
        _instruction(
            instruction_id="gpt-unsafe-patch-1",
            instruction_type=InstructionType.PATCH_MANIFEST,
            target_step_id="apply-failure-fixture",
        ).model_copy(update={"manifest_patch": {"spec": {"template": "unsafe"}}})
    )

    stored = repo.get_job(job.job_id)
    actions = [event.action for event in repo.list_audit_events(job.job_id)]
    assert not result.accepted
    assert result.status == "policy_blocked"
    assert result.policy_blocks[0].code == "UNSAFE_INSTRUCTION_BLOCKED"
    assert stored is not None
    assert stored.state == JobState.DECISION_REQUIRED
    assert "instruction_policy_blocked" in actions


def _runtime_for_failure(
    tmp_path: Path,
    *,
    bundle_root: Path,
    injected_error: ErrorCode | None,
) -> tuple[JsonExecutionRepository, WorkerRuntime, FailureK8sClient]:
    repo = JsonExecutionRepository(tmp_path / "repo.json")
    redis_client = InMemoryRedisLikeClient()
    fake_k8s = FailureK8sClient(injected_error=injected_error)
    fake_helm = FailureHelmClient(injected_error=injected_error)
    runtime = WorkerRuntime(
        repository=repo,
        queue=InMemoryJobQueue(repo),
        lock_service=NamespaceLockService(redis_client),
        heartbeat_service=WorkerHeartbeatService(redis_client),
        dry_runs=DryRunExecutor(
            bundle_root=bundle_root,
            k8s_client=fake_k8s,
            helm_client=fake_helm,
        ),
    )
    return repo, runtime, fake_k8s


def _job(*, target_namespace: str) -> ExecutionJob:
    return ExecutionJob(
        job_id="job-1",
        bundle_id="bundle-1",
        target_namespace=target_namespace,
        execution_mode=ExecutionMode.DRY_RUN_ONLY,
    )


def _instruction(
    *,
    instruction_id: str,
    instruction_type: InstructionType,
    target_step_id: str,
) -> ExternalInstruction:
    return ExternalInstruction(
        instruction_id=instruction_id,
        job_id="job-1",
        instruction_type=instruction_type,
        controller_id="codex-gpt-kiro",
        issued_by="external_llm",
        target_step_id=target_step_id,
        rationale="External LLM supplied explicit instruction.",
    )


def _latest_observation(
    repo: JsonExecutionRepository,
    job_id: str,
    observation_type: ObservationType,
) -> Observation:
    return [
        observation
        for observation in repo.get_observations(job_id)
        if observation.observation_type == observation_type
    ][-1]


class FailureK8sClient:
    def __init__(self, *, injected_error: ErrorCode | None) -> None:
        self.injected_error = injected_error
        self.mutation_calls: list[dict[str, Any]] = []

    def dry_run_apply(self, manifest: dict[str, Any], namespace: str) -> McpCallResult:
        if self.injected_error is None:
            return _mcp_result(
                server_name="k8s",
                tool_name="manifest.server_side_dry_run_apply",
                data={"manifest": manifest, "namespace": namespace},
            )
        return _mcp_result(
            server_name="k8s",
            tool_name="manifest.server_side_dry_run_apply",
            success=False,
            error_code=self.injected_error,
            message=f"{self.injected_error.value.lower()}: fake-token-value",
        )


class FailureHelmClient:
    def __init__(self, *, injected_error: ErrorCode | None) -> None:
        self.injected_error = injected_error

    def template(
        self,
        *,
        release_name: str,
        chart: str,
        namespace: str,
        values: dict[str, Any] | None = None,
        version: str | None = None,
        repo_name: str | None = None,
        repo_url: str | None = None,
    ) -> McpCallResult:
        if self.injected_error == ErrorCode.HELM_RENDER_FAILED:
            return _mcp_result(
                server_name="helm",
                tool_name="chart.template",
                success=False,
                error_code=ErrorCode.HELM_RENDER_FAILED,
                message="helm_render_failed: fake-password-value",
            )
        return _mcp_result(
            server_name="helm",
            tool_name="chart.template",
            data={"release_name": release_name, "chart": chart, "namespace": namespace},
        )

    def dry_run_install_upgrade(
        self,
        *,
        release_name: str,
        chart: str,
        namespace: str,
        values: dict[str, Any] | None = None,
        version: str | None = None,
        repo_name: str | None = None,
        repo_url: str | None = None,
    ) -> McpCallResult:
        return _mcp_result(
            server_name="helm",
            tool_name="release.dry_run_install_upgrade",
            data={"release_name": release_name, "chart": chart, "namespace": namespace},
        )


def _mcp_result(
    *,
    server_name: str,
    tool_name: str,
    success: bool = True,
    data: dict[str, Any] | None = None,
    error_code: ErrorCode | None = None,
    message: str | None = None,
) -> McpCallResult:
    error = None
    if not success:
        error = McpStructuredError(
            error_code=error_code or ErrorCode.UNKNOWN_ERROR,
            message=message or "fixture_error",
        )
    return McpCallResult(
        server_name=server_name,
        tool_name=tool_name,
        success=success,
        data=data or {},
        error=error,
        observation=Observation(
            observation_id=f"obs-{server_name}-{tool_name}",
            job_id="job-1",
            severity=ObservationSeverity.INFO if success else ObservationSeverity.ERROR,
            observation_type=ObservationType.MCP_CALL_RESULT,
            summary=f"{server_name}.{tool_name}",
            result=data or {},
        ),
    )
