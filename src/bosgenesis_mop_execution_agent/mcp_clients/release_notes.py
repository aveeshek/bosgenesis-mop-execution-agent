"""Typed client for bosgenesis-release-note-agent."""

from __future__ import annotations

from typing import Any

from bosgenesis_mop_execution_agent.mcp_clients.base import McpClientBase
from bosgenesis_mop_execution_agent.mcp_clients.models import McpCallResult


class ReleaseNoteMcpClient(McpClientBase):
    """Release-note MCP client for final reports."""

    def create_execution_notes(
        self,
        *,
        job_id: str,
        executed_steps: list[dict[str, Any]],
        warnings: list[str],
        trace_id: str | None = None,
    ) -> McpCallResult:
        return self.call_tool(
            "release_notes.create_execution_notes",
            {
                "job_id": job_id,
                "executed_steps": executed_steps,
                "warnings": warnings,
                "trace_id": trace_id,
            },
        )

    def create_rollback_notes(
        self,
        *,
        job_id: str,
        rollback_steps: list[dict[str, Any]],
        warnings: list[str],
        trace_id: str | None = None,
    ) -> McpCallResult:
        return self.call_tool(
            "release_notes.create_rollback_notes",
            {
                "job_id": job_id,
                "rollback_steps": rollback_steps,
                "warnings": warnings,
                "trace_id": trace_id,
            },
        )
