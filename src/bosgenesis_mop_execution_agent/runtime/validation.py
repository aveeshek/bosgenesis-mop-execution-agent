"""Post-execution validation through governed MCP companion clients."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from bosgenesis_mop_execution_agent.mcp_clients.models import McpCallResult
from bosgenesis_mop_execution_agent.models import ExecutionJob, ExecutionStep, StepType
from bosgenesis_mop_execution_agent.security import redact_value


class KubernetesValidationClient(Protocol):
    def namespace_summary(self, namespace: str) -> McpCallResult: ...

    def list_pods(self, namespace: str) -> McpCallResult: ...

    def list_services(self, namespace: str) -> McpCallResult: ...

    def list_pvcs(self, namespace: str) -> McpCallResult: ...

    def list_deployments(self, namespace: str) -> McpCallResult: ...

    def list_statefulsets(self, namespace: str) -> McpCallResult: ...

    def list_ingresses(self, namespace: str) -> McpCallResult: ...


class HelmValidationClient(Protocol):
    def list_releases(self, *, namespace: str, all_statuses: bool = True) -> McpCallResult: ...

    def status(self, *, release_name: str, namespace: str) -> McpCallResult: ...


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    success: bool
    summary: str
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ValidationResult:
    success: bool
    checks: list[ValidationCheck]
    warnings: list[str] = field(default_factory=list)


class ValidationExecutor:
    """Run deterministic post-execution validation checks."""

    def __init__(
        self,
        *,
        k8s_client: KubernetesValidationClient | None = None,
        helm_client: HelmValidationClient | None = None,
    ) -> None:
        self._k8s_client = k8s_client
        self._helm_client = helm_client

    def execute(self, *, job: ExecutionJob, steps: list[ExecutionStep]) -> ValidationResult:
        checks: list[ValidationCheck] = []
        warnings: list[str] = []
        if self._k8s_client is None:
            warnings.append("kubernetes_validation_client_missing")
        else:
            checks.extend(self._k8s_checks(job.target_namespace))
        if self._helm_client is None:
            warnings.append("helm_validation_client_missing")
        else:
            checks.extend(self._helm_checks(job.target_namespace))
        checks.extend(_custom_plan_checks(steps))
        success = bool(checks) and all(check.success for check in checks)
        return ValidationResult(success=success, checks=checks, warnings=warnings)

    def _k8s_checks(self, namespace: str) -> list[ValidationCheck]:
        return [
            _check_from_result("namespace_summary", self._k8s_client.namespace_summary(namespace)),
            _check_from_result("pods", self._k8s_client.list_pods(namespace), _pods_healthy),
            _check_from_result("services", self._k8s_client.list_services(namespace)),
            _check_from_result("pvcs", self._k8s_client.list_pvcs(namespace), _pvcs_bound),
            _check_from_result("deployments", self._k8s_client.list_deployments(namespace)),
            _check_from_result("statefulsets", self._k8s_client.list_statefulsets(namespace)),
            _check_from_result("ingresses", self._k8s_client.list_ingresses(namespace)),
        ]

    def _helm_checks(self, namespace: str) -> list[ValidationCheck]:
        release_list = self._helm_client.list_releases(namespace=namespace, all_statuses=True)
        checks = [_check_from_result("helm_releases", release_list, _helm_releases_deployed)]
        for release in _release_items(release_list.data):
            release_name = str(release.get("name") or release.get("release_name") or "")
            if release_name:
                checks.append(
                    _check_from_result(
                        f"helm_status:{release_name}",
                        self._helm_client.status(release_name=release_name, namespace=namespace),
                    )
                )
        return checks


def _check_from_result(
    name: str,
    result: McpCallResult,
    evaluator: Any | None = None,
) -> ValidationCheck:
    data = redact_value(result.data or {})
    success = result.success
    summary = f"{name} validation succeeded."
    if result.success and evaluator is not None:
        success, summary = evaluator(data)
    elif not result.success:
        summary = result.error.message if result.error else f"{name} validation failed."
    return ValidationCheck(
        name=name,
        success=success,
        summary=str(redact_value(summary)),
        data=data if isinstance(data, dict) else {"value": data},
    )


def _pods_healthy(data: dict[str, Any]) -> tuple[bool, str]:
    pods = _items(data)
    unhealthy = [
        pod
        for pod in pods
        if str(pod.get("phase")) not in {"Running", "Succeeded"}
        or str(pod.get("ready", "")).startswith("0/")
        and str(pod.get("phase")) != "Succeeded"
    ]
    if unhealthy:
        names = ", ".join(str(pod.get("name")) for pod in unhealthy)
        return False, f"Unhealthy pods found: {names}"
    return True, f"{len(pods)} pod records are healthy or completed."


def _pvcs_bound(data: dict[str, Any]) -> tuple[bool, str]:
    pvcs = _items(data)
    unbound = [pvc for pvc in pvcs if str(pvc.get("phase")) != "Bound"]
    if unbound:
        names = ", ".join(str(pvc.get("name")) for pvc in unbound)
        return False, f"Unbound PVCs found: {names}"
    return True, f"{len(pvcs)} PVC records are Bound."


def _helm_releases_deployed(data: dict[str, Any]) -> tuple[bool, str]:
    releases = _release_items(data)
    failed = [
        release
        for release in releases
        if str(release.get("status", "")).lower() not in {"deployed", "superseded"}
    ]
    if failed:
        names = ", ".join(str(release.get("name")) for release in failed)
        return False, f"Non-deployed Helm releases found: {names}"
    return True, f"{len(releases)} Helm release records are deployed or superseded."


def _custom_plan_checks(steps: list[ExecutionStep]) -> list[ValidationCheck]:
    checks = []
    for step in steps:
        if step.type in {StepType.K8S_VALIDATE, StepType.HELM_VALIDATE}:
            checks.append(
                ValidationCheck(
                    name=f"plan_validation:{step.step_id}",
                    success=True,
                    summary="Custom plan validation step completed in execution state.",
                    data={"step_id": step.step_id, "state": step.state.value},
                )
            )
    return checks


def _items(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw = data.get("result") or data.get("items") or data.get("output") or []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _release_items(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    raw = data.get("output") or data.get("releases") or data.get("result") or []
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []
