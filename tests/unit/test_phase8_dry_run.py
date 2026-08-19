from __future__ import annotations

from pathlib import Path
from typing import Any

from bosgenesis_mop_execution_agent.mcp_clients.models import (
    McpCallResult,
    McpStructuredError,
)
from bosgenesis_mop_execution_agent.models import (
    ErrorCode,
    ExecutionJob,
    ExecutionMode,
    ExecutionStep,
    JobState,
    Observation,
    ObservationSeverity,
    ObservationType,
    StepState,
    StepType,
)
from bosgenesis_mop_execution_agent.persistence import (
    InMemoryRedisLikeClient,
    NamespaceLockService,
    WorkerHeartbeatService,
)
from bosgenesis_mop_execution_agent.persistence.repositories import JsonExecutionRepository
from bosgenesis_mop_execution_agent.plans.machine_plan_parser import parse_machine_plan
from bosgenesis_mop_execution_agent.policy.command_fingerprint import command_fingerprint
from bosgenesis_mop_execution_agent.runtime import DryRunExecutor, InMemoryJobQueue, WorkerRuntime


def test_dry_run_only_e2e_uses_kubernetes_server_side_dry_run_sample_bundle(
    tmp_path: Path,
) -> None:
    bundle_root = Path("tests/fixtures/sample_mop_bundle")
    repo, runtime = _runtime(tmp_path, bundle_root=bundle_root, k8s_client=FakeK8sClient())
    job = _job(target_namespace="sample-target")
    repo.save_job(job)
    runtime.seed_plan(job.job_id, parse_machine_plan(bundle_root / "machine_execution_plan.yaml"))
    runtime.enqueue(job.job_id)

    actions = [runtime.run_once().action for _ in range(5)]

    stored = repo.get_job(job.job_id)
    assert actions == [
        "accepted",
        "plan_ready",
        "dry_run_started",
        "dry_run_step_completed",
        "completed",
    ]
    assert stored is not None
    assert stored.state == JobState.COMPLETED
    assert stored.execution_mode == ExecutionMode.DRY_RUN_ONLY
    stored_step = repo.get_steps(job.job_id)[0]
    assert stored_step.state == StepState.DRY_RUN_SUCCEEDED
    assert stored_step.command_fingerprint is not None
    assert stored_step.command_fingerprint.startswith("sha256:")
    mutation_command = next(
        str(command["command"])
        for command in stored_step.commands
        if command.get("mutating") is True
    )
    assert stored_step.command_fingerprint == command_fingerprint(
        mutation_command,
        {"step_type": stored_step.type.value},
    )
    dry_run_observations = [
        observation
        for observation in repo.get_observations(job.job_id)
        if observation.observation_type == ObservationType.DRY_RUN_RESULT
    ]
    assert dry_run_observations
    assert dry_run_observations[-1].result["mutation_performed"] is False
    assert dry_run_observations[-1].result["outputs"][0]["tool"] == (
        "manifest.server_side_dry_run_apply"
    )
    assert dry_run_observations[-1].mcp_server == "k8s"
    assert dry_run_observations[-1].mcp_tool == "manifest.server_side_dry_run_apply"
    assert "plain-secret-value" not in str(dry_run_observations[-1].result)


def test_yaml_syntax_error_fixture_pauses_before_mcp_call(tmp_path: Path) -> None:
    bundle_root = Path("tests/fixtures/phase8_yaml_error")
    k8s_client = FakeK8sClient()
    repo, runtime = _runtime(tmp_path, bundle_root=bundle_root, k8s_client=k8s_client)
    job = _job(target_namespace="signoz")
    repo.save_job(job)
    runtime.seed_plan(job.job_id, parse_machine_plan(bundle_root / "machine_execution_plan.yaml"))
    runtime.enqueue(job.job_id)

    actions = [runtime.run_once().action for _ in range(4)]

    stored = repo.get_job(job.job_id)
    assert actions[-1] == "decision_required"
    assert stored is not None
    assert stored.state == JobState.DECISION_REQUIRED
    assert k8s_client.calls == []
    latest = _latest_dry_run_observation(repo, job.job_id)
    assert latest.observation_type == ObservationType.DRY_RUN_RESULT
    assert latest.result["error_code"] == ErrorCode.PLAN_SCHEMA_INVALID.value
    assert latest.result["worker_reasoning_triggered"] is False


def test_helm_render_failure_fixture_pauses_before_dry_run_install(tmp_path: Path) -> None:
    bundle_root = Path("tests/fixtures/phase8_helm_failure")
    helm_client = FakeHelmClient(template_success=False)
    repo, runtime = _runtime(tmp_path, bundle_root=bundle_root, helm_client=helm_client)
    job = _job(target_namespace="signoz")
    repo.save_job(job)
    runtime.seed_plan(job.job_id, parse_machine_plan(bundle_root / "machine_execution_plan.yaml"))
    runtime.enqueue(job.job_id)

    actions = [runtime.run_once().action for _ in range(4)]

    stored = repo.get_job(job.job_id)
    assert actions[-1] == "decision_required"
    assert stored is not None
    assert stored.state == JobState.DECISION_REQUIRED
    assert helm_client.calls == ["template"]
    latest = _latest_dry_run_observation(repo, job.job_id)
    assert latest.result["error_code"] == ErrorCode.HELM_RENDER_FAILED.value
    assert latest.result["outputs"][0]["tool"] == "chart.template"


