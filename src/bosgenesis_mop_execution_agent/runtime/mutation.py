"""Approved mutation execution for namespace-scoped plan steps."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from bosgenesis_mop_execution_agent.common.ids import new_id
from bosgenesis_mop_execution_agent.mcp_clients.models import McpCallResult
from bosgenesis_mop_execution_agent.models import (
    ActorType,
    AuditEvent,
    ErrorCode,
    ExecutionJob,
    ExecutionStep,
    ExternalInstruction,
    HumanApproval,
    InstructionType,
    PolicyBlock,
    PolicySeverity,
    ResourceRef,
    StepType,
)
from bosgenesis_mop_execution_agent.persistence.audit import AppendOnlyAuditWriter
from bosgenesis_mop_execution_agent.persistence.idempotency import IdempotencyRecord, stable_hash
from bosgenesis_mop_execution_agent.policy import PolicyEvaluationContext, evaluate_policy


class KubernetesMutationClient(Protocol):
    def apply(self, manifest: dict[str, Any], namespace: str) -> McpCallResult:
        """Apply a Kubernetes manifest."""


class HelmMutationClient(Protocol):
    def install_upgrade(
        self,
        *,
        release_name: str,
        chart: str,
        namespace: str,
        values: dict[str, Any] | None = None,
    ) -> McpCallResult:
        """Install or upgrade a Helm release."""


@dataclass(frozen=True)
class MutationActionResult:
    """Result of an approved mutation attempt."""

    success: bool
    action: str
    outputs: list[dict[str, Any]] = field(default_factory=list)
    policy_blocks: list[PolicyBlock] = field(default_factory=list)
    resource_mutations: list[dict[str, Any]] = field(default_factory=list)
    error_code: ErrorCode | None = None
    message: str | None = None
    unknown_mutation_outcome: bool = False


class MutationExecutor:
    """Run namespace-scoped mutations after all deterministic gates pass."""

    def __init__(
        self,
        *,
        bundle_root: str | Path,
        k8s_client: KubernetesMutationClient | None = None,
        helm_client: HelmMutationClient | None = None,
        audit_writer: AppendOnlyAuditWriter,
    ) -> None:
        self._bundle_root = Path(bundle_root)
        self._k8s_client = k8s_client
        self._helm_client = helm_client
        self._audit_writer = audit_writer

    def execute(
        self,
        *,
        job: ExecutionJob,
        step: ExecutionStep,
        approvals: list[HumanApproval],
        instructions: list[ExternalInstruction],
    ) -> MutationActionResult:
        """Evaluate gates and run the mapped mutation."""
        instruction = _matching_continue_instruction(step, instructions)
        if instruction is None:
            return MutationActionResult(
                success=False,
                action="mutation_gate",
                policy_blocks=[
                    _block(
                        "INSTRUCTION_REQUIRED",
                        "Mutation requires an explicit continue instruction.",
                        "external_instruction",
                    )
                ],
                message="mutation_instruction_required",
            )

        command = _mutation_command(step)
        if command is None:
            return MutationActionResult(
                success=False,
                action="mutation_gate",
                error_code=ErrorCode.DRY_RUN_FAILED,
                message="mutation_command_missing",
            )

        manifests_result = self._load_manifests(step)
        if isinstance(manifests_result, MutationActionResult):
            return manifests_result
        manifests = manifests_result
        resource_refs = _resource_refs(step, manifests, job.target_namespace)
        request_payload = {
            "job_id": job.job_id,
            "step_id": step.step_id,
            "instruction_id": instruction.instruction_id,
            "command": command,
            "manifest_refs": step.manifest_refs,
            "values_refs": step.values_refs,
        }
        idempotency = IdempotencyRecord(
            idempotency_key=instruction.instruction_id,
            scope="mutation",
            request_hash=stable_hash(request_payload),
            correlation_id=job.correlation_id,
            trace_id=job.trace_id,
        )
        self._write_pre_mutation_audit(job=job, step=step, command=command)
        decision = evaluate_policy(
            PolicyEvaluationContext(
                job_id=job.job_id,
                target_namespace=job.target_namespace,
                mutating=True,
                phase_id=step.phase_id,
                step_id=step.step_id,
                command=command,
                command_metadata={"step_type": step.type.value},
                resource_refs=resource_refs,
                manifests=manifests,
                approvals=approvals,
                instructions=[instruction.model_dump(mode="json")],
                dry_run_satisfied=step.dry_run_status is not None
                and step.dry_run_status.value == "dry_run_succeeded",
                idempotency_record=idempotency,
                request_payload=request_payload,
                retry_attempts=step.attempt_number,
                audit_written=True,
            )
        )
        if not decision.allowed:
            return MutationActionResult(
                success=False,
                action="mutation_gate",
                policy_blocks=decision.blocks,
                message="mutation_policy_blocked",
            )

        if step.type == StepType.K8S_APPLY:
            return self._execute_k8s_apply(job=job, manifests=manifests)
        if step.type in {StepType.HELM_INSTALL, StepType.HELM_UPGRADE}:
            return self._execute_helm(job=job, step=step)
        return MutationActionResult(
            success=False,
            action="mutation_gate",
            error_code=ErrorCode.DRY_RUN_FAILED,
            message=f"unsupported_mutation_step_type:{step.type.value}",
        )

    def _execute_k8s_apply(
        self,
        *,
        job: ExecutionJob,
        manifests: list[dict[str, Any]],
    ) -> MutationActionResult:
        if self._k8s_client is None:
            return MutationActionResult(
                success=False,
                action="k8s.apply",
                error_code=ErrorCode.MCP_UNAVAILABLE,
                message="kubernetes_mutation_client_missing",
            )
        outputs: list[dict[str, Any]] = []
        mutations: list[dict[str, Any]] = []
        for manifest in manifests:
            try:
                result = self._k8s_client.apply(manifest, job.target_namespace)
            except Exception as exc:
                return MutationActionResult(
                    success=False,
                    action="k8s.apply",
                    outputs=outputs,
                    resource_mutations=mutations,
                    error_code=ErrorCode.UNKNOWN_ERROR,
                    message=f"unknown_mutation_outcome:{type(exc).__name__}",
                    unknown_mutation_outcome=True,
                )
            outputs.append(_mcp_output(result))
            mutations.append(_mutation_record("k8s_apply", manifest=manifest))
            if not result.success:
                return MutationActionResult(
                    success=False,
                    action="k8s.apply",
                    outputs=outputs,
                    resource_mutations=mutations,
                    error_code=ErrorCode.UNKNOWN_ERROR,
                    message=result.error.message if result.error else "k8s_apply_failed",
                )
        return MutationActionResult(
            success=True,
            action="k8s.apply",
            outputs=outputs,
            resource_mutations=mutations,
        )

    def _execute_helm(self, *, job: ExecutionJob, step: ExecutionStep) -> MutationActionResult:
        if self._helm_client is None:
            return MutationActionResult(
                success=False,
                action="helm.install_upgrade",
                error_code=ErrorCode.MCP_UNAVAILABLE,
                message="helm_mutation_client_missing",
            )
        release_name, chart = _parse_helm_command(_mutation_command(step) or "")
        if not release_name or not chart:
            return MutationActionResult(
                success=False,
                action="helm.install_upgrade",
                error_code=ErrorCode.DRY_RUN_FAILED,
                message="helm_step_missing_release_or_chart",
            )
        values_result = self._load_values(step.values_refs)
        if isinstance(values_result, MutationActionResult):
            return values_result
        try:
            result = self._helm_client.install_upgrade(
                release_name=release_name,
                chart=chart,
                namespace=job.target_namespace,
                values=values_result,
            )
        except Exception as exc:
            return MutationActionResult(
                success=False,
                action="helm.install_upgrade",
                error_code=ErrorCode.UNKNOWN_ERROR,
                message=f"unknown_mutation_outcome:{type(exc).__name__}",
                unknown_mutation_outcome=True,
            )
        outputs = [_mcp_output(result)]
        mutations = [
            {
                "action": "helm_install_upgrade",
                "release_name": release_name,
                "chart": chart,
                "namespace": job.target_namespace,
            }
        ]
        if not result.success:
            return MutationActionResult(
                success=False,
                action="helm.install_upgrade",
                outputs=outputs,
                resource_mutations=mutations,
                error_code=ErrorCode.UNKNOWN_ERROR,
                message=result.error.message if result.error else "helm_install_upgrade_failed",
            )
        return MutationActionResult(
            success=True,
            action="helm.install_upgrade",
            outputs=outputs,
            resource_mutations=mutations,
        )

    def _load_manifests(
        self,
        step: ExecutionStep,
    ) -> list[dict[str, Any]] | MutationActionResult:
        if step.type != StepType.K8S_APPLY:
            return []
        if not step.manifest_refs:
            return MutationActionResult(
                success=False,
                action="k8s.apply",
                error_code=ErrorCode.BUNDLE_MISSING_REQUIRED_FILE,
                message="k8s_apply_step_missing_manifest_refs",
            )
        manifests: list[dict[str, Any]] = []
        for manifest_ref in step.manifest_refs:
            try:
                manifests.extend(self._load_yaml_documents(manifest_ref))
            except FileNotFoundError:
                return MutationActionResult(
                    success=False,
                    action="k8s.apply",
                    error_code=ErrorCode.BUNDLE_MISSING_REQUIRED_FILE,
                    message=f"manifest_not_found:{manifest_ref}",
                )
            except yaml.YAMLError as exc:
                return MutationActionResult(
                    success=False,
                    action="k8s.apply",
                    error_code=ErrorCode.PLAN_SCHEMA_INVALID,
                    message=f"invalid_yaml:{manifest_ref}:{exc.__class__.__name__}",
                )
        return manifests

    def _load_values(
        self,
        values_refs: list[str],
    ) -> dict[str, Any] | MutationActionResult:
        merged: dict[str, Any] = {}
        for values_ref in values_refs:
            try:
                documents = self._load_yaml_documents(values_ref)
            except FileNotFoundError:
                return MutationActionResult(
                    success=False,
                    action="helm.load_values",
                    error_code=ErrorCode.BUNDLE_MISSING_REQUIRED_FILE,
                    message=f"values_not_found:{values_ref}",
                )
            except yaml.YAMLError as exc:
                return MutationActionResult(
                    success=False,
                    action="helm.load_values",
                    error_code=ErrorCode.PLAN_SCHEMA_INVALID,
                    message=f"invalid_yaml:{values_ref}:{exc.__class__.__name__}",
                )
            for document in documents:
                merged.update(document)
        return merged

    def _load_yaml_documents(self, artifact_ref: str) -> list[dict[str, Any]]:
        path = self._bundle_root / artifact_ref
        if not path.is_file():
            raise FileNotFoundError(artifact_ref)
        loaded = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
        return [item for item in loaded if isinstance(item, dict)]

    def _write_pre_mutation_audit(
        self,
        *,
        job: ExecutionJob,
        step: ExecutionStep,
        command: str,
    ) -> None:
        self._audit_writer.write(
            AuditEvent(
                audit_event_id=new_id("audit"),
                actor_type=ActorType.WORKER,
                actor_id="mutation-gate",
                action="mutation_pre_event",
                job_id=job.job_id,
                correlation_id=job.correlation_id,
                trace_id=job.trace_id,
                input_hash=stable_hash({"step_id": step.step_id, "command": command}),
                details={
                    "phase_id": step.phase_id,
                    "step_id": step.step_id,
                    "target_namespace": job.target_namespace,
                },
            )
        )


def _matching_continue_instruction(
    step: ExecutionStep,
    instructions: list[ExternalInstruction],
) -> ExternalInstruction | None:
    matches = [
        instruction
        for instruction in instructions
        if instruction.instruction_type == InstructionType.CONTINUE
        and instruction.target_step_id in {None, step.step_id}
        and instruction.target_phase_id in {None, step.phase_id}
    ]
    if not matches:
        return None
    return sorted(
        matches,
        key=lambda item: (
            item.issued_at is not None,
            item.issued_at.isoformat() if item.issued_at else "",
            item.instruction_id,
        ),
    )[-1]


def _mutation_command(step: ExecutionStep) -> str | None:
    for command in step.commands:
        if command.get("mutating") is True:
            return str(command.get("command", ""))
    for command in step.commands:
        raw = str(command.get("command", ""))
        if "apply" in raw or "helm upgrade" in raw or "helm install" in raw:
            return raw
    return None


def _resource_refs(
    step: ExecutionStep,
    manifests: list[dict[str, Any]],
    target_namespace: str,
) -> list[ResourceRef]:
    refs = list(step.resource_refs)
    for manifest in manifests:
        metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
        refs.append(
            ResourceRef(
                api_version=str(manifest.get("apiVersion", "")),
                kind=str(manifest.get("kind", "")),
                namespace=str(metadata.get("namespace") or target_namespace),
                name=str(metadata.get("name", "")),
            )
        )
    if step.type in {StepType.HELM_INSTALL, StepType.HELM_UPGRADE}:
        release_name, chart = _parse_helm_command(_mutation_command(step) or "")
        refs.append(
            ResourceRef(
                kind="HelmRelease",
                namespace=target_namespace,
                name=release_name,
                helm_release_name=release_name,
                file_path=chart,
            )
        )
    return refs


def _mutation_record(action: str, *, manifest: dict[str, Any]) -> dict[str, Any]:
    metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
    return {
        "action": action,
        "api_version": manifest.get("apiVersion"),
        "kind": manifest.get("kind"),
        "namespace": metadata.get("namespace"),
        "name": metadata.get("name"),
    }


def _mcp_output(result: McpCallResult) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "server": result.server_name,
        "tool": result.tool_name,
        "success": result.success,
        "correlation_id": result.correlation_id,
        "trace_id": result.trace_id,
        "data": result.data or {},
    }
    if result.error is not None:
        payload["error"] = result.error.model_dump(mode="json")
    return payload


def _parse_helm_command(command: str) -> tuple[str | None, str | None]:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None, None
    if not parts or parts[0] != "helm":
        return None, None
    if "upgrade" in parts and "--install" in parts:
        index = parts.index("upgrade") + 1
    elif "install" in parts:
        index = parts.index("install") + 1
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


def _block(code: str, message: str, guardrail: str) -> PolicyBlock:
    return PolicyBlock(
        code=code,
        message=message,
        severity=PolicySeverity.BLOCK,
        guardrail=guardrail,
    )
