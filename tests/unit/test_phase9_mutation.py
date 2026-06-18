from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bosgenesis_mop_execution_agent.mcp_clients.models import McpCallResult
from bosgenesis_mop_execution_agent.models import (
    ApprovalScope,
    ExecutionJob,
    ExecutionMode,
    ExternalInstruction,
    HumanApproval,
    InstructionType,
    JobState,
    Observation,
    ObservationSeverity,
    ObservationType,
    PhaseStatus,
    StepState,
)
from bosgenesis_mop_execution_agent.persistence import (
    AppendOnlyAuditWriter,
    InMemoryRedisLikeClient,
    NamespaceLockService,
    WorkerHeartbeatService,
)
from bosgenesis_mop_execution_agent.persistence.repositories import JsonExecutionRepository
from bosgenesis_mop_execution_agent.plans.machine_plan_parser import parse_machine_plan
from bosgenesis_mop_execution_agent.policy import command_fingerprint
from bosgenesis_mop_execution_agent.runtime import InMemoryJobQueue, MutationExecutor, WorkerRuntime

NOW = datetime(2026, 6, 18, tzinfo=UTC)
APPROVAL_EXPIRES = datetime(2030, 1, 1, tzinfo=UTC)


def test_approved_disposable_namespace_k8s_mutation_executes_after_all_gates(
    tmp_path: Path,
) -> None:
    bundle_root = Path("tests/fixtures/sample_mop_bundle")
    k8s_client = FakeK8sMutationClient()
    repo, runtime = _runtime(tmp_path, bundle_root=bundle_root, k8s_client=k8s_client)
    job = _seed_sample_job(repo, runtime, target_namespace="sample-target")
    _approve_and_continue(repo, job, step_id="apply-sample-configmap")
    runtime.enqueue(job.job_id)

    assert runtime.run_once().action == "mutation_step_completed"
    assert runtime.run_once().action == "completed"

    stored = repo.get_job(job.job_id)
    assert stored is not None
    assert stored.state == JobState.COMPLETED
    assert len(k8s_client.calls) == 1
    assert k8s_client.calls[0]["namespace"] == "sample-target"
    assert repo.get_steps(job.job_id)[0].state == StepState.MUTATION_SUCCEEDED
    mutation_observation = _latest_mutation_observation(repo, job.job_id)
    assert mutation_observation.result["resource_mutations"][0]["kind"] == "ConfigMap"
    assert any(event.action == "mutation_pre_event" for event in repo.list_audit_events(job.job_id))


def test_approved_helm_install_upgrade_mutation_uses_helm_executor(tmp_path: Path) -> None:
    bundle_root = Path("tests/fixtures/phase8_helm_failure")
    helm_client = FakeHelmMutationClient()
    repo, runtime = _runtime(tmp_path, bundle_root=bundle_root, helm_client=helm_client)
    job = _seed_job_from_plan(repo, runtime, bundle_root=bundle_root, target_namespace="signoz")
    _approve_and_continue(repo, job, step_id="helm-render-failure")
    runtime.enqueue(job.job_id)

    assert runtime.run_once().action == "mutation_step_completed"

    assert helm_client.calls == [
        {
            "release_name": "signoz",
            "chart": "signoz/signoz",
            "namespace": "signoz",
            "values": {
                "global": {"namespace": "signoz"},
                "image": {"tag": "render-failure-fixture"},
            },
        }
    ]


def test_mutation_gates_block_missing_dry_run_approval_scope_and_namespace(
    tmp_path: Path,
) -> None:
    bundle_root = tmp_path / "bundle"
    _write_bundle_with_namespace(bundle_root, namespace="other-ns")
    k8s_client = FakeK8sMutationClient()
    repo, runtime = _runtime(tmp_path, bundle_root=bundle_root, k8s_client=k8s_client)
    job = _seed_job_from_plan(repo, runtime, bundle_root=bundle_root, target_namespace="signoz")
    step = repo.get_steps(job.job_id)[0]
    repo.save_step(step.model_copy(update={"dry_run_status": None}))
    _continue(repo, job, step_id=step.step_id)
    repo.save_approval(
        _approval(job, step_id="different-step", command=_mutating_command(step.commands))
    )
    runtime.enqueue(job.job_id)

    decision = runtime.run_once()

    assert decision.action == "decision_required"
    assert k8s_client.calls == []
    blocks = {block.code for block in _latest_mutation_observation(repo, job.job_id).policy_blocks}
    assert blocks >= {
        "DRY_RUN_REQUIRED",
        "APPROVAL_SCOPE_MISMATCH",
        "RESOURCE_NAMESPACE_OUT_OF_SCOPE",
    }


