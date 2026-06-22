"""REST API routes for Phase 5 job control."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse

from bosgenesis_mop_execution_agent.api.dependencies import get_api_service, require_api_actor
from bosgenesis_mop_execution_agent.api.service import MopExecutionApiService

router = APIRouter(prefix="/v1", dependencies=[Depends(require_api_actor)])
ApiServiceDep = Annotated[MopExecutionApiService, Depends(get_api_service)]


@router.get("/capabilities")
async def get_capabilities(service: ApiServiceDep) -> JSONResponse:
    return _response(service.capabilities())


@router.get("/config/effective")
async def get_effective_config(service: ApiServiceDep) -> JSONResponse:
    return _response(service.effective_config())


@router.post("/artifact-bundles")
async def register_bundle(
    payload: dict[str, Any],
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.register_bundle(payload))


@router.get("/artifact-bundles")
async def list_bundles(service: ApiServiceDep) -> JSONResponse:
    return _response(service.list_bundles())


@router.post("/artifact-bundles/from-upload")
async def register_uploaded_bundle(
    payload: dict[str, Any],
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.register_bundle(payload))


@router.get("/artifact-bundles/{bundle_id}")
async def get_bundle(
    bundle_id: str,
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.get_bundle(bundle_id))


@router.post("/artifact-bundles/{bundle_id}/validate")
async def validate_bundle(
    bundle_id: str,
    service: ApiServiceDep,
    payload: dict[str, Any] | None = None,
) -> JSONResponse:
    return _response(service.validate_bundle(bundle_id, payload or {}))


@router.post("/execution-jobs")
async def create_job(
    payload: dict[str, Any],
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.create_job(payload))


@router.get("/execution-jobs")
async def list_jobs(service: ApiServiceDep) -> JSONResponse:
    return _response(service.list_jobs())


@router.get("/execution-jobs/{job_id}")
async def get_job(job_id: str, service: ApiServiceDep) -> JSONResponse:
    return _response(service.get_job(job_id))


@router.post("/execution-jobs/{job_id}/start")
async def start_job(job_id: str, service: ApiServiceDep) -> JSONResponse:
    return _response(service.start_job(job_id))


@router.post("/execution-jobs/{job_id}/pause")
async def pause_job(job_id: str, service: ApiServiceDep) -> JSONResponse:
    return _response(service.pause_job(job_id))


@router.post("/execution-jobs/{job_id}/resume")
async def resume_job(job_id: str, service: ApiServiceDep) -> JSONResponse:
    return _response(service.resume_job(job_id))


@router.post("/execution-jobs/{job_id}/cancel")
async def cancel_job(job_id: str, service: ApiServiceDep) -> JSONResponse:
    return _response(service.cancel_job(job_id))


@router.post("/execution-jobs/{job_id}/rollback")
async def request_rollback(
    job_id: str,
    service: ApiServiceDep,
    payload: dict[str, Any] | None = None,
) -> JSONResponse:
    return _response(service.request_rollback(job_id, payload or {}))


@router.post("/execution-jobs/{job_id}/rollback/execute")
async def execute_rollback(
    job_id: str,
    payload: dict[str, Any],
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.execute_rollback(job_id, payload))


@router.post("/execution-jobs/{job_id}/validate")
async def run_validation(job_id: str, service: ApiServiceDep) -> JSONResponse:
    return _response(service.run_validation(job_id))


@router.post("/namespaces/{namespace}/revert")
async def revert_namespace(
    namespace: str,
    payload: dict[str, Any],
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.revert_namespace({"target_namespace": namespace, **payload}))


@router.post("/execution-jobs/{job_id}/instructions")
async def submit_instruction(
    job_id: str,
    payload: dict[str, Any],
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.submit_instruction(job_id, payload))


@router.post("/execution-jobs/{job_id}/approvals")
async def submit_approval(
    job_id: str,
    payload: dict[str, Any],
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.submit_approval(job_id, payload))


@router.get("/execution-jobs/{job_id}/plan")
async def get_plan(job_id: str, service: ApiServiceDep) -> JSONResponse:
    return _response(service.get_plan(job_id))


@router.get("/execution-jobs/{job_id}/observations")
async def list_observations(
    job_id: str,
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.list_observations(job_id))


@router.get("/execution-jobs/{job_id}/events")
async def list_events(job_id: str, service: ApiServiceDep) -> JSONResponse:
    return _response(service.list_events(job_id))


@router.get("/execution-jobs/{job_id}/audit-events")
async def list_audit_events(
    job_id: str,
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.list_audit_events(job_id))


@router.get("/execution-jobs/{job_id}/memory-context")
async def get_memory_context(
    job_id: str,
    service: ApiServiceDep,
    namespace: str | None = Query(default=None),
    chart: str | None = Query(default=None),
    kind: str | None = Query(default=None),
    error_code: str | None = Query(default=None),
    mcp_source: str | None = Query(default=None),
    tenant: str | None = Query(default=None),
    environment: str | None = Query(default=None),
) -> JSONResponse:
    return _response(
        service.memory_context(
            job_id,
            {
                "namespace": namespace,
                "chart": chart,
                "kind": kind,
                "error_code": error_code,
                "mcp_source": mcp_source,
                "tenant": tenant,
                "environment": environment,
            },
        )
    )


@router.get("/execution-jobs/{job_id}/stream")
async def stream_job_events(job_id: str, service: ApiServiceDep) -> StreamingResponse:
    envelope = service.list_events(job_id)
    return StreamingResponse(
        iter([f"event: snapshot\ndata: {envelope}\n\n"]),
        media_type="text/event-stream",
        status_code=200 if envelope.get("ok") is True else 404,
    )


@router.get("/execution-jobs/{job_id}/reports")
async def list_reports(
    job_id: str,
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.list_reports(job_id))


@router.get("/execution-jobs/{job_id}/reports/{report_id}")
async def get_report_metadata(
    job_id: str,
    report_id: str,
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.get_report_metadata(job_id, report_id))


@router.get("/execution-jobs/{job_id}/reports/{report_id}/download", response_model=None)
async def download_report_artifact(
    job_id: str,
    report_id: str,
    service: ApiServiceDep,
    artifact: str = Query(default="pdf"),
) -> Response:
    envelope = service.resolve_report_download(job_id, report_id, artifact)
    if envelope.get("ok") is not True:
        return _response(envelope)
    download = envelope["data"]["download"]
    return FileResponse(
        download["path"],
        media_type=download["media_type"],
        filename=download["filename"],
    )


@router.post("/execution-jobs/{job_id}/reports/release-notes")
async def generate_release_notes(
    job_id: str,
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.generate_release_notes(job_id))


@router.post("/execution-jobs/{job_id}/reports/execution-summary")
async def generate_execution_report(
    job_id: str,
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.generate_execution_report(job_id))


@router.post("/execution-jobs/{job_id}/reports/validation")
async def generate_validation_report(
    job_id: str,
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.generate_validation_report(job_id))


@router.post("/execution-jobs/{job_id}/reports/rollback")
async def generate_rollback_report(
    job_id: str,
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.generate_rollback_report(job_id))


@router.post("/execution-jobs/{job_id}/reports/change-summary")
async def generate_change_report(
    job_id: str,
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.generate_change_report(job_id))


@router.post("/policy/evaluate")
async def evaluate_policy(
    payload: dict[str, Any],
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.evaluate_policy(payload))


@router.post("/redaction/preview")
async def preview_redaction(
    payload: dict[str, Any],
    service: ApiServiceDep,
) -> JSONResponse:
    return _response(service.preview_redaction(payload))


def _response(envelope: dict[str, Any]) -> JSONResponse:
    status = 200 if envelope.get("ok") is True else 404 if _has_code(envelope, "NOT_FOUND") else 409
    return JSONResponse(envelope, status_code=status)


def _has_code(envelope: dict[str, Any], code: str) -> bool:
    return any(block.get("code") == code for block in envelope.get("policy_blocks", []))
