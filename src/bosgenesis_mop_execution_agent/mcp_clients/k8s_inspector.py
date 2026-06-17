"""Typed client for bosgenesis-k8s-inspector-agent."""

from __future__ import annotations

from typing import Any

from bosgenesis_mop_execution_agent.mcp_clients.base import McpClientBase
from bosgenesis_mop_execution_agent.mcp_clients.models import McpCallResult


class KubernetesInspectorMcpClient(McpClientBase):
    """Governed Kubernetes Inspector MCP client."""

    def namespace(self, namespace: str) -> McpCallResult:
        return self.call_tool("namespace.get", {"namespace": namespace}, safe_retry=True)

    def create_namespace(
        self,
        namespace: str,
        labels: dict[str, str] | None = None,
    ) -> McpCallResult:
        return self.call_tool(
            "namespace.create",
            {"namespace": namespace, "labels": labels or {}},
            mutating=True,
        )

    def dry_run_apply(self, manifest: dict[str, Any], namespace: str) -> McpCallResult:
        return self.call_tool(
            "manifest.server_side_dry_run_apply",
            {"manifest": manifest, "namespace": namespace},
        )

    def apply(self, manifest: dict[str, Any], namespace: str) -> McpCallResult:
        return self.call_tool(
            "manifest.apply",
            {"manifest": manifest, "namespace": namespace},
            mutating=True,
        )

    def get_resource(
        self,
        *,
        namespace: str,
        kind: str,
        name: str,
        api_version: str | None = None,
    ) -> McpCallResult:
        return self.call_tool(
            "resource.get",
            {
                "namespace": namespace,
                "kind": kind,
                "name": name,
                "api_version": api_version,
            },
            safe_retry=True,
        )

    def list_resources(
        self,
        *,
        namespace: str,
        kind: str,
        label_selector: str | None = None,
    ) -> McpCallResult:
        return self.call_tool(
            "resource.list",
            {"namespace": namespace, "kind": kind, "label_selector": label_selector},
            safe_retry=True,
        )

    def describe_resource(self, *, namespace: str, kind: str, name: str) -> McpCallResult:
        return self.call_tool(
            "resource.describe",
            {"namespace": namespace, "kind": kind, "name": name},
            safe_retry=True,
        )

    def events(
        self,
        *,
        namespace: str,
        involved_object_name: str | None = None,
    ) -> McpCallResult:
        return self.call_tool(
            "events.list",
            {"namespace": namespace, "involved_object_name": involved_object_name},
            safe_retry=True,
        )

    def pod_status(self, *, namespace: str, pod_name: str) -> McpCallResult:
        return self.call_tool(
            "pod.status",
            {"namespace": namespace, "pod_name": pod_name},
            safe_retry=True,
        )

    def pod_logs(
        self,
        *,
        namespace: str,
        pod_name: str,
        container: str | None = None,
        tail_lines: int | None = None,
    ) -> McpCallResult:
        return self.call_tool(
            "pod.logs",
            {
                "namespace": namespace,
                "pod_name": pod_name,
                "container": container,
                "tail_lines": tail_lines,
                "redaction_required": True,
            },
            safe_retry=True,
        )

    def delete_resource(
        self,
        *,
        namespace: str,
        kind: str,
        name: str,
        api_version: str | None = None,
    ) -> McpCallResult:
        return self.call_tool(
            "resource.delete",
            {
                "namespace": namespace,
                "kind": kind,
                "name": name,
                "api_version": api_version,
            },
            mutating=True,
        )

    def wait_for_condition(
        self,
        *,
        namespace: str,
        kind: str,
        name: str,
        condition: str,
        timeout_seconds: int,
    ) -> McpCallResult:
        return self.call_tool(
            "resource.wait_for_condition",
            {
                "namespace": namespace,
                "kind": kind,
                "name": name,
                "condition": condition,
                "timeout_seconds": timeout_seconds,
            },
            safe_retry=True,
        )

    def rollout_status(
        self,
        *,
        namespace: str,
        kind: str,
        name: str,
        timeout_seconds: int,
    ) -> McpCallResult:
        return self.call_tool(
            "workload.rollout_status",
            {
                "namespace": namespace,
                "kind": kind,
                "name": name,
                "timeout_seconds": timeout_seconds,
            },
            safe_retry=True,
        )
