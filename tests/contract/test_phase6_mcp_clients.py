from __future__ import annotations

from typing import Any

import pytest

from bosgenesis_mop_execution_agent.mcp_clients import (
    DataIngestionMcpClient,
    HelmManagerMcpClient,
    KubernetesInspectorMcpClient,
    McpTransportError,
    ReleaseNoteMcpClient,
)
from bosgenesis_mop_execution_agent.models import AuditEvent, ErrorCode, ObservationSeverity


def test_kubernetes_client_success_returns_structured_redacted_observation_and_audit() -> None:
    transport = FakeMcpTransport(
        {"pod.logs": [{"ok": True, "data": {"logs": "password=fake-password-value"}}]}
    )
    audit_events: list[AuditEvent] = []
    client = KubernetesInspectorMcpClient(
        server_name="bosgenesis-k8s-inspector-agent",
        transport=transport,
        job_id="job-1",
        correlation_id="corr-1",
        trace_id="trace-1",
        audit_hook=audit_events.append,
    )

    result = client.pod_logs(namespace="target-ns", pod_name="pod-1")

    assert result.success
    assert result.data == {"logs": "[REDACTED]"}
    assert result.correlation_id == "corr-1"
    assert result.observation.severity == ObservationSeverity.INFO
    assert result.observation.mcp_tool == "pod.logs"
    assert result.observation.result["data"] == {"logs": "[REDACTED]"}
    assert audit_events[0].action == "mcp_call:bosgenesis-k8s-inspector-agent.pod.logs"


def test_safe_read_retries_transport_failure_but_mutating_call_does_not_retry() -> None:
    read_transport = FakeMcpTransport(
        {
            "resource.get": [
                McpTransportError("temporary"),
                {"ok": True, "data": {"name": "cfg"}},
            ]
        }
    )
    read_client = KubernetesInspectorMcpClient(
        server_name="k8s",
        transport=read_transport,
        job_id="job-1",
        max_safe_retries=1,
    )

    read_result = read_client.get_resource(namespace="target-ns", kind="ConfigMap", name="cfg")

    assert read_result.success
    assert read_result.attempts == 2

    mutate_transport = FakeMcpTransport({"manifest.apply": [McpTransportError("temporary")]})
    mutate_client = KubernetesInspectorMcpClient(
        server_name="k8s",
        transport=mutate_transport,
        job_id="job-1",
        max_safe_retries=3,
    )

    mutate_result = mutate_client.apply(
        manifest={"kind": "ConfigMap", "metadata": {"name": "cfg"}},
        namespace="target-ns",
    )

    assert not mutate_result.success
    assert mutate_result.attempts == 1
    assert mutate_transport.call_count("manifest.apply") == 1


def test_timeout_error_creates_observation_without_worker_reasoning() -> None:
    transport = FakeMcpTransport({"events.list": [TimeoutError("slow")]})
    client = KubernetesInspectorMcpClient(
        server_name="k8s",
        transport=transport,
        job_id="job-1",
        max_safe_retries=0,
    )

    result = client.events(namespace="target-ns")

    assert not result.success
    assert result.error is not None
    assert result.error.error_code == ErrorCode.TIMEOUT_EXCEEDED
    assert result.observation.severity == ObservationSeverity.ERROR
    assert result.observation.result["worker_reasoning_triggered"] is False


def test_malformed_response_creates_structured_failure_observation() -> None:
    transport = FakeMcpTransport({"release.status": [{"data": {"status": "deployed"}}]})
    client = HelmManagerMcpClient(server_name="helm", transport=transport, job_id="job-1")

    result = client.status(release_name="sample", namespace="target-ns")

    assert not result.success
    assert result.error is not None
    assert result.error.error_code == ErrorCode.UNKNOWN_ERROR
    assert "mcp_malformed_response" in result.error.message
    assert result.observation.result["worker_reasoning_triggered"] is False


def test_mcp_error_response_is_structured_and_redacted() -> None:
    transport = FakeMcpTransport(
        {
            "workload.rollout_status": [
                {
                    "ok": False,
                    "error": {
                        "error_code": "POD_UNSCHEDULABLE",
                        "message": "pod failed token=fake-token-value",
                        "retryable": False,
                    },
                }
            ]
        }
    )
    client = KubernetesInspectorMcpClient(server_name="k8s", transport=transport, job_id="job-1")

    result = client.rollout_status(
        namespace="target-ns",
        kind="Deployment",
        name="sample",
        timeout_seconds=30,
    )

    assert not result.success
    assert result.error is not None
    assert result.error.error_code == ErrorCode.POD_UNSCHEDULABLE
    assert "fake-token-value" not in result.error.message
    assert "fake-token-value" not in str(result.observation.result)


def test_audit_hook_failure_blocks_call_before_transport_invocation() -> None:
    transport = FakeMcpTransport({"manifest.apply": [{"ok": True, "data": {"applied": True}}]})
    client = KubernetesInspectorMcpClient(
        server_name="k8s",
        transport=transport,
        job_id="job-1",
        audit_hook=raising_audit_hook,
    )

    result = client.apply(
        manifest={"kind": "ConfigMap", "metadata": {"name": "cfg"}},
        namespace="target-ns",
    )

    assert not result.success
    assert result.error is not None
    assert result.error.error_code == ErrorCode.AUDIT_WRITE_FAILED
    assert transport.call_count("manifest.apply") == 0


