"""FastAPI application factory."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI

from bosgenesis_mop_execution_agent import __version__
from bosgenesis_mop_execution_agent.api.routes import router as api_router
from bosgenesis_mop_execution_agent.api.schemas import HealthResponse
from bosgenesis_mop_execution_agent.api.service import MopExecutionApiService
from bosgenesis_mop_execution_agent.mcp_server.routes import router as mcp_router


def create_app() -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(
        title="BOS Genesis MoP Execution Agent API",
        version=__version__,
        summary="Async, externally controlled execution API for MoP artifact bundles.",
    )
    mop_execution_service = MopExecutionApiService()
    app.state.mop_execution_service = mop_execution_service

    @app.get("/healthz", response_model=HealthResponse, tags=["Health"])
    async def get_health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service="bosgenesis-mop-execution-agent",
            version=__version__,
            timestamp=datetime.now(UTC),
        )

    @app.get("/readyz", tags=["Health"])
    async def get_ready() -> dict[str, object]:
        return mop_execution_service.ready()

    app.include_router(api_router)
    app.include_router(mcp_router)

    return app
