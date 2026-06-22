"""REST adapters for governed MCP companion services used by the runtime."""

from __future__ import annotations

from typing import Any

import httpx

from bosgenesis_mop_execution_agent.common.ids import new_id
from bosgenesis_mop_execution_agent.mcp_clients.models import McpCallResult, McpStructuredError
from bosgenesis_mop_execution_agent.models import (
    ErrorCode,
    Observation,
    ObservationSeverity,
    ObservationType,
)
from bosgenesis_mop_execution_agent.security import redact_value


class HttpMcpCompatibilityTransport:
    """Small transport for MCP servers exposing /mcp/tools/{tool_name} compatibility APIs."""

    def __init__(self, *, base_url: str, api_key: str | None = None) -> None:
        self._base_url = _rest_base_url(base_url)
        self._api_key = api_key

    def call_tool(
        self,
        *,
        server_name: str,
        tool_name: str,
        arguments: dict[str, Any],
        timeout_seconds: float,
        correlation_id: str | None,
        trace_id: str | None,
    ) -> dict[str, Any]:
        headers = {"X-API-Key": self._api_key} if self._api_key else {}
        payload = {
            **arguments,
            "correlation_id": correlation_id,
            "trace_id": trace_id,
        }
        try:
            with httpx.Client(timeout=timeout_seconds) as client:
                response = client.post(
                    f"{self._base_url}/mcp/tools/{tool_name}",
                    json=payload,
                    headers=headers,
                )
        except httpx.TimeoutException:
            return _compat_error(ErrorCode.TIMEOUT_EXCEEDED, f"mcp_timeout:{tool_name}")
        except httpx.HTTPError as exc:
            return _compat_error(
                ErrorCode.MCP_UNAVAILABLE,
                f"mcp_unavailable:{server_name}:{type(exc).__name__}",
            )
        data = _response_data(response)
        if not response.is_success:
            return _compat_error(
                _error_code_from_response(response, data),
                _message_from_response(response, data),
            )
        if data.get("ok") is True:
            nested = data.get("data", {})
            return {"ok": True, "data": nested if isinstance(nested, dict) else {"value": nested}}
        if data.get("ok") is False:
            error = data.get("error")
            return {
                "ok": False,
                "error": error if isinstance(error, dict) else {"message": str(error)},
            }
        return {"ok": True, "data": data}


