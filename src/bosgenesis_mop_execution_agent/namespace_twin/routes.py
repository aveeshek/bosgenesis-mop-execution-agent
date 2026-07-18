"""REST boundary for the real namespace twin foundation."""

from __future__ import annotations

from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from bosgenesis_mop_execution_agent.api.dependencies import require_api_actor
from bosgenesis_mop_execution_agent.artifacts.models import BundleSource
from bosgenesis_mop_execution_agent.namespace_twin.persistence import NamespaceTwinPersistenceError
from bosgenesis_mop_execution_agent.namespace_twin.service import (
    NamespaceTwinError,
    NamespaceTwinService,
    translate_persistence_error,
)


class NamespaceTwinCreateRequest(BaseModel):
    source: BundleSource
    target_namespace: str = Field(min_length=1, max_length=253)
    target_cluster: str = Field(default="configured-cluster", min_length=1, max_length=253)
    idempotency_key: str | None = Field(default=None, max_length=200)
    supersedes_twin_id: str | None = Field(default=None, max_length=200)


class NamespaceTwinDryRunEvidenceRequest(BaseModel):
    dry_run_job_id: str = Field(min_length=1, max_length=200)
    bundle_hash: str | None = Field(default=None, min_length=64, max_length=64)
    input_hash: str | None = Field(default=None, min_length=64, max_length=64)
    command_fingerprint_hash: str | None = Field(default=None, min_length=64, max_length=64)
    wait_seconds: int = Field(default=0, ge=0, le=30)
    poll_interval_ms: int = Field(default=500, ge=100, le=5000)


class ReleaseNoteClaimRequest(BaseModel):
    category: str = Field(default="other", max_length=80)
    claim: str = Field(min_length=1, max_length=4000)


class ReleaseNoteExtractionRequest(BaseModel):
    method: str = Field(default="bounded_model_with_fallback", max_length=100)
    model_profile: str | None = Field(default=None, max_length=200)
    prompt_version: str = Field(min_length=1, max_length=200)
    prompt_hash: str = Field(min_length=64, max_length=64)
    input_hash: str = Field(min_length=64, max_length=64)
    fallback_used: bool = False
    safe_summary: str = Field(min_length=1, max_length=1000)


class NamespaceTwinReleaseNoteValidationRequest(BaseModel):
    release_note_artifact_id: str = Field(min_length=1, max_length=500)
    release_note_artifact_hash: str = Field(min_length=64, max_length=64)
    claims: list[ReleaseNoteClaimRequest] = Field(default_factory=list, max_length=100)
    extraction: ReleaseNoteExtractionRequest

def get_namespace_twin_service(request: Request) -> NamespaceTwinService:
    return cast("NamespaceTwinService", request.app.state.namespace_twin_service)


router = APIRouter(prefix="/v1/namespace-twins", tags=["Namespace Digital Twin"])
NamespaceTwinServiceDep = Annotated[NamespaceTwinService, Depends(get_namespace_twin_service)]
ActorDep = Annotated[str, Depends(require_api_actor)]


def _success(data: Any, message: str) -> JSONResponse:
    return JSONResponse(
        status_code=200,
        content={"ok": True, "message": message, "data": data, "data_mode": "real_core"},
        headers={"Cache-Control": "no-store", "X-Data-Mode": "real_core"},
    )


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, NamespaceTwinPersistenceError):
        exc = translate_persistence_error(exc)
    if isinstance(exc, NamespaceTwinError):
        return JSONResponse(
            status_code=exc.status_code,
            content={
                "ok": False,
                "data_mode": "real_core",
                "error": {
                    "code": exc.code,
                    "message": exc.message,
                    "retryable": False,
                    "details": exc.details,
                },
            },
            headers={"Cache-Control": "no-store", "X-Data-Mode": "real_core"},
        )
    raise exc


