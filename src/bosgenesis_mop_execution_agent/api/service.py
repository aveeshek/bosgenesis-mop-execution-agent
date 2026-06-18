"""Deterministic REST/MCP application service for Phase 5."""

from __future__ import annotations

from typing import Any, cast

from bosgenesis_mop_execution_agent import __version__
from bosgenesis_mop_execution_agent.artifacts.models import BundleSource
from bosgenesis_mop_execution_agent.common.ids import new_id
from bosgenesis_mop_execution_agent.common.time import utc_now
from bosgenesis_mop_execution_agent.models import (
    ActorType,
    ApprovalStatus,
    AuditEvent,
    ExecutionJob,
    ExternalInstruction,
    HumanApproval,
    InstructionType,
    JobState,
    Observation,
    ObservationSeverity,
    ObservationType,
    ReportArtifact,
    ReportType,
)
from bosgenesis_mop_execution_agent.policy import PolicyEvaluationContext, evaluate_policy
from bosgenesis_mop_execution_agent.security import redact_value
from bosgenesis_mop_execution_agent.state.machine import (
    DEFAULT_STATE_MACHINE,
    InvalidTransitionError,
)


class MopExecutionApiService:
    """Small stateful service backing Phase 5 REST and MCP endpoints."""

    def __init__(self) -> None:
        self._bundles: dict[str, dict[str, Any]] = {}
        self._jobs: dict[str, ExecutionJob] = {}
        self._instructions: dict[str, ExternalInstruction] = {}
        self._approvals: dict[str, HumanApproval] = {}
        self._observations: dict[str, list[Observation]] = {}
        self._audit_events: dict[str, list[AuditEvent]] = {}
        self._reports: dict[str, list[ReportArtifact]] = {}
        self._plans: dict[str, dict[str, Any]] = {}

    def health(self) -> dict[str, Any]:
        return self._ok(
            "MoP Execution Agent is healthy.",
            data={
                "status": "ok",
                "service": "bosgenesis-mop-execution-agent",
                "version": __version__,
                "timestamp": utc_now().isoformat(),
            },
        )

    def ready(self) -> dict[str, Any]:
        return self._ok(
            "MoP Execution Agent is ready.",
            data={"status": "ready", "service": "bosgenesis-mop-execution-agent"},
        )

    def capabilities(self) -> dict[str, Any]:
        return self._ok("Capabilities returned.", data=capabilities_payload())

    def effective_config(self) -> dict[str, Any]:
        return self._ok(
            "Effective config returned.",
            data={
                "service_name": "bosgenesis-mop-execution-agent",
                "version": __version__,
                "policy_profile": "namespace-only-v1",
                "redaction_applied": True,
                "secrets": {
                    "database_url": "[REDACTED]",
                    "redis_url": "[REDACTED]",
                    "langfuse_secret_key": "[REDACTED]",
                },
                "guardrails": capabilities_payload()["guardrails"],
            },
            redact_data=False,
        )

    def register_bundle(self, payload: dict[str, Any]) -> dict[str, Any]:
        bundle_id = payload.get("bundle_id") or new_id("bundle")
        source = payload.get("source") or {"type": "local_path", "value": ""}
        target_namespace = str(payload.get("target_namespace") or "")
        self._bundles[bundle_id] = {
            "bundle_id": bundle_id,
            "source": redact_value(source),
            "target_namespace": target_namespace,
            "registered_at": utc_now().isoformat(),
            "validation_status": "not_validated",
        }
        return self._ok(
            "Artifact bundle registered.",
            data=self._bundles[bundle_id],
            bundle_id=bundle_id,
        )

    def list_bundles(self) -> dict[str, Any]:
        bundles = sorted(self._bundles.values(), key=lambda item: str(item["registered_at"]))
        return self._ok("Artifact bundles returned.", data={"bundles": bundles})

    def validate_bundle(self, bundle_id: str | None, payload: dict[str, Any]) -> dict[str, Any]:
        if bundle_id is None:
            registered = self.register_bundle(payload)
            bundle_id = str(registered["bundle_id"])
        bundle = self._bundles.get(bundle_id)
        if bundle is None:
            return self._not_found("Bundle not found.", bundle_id=bundle_id)
        source = bundle.get("source")
        source_valid = isinstance(source, dict)
        if source_valid:
            try:
                BundleSource.model_validate(source)
            except Exception:
                source_valid = False
        bundle["validation_status"] = "valid" if source_valid else "invalid"
        bundle["validated_at"] = utc_now().isoformat()
        return self._ok(
            "Artifact bundle validation complete.",
            data={"bundle": bundle, "valid": source_valid},
            bundle_id=bundle_id,
        )

    def get_bundle(self, bundle_id: str) -> dict[str, Any]:
        bundle = self._bundles.get(bundle_id)
        if bundle is None:
            return self._not_found("Bundle not found.", bundle_id=bundle_id)
        return self._ok("Artifact bundle returned.", data=bundle, bundle_id=bundle_id)

    def create_job(self, payload: dict[str, Any]) -> dict[str, Any]:
        job_id = payload.get("job_id") or new_id("job")
        job = ExecutionJob(
            job_id=str(job_id),
            bundle_id=str(payload.get("bundle_id") or "unregistered"),
            target_namespace=str(payload.get("target_namespace") or ""),
            job_name=payload.get("job_name"),
            correlation_id=payload.get("correlation_id"),
            trace_id=payload.get("trace_id"),
        )
        self._jobs[job.job_id] = job
        self._plans[job.job_id] = payload.get("plan") or {}
        self._add_observation(
            job.job_id,
            "Execution job created.",
            {"state": job.state.value, "bundle_id": job.bundle_id},
        )
        self._add_audit(job.job_id, "job_created", {"bundle_id": job.bundle_id})
        return self._ok(
            "Execution job created.",
            data={"job": _dump(job)},
            job_id=job.job_id,
            state=job.state,
        )

    def list_jobs(self) -> dict[str, Any]:
        jobs = [_dump(job) for job in sorted(self._jobs.values(), key=lambda item: item.created_at)]
        return self._ok("Execution jobs returned.", data={"jobs": jobs})

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            return self._not_found("Job not found.", job_id=job_id)
        return self._ok(
            "Execution job returned.",
            data={"job": _dump(job)},
            job_id=job_id,
            state=job.state,
        )

    def start_job(self, job_id: str) -> dict[str, Any]:
        return self._transition(job_id, JobState.VALIDATING_BUNDLE, "Job start requested.")

    def pause_job(self, job_id: str) -> dict[str, Any]:
        return self._transition(job_id, JobState.PAUSED, "Job pause requested.")

    def resume_job(self, job_id: str) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is not None and job.state == JobState.PAUSED:
            target = JobState.EXECUTING
        else:
            target = JobState.AWAITING_LLM_INSTRUCTION
        return self._transition(job_id, target, "Job resume requested.")

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        return self._transition(job_id, JobState.CANCELLED, "Job cancel requested.")

    def request_rollback(
        self,
        job_id: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self._transition(
            job_id,
            JobState.ROLLBACK_REQUESTED,
            "Rollback requested.",
            payload or {},
        )

    def submit_instruction(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if job_id not in self._jobs:
            return self._not_found("Job not found.", job_id=job_id)
        data = {
            "instruction_id": payload.get("instruction_id") or new_id("instr"),
            "job_id": job_id,
            "instruction_type": payload.get("instruction_type") or InstructionType.CONTINUE.value,
            "controller_id": payload.get("controller_id") or "external-llm-controller",
            "issued_by": payload.get("issued_by") or "external_llm_controller",
            **{key: value for key, value in payload.items() if key not in {"job_id"}},
        }
        instruction = ExternalInstruction.model_validate(data)
        self._instructions[instruction.instruction_id] = instruction
        self._add_observation(
            job_id,
            "External instruction submitted.",
            {"instruction": _dump(instruction)},
        )
        self._add_audit(
            job_id,
            "instruction_submitted",
            {"instruction_id": instruction.instruction_id},
        )
        return self._ok(
            "Instruction submitted.",
            data={"instruction": _dump(instruction)},
            job_id=job_id,
            state=self._jobs[job_id].state,
        )

    def submit_approval(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if job_id not in self._jobs:
            return self._not_found("Job not found.", job_id=job_id)
        data = {
            "approval_id": payload.get("approval_id") or new_id("approval"),
            "job_id": job_id,
            "approver_id": payload.get("approver_id") or "human-approver",
            "approval_scope": payload.get("approval_scope") or "mutation",
            "ticket_reference": payload.get("ticket_reference") or "TICKET-PENDING",
            "statement": payload.get("statement") or "Approval reference recorded.",
            **{key: value for key, value in payload.items() if key not in {"job_id"}},
        }
        approval = HumanApproval.model_validate(data)
        self._approvals[approval.approval_id] = approval
        job = self._jobs[job_id].model_copy(update={"approval_status": ApprovalStatus.ACTIVE})
        self._jobs[job_id] = job
        self._add_observation(job_id, "Human approval submitted.", {"approval": _dump(approval)})
        self._add_audit(job_id, "approval_submitted", {"approval_id": approval.approval_id})
        return self._ok(
            "Approval submitted.",
            data={"approval": _dump(approval), "job": _dump(job)},
            job_id=job_id,
            state=job.state,
        )

    def get_plan(self, job_id: str) -> dict[str, Any]:
        if job_id not in self._jobs:
            return self._not_found("Job not found.", job_id=job_id)
        return self._ok("Plan returned.", data={"plan": self._plans.get(job_id, {})}, job_id=job_id)

    def list_observations(self, job_id: str) -> dict[str, Any]:
        if job_id not in self._jobs:
            return self._not_found("Job not found.", job_id=job_id)
        return self._ok(
            "Observations returned.",
            data={"observations": [_dump(item) for item in self._observations.get(job_id, [])]},
            job_id=job_id,
        )

    def list_events(self, job_id: str) -> dict[str, Any]:
        if job_id not in self._jobs:
            return self._not_found("Job not found.", job_id=job_id)
        events = [_dump(item) for item in self._observations.get(job_id, [])]
        events.extend(_dump(item) for item in self._audit_events.get(job_id, []))
        return self._ok("Events returned.", data={"events": events}, job_id=job_id)

    def list_audit_events(self, job_id: str) -> dict[str, Any]:
        if job_id not in self._jobs:
            return self._not_found("Job not found.", job_id=job_id)
        return self._ok(
            "Audit events returned.",
            data={"audit_events": [_dump(item) for item in self._audit_events.get(job_id, [])]},
            job_id=job_id,
        )

    def memory_context(self, job_id: str) -> dict[str, Any]:
        if job_id not in self._jobs:
            return self._not_found("Job not found.", job_id=job_id)
        return self._ok(
            "Memory context returned.",
            data={
                "memory_context": {
                    "job_id": job_id,
                    "authority": "context_only_not_decision_authority",
                    "redaction_applied": True,
                    "facts": [],
                }
            },
            job_id=job_id,
        )

    def next_required_decision(self, job_id: str) -> dict[str, Any]:
        if job_id not in self._jobs:
            return self._not_found("Job not found.", job_id=job_id)
        return self._ok(
            "Next required decision returned.",
            data={"next_required_decision": None},
            job_id=job_id,
            next_required_decision=None,
        )

    def list_reports(self, job_id: str) -> dict[str, Any]:
        if job_id not in self._jobs:
            return self._not_found("Job not found.", job_id=job_id)
        return self._ok(
            "Reports returned.",
            data={"reports": [_dump(item) for item in self._reports.get(job_id, [])]},
            job_id=job_id,
        )

    def get_report_metadata(self, job_id: str, report_id: str) -> dict[str, Any]:
        if job_id not in self._jobs:
            return self._not_found("Job not found.", job_id=job_id)
        for report in self._reports.get(job_id, []):
            if report.report_id == report_id:
                return self._ok(
                    "Report metadata returned.",
                    data={"report": _dump(report), "download_ready": False},
                    job_id=job_id,
                )
        return self._not_found("Report not found.", job_id=job_id)

    def generate_release_notes(self, job_id: str) -> dict[str, Any]:
        if job_id not in self._jobs:
            return self._not_found("Job not found.", job_id=job_id)
        report = ReportArtifact(
            report_id=new_id("report"),
            report_type=ReportType.RELEASE_NOTES,
            path=f"reports/{job_id}/release-notes.md",
            job_id=job_id,
            download_url=f"/v1/execution-jobs/{job_id}/reports/release-notes",
        )
        self._reports.setdefault(job_id, []).append(report)
        self._add_audit(job_id, "release_notes_requested", {"report_id": report.report_id})
        return self._ok("Release notes requested.", data={"report": _dump(report)}, job_id=job_id)

    def evaluate_policy(self, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            context = PolicyEvaluationContext.model_validate(payload)
        except Exception as exc:
            return self._error(
                "Policy evaluation input is invalid.",
                code="POLICY_INPUT_INVALID",
                data={"error": str(exc)},
            )
        decision = evaluate_policy(context)
        return self._ok(
            "Policy evaluated.",
            data=decision.model_dump(mode="json"),
            policy_blocks=[block.model_dump(mode="json") for block in decision.blocks],
        )

    def preview_redaction(self, payload: dict[str, Any]) -> dict[str, Any]:
        content = payload.get("content", payload)
        return self._ok(
            "Redaction preview returned.",
            data={
                "redacted_content": redact_value(content),
                "redaction_applied": True,
            },
        )

    def _transition(
        self,
        job_id: str,
        target_state: JobState,
        reason: str,
        details: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        job = self._jobs.get(job_id)
        if job is None:
            return self._not_found("Job not found.", job_id=job_id)
        try:
            result = DEFAULT_STATE_MACHINE.transition(
                job_id=job_id,
                from_state=job.state,
                to_state=target_state,
                actor_type=ActorType.WORKER,
                reason=reason,
                details=details,
            )
        except InvalidTransitionError as exc:
            return self._error(
                str(exc),
                code=exc.error_code.value,
                job_id=job_id,
                state=job.state,
            )
        updated = job.model_copy(update={"state": target_state, "updated_at": utc_now()})
        self._jobs[job_id] = updated
        self._observations.setdefault(job_id, []).append(result.observation)
        self._audit_events.setdefault(job_id, []).append(result.audit_event)
        return self._ok(
            reason,
            data={"job": _dump(updated), "transition": result.transition.model_dump(mode="json")},
            job_id=job_id,
            state=target_state,
        )

    def _add_observation(self, job_id: str, summary: str, result: dict[str, Any]) -> None:
        self._observations.setdefault(job_id, []).append(
            Observation(
                observation_id=new_id("obs"),
                job_id=job_id,
                severity=ObservationSeverity.INFO,
                observation_type=ObservationType.POLICY_CHECK,
                summary=summary,
                result=cast("dict[str, Any]", redact_value(result)),
            )
        )

    def _add_audit(self, job_id: str, action: str, details: dict[str, Any]) -> None:
        self._audit_events.setdefault(job_id, []).append(
            AuditEvent(
                audit_event_id=new_id("audit"),
                actor_type=ActorType.WORKER,
                action=action,
                job_id=job_id,
                details=cast("dict[str, Any]", redact_value(details)),
                redacted=True,
            )
        )

    def _ok(
        self,
        message: str,
        *,
        data: dict[str, Any] | None = None,
        job_id: str | None = None,
        bundle_id: str | None = None,
        state: JobState | None = None,
        policy_blocks: list[dict[str, Any]] | None = None,
        next_required_decision: dict[str, Any] | None = None,
        redact_data: bool = True,
    ) -> dict[str, Any]:
        response_data = redact_value(data or {}) if redact_data else data or {}
        return {
            "ok": True,
            "message": message,
            "job_id": job_id,
            "bundle_id": bundle_id,
            "state": state.value if state is not None else None,
            "data": response_data,
            "observations": [],
            "policy_blocks": policy_blocks or [],
            "next_required_decision": next_required_decision,
            "redaction_applied": True,
        }

    def _not_found(
        self,
        message: str,
        *,
        job_id: str | None = None,
        bundle_id: str | None = None,
    ) -> dict[str, Any]:
        return self._error(message, code="NOT_FOUND", job_id=job_id, bundle_id=bundle_id)

    def _error(
        self,
        message: str,
        *,
        code: str,
        data: dict[str, Any] | None = None,
        job_id: str | None = None,
        bundle_id: str | None = None,
        state: JobState | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "message": message,
            "job_id": job_id,
            "bundle_id": bundle_id,
            "state": state.value if state is not None else None,
            "data": redact_value(data or {}),
            "observations": [],
            "policy_blocks": [{"code": code, "message": message, "severity": "block"}],
            "next_required_decision": None,
            "redaction_applied": True,
        }


def capabilities_payload() -> dict[str, Any]:
    return {
        "server_name": "bosgenesis-mop-execution-agent",
        "version": __version__,
        "reasoning_authority": {
            "worker_agent_reasoning_authority": False,
            "external_llm_controller_reasoning_authority": True,
            "guardrails_override_all_inputs": True,
        },
        "guardrails": [
            "target_namespace_only",
            "dry_run_before_mutation",
            "human_approval_before_mutation",
            "no_secret_values",
            "no_production_data_copy",
            "audit_before_mutation",
            "redaction_required",
        ],
        "instruction_types": [item.value for item in InstructionType],
        "tools": [
            "mop_execution_health",
            "mop_execution_get_capabilities",
            "mop_execution_register_bundle",
            "mop_execution_validate_bundle",
            "mop_execution_create_job",
            "mop_execution_get_job",
            "mop_execution_list_jobs",
            "mop_execution_start_job",
            "mop_execution_pause_job",
            "mop_execution_resume_job",
            "mop_execution_cancel_job",
            "mop_execution_submit_instruction",
            "mop_execution_submit_approval",
            "mop_execution_get_plan",
            "mop_execution_get_next_required_decision",
            "mop_execution_list_observations",
            "mop_execution_list_audit_events",
            "mop_execution_get_memory_context",
            "mop_execution_evaluate_policy",
            "mop_execution_request_rollback",
            "mop_execution_generate_release_notes",
        ],
        "mcp_adapters": [
            "bosgenesis_k8s",
            "bosgenesis_helm",
            "data_ingestion_agent",
            "bosgenesis_release_note_agent",
        ],
    }


def _dump(model: Any) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return cast("dict[str, Any]", model.model_dump(mode="json"))
    if isinstance(model, dict):
        return cast("dict[str, Any]", redact_value(model))
    return {"value": model}