class KubernetesInspectorRestDryRunClient:
    """Dry-run client backed by the K8s Inspector REST API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        job_id: str,
        timeout_seconds: float = 30.0,
        correlation_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        self._base_url = _rest_base_url(base_url)
        self._api_key = api_key
        self._job_id = job_id
        self._timeout_seconds = timeout_seconds
        self._correlation_id = correlation_id
        self._trace_id = trace_id

    def dry_run_apply(self, manifest: dict[str, Any], namespace: str) -> McpCallResult:
        manifest = _manifest_for_namespace(manifest, namespace)
        payload = {
            "manifest": manifest,
            "dry_run": True,
            "actor": "bosgenesis-mop-execution-agent",
            "correlation_id": self._correlation_id,
        }
        return self._post(
            "/apply",
            payload,
            server_name="bosgenesis_k8s",
            tool_name="manifest.server_side_dry_run_apply",
        )

    def apply(self, manifest: dict[str, Any], namespace: str) -> McpCallResult:
        manifest = _manifest_for_namespace(manifest, namespace)
        payload = {
            "manifest": manifest,
            "dry_run": False,
            "actor": "bosgenesis-mop-execution-agent",
            "correlation_id": self._correlation_id,
        }
        return self._post(
            "/apply",
            payload,
            server_name="bosgenesis_k8s",
            tool_name="manifest.apply",
        )

    def namespace_summary(self, namespace: str) -> McpCallResult:
        return self._get(
            "/namespace/summary",
            {"namespace": namespace, "actor": "bosgenesis-mop-execution-agent"},
            server_name="bosgenesis_k8s",
            tool_name="namespace.summary",
        )

    def list_pods(self, namespace: str) -> McpCallResult:
        return self._get_collection("/pods", namespace, "pod.list")

    def list_services(self, namespace: str) -> McpCallResult:
        return self._get_collection("/services", namespace, "service.list")

    def list_pvcs(self, namespace: str) -> McpCallResult:
        return self._get_collection("/pvcs", namespace, "pvc.list")

    def list_deployments(self, namespace: str) -> McpCallResult:
        return self._get_collection("/deployments", namespace, "deployment.list")

    def list_statefulsets(self, namespace: str) -> McpCallResult:
        return self._get_collection("/statefulsets", namespace, "statefulset.list")

    def list_ingresses(self, namespace: str) -> McpCallResult:
        return self._get_collection("/ingresses", namespace, "ingress.list")

    def delete_collection(
        self,
        *,
        resource: str,
        namespace: str,
        dry_run: bool = False,
        label_selector: str | None = None,
        field_selector: str | None = None,
    ) -> McpCallResult:
        payload = {
            "resource": resource,
            "namespace": namespace,
            "dry_run": dry_run,
            "label_selector": label_selector,
            "field_selector": field_selector,
            "actor": "bosgenesis-mop-execution-agent",
            "correlation_id": self._correlation_id,
        }
        return self._post(
            "/deletecollection",
            payload,
            server_name="bosgenesis_k8s",
            tool_name=f"{resource}.delete_collection",
        )

    def _get_collection(self, path: str, namespace: str, tool_name: str) -> McpCallResult:
        return self._get(
            path,
            {"namespace": namespace, "actor": "bosgenesis-mop-execution-agent"},
            server_name="bosgenesis_k8s",
            tool_name=tool_name,
        )

    def _get(
        self,
        path: str,
        params: dict[str, Any],
        *,
        server_name: str,
        tool_name: str,
    ) -> McpCallResult:
        headers = {"X-API-Key": self._api_key} if self._api_key else {}
        try:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.get(f"{self._base_url}{path}", params=params, headers=headers)
        except httpx.TimeoutException:
            return self._failure(
                server_name,
                tool_name,
                ErrorCode.TIMEOUT_EXCEEDED,
                f"mcp_timeout:{tool_name}",
                retryable=True,
            )
        except httpx.HTTPError as exc:
            return self._failure(
                server_name,
                tool_name,
                ErrorCode.MCP_UNAVAILABLE,
                f"mcp_unavailable:{tool_name}:{type(exc).__name__}",
                retryable=True,
            )
        data = _response_data(response)
        if response.is_success:
            return self._success(server_name, tool_name, data)
        return self._failure(
            server_name,
            tool_name,
            _error_code_from_response(response, data),
            _message_from_response(response, data),
            data=data,
        )

    def _post(
        self,
        path: str,
        payload: dict[str, Any],
        *,
        server_name: str,
        tool_name: str,
    ) -> McpCallResult:
        headers = {"X-API-Key": self._api_key} if self._api_key else {}
        try:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.post(f"{self._base_url}{path}", json=payload, headers=headers)
        except httpx.TimeoutException:
            return self._failure(
                server_name,
                tool_name,
                ErrorCode.TIMEOUT_EXCEEDED,
                f"mcp_timeout:{tool_name}",
                retryable=True,
            )
        except httpx.HTTPError as exc:
            return self._failure(
                server_name,
                tool_name,
                ErrorCode.MCP_UNAVAILABLE,
                f"mcp_unavailable:{tool_name}:{type(exc).__name__}",
                retryable=True,
            )

        data = _response_data(response)
        if response.is_success:
            return self._success(server_name, tool_name, data)
        return self._failure(
            server_name,
            tool_name,
            _error_code_from_response(response, data),
            _message_from_response(response, data),
            data=data,
        )

    def _success(self, server_name: str, tool_name: str, data: dict[str, Any]) -> McpCallResult:
        observation = _observation(
            job_id=self._job_id,
            server_name=server_name,
            tool_name=tool_name,
            severity=ObservationSeverity.INFO,
            summary=f"MCP REST call succeeded: {server_name}.{tool_name}",
            result={"success": True, "data": data},
            correlation_id=self._correlation_id,
            trace_id=self._trace_id,
        )
        return McpCallResult(
            server_name=server_name,
            tool_name=tool_name,
            success=True,
            data=data,
            correlation_id=self._correlation_id,
            trace_id=self._trace_id,
            observation=observation,
        )

    def _failure(
        self,
        server_name: str,
        tool_name: str,
        error_code: ErrorCode,
        message: str,
        *,
        retryable: bool = False,
        data: dict[str, Any] | None = None,
    ) -> McpCallResult:
        error = McpStructuredError(
            error_code=error_code,
            message=str(redact_value(message)),
            retryable=retryable,
            raw_type=error_code.value,
        )
        observation = _observation(
            job_id=self._job_id,
            server_name=server_name,
            tool_name=tool_name,
            severity=ObservationSeverity.ERROR,
            summary=f"MCP REST call failed: {server_name}.{tool_name}",
            result={
                "success": False,
                "error": error.model_dump(mode="json"),
                "data": data or {},
                "worker_reasoning_triggered": False,
            },
            correlation_id=self._correlation_id,
            trace_id=self._trace_id,
        )
        return McpCallResult(
            server_name=server_name,
            tool_name=tool_name,
            success=False,
            error=error,
            correlation_id=self._correlation_id,
            trace_id=self._trace_id,
            observation=observation,
        )


class HelmManagerRestDryRunClient:
    """Dry-run client backed by the Helm Manager REST API."""

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        job_id: str,
        timeout_seconds: float = 30.0,
        helm_operation_timeout: str | None = None,
        mutation_wait: bool = True,
        mutation_atomic: bool = True,
        correlation_id: str | None = None,
        trace_id: str | None = None,
    ) -> None:
        self._base_url = _rest_base_url(base_url)
        self._api_key = api_key
        self._job_id = job_id
        self._timeout_seconds = timeout_seconds
        self._helm_operation_timeout = helm_operation_timeout
        self._mutation_wait = mutation_wait
        self._mutation_atomic = mutation_atomic
        self._correlation_id = correlation_id
        self._trace_id = trace_id

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
        repo_result = self._ensure_repo(repo_name=repo_name, repo_url=repo_url)
        if repo_result is not None and not repo_result.success:
            return repo_result
        payload = {
            "release_name": release_name,
            "chart_ref": chart,
            "namespace": namespace,
            "version": version,
            "values": values or {},
            "actor": "bosgenesis-mop-execution-agent",
            "correlation_id": self._correlation_id,
        }
        return self._post("/charts/template", payload, "helm.template")

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
        repo_result = self._ensure_repo(repo_name=repo_name, repo_url=repo_url)
        if repo_result is not None and not repo_result.success:
            return repo_result
        payload = {
            "release_name": release_name,
            "chart_ref": chart,
            "namespace": namespace,
            "version": version,
            "values": values or {},
            "dry_run": True,
            "install": True,
            "wait": self._mutation_wait,
            "atomic": self._mutation_atomic,
            "timeout": self._helm_operation_timeout,
            "actor": "bosgenesis-mop-execution-agent",
            "correlation_id": self._correlation_id,
        }
        return self._post("/releases/upgrade", payload, "helm.dry_run_install_upgrade")

    def install_upgrade(
        self,
        *,
        release_name: str,
        chart: str,
        namespace: str,
        values: dict[str, Any] | None = None,
        version: str | None = None,
        repo_name: str | None = None,
        repo_url: str | None = None,
        timeout: str | None = None,
    ) -> McpCallResult:
        repo_result = self._ensure_repo(repo_name=repo_name, repo_url=repo_url)
        if repo_result is not None and not repo_result.success:
            return repo_result
        payload = {
            "release_name": release_name,
            "chart_ref": chart,
            "namespace": namespace,
            "version": version,
            "values": values or {},
            "dry_run": False,
            "install": True,
            "wait": self._mutation_wait,
            "atomic": self._mutation_atomic,
            "timeout": timeout or self._helm_operation_timeout,
            "actor": "bosgenesis-mop-execution-agent",
            "correlation_id": self._correlation_id,
        }
        return self._post("/releases/upgrade", payload, "helm.install_upgrade")

    def list_releases(self, *, namespace: str, all_statuses: bool = True) -> McpCallResult:
        return self._get(
            "/releases",
            {
                "namespace": namespace,
                "all_statuses": all_statuses,
                "actor": "bosgenesis-mop-execution-agent",
            },
            "helm.list",
        )

    def status(self, *, release_name: str, namespace: str) -> McpCallResult:
        return self._get(
            f"/releases/{release_name}/status",
            {"namespace": namespace, "actor": "bosgenesis-mop-execution-agent"},
            "helm.status",
        )

    def rollback(
        self,
        *,
        release_name: str,
        namespace: str,
        revision: int,
        dry_run: bool = False,
    ) -> McpCallResult:
        payload = {
            "release_name": release_name,
            "namespace": namespace,
            "revision": revision,
            "dry_run": dry_run,
            "wait": self._mutation_wait,
            "timeout": self._helm_operation_timeout,
            "actor": "bosgenesis-mop-execution-agent",
            "correlation_id": self._correlation_id,
        }
        return self._post("/releases/rollback", payload, "helm.rollback")

    def uninstall(
        self,
        *,
        release_name: str,
        namespace: str,
        dry_run: bool = False,
        keep_history: bool = False,
        force_purge_release_storage: bool = False,
    ) -> McpCallResult:
        payload = {
            "release_name": release_name,
            "namespace": namespace,
            "dry_run": dry_run,
            "keep_history": keep_history,
            "force_purge_release_storage": force_purge_release_storage,
            "wait": self._mutation_wait,
            "timeout": self._helm_operation_timeout,
            "actor": "bosgenesis-mop-execution-agent",
            "correlation_id": self._correlation_id,
        }
        return self._post("/releases/uninstall", payload, "helm.uninstall")

    def _ensure_repo(
        self,
        *,
        repo_name: str | None,
        repo_url: str | None,
    ) -> McpCallResult | None:
        if not repo_name or not repo_url:
            return None
        payload = {
            "name": repo_name,
            "url": repo_url,
            "force_update": True,
            "actor": "bosgenesis-mop-execution-agent",
            "correlation_id": self._correlation_id,
        }
        return self._post("/repos/add", payload, "helm.repo_add")

    def _post(self, path: str, payload: dict[str, Any], tool_name: str) -> McpCallResult:
        headers = {"X-API-Key": self._api_key} if self._api_key else {}
        try:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.post(f"{self._base_url}{path}", json=payload, headers=headers)
        except httpx.TimeoutException:
            return _standalone_failure(
                self._job_id,
                "bosgenesis_helm",
                tool_name,
                ErrorCode.TIMEOUT_EXCEEDED,
                f"mcp_timeout:{tool_name}",
                self._correlation_id,
                self._trace_id,
                retryable=True,
            )
        except httpx.HTTPError as exc:
            return _standalone_failure(
                self._job_id,
                "bosgenesis_helm",
                tool_name,
                ErrorCode.MCP_UNAVAILABLE,
                f"mcp_unavailable:{tool_name}:{type(exc).__name__}",
                self._correlation_id,
                self._trace_id,
                retryable=True,
            )

        data = _response_data(response)
        if response.is_success:
            return _standalone_success(
                self._job_id,
                "bosgenesis_helm",
                tool_name,
                data,
                self._correlation_id,
                self._trace_id,
            )
        return _standalone_failure(
            self._job_id,
            "bosgenesis_helm",
            tool_name,
            _error_code_from_response(response, data),
            _message_from_response(response, data),
            self._correlation_id,
            self._trace_id,
            data=data,
        )

    def _get(
        self,
        path: str,
        params: dict[str, Any],
        tool_name: str,
    ) -> McpCallResult:
        headers = {"X-API-Key": self._api_key} if self._api_key else {}
        try:
            with httpx.Client(timeout=self._timeout_seconds) as client:
                response = client.get(f"{self._base_url}{path}", params=params, headers=headers)
        except httpx.TimeoutException:
            return _standalone_failure(
                self._job_id,
                "bosgenesis_helm",
                tool_name,
                ErrorCode.TIMEOUT_EXCEEDED,
                f"mcp_timeout:{tool_name}",
                self._correlation_id,
                self._trace_id,
                retryable=True,
            )
        except httpx.HTTPError as exc:
            return _standalone_failure(
                self._job_id,
                "bosgenesis_helm",
                tool_name,
                ErrorCode.MCP_UNAVAILABLE,
                f"mcp_unavailable:{tool_name}:{type(exc).__name__}",
                self._correlation_id,
                self._trace_id,
                retryable=True,
            )

        data = _response_data(response)
        if response.is_success:
            return _standalone_success(
                self._job_id,
                "bosgenesis_helm",
                tool_name,
                data,
                self._correlation_id,
                self._trace_id,
            )
        return _standalone_failure(
            self._job_id,
            "bosgenesis_helm",
            tool_name,
            _error_code_from_response(response, data),
            _message_from_response(response, data),
            self._correlation_id,
            self._trace_id,
            data=data,
        )


def _manifest_for_namespace(manifest: dict[str, Any], namespace: str) -> dict[str, Any]:
    patched = dict(manifest)
    metadata = dict(patched.get("metadata") or {})
    if patched.get("kind") != "Namespace":
        metadata["namespace"] = namespace
    patched["metadata"] = metadata
    if patched.get("kind") == "Ingress":
        patched = _ingress_for_namespace(patched, namespace)
    return patched


def _ingress_for_namespace(manifest: dict[str, Any], namespace: str) -> dict[str, Any]:
    patched = dict(manifest)
    spec = dict(patched.get("spec") or {})
    rules = []
    for rule in spec.get("rules") or []:
        if not isinstance(rule, dict):
            rules.append(rule)
            continue
        updated_rule = dict(rule)
        host = updated_rule.get("host")
        if isinstance(host, str):
            updated_rule["host"] = _namespace_prefixed_host(host, namespace)
        rules.append(updated_rule)
    if rules:
        spec["rules"] = rules
    tls_entries = []
    for tls in spec.get("tls") or []:
        if not isinstance(tls, dict):
            tls_entries.append(tls)
            continue
        updated_tls = dict(tls)
        hosts = updated_tls.get("hosts")
        if isinstance(hosts, list):
            updated_tls["hosts"] = [
                _namespace_prefixed_host(host, namespace) if isinstance(host, str) else host
                for host in hosts
            ]
        tls_entries.append(updated_tls)
    if tls_entries:
        spec["tls"] = tls_entries
    patched["spec"] = spec
    return patched


def _namespace_prefixed_host(host: str, namespace: str) -> str:
    if not namespace or f"-{namespace}." in host or host.startswith(f"{namespace}."):
        return host
    labels = host.split(".", 1)
    if len(labels) == 1:
        return f"{host}-{namespace}"
    return f"{labels[0]}-{namespace}.{labels[1]}"


def _rest_base_url(base_url: str) -> str:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/mcp"):
        return normalized[: -len("/mcp")]
    return normalized


def _response_data(response: httpx.Response) -> dict[str, Any]:
    try:
        loaded = response.json()
    except ValueError:
        return {"text": response.text}
    return loaded if isinstance(loaded, dict) else {"response": loaded}


def _error_code_from_response(response: httpx.Response, data: dict[str, Any]) -> ErrorCode:
    detail = str(data.get("detail") or data.get("message") or data)
    lowered = detail.lower()
    if "already defined in ingress" in lowered or "ingress" in lowered and "conflict" in lowered:
        return ErrorCode.INGRESS_CONFLICT
    if response.status_code in {401, 403}:
        return ErrorCode.VALIDATION_FAILED
    if response.status_code in {502, 503, 504}:
        return ErrorCode.MCP_UNAVAILABLE
    return ErrorCode.DRY_RUN_FAILED


def _message_from_response(response: httpx.Response, data: dict[str, Any]) -> str:
    detail = data.get("detail") or data.get("message") or data.get("error") or data
    return f"http_{response.status_code}:{redact_value(detail)}"


def _compat_error(error_code: ErrorCode, message: str) -> dict[str, Any]:
    return {
        "ok": False,
        "error": {
            "error_code": error_code.value,
            "message": str(redact_value(message)),
            "retryable": error_code
            in {ErrorCode.TIMEOUT_EXCEEDED, ErrorCode.MCP_UNAVAILABLE},
        },
    }


def _standalone_success(
    job_id: str,
    server_name: str,
    tool_name: str,
    data: dict[str, Any],
    correlation_id: str | None,
    trace_id: str | None,
) -> McpCallResult:
    return McpCallResult(
        server_name=server_name,
        tool_name=tool_name,
        success=True,
        data=redact_value(data),
        correlation_id=correlation_id,
        trace_id=trace_id,
        observation=_observation(
            job_id=job_id,
            server_name=server_name,
            tool_name=tool_name,
            severity=ObservationSeverity.INFO,
            summary=f"MCP REST call succeeded: {server_name}.{tool_name}",
            result={"success": True, "data": data},
            correlation_id=correlation_id,
            trace_id=trace_id,
        ),
    )


def _standalone_failure(
    job_id: str,
    server_name: str,
    tool_name: str,
    error_code: ErrorCode,
    message: str,
    correlation_id: str | None,
    trace_id: str | None,
    *,
    retryable: bool = False,
    data: dict[str, Any] | None = None,
) -> McpCallResult:
    error = McpStructuredError(
        error_code=error_code,
        message=str(redact_value(message)),
        retryable=retryable,
        raw_type=error_code.value,
    )
    return McpCallResult(
        server_name=server_name,
        tool_name=tool_name,
        success=False,
        error=error,
        correlation_id=correlation_id,
        trace_id=trace_id,
        observation=_observation(
            job_id=job_id,
            server_name=server_name,
            tool_name=tool_name,
            severity=ObservationSeverity.ERROR,
            summary=f"MCP REST call failed: {server_name}.{tool_name}",
            result={
                "success": False,
                "error": error.model_dump(mode="json"),
                "data": data or {},
                "worker_reasoning_triggered": False,
            },
            correlation_id=correlation_id,
            trace_id=trace_id,
        ),
    )


def _observation(
    *,
    job_id: str,
    server_name: str,
    tool_name: str,
    severity: ObservationSeverity,
    summary: str,
    result: dict[str, Any],
    correlation_id: str | None,
    trace_id: str | None,
) -> Observation:
    return Observation(
        observation_id=new_id("obs"),
        job_id=job_id,
        severity=severity,
        observation_type=ObservationType.MCP_CALL_RESULT,
        summary=summary,
        correlation_id=correlation_id,
        trace_id=trace_id,
        mcp_server=server_name,
        mcp_tool=tool_name,
        result=redact_value(result),
        redaction_applied=True,
    )
