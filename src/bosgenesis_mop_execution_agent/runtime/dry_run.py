"""Dry-run execution mapping for Kubernetes and Helm plan steps."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from bosgenesis_mop_execution_agent.mcp_clients.models import McpCallResult
from bosgenesis_mop_execution_agent.models import ErrorCode, ExecutionJob, ExecutionStep, StepType


class KubernetesDryRunClient(Protocol):
    """Subset of Kubernetes MCP methods used by the dry-run runtime."""

    def dry_run_apply(self, manifest: dict[str, Any], namespace: str) -> McpCallResult:
        """Run server-side dry-run apply."""


class HelmDryRunClient(Protocol):
    """Subset of Helm MCP methods used by the dry-run runtime."""

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
        """Render Helm manifests without mutation."""

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
        """Run Helm install/upgrade dry-run without mutation."""


@dataclass(frozen=True)
class DryRunActionResult:
    """Result of a mapped dry-run action."""

    success: bool
    action: str
    outputs: list[dict[str, Any]] = field(default_factory=list)
    error_code: ErrorCode | None = None
    message: str | None = None


class DryRunExecutor:
    """Execute deterministic preflight actions for plan steps."""

    def __init__(
        self,
        *,
        bundle_root: str | Path,
        k8s_client: KubernetesDryRunClient | None = None,
        helm_client: HelmDryRunClient | None = None,
    ) -> None:
        self._bundle_root = Path(bundle_root)
        self._k8s_client = k8s_client
        self._helm_client = helm_client

    def execute(self, *, job: ExecutionJob, step: ExecutionStep) -> DryRunActionResult:
        """Map a plan step to one or more dry-run actions."""
        if step.type == StepType.K8S_APPLY:
            return self._execute_k8s_apply(job=job, step=step)
        if step.type in {StepType.HELM_INSTALL, StepType.HELM_UPGRADE, StepType.HELM_VALIDATE}:
            return self._execute_helm(job=job, step=step)
        return DryRunActionResult(
            success=False,
            action="unsupported",
            error_code=ErrorCode.DRY_RUN_FAILED,
            message=f"unsupported_dry_run_step_type:{step.type.value}",
        )

    def _execute_k8s_apply(
        self,
        *,
        job: ExecutionJob,
        step: ExecutionStep,
    ) -> DryRunActionResult:
        if self._k8s_client is None:
            return DryRunActionResult(
                success=False,
                action="k8s.server_side_dry_run_apply",
                error_code=ErrorCode.MCP_UNAVAILABLE,
                message="kubernetes_dry_run_client_missing",
            )
        if not step.manifest_refs:
            return DryRunActionResult(
                success=False,
                action="k8s.server_side_dry_run_apply",
                error_code=ErrorCode.BUNDLE_MISSING_REQUIRED_FILE,
                message="k8s_apply_step_missing_manifest_refs",
            )

        outputs: list[dict[str, Any]] = []
        for manifest_ref in step.manifest_refs:
            try:
                manifests = self._load_yaml_documents(manifest_ref)
            except FileNotFoundError:
                return DryRunActionResult(
                    success=False,
                    action="k8s.server_side_dry_run_apply",
                    error_code=ErrorCode.BUNDLE_MISSING_REQUIRED_FILE,
                    message=f"manifest_not_found:{manifest_ref}",
                )
            except yaml.YAMLError as exc:
                return DryRunActionResult(
                    success=False,
                    action="k8s.server_side_dry_run_apply",
                    error_code=ErrorCode.PLAN_SCHEMA_INVALID,
                    message=f"invalid_yaml:{manifest_ref}:{exc.__class__.__name__}",
                )

            for manifest in manifests:
                result = self._k8s_client.dry_run_apply(manifest, job.target_namespace)
                outputs.append(self._mcp_output(result, artifact_ref=manifest_ref))
                if not result.success:
                    return DryRunActionResult(
                        success=False,
                        action="k8s.server_side_dry_run_apply",
                        outputs=outputs,
                        error_code=result.error.error_code
                        if result.error
                        else ErrorCode.DRY_RUN_FAILED,
                        message=result.error.message if result.error else "k8s_dry_run_failed",
                    )

        return DryRunActionResult(
            success=True,
            action="k8s.server_side_dry_run_apply",
            outputs=outputs,
        )

    def _execute_helm(
        self,
        *,
        job: ExecutionJob,
        step: ExecutionStep,
    ) -> DryRunActionResult:
        if self._helm_client is None:
            return DryRunActionResult(
                success=False,
                action="helm.dry_run",
                error_code=ErrorCode.MCP_UNAVAILABLE,
                message="helm_dry_run_client_missing",
            )

        release_name, chart = self._helm_release_and_chart(step)
        if not release_name or not chart:
            return DryRunActionResult(
                success=False,
                action="helm.dry_run",
                error_code=ErrorCode.DRY_RUN_FAILED,
                message="helm_step_missing_release_or_chart",
            )

        values_result = self._load_values(step.values_refs)
        if isinstance(values_result, DryRunActionResult):
            return values_result
        values = values_result
        metadata = _helm_metadata(step)

        template_result = self._helm_client.template(
            release_name=release_name,
            chart=chart,
            namespace=job.target_namespace,
            values=values,
            version=metadata.get("chart_version"),
            repo_name=metadata.get("repo_name"),
            repo_url=metadata.get("repo_url"),
        )
        outputs = [self._mcp_output(template_result, artifact_ref=",".join(step.values_refs))]
        if not template_result.success:
            return DryRunActionResult(
                success=False,
                action="helm.template",
                outputs=outputs,
                error_code=ErrorCode.HELM_RENDER_FAILED,
                message=template_result.error.message
                if template_result.error
                else "helm_template_failed",
            )

        if step.type == StepType.HELM_VALIDATE:
            return DryRunActionResult(success=True, action="helm.template", outputs=outputs)

        dry_run_result = self._helm_client.dry_run_install_upgrade(
            release_name=release_name,
            chart=chart,
            namespace=job.target_namespace,
            values=values,
            version=metadata.get("chart_version"),
            repo_name=metadata.get("repo_name"),
            repo_url=metadata.get("repo_url"),
        )
        outputs.append(self._mcp_output(dry_run_result, artifact_ref=",".join(step.values_refs)))
        if not dry_run_result.success:
            return DryRunActionResult(
                success=False,
                action="helm.dry_run_install_upgrade",
                outputs=outputs,
                error_code=dry_run_result.error.error_code
                if dry_run_result.error
                else ErrorCode.DRY_RUN_FAILED,
                message=dry_run_result.error.message
                if dry_run_result.error
                else "helm_dry_run_failed",
            )
        return DryRunActionResult(
            success=True,
            action="helm.dry_run_install_upgrade",
            outputs=outputs,
        )

    def _load_yaml_documents(self, artifact_ref: str) -> list[dict[str, Any]]:
        path = self._bundle_root / artifact_ref
        if not path.is_file():
            raise FileNotFoundError(artifact_ref)
        loaded = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        return [item for item in loaded if isinstance(item, dict)]

    def _load_values(
        self,
        values_refs: list[str],
    ) -> dict[str, Any] | DryRunActionResult:
        merged: dict[str, Any] = {}
        for values_ref in values_refs:
            try:
                documents = self._load_yaml_documents(values_ref)
            except FileNotFoundError:
                return DryRunActionResult(
                    success=False,
                    action="helm.load_values",
                    error_code=ErrorCode.BUNDLE_MISSING_REQUIRED_FILE,
                    message=f"values_not_found:{values_ref}",
                )
            except yaml.YAMLError as exc:
                return DryRunActionResult(
                    success=False,
                    action="helm.load_values",
                    error_code=ErrorCode.PLAN_SCHEMA_INVALID,
                    message=f"invalid_yaml:{values_ref}:{exc.__class__.__name__}",
                )
            for document in documents:
                merged.update(document)
        return merged

    def _helm_release_and_chart(self, step: ExecutionStep) -> tuple[str | None, str | None]:
        metadata = _helm_metadata(step)
        release_name = metadata.get("release_name")
        chart = metadata.get("chart_ref")
        if release_name and chart:
            return release_name, chart
        for command in step.commands:
            raw = str(command.get("command", ""))
            release_name, chart = _parse_helm_command(raw)
            if release_name and chart:
                return release_name, chart
        return None, None

    def _mcp_output(self, result: McpCallResult, *, artifact_ref: str) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "server": result.server_name,
            "tool": result.tool_name,
            "success": result.success,
            "artifact_ref": artifact_ref,
            "correlation_id": result.correlation_id,
            "trace_id": result.trace_id,
            "data": result.data or {},
        }
        if result.error is not None:
            payload["error"] = result.error.model_dump(mode="json")
        return payload


def _parse_helm_command(command: str) -> tuple[str | None, str | None]:
    """Extract release and chart from common Helm command strings."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return None, None
    if not parts:
        return None, None
    if parts[0] != "helm":
        return None, None
    if "upgrade" in parts and "--install" in parts:
        index = parts.index("upgrade") + 1
    elif "install" in parts:
        index = parts.index("install") + 1
    elif "template" in parts:
        index = parts.index("template") + 1
    else:
        return None, None
    positional = [
        part
        for part in parts[index:]
        if not part.startswith("-") and part not in {"true", "false"}
    ]
    if len(positional) < 2:
        return None, None
    return positional[0], positional[1]


def _helm_metadata(step: ExecutionStep) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for key in ("release_name", "chart_ref", "chart_version", "repo_name", "repo_url"):
        value = step.metadata.get(key)
        if isinstance(value, str) and value:
            metadata[key] = value
    return metadata