def test_mutation_cannot_occur_without_continue_instruction(tmp_path: Path) -> None:
    bundle_root = Path("tests/fixtures/sample_mop_bundle")
    k8s_client = FakeK8sMutationClient()
    repo, runtime = _runtime(tmp_path, bundle_root=bundle_root, k8s_client=k8s_client)
    job = _seed_sample_job(repo, runtime, target_namespace="sample-target")
    step = repo.get_steps(job.job_id)[0]
    repo.save_approval(
        _approval(job, step_id=step.step_id, command=_mutating_command(step.commands))
    )
    runtime.enqueue(job.job_id)

    assert runtime.run_once().action == "decision_required"
    assert k8s_client.calls == []
    assert _latest_mutation_observation(repo, job.job_id).policy_blocks[0].code == (
        "INSTRUCTION_REQUIRED"
    )


def test_unknown_mutation_outcome_pauses_with_critical_observation(tmp_path: Path) -> None:
    bundle_root = Path("tests/fixtures/sample_mop_bundle")
    k8s_client = FakeK8sMutationClient(raise_on_apply=True)
    repo, runtime = _runtime(tmp_path, bundle_root=bundle_root, k8s_client=k8s_client)
    job = _seed_sample_job(repo, runtime, target_namespace="sample-target")
    _approve_and_continue(repo, job, step_id="apply-sample-configmap")
    runtime.enqueue(job.job_id)

    decision = runtime.run_once()

    observation = _latest_mutation_observation(repo, job.job_id)
    assert decision.action == "decision_required"
    assert observation.severity == ObservationSeverity.CRITICAL
    assert observation.result["unknown_mutation_outcome"] is True
    assert observation.result["worker_reasoning_triggered"] is False


def test_duplicate_continue_instruction_idempotency_replays_same_request(tmp_path: Path) -> None:
    bundle_root = Path("tests/fixtures/sample_mop_bundle")
    k8s_client = FakeK8sMutationClient()
    repo, runtime = _runtime(tmp_path, bundle_root=bundle_root, k8s_client=k8s_client)
    job = _seed_sample_job(repo, runtime, target_namespace="sample-target")
    _approve_and_continue(repo, job, step_id="apply-sample-configmap", instruction_id="same-id")
    _approve_and_continue(repo, job, step_id="apply-sample-configmap", instruction_id="same-id")
    runtime.enqueue(job.job_id)

    assert runtime.run_once().action == "mutation_step_completed"
    assert len(k8s_client.calls) == 1


def _runtime(
    tmp_path: Path,
    *,
    bundle_root: Path,
    k8s_client: FakeK8sMutationClient | None = None,
    helm_client: FakeHelmMutationClient | None = None,
) -> tuple[JsonExecutionRepository, WorkerRuntime]:
    repo = JsonExecutionRepository(tmp_path / "repo.json")
    redis_client = InMemoryRedisLikeClient()
    runtime = WorkerRuntime(
        repository=repo,
        queue=InMemoryJobQueue(repo),
        lock_service=NamespaceLockService(redis_client),
        heartbeat_service=WorkerHeartbeatService(redis_client),
        mutations=MutationExecutor(
            bundle_root=bundle_root,
            k8s_client=k8s_client,
            helm_client=helm_client,
            audit_writer=AppendOnlyAuditWriter(repo),
        ),
    )
    return repo, runtime


def _seed_sample_job(
    repo: JsonExecutionRepository,
    runtime: WorkerRuntime,
    *,
    target_namespace: str,
) -> ExecutionJob:
    return _seed_job_from_plan(
        repo,
        runtime,
        bundle_root=Path("tests/fixtures/sample_mop_bundle"),
        target_namespace=target_namespace,
    )


def _seed_job_from_plan(
    repo: JsonExecutionRepository,
    runtime: WorkerRuntime,
    *,
    bundle_root: Path,
    target_namespace: str,
) -> ExecutionJob:
    job = ExecutionJob(
        job_id="job-1",
        bundle_id="bundle-1",
        target_namespace=target_namespace,
        execution_mode=ExecutionMode.EXECUTE_AFTER_APPROVAL,
        state=JobState.EXECUTING,
        dry_run_satisfied=True,
    )
    repo.save_job(job)
    runtime.seed_plan(job.job_id, parse_machine_plan(bundle_root / "machine_execution_plan.yaml"))
    for phase in repo.get_phases(job.job_id):
        repo.save_phase(phase.model_copy(update={"status": PhaseStatus.COMPLETED}))
    for step in repo.get_steps(job.job_id):
        repo.save_step(
            step.model_copy(
                update={
                    "state": StepState.DRY_RUN_SUCCEEDED,
                    "dry_run_status": StepState.DRY_RUN_SUCCEEDED,
                }
            )
        )
    return job


