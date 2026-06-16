"""FastAPI application factory."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI

from bosgenesis_mop_execution_agent import __version__
from bosgenesis_mop_execution_agent.api.schemas import HealthResponse


def create_app() -> FastAPI:
    """Create the FastAPI application."""
    app = FastAPI(
        title="BOS Genesis MoP Execution Agent API",
        version=__version__,
        summary="Async, externally controlled execution API for MoP artifact bundles.",
    )

    @app.get("/healthz", response_model=HealthResponse, tags=["Health"])
    async def get_health() -> HealthResponse:
        return HealthResponse(
            status="ok",
            service="bosgenesis-mop-execution-agent",
            version=__version__,
            timestamp=datetime.now(UTC),
        )

    return app
