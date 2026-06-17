"""Typed client for bosgenesis-helm-manager-mcp."""

from __future__ import annotations

from typing import Any

from bosgenesis_mop_execution_agent.mcp_clients.base import McpClientBase
from bosgenesis_mop_execution_agent.mcp_clients.models import McpCallResult


class HelmManagerMcpClient(McpClientBase):
    """Governed Helm Manager MCP client."""

    def repo_add(self, *, name: str, url: str) -> McpCallResult:
        return self.call_tool("repo.add", {"name": name, "url": url}, mutating=True)

    def repo_update(self, *, name: str | None = None) -> McpCallResult:
        return self.call_tool("repo.update", {"name": name}, mutating=True)

    def template(
        self,
        *,
        release_name: str,
        chart: str,
        namespace: str,
        values: dict[str, Any] | None = None,
    ) -> McpCallResult:
        return self.call_tool(
            "chart.template",
            {
                "release_name": release_name,
                "chart": chart,
                "namespace": namespace,
                "values": values or {},
                "redaction_required": True,
            },
            safe_retry=True,
        )

    def dry_run_install_upgrade(
        self,
        *,
        release_name: str,
        chart: str,
        namespace: str,
        values: dict[str, Any] | None = None,
    ) -> McpCallResult:
        return self.call_tool(
            "release.dry_run_install_upgrade",
            {
                "release_name": release_name,
                "chart": chart,
                "namespace": namespace,
                "values": values or {},
                "redaction_required": True,
            },
        )

    def install_upgrade(
        self,
        *,
        release_name: str,
        chart: str,
        namespace: str,
        values: dict[str, Any] | None = None,
    ) -> McpCallResult:
        return self.call_tool(
            "release.install_upgrade",
            {
                "release_name": release_name,
                "chart": chart,
                "namespace": namespace,
                "values": values or {},
            },
            mutating=True,
        )

    def status(self, *, release_name: str, namespace: str) -> McpCallResult:
        return self.call_tool(
            "release.status",
            {"release_name": release_name, "namespace": namespace},
            safe_retry=True,
        )

    def history(self, *, release_name: str, namespace: str) -> McpCallResult:
        return self.call_tool(
            "release.history",
            {"release_name": release_name, "namespace": namespace},
            safe_retry=True,
        )

    def rollback(
        self,
        *,
        release_name: str,
        namespace: str,
        revision: int,
    ) -> McpCallResult:
        return self.call_tool(
            "release.rollback",
            {"release_name": release_name, "namespace": namespace, "revision": revision},
            mutating=True,
        )

    def uninstall(self, *, release_name: str, namespace: str) -> McpCallResult:
        return self.call_tool(
            "release.uninstall",
            {"release_name": release_name, "namespace": namespace},
            mutating=True,
        )

    def list_releases(self, *, namespace: str) -> McpCallResult:
        return self.call_tool(
            "release.list",
            {"namespace": namespace},
            safe_retry=True,
        )

    def validate_values(self, *, chart: str, values: dict[str, Any]) -> McpCallResult:
        return self.call_tool(
            "values.validate",
            {"chart": chart, "values": values, "redaction_required": True},
        )
