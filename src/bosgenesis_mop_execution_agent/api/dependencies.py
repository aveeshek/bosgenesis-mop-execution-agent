"""FastAPI dependency placeholders."""

from __future__ import annotations

from typing import cast

from fastapi import Request

from bosgenesis_mop_execution_agent.api.service import MopExecutionApiService


async def require_api_actor() -> str:
    """Authentication placeholder for Phase 5.

    Later phases can replace this with OIDC/API-key validation. Keeping it as a
    dependency now makes every route auth-ready without exposing secrets.
    """
    return "authenticated-placeholder"


def get_api_service(request: Request) -> MopExecutionApiService:
    """Return the application-scoped API service."""
    return cast("MopExecutionApiService", request.app.state.mop_execution_service)