@router.post("")
async def create_namespace_twin(
    payload: NamespaceTwinCreateRequest,
    service: NamespaceTwinServiceDep,
    actor_id: ActorDep,
) -> JSONResponse:
    try:
        twin = service.create(payload.model_dump(mode="json"), actor_id=actor_id)
        return _success(twin, "Real provisional namespace twin created.")
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.get("")
async def list_namespace_twins(
    service: NamespaceTwinServiceDep,
    _: ActorDep,
    q: str | None = Query(default=None),
    decision: str | None = Query(default=None),
    freshness: str | None = Query(default=None),
    bundle_name: str | None = Query(default=None),
    actor_id: str | None = Query(default=None),
    created_from: str | None = Query(default=None),
    created_to: str | None = Query(default=None),
    linked_execution: str | None = Query(default=None),
    sort: str = Query(default="created_at"),
    direction: str = Query(default="desc"),
    cursor: str | None = Query(default=None),
    lifecycle_status: str | None = Query(default=None),
    target_namespace: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    try:
        result = service.list(
            {
                "q": q,
                "decision": decision,
                "freshness": freshness,
                "bundle_name": bundle_name,
                "actor_id": actor_id,
                "created_from": created_from,
                "created_to": created_to,
                "linked_execution": linked_execution,
                "sort": sort,
                "direction": direction,
                "cursor": cursor,
                "lifecycle_status": lifecycle_status,
                "target_namespace": target_namespace,
                "limit": limit,
                "offset": offset,
            }
        )
        return _success(result, "Real namespace twin runs returned.")
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.get("/{twin_id}")
async def get_namespace_twin(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    _: ActorDep,
) -> JSONResponse:
    try:
        return _success(service.get(twin_id), "Real namespace twin returned.")
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.get("/{twin_id}/overview")
async def get_namespace_twin_overview(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    _: ActorDep,
) -> JSONResponse:
    try:
        return _success(
            service.overview(twin_id), "Authoritative namespace twin Overview returned."
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.get("/{twin_id}/release-delta")
async def get_namespace_twin_release_delta(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    _: ActorDep,
    action: str | None = Query(default=None),
    risk: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=25),
) -> JSONResponse:
    try:
        return _success(
            service.release_delta(
                twin_id,
                action=action,
                risk=risk,
                kind=kind,
                cursor=cursor,
                limit=limit,
            ),
            "Authoritative canonical Release Delta facts returned.",
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.get("/{twin_id}/dependency-graph")
async def get_namespace_twin_dependency_graph(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    _: ActorDep,
    kind: str | None = Query(default=None),
    risk: str | None = Query(default=None),
    status: str | None = Query(default=None),
    namespace: str | None = Query(default=None),
    relationship: str | None = Query(default=None),
    confidence: str | None = Query(default=None),
    edge_status: str | None = Query(default=None),
    search: str | None = Query(default=None),
    missing_only: bool = Query(default=False),
    resource: str | None = Query(default=None),
    node_cursor: str | None = Query(default=None),
    edge_cursor: str | None = Query(default=None),
    limit: int = Query(default=100),
) -> JSONResponse:
    try:
        return _success(
            service.dependency_graph(
                twin_id,
                kind=kind,
                risk=risk,
                status=status,
                namespace=namespace,
                relationship=relationship,
                confidence=confidence,
                edge_status=edge_status,
                search=search,
                missing_only=missing_only,
                resource=resource,
                node_cursor=node_cursor,
                edge_cursor=edge_cursor,
                limit=limit,
            ),
            "Authoritative Namespace Twin dependency facts returned.",
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.get("/{twin_id}/policy")
async def get_namespace_twin_policy(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    _: ActorDep,
    severity: str | None = Query(default=None),
    category: str | None = Query(default=None),
    effect: str | None = Query(default=None),
) -> JSONResponse:
    try:
        return _success(
            service.policy(
                twin_id,
                severity=severity,
                category=category,
                effect=effect,
            ),
            "Authoritative deterministic Namespace Twin policy facts returned.",
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.post("/{twin_id}/dry-run-evidence")
async def attach_namespace_twin_dry_run_evidence(
    twin_id: str,
    payload: NamespaceTwinDryRunEvidenceRequest,
    service: NamespaceTwinServiceDep,
    _: ActorDep,
) -> JSONResponse:
    try:
        return _success(
            service.attach_dry_run_evidence(
                twin_id,
                payload.model_dump(mode="json", exclude_none=True),
            ),
            "Authoritative dry-run evidence attached and decision calculated.",
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.get("/{twin_id}/dry-run")
async def get_namespace_twin_dry_run(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    _: ActorDep,
    phase: str | None = Query(default=None),
    step: str | None = Query(default=None),
    resource: str | None = Query(default=None),
    tool: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
) -> JSONResponse:
    try:
        return _success(
            service.dry_run(
                twin_id,
                phase=phase,
                step=step,
                resource=resource,
                tool=tool,
                outcome=outcome,
            ),
            "Authoritative dry-run and structured diff facts returned.",
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.get("/{twin_id}/rollback")
async def get_namespace_twin_rollback(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    _: ActorDep,
) -> JSONResponse:
    try:
        return _success(
            service.rollback(twin_id),
            "Deterministic Namespace Twin rollback readiness facts returned.",
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.get("/{twin_id}/drift")
async def get_namespace_twin_drift(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    _: ActorDep,
) -> JSONResponse:
    try:
        return _success(
            service.drift(twin_id),
            "Deterministic Namespace Drift Twin facts returned.",
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.post("/{twin_id}/drift/refresh")
async def refresh_namespace_twin_drift(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    actor_id: ActorDep,
) -> JSONResponse:
    try:
        return _success(
            service.refresh_drift(twin_id, actor_id=actor_id),
            "Read-only Namespace Drift Twin refresh completed.",
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.get("/{twin_id}/runtime-behavior")
async def get_namespace_twin_runtime_behavior(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    _: ActorDep,
) -> JSONResponse:
    try:
        return _success(
            service.runtime_behavior(twin_id),
            "Rules-first Namespace Runtime Behavior Twin facts returned.",
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.post("/{twin_id}/runtime-behavior/refresh")
async def refresh_namespace_twin_runtime_behavior(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    actor_id: ActorDep,
) -> JSONResponse:
    try:
        return _success(
            service.refresh_runtime_behavior(twin_id, actor_id=actor_id),
            "Read-only Namespace Runtime Behavior Twin refresh completed.",
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.get("/{twin_id}/release-note-validation")
async def get_namespace_twin_release_note_validation(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    _: ActorDep,
) -> JSONResponse:
    try:
        return _success(
            service.release_note_validation(twin_id),
            "Namespace Twin release-note validation facts returned.",
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.post("/{twin_id}/release-note-validation")
async def validate_namespace_twin_release_note(
    twin_id: str,
    payload: NamespaceTwinReleaseNoteValidationRequest,
    service: NamespaceTwinServiceDep,
    actor_id: ActorDep,
) -> JSONResponse:
    try:
        return _success(
            service.validate_release_note(
                twin_id,
                payload.model_dump(mode="json"),
                actor_id=actor_id,
            ),
            "Namespace Twin release-note validation completed.",
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)

@router.get("/{twin_id}/actions")
async def get_namespace_twin_actions(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    _: ActorDep,
) -> JSONResponse:
    try:
        twin = service.get(twin_id)
        return _success(
            {
                "schema_version": twin["schema_version"],
                "twin_id": twin_id,
                "decision_version": twin["decision_version"],
                "lifecycle_status": twin["lifecycle_status"],
                "freshness": twin["freshness"],
                "actions": twin["actions"],
            },
            "Authoritative namespace twin actions returned.",
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.get("/{twin_id}/events")
async def list_namespace_twin_events(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    _: ActorDep,
    limit: int = Query(default=100, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
) -> JSONResponse:
    try:
        return _success(
            service.events(twin_id, limit=limit, offset=offset),
            "Ordered redacted namespace twin events returned.",
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.get("/{twin_id}/audit")
async def get_namespace_twin_audit(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    _: ActorDep,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
) -> JSONResponse:
    try:
        return _success(
            service.audit(twin_id, cursor=cursor, limit=limit),
            "Cursor-paginated append-only Namespace Twin audit returned.",
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.get("/{twin_id}/reports/json")
async def download_namespace_twin_json_report(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    _: ActorDep,
) -> JSONResponse:
    try:
        return JSONResponse(
            content=service.report(twin_id),
            headers={
                "Cache-Control": "no-store",
                "X-Data-Mode": "real_core",
                "Content-Disposition": f'attachment; filename="{twin_id}-audit-report.json"',
            },
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.get("/{twin_id}/reports/markdown")
async def download_namespace_twin_markdown_report(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    _: ActorDep,
) -> Response:
    try:
        return Response(
            content=service.report_markdown(twin_id),
            media_type="text/markdown; charset=utf-8",
            headers={
                "Cache-Control": "no-store",
                "X-Data-Mode": "real_core",
                "Content-Disposition": f'attachment; filename="{twin_id}-audit-report.md"',
            },
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)


@router.post("/{twin_id}/cancel")
async def cancel_namespace_twin(
    twin_id: str,
    service: NamespaceTwinServiceDep,
    actor_id: ActorDep,
) -> JSONResponse:
    try:
        return _success(
            service.cancel(twin_id, actor_id=actor_id),
            "Namespace twin generation cancelled.",
        )
    except (NamespaceTwinError, NamespaceTwinPersistenceError) as exc:
        return _error(exc)