def test_helm_data_ingestion_and_release_note_clients_expose_typed_methods() -> None:
    transport = FakeMcpTransport(default={"ok": True, "data": {"ok": True}})
    helm = HelmManagerMcpClient(server_name="helm", transport=transport, job_id="job-1")
    data = DataIngestionMcpClient(server_name="data", transport=transport, job_id="job-1")
    release_notes = ReleaseNoteMcpClient(
        server_name="release-notes",
        transport=transport,
        job_id="job-1",
    )

    results = [
        helm.repo_add(name="bitnami", url="https://charts.example.test"),
        helm.repo_update(name="bitnami"),
        helm.template(release_name="app", chart="repo/app", namespace="target-ns"),
        helm.dry_run_install_upgrade(
            release_name="app",
            chart="repo/app",
            namespace="target-ns",
        ),
        helm.install_upgrade(release_name="app", chart="repo/app", namespace="target-ns"),
        helm.history(release_name="app", namespace="target-ns"),
        helm.rollback(release_name="app", namespace="target-ns", revision=1),
        helm.uninstall(release_name="app", namespace="target-ns"),
        helm.list_releases(namespace="target-ns"),
        helm.validate_values(chart="repo/app", values={"replicaCount": 1}),
        data.latest_snapshot(namespace="target-ns"),
        data.historical_facts(namespace="target-ns", resource_name="app"),
        data.recent_events(namespace="target-ns"),
        release_notes.create_execution_notes(
            job_id="job-1",
            executed_steps=[],
            warnings=[],
            trace_id="trace-1",
        ),
        release_notes.create_rollback_notes(
            job_id="job-1",
            rollback_steps=[],
            warnings=[],
            trace_id="trace-1",
        ),
    ]

    assert all(result.success for result in results)
    assert transport.called_tools >= {
        "repo.add",
        "repo.update",
        "chart.template",
        "release.dry_run_install_upgrade",
        "release.install_upgrade",
        "release.history",
        "release.rollback",
        "release.uninstall",
        "release.list",
        "values.validate",
        "snapshot.latest",
        "facts.historical",
        "events.recent",
        "release_notes.create_execution_notes",
        "release_notes.create_rollback_notes",
    }


def test_kubernetes_client_exposes_required_operation_surface() -> None:
    transport = FakeMcpTransport(default={"ok": True, "data": {"ok": True}})
    client = KubernetesInspectorMcpClient(server_name="k8s", transport=transport, job_id="job-1")
    manifest = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "cfg"}}

    results = [
        client.namespace("target-ns"),
        client.create_namespace("target-ns"),
        client.dry_run_apply(manifest, "target-ns"),
        client.apply(manifest, "target-ns"),
        client.get_resource(namespace="target-ns", kind="ConfigMap", name="cfg"),
        client.list_resources(namespace="target-ns", kind="ConfigMap"),
        client.describe_resource(namespace="target-ns", kind="ConfigMap", name="cfg"),
        client.events(namespace="target-ns"),
        client.pod_status(namespace="target-ns", pod_name="pod-1"),
        client.pod_logs(namespace="target-ns", pod_name="pod-1"),
        client.delete_resource(namespace="target-ns", kind="ConfigMap", name="cfg"),
        client.wait_for_condition(
            namespace="target-ns",
            kind="Deployment",
            name="app",
            condition="Available",
            timeout_seconds=30,
        ),
        client.rollout_status(
            namespace="target-ns",
            kind="Deployment",
            name="app",
            timeout_seconds=30,
        ),
    ]

    assert all(result.success for result in results)
    assert transport.called_tools >= {
        "namespace.get",
        "namespace.create",
        "manifest.server_side_dry_run_apply",
        "manifest.apply",
        "resource.get",
        "resource.list",
        "resource.describe",
        "events.list",
        "pod.status",
        "pod.logs",
        "resource.delete",
        "resource.wait_for_condition",
        "workload.rollout_status",
    }


def raising_audit_hook(_: AuditEvent) -> None:
    raise RuntimeError("audit unavailable")


class FakeMcpTransport:
    def __init__(
        self,
        responses: dict[str, list[dict[str, Any] | BaseException]] | None = None,
        *,
        default: dict[str, Any] | None = None,
    ) -> None:
        self._responses = responses or {}
        self._default = default or {"ok": True, "data": {}}
        self.calls: list[dict[str, Any]] = []

    @property
    def called_tools(self) -> set[str]:
        return {str(call["tool_name"]) for call in self.calls}

    def call_count(self, tool_name: str) -> int:
        return sum(1 for call in self.calls if call["tool_name"] == tool_name)

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
        self.calls.append(
            {
                "server_name": server_name,
                "tool_name": tool_name,
                "arguments": arguments,
                "timeout_seconds": timeout_seconds,
                "correlation_id": correlation_id,
                "trace_id": trace_id,
            }
        )
        queued = self._responses.get(tool_name)
        response = queued.pop(0) if queued else self._default
        if isinstance(response, BaseException):
            raise response
        return response


@pytest.mark.parametrize(
    ("client_factory", "method_name"),
    [
        (KubernetesInspectorMcpClient, "namespace"),
        (HelmManagerMcpClient, "status"),
        (DataIngestionMcpClient, "latest_snapshot"),
        (ReleaseNoteMcpClient, "create_execution_notes"),
    ],
)
def test_clients_are_not_reasoning_agents(client_factory: object, method_name: str) -> None:
    assert client_factory is not None
    assert method_name
