"""FastAPI application factory."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response

from bosgenesis_mop_execution_agent import __version__
from bosgenesis_mop_execution_agent.api.routes import router as api_router
from bosgenesis_mop_execution_agent.api.schemas import HealthResponse
from bosgenesis_mop_execution_agent.api.service import MopExecutionApiService
from bosgenesis_mop_execution_agent.mcp_server.routes import router as mcp_router
from bosgenesis_mop_execution_agent.observability import (
    METRICS,
    configure_logging,
    configure_tracing,
)
from bosgenesis_mop_execution_agent.observability.middleware import observability_middleware


def create_app() -> FastAPI:
    """Create the FastAPI application."""
    configure_logging()
    app = FastAPI(
        title="BOS Genesis MoP Execution Agent API",
        version=__version__,
        summary="Async, externally controlled execution API for MoP artifact bundles.",
    )
    app.middleware("http")(observability_middleware)
    app.state.otel = configure_tracing(app)
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
    async def get_ready() -> JSONResponse:
        payload = mop_execution_service.ready()
        return JSONResponse(payload, status_code=200 if payload.get("ok") is True else 503)

    @app.get("/metrics", tags=["Health"])
    async def get_metrics() -> Response:
        return Response(METRICS.render_prometheus(), media_type="text/plain; version=0.0.4")

    app.include_router(api_router)
    app.include_router(mcp_router)

    return app
