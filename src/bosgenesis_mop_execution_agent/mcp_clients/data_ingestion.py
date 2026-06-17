"""Typed client for bosgenesis-k8s-data-ingestion-agent."""

from bosgenesis_mop_execution_agent.mcp_clients.base import McpClientBase
from bosgenesis_mop_execution_agent.mcp_clients.models import McpCallResult


class DataIngestionMcpClient(McpClientBase):
    """Read-only data ingestion MCP client."""

    def latest_snapshot(self, *, namespace: str) -> McpCallResult:
        return self.call_tool(
            "snapshot.latest",
            {"namespace": namespace},
            safe_retry=True,
        )

    def historical_facts(
        self,
        *,
        namespace: str,
        resource_name: str | None = None,
        lookback_hours: int | None = None,
    ) -> McpCallResult:
        return self.call_tool(
            "facts.historical",
            {
                "namespace": namespace,
                "resource_name": resource_name,
                "lookback_hours": lookback_hours,
            },
            safe_retry=True,
        )

    def recent_events(self, *, namespace: str, lookback_hours: int = 24) -> McpCallResult:
        return self.call_tool(
            "events.recent",
            {"namespace": namespace, "lookback_hours": lookback_hours},
            safe_retry=True,
        )
