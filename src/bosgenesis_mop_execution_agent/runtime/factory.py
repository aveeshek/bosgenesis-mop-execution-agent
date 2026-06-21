"""Runtime construction helpers shared by API, worker, and reconciler entrypoints."""

from __future__ import annotations

import os
from pathlib import Path

from bosgenesis_mop_execution_agent.models import ExecutionJob
from bosgenesis_mop_execution_agent.persistence import (
    AppendOnlyAuditWriter,
    InMemoryRedisLikeClient,
    NamespaceLockService,
    WorkerHeartbeatService,
)
from bosgenesis_mop_execution_agent.persistence.repositories import JsonExecutionRepository
from bosgenesis_mop_execution_agent.runtime.dry_run import DryRunExecutor
from bosgenesis_mop_execution_agent.runtime.mcp_rest_adapters import (
    HelmManagerRestDryRunClient,
    KubernetesInspectorRestDryRunClient,
)
from bosgenesis_mop_execution_agent.runtime.mutation import MutationExecutor
from bosgenesis_mop_execution_agent.runtime.queue import InMemoryJobQueue
from bosgenesis_mop_execution_agent.runtime.worker import WorkerRuntime

BUNDLE_ROOT_LINK_KEY = "bundle_root_path"


def create_worker_runtime(
    *,
    repository: JsonExecutionRepository,
    queue: InMemoryJobQueue,
    redis_client: InMemoryRedisLikeClient,
    worker_id: str,
) -> WorkerRuntime:
    """Build a worker runtime that creates MCP-backed executors per persisted job."""
    return WorkerRuntime(
        repository=repository,
        queue=queue,
        lock_service=NamespaceLockService(redis_client),
        heartbeat_service=WorkerHeartbeatService(redis_client),
        worker_id=worker_id,
        dry_run_factory=lambda job: create_dry_run_executor(job),
        mutation_factory=lambda job: create_mutation_executor(job, repository),
    )


def create_dry_run_executor(job: ExecutionJob) -> DryRunExecutor | None:
    root = bundle_root_from_job(job)
    if root is None:
        return None
    k8s_client, helm_client = _mcp_clients(job)
    return DryRunExecutor(bundle_root=root, k8s_client=k8s_client, helm_client=helm_client)


def create_mutation_executor(
    job: ExecutionJob,
    repository: JsonExecutionRepository,
) -> MutationExecutor | None:
    root = bundle_root_from_job(job)
    if root is None:
        return None
    k8s_client, helm_client = _mcp_clients(job)
    return MutationExecutor(
        bundle_root=root,
        k8s_client=k8s_client,
        helm_client=helm_client,
        audit_writer=AppendOnlyAuditWriter(repository),
    )


def bundle_root_from_job(job: ExecutionJob) -> Path | None:
    raw = job.links.get(BUNDLE_ROOT_LINK_KEY) or job.links.get("artifact_bundle_root")
    if raw is None:
        return None
    root = Path(str(raw))
    return root if root.exists() else None


def with_bundle_root_link(job: ExecutionJob, bundle_root: Path) -> ExecutionJob:
    links = dict(job.links)
    links[BUNDLE_ROOT_LINK_KEY] = str(bundle_root)
    return job.model_copy(update={"links": links})


def _mcp_clients(
    job: ExecutionJob,
) -> tuple[KubernetesInspectorRestDryRunClient, HelmManagerRestDryRunClient]:
    k8s_client = KubernetesInspectorRestDryRunClient(
        base_url=os.getenv(
            "K8S_INSPECTOR_MCP_ENDPOINT",
            "http://bosgenesis-k8s-inspector-mcp:8080",
        ),
        api_key=os.getenv("K8S_INSPECTOR_API_KEY") or os.getenv("BOSGENESIS_API_KEY"),
        job_id=job.job_id,
        correlation_id=job.correlation_id,
        trace_id=job.trace_id,
    )
    helm_client = HelmManagerRestDryRunClient(
        base_url=os.getenv(
            "HELM_MANAGER_MCP_ENDPOINT",
            "http://bosgenesis-helm-manager-mcp:8080",
        ),
        api_key=os.getenv("HELM_MANAGER_API_KEY") or os.getenv("BOSGENESIS_API_KEY"),
        job_id=job.job_id,
        timeout_seconds=_env_float("HELM_MANAGER_REST_TIMEOUT_SECONDS", default=30.0),
        helm_operation_timeout=os.getenv("HELM_MANAGER_OPERATION_TIMEOUT"),
        mutation_wait=_env_bool("HELM_MANAGER_MUTATION_WAIT", default=True),
        mutation_atomic=_env_bool("HELM_MANAGER_MUTATION_ATOMIC", default=True),
        correlation_id=job.correlation_id,
        trace_id=job.trace_id,
    )
    return k8s_client, helm_client


def _env_float(name: str, *, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
