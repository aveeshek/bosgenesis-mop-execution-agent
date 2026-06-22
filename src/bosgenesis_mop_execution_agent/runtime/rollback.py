"""Rollback and namespace revert executors."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from bosgenesis_mop_execution_agent.mcp_clients.models import McpCallResult
from bosgenesis_mop_execution_agent.models import (
    ApprovalScope,
    ExecutionJob,
    ExternalInstruction,
    HumanApproval,
    InstructionType,
)
from bosgenesis_mop_execution_agent.security import redact_value


class KubernetesRollbackClient(Protocol):
    def delete_collection(
        self,
        *,
        resource: str,
        namespace: str,
        dry_run: bool = False,
        label_selector: str | None = None,
        field_selector: str | None = None,
    ) -> McpCallResult: ...


class HelmRollbackClient(Protocol):
    def list_releases(self, *, namespace: str, all_statuses: bool = True) -> McpCallResult: ...

    def rollback(
        self,
        *,
        release_name: str,
        namespace: str,
        revision: int,
        dry_run: bool = False,
    ) -> McpCallResult: ...

    def uninstall(
        self,
        *,
        release_name: str,
        namespace: str,
        dry_run: bool = False,
        keep_history: bool = False,
        force_purge_release_storage: bool = False,
    ) -> McpCallResult: ...


@dataclass(frozen=True)
class RollbackStepResult:
    action: str
    success: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RollbackResult:
    success: bool
    steps: list[RollbackStepResult]
    warnings: list[str] = field(default_factory=list)


ROLLBACK_SCOPES = {ApprovalScope.ROLLBACK, ApprovalScope.DESTRUCTIVE_ROLLBACK}
NAMESPACE_REVERT_RESOURCES = [
    "ingresses",
    "deployments",
    "statefulsets",
    "daemonsets",
    "jobs",
    "cronjobs",
    "services",
    "configmaps",
    "serviceaccounts",
    "persistentvolumeclaims",
    "pods",
]


class RollbackExecutor:
    """Run deterministic rollback/revert operations through governed clients."""

    def __init__(
        self,
        *,
        k8s_client: KubernetesRollbackClient | None = None,
        helm_client: HelmRollbackClient | None = None,
    ) -> None:
        self._k8s_client = k8s_client
        self._helm_client = helm_client

    def execute(
        self,
        *,
        job: ExecutionJob,
        approvals: list[HumanApproval],
        instructions: list[ExternalInstruction],
        mode: str = "namespace_revert",
        dry_run: bool = False,
        release_name: str | None = None,
        revision: int | None = None,
        force_purge_release_storage: bool = True,
    ) -> RollbackResult:
        auth_warnings = _authorization_warnings(approvals, instructions)
        if auth_warnings:
            return RollbackResult(success=False, steps=[], warnings=auth_warnings)
        return self.revert_namespace(
            job=job,
            mode=mode,
            dry_run=dry_run,
            release_name=release_name,
            revision=revision,
            force_purge_release_storage=force_purge_release_storage,
        )

    def revert_namespace(
        self,
        *,
        job: ExecutionJob,
        mode: str = "namespace_revert",
        dry_run: bool = False,
        release_name: str | None = None,
        revision: int | None = None,
        force_purge_release_storage: bool = True,
    ) -> RollbackResult:
        steps: list[RollbackStepResult] = []
        warnings: list[str] = []
        if self._helm_client is None:
            warnings.append("helm_rollback_client_missing")
        else:
            releases = self._release_names(job.target_namespace)
            for name in releases:
                if release_name and name != release_name:
                    continue
                if mode == "helm_revision_rollback" and revision is not None:
                    result = self._helm_client.rollback(
                        release_name=name,
                        namespace=job.target_namespace,
                        revision=revision,
                        dry_run=dry_run,
                    )
                    steps.append(_step("helm.rollback", result))
                else:
                    result = self._helm_client.uninstall(
                        release_name=name,
                        namespace=job.target_namespace,
                        dry_run=dry_run,
                        keep_history=False,
                        force_purge_release_storage=force_purge_release_storage,
                    )
                    steps.append(_step("helm.uninstall", result))
        if self._k8s_client is None:
            warnings.append("kubernetes_rollback_client_missing")
        else:
            for resource in NAMESPACE_REVERT_RESOURCES:
                result = self._k8s_client.delete_collection(
                    resource=resource,
                    namespace=job.target_namespace,
                    dry_run=dry_run,
                    field_selector=f"metadata.namespace={job.target_namespace}",
                )
                steps.append(_step(f"k8s.delete_collection:{resource}", result))
        return RollbackResult(
            success=bool(steps) and all(step.success for step in steps),
            steps=steps,
            warnings=warnings,
        )

    def _release_names(self, namespace: str) -> list[str]:
        if self._helm_client is None:
            return []
        result = self._helm_client.list_releases(namespace=namespace, all_statuses=True)
        if not result.success:
            return []
        releases = result.data.get("output") if isinstance(result.data, dict) else []
        if not isinstance(releases, list):
            return []
        return [
            str(item["name"])
            for item in releases
            if isinstance(item, dict) and item.get("name")
        ]


def _authorization_warnings(
    approvals: list[HumanApproval],
    instructions: list[ExternalInstruction],
) -> list[str]:
    warnings = []
    if not any(approval.approval_scope in ROLLBACK_SCOPES for approval in approvals):
        warnings.append("rollback_approval_required")
    if not any(
        instruction.instruction_type == InstructionType.ROLLBACK
        for instruction in instructions
    ):
        warnings.append("external_rollback_instruction_required")
    return warnings


def _step(action: str, result: McpCallResult) -> RollbackStepResult:
    data = redact_value(result.data or {})
    return RollbackStepResult(
        action=action,
        success=result.success,
        summary="succeeded"
        if result.success
        else (result.error.message if result.error else "failed"),
        data=data if isinstance(data, dict) else {"value": data},
    )