def _approve_and_continue(
    repo: JsonExecutionRepository,
    job: ExecutionJob,
    *,
    step_id: str,
    instruction_id: str = "instruction-1",
) -> None:
    step = next(step for step in repo.get_steps(job.job_id) if step.step_id == step_id)
    _continue(repo, job, step_id=step_id, instruction_id=instruction_id)
    repo.save_approval(_approval(job, step_id=step_id, command=_mutating_command(step.commands)))


def _continue(
    repo: JsonExecutionRepository,
    job: ExecutionJob,
    *,
    step_id: str,
    instruction_id: str = "instruction-1",
) -> None:
    repo.save_instruction(
        ExternalInstruction(
            instruction_id=instruction_id,
            job_id=job.job_id,
            instruction_type=InstructionType.CONTINUE,
            controller_id="codex",
            issued_by="codex",
            issued_at=NOW,
            target_step_id=step_id,
        )
    )


def _approval(job: ExecutionJob, *, step_id: str, command: str) -> HumanApproval:
    return HumanApproval(
        approval_id=f"approval-{step_id}",
        job_id=job.job_id,
        approver_id="operator@example.com",
        approval_scope=ApprovalScope.MUTATION,
        ticket_reference="CHG-1",
        statement="Approved disposable namespace mutation.",
        expires_at=APPROVAL_EXPIRES,
        approved_step_ids=[step_id],
        command_fingerprint=command_fingerprint(
            command,
            {"step_type": "helm_install" if command.startswith("helm ") else "k8s_apply"},
        ),
    )


def _mutating_command(commands: list[dict[str, Any]]) -> str:
    return next(str(command["command"]) for command in commands if command.get("mutating") is True)


def _latest_mutation_observation(repo: JsonExecutionRepository, job_id: str) -> Observation:
    return [
        observation
        for observation in repo.get_observations(job_id)
        if observation.observation_type == ObservationType.MUTATION_RESULT
    ][-1]


def _write_bundle_with_namespace(bundle_root: Path, *, namespace: str) -> None:
    generated = bundle_root / "generated"
    generated.mkdir(parents=True)
    (generated / "configmap.yaml").write_text(
        "\n".join(
            [
                "apiVersion: v1",
                "kind: ConfigMap",
                "metadata:",
                "  name: blocked-config",
                f"  namespace: {namespace}",
                "data:",
                "  mode: blocked",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (bundle_root / "machine_execution_plan.yaml").write_text(
        "\n".join(
            [
                'schema_version: "0.1.0"',
                "target_namespace: signoz",
                "phases:",
                "  - phase_id: apply_configmaps",
                "    objective: blocked namespace",
                "    steps:",
                "      - step_id: apply-blocked-configmap",
                "        title: Apply blocked ConfigMap",
                "        type: k8s_apply",
                "        manifest_refs:",
                "          - generated/configmap.yaml",
                "        commands:",
                "          - kind: dry_run",
                "            command: k8s.server_side_dry_run_apply generated/configmap.yaml",
                "            dry_run: true",
                "            mutating: false",
                "          - kind: apply",
                "            command: kubectl apply -f generated/configmap.yaml -n signoz",
                "            dry_run: false",
                "            mutating: true",
                "",
            ]
        ),
        encoding="utf-8",
    )


class FakeK8sMutationClient:
    def __init__(self, *, raise_on_apply: bool = False) -> None:
        self.raise_on_apply = raise_on_apply
        self.calls: list[dict[str, Any]] = []

    def apply(self, manifest: dict[str, Any], namespace: str) -> McpCallResult:
        self.calls.append({"manifest": manifest, "namespace": namespace})
        if self.raise_on_apply:
            raise TimeoutError("lost response after mutation request")
        return _mcp_result(
            server_name="k8s",
            tool_name="manifest.apply",
            data={
                "namespace": namespace,
                "kind": manifest["kind"],
                "name": manifest["metadata"]["name"],
            },
        )


class FakeHelmMutationClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def install_upgrade(
        self,
        *,
        release_name: str,
        chart: str,
        namespace: str,
        values: dict[str, Any] | None = None,
    ) -> McpCallResult:
        self.calls.append(
            {
                "release_name": release_name,
                "chart": chart,
                "namespace": namespace,
                "values": values or {},
            }
        )
        return _mcp_result(
            server_name="helm",
            tool_name="release.install_upgrade",
            data={"release_name": release_name, "chart": chart, "namespace": namespace},
        )


def _mcp_result(
    *,
    server_name: str,
    tool_name: str,
    data: dict[str, Any],
) -> McpCallResult:
    return McpCallResult(
        server_name=server_name,
        tool_name=tool_name,
        success=True,
        data=data,
        observation=Observation(
            observation_id=f"obs-{server_name}-{tool_name}",
            job_id="job-1",
            severity=ObservationSeverity.INFO,
            observation_type=ObservationType.MCP_CALL_RESULT,
            summary=f"{server_name}.{tool_name}",
            result=data,
        ),
    )