def test_helm_step_metadata_is_forwarded_to_helm_client(tmp_path: Path) -> None:
    values_dir = tmp_path / "values"
    values_dir.mkdir()
    (values_dir / "values-signoz.yaml").write_text("global: {}\n", encoding="utf-8")
    helm_client = FakeHelmClient()
    executor = DryRunExecutor(bundle_root=tmp_path, helm_client=helm_client)
    step = ExecutionStep(
        step_id="helm-1-signoz",
        job_id="job-1",
        phase_id="install_helm_releases",
        sequence_index=0,
        type=StepType.HELM_UPGRADE,
        values_refs=["values/values-signoz.yaml"],
        metadata={
            "release_name": "signoz",
            "chart_ref": "signoz/signoz",
            "chart_version": "0.129.0",
            "repo_name": "signoz",
            "repo_url": "https://charts.signoz.io",
        },
    )

    result = executor.execute(job=_job(target_namespace="agent-testing"), step=step)

    assert result.success is True
    assert helm_client.template_kwargs[-1] == {
        "release_name": "signoz",
        "chart": "signoz/signoz",
        "namespace": "agent-testing",
        "version": "0.129.0",
        "repo_name": "signoz",
        "repo_url": "https://charts.signoz.io",
    }
    assert helm_client.dry_run_kwargs[-1]["repo_url"] == "https://charts.signoz.io"


def test_dry_run_skips_excluded_ingress_but_checks_other_manifests(tmp_path: Path) -> None:
    manifest_path = tmp_path / "mixed.yaml"
    manifest_path.write_text(
        """apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: demo-ingress
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: demo-config
data:
  mode: demo
""",
        encoding="utf-8",
    )
    client = FakeK8sClient()
    executor = DryRunExecutor(
        bundle_root=tmp_path,
        k8s_client=client,
        excluded_kinds={"Ingress"},
    )
    step = ExecutionStep(
        step_id="mixed-step",
        job_id="job-1",
        phase_id="apply",
        sequence_index=0,
        type=StepType.K8S_APPLY,
        manifest_refs=["mixed.yaml"],
    )

    result = executor.execute(job=_job(target_namespace="agent-testing"), step=step)

    assert result.success is True
    assert [call["manifest"]["kind"] for call in client.calls] == ["ConfigMap"]
    assert result.outputs[0]["tool"] == "manifest.skip_excluded_kind"
    assert result.outputs[0]["data"]["kind"] == "Ingress"


def test_dry_run_all_excluded_ingress_manifests_need_no_k8s_client(tmp_path: Path) -> None:
    manifest_path = tmp_path / "ingress.yaml"
    manifest_path.write_text(
        """apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: demo-ingress
""",
        encoding="utf-8",
    )
    executor = DryRunExecutor(
        bundle_root=tmp_path,
        excluded_kinds={"ingress"},
    )
    step = ExecutionStep(
        step_id="ingress-step",
        job_id="job-1",
        phase_id="apply",
        sequence_index=0,
        type=StepType.K8S_APPLY,
        manifest_refs=["ingress.yaml"],
    )

    result = executor.execute(job=_job(target_namespace="agent-testing"), step=step)

    assert result.success is True
    assert result.outputs[0]["tool"] == "manifest.skip_excluded_kind"

def _runtime(
    tmp_path: Path,
    *,
    bundle_root: Path,
    k8s_client: FakeK8sClient | None = None,
    helm_client: FakeHelmClient | None = None,
) -> tuple[JsonExecutionRepository, WorkerRuntime]:
    repo = JsonExecutionRepository(tmp_path / "repo.json")
    redis_client = InMemoryRedisLikeClient()
    runtime = WorkerRuntime(
        repository=repo,
        queue=InMemoryJobQueue(repo),
        lock_service=NamespaceLockService(redis_client),
        heartbeat_service=WorkerHeartbeatService(redis_client),
        dry_runs=DryRunExecutor(
            bundle_root=bundle_root,
            k8s_client=k8s_client,
            helm_client=helm_client,
        ),
    )
    return repo, runtime


def _job(*, target_namespace: str) -> ExecutionJob:
    return ExecutionJob(
        job_id="job-1",
        bundle_id="bundle-1",
        target_namespace=target_namespace,
        execution_mode=ExecutionMode.DRY_RUN_ONLY,
    )


def _latest_dry_run_observation(
    repo: JsonExecutionRepository,
    job_id: str,
) -> Observation:
    return [
        observation
        for observation in repo.get_observations(job_id)
        if observation.observation_type == ObservationType.DRY_RUN_RESULT
    ][-1]


class FakeK8sClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def dry_run_apply(self, manifest: dict[str, Any], namespace: str) -> McpCallResult:
        self.calls.append({"manifest": manifest, "namespace": namespace})
        return _mcp_result(
            server_name="k8s",
            tool_name="manifest.server_side_dry_run_apply",
            data={
                "namespace": namespace,
                "kind": manifest["kind"],
                "name": manifest["metadata"]["name"],
                "password": "plain-secret-value",
            },
        )


class FakeHelmClient:
    def __init__(self, *, template_success: bool = True) -> None:
        self.template_success = template_success
        self.calls: list[str] = []
        self.template_kwargs: list[dict[str, Any]] = []
        self.dry_run_kwargs: list[dict[str, Any]] = []

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
        self.calls.append("template")
        self.template_kwargs.append(
            {
                "release_name": release_name,
                "chart": chart,
                "namespace": namespace,
                "version": version,
                "repo_name": repo_name,
                "repo_url": repo_url,
            }
        )
        if not self.template_success:
            return _mcp_result(
                server_name="helm",
                tool_name="chart.template",
                success=False,
                error_code=ErrorCode.HELM_RENDER_FAILED,
                message="helm_render_failed:fixture",
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
        self.calls.append("dry_run_install_upgrade")
        self.dry_run_kwargs.append(
            {
                "release_name": release_name,
                "chart": chart,
                "namespace": namespace,
                "version": version,
                "repo_name": repo_name,
                "repo_url": repo_url,
            }
        )
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
