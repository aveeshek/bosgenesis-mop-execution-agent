"""FastAPI dependency placeholders."""

from __future__ import annotations

from typing import cast

from fastapi import Request

from bosgenesis_mop_execution_agent.api.service import MopExecutionApiService


async def require_api_actor(request: Request) -> str:
    """Return the bounded ESDA actor identity carried across the API boundary.

    Full service authentication remains owned by the deployment boundary. The
    actor header supplies audit attribution only and never grants permissions.
    """
    actor_id = " ".join(str(request.headers.get("x-esda-actor") or "").split())[:200]
    return actor_id or "authenticated-placeholder"


def get_api_service(request: Request) -> MopExecutionApiService:
    """Return the application-scoped API service."""
    return cast("MopExecutionApiService", request.app.state.mop_execution_service)
