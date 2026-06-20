"""Deterministic REST/MCP application service for Phase 5."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any, cast

from bosgenesis_mop_execution_agent import __version__
from bosgenesis_mop_execution_agent.artifacts.bundle_validator import load_and_validate_bundle
from bosgenesis_mop_execution_agent.artifacts.models import BundleSource
from bosgenesis_mop_execution_agent.common.ids import new_id
from bosgenesis_mop_execution_agent.common.time import utc_now
from bosgenesis_mop_execution_agent.memory import ExecutionMemoryStore
from bosgenesis_mop_execution_agent.models import (
    MEMORY_AUTHORITY,
    ActorType,
    ApprovalStatus,
    AuditEvent,
    ExecutionJob,
    ExternalInstruction,
    HumanApproval,
    InstructionType,
    JobState,
    MemoryLayer,
    MemoryQuery,
    Observation,
    ObservationSeverity,
    ObservationType,
    ReportArtifact,
    ReportType,
)
from bosgenesis_mop_execution_agent.models.enums import ExecutionMode
from bosgenesis_mop_execution_agent.persistence import (
    AppendOnlyAuditWriter,
    InMemoryRedisLikeClient,
    NamespaceLockService,
    WorkerHeartbeatService,
)
from bosgenesis_mop_execution_agent.persistence.repositories import JsonExecutionRepository
from bosgenesis_mop_execution_agent.plans.models import MachineExecutionPlan
from bosgenesis_mop_execution_agent.policy import PolicyEvaluationContext, evaluate_policy
from bosgenesis_mop_execution_agent.runtime.dry_run import DryRunExecutor
from bosgenesis_mop_execution_agent.runtime.mcp_rest_adapters import (
    HelmManagerRestDryRunClient,
    KubernetesInspectorRestDryRunClient,
)
from bosgenesis_mop_execution_agent.runtime.mutation import MutationExecutor
from bosgenesis_mop_execution_agent.runtime.queue import InMemoryJobQueue
from bosgenesis_mop_execution_agent.runtime.worker import WorkerRuntime
from bosgenesis_mop_execution_agent.security import redact_value
from bosgenesis_mop_execution_agent.state.machine import (
    DEFAULT_STATE_MACHINE,
    InvalidTransitionError,
)


class MopExecutionApiService:
    """Small stateful service backing Phase 5 REST and MCP endpoints."""

    def __init__(self) -> None:
        self._repository = JsonExecutionRepository(_repository_path())
        self._redis = InMemoryRedisLikeClient()
        self._bundles: dict[str, dict[str, Any]] = {}
        self._bundle_plans: dict[str, MachineExecutionPlan] = {}
        self._bundle_roots: dict[str, Path] = {}
        self._job_bundle_roots: dict[str, Path] = {}
        self._jobs: dict[str, ExecutionJob] = {}
        self._instructions: dict[str, ExternalInstruction] = {}
        self._approvals: dict[str, HumanApproval] = {}
        self._observations: dict[str, list[Observation]] = {}
        self._audit_events: dict[str, list[AuditEvent]] = {}
        self._reports: dict[str, list[ReportArtifact]] = {}
        self._plans: dict[str, dict[str, Any]] = {}
        self._memory = ExecutionMemoryStore()

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
                "memory": {
                    "enabled": _env_bool("MEMORY_ENABLED", default=True),
                    "authority": MEMORY_AUTHORITY,
                    "postgres_enabled": _env_bool("MEMORY_POSTGRES_ENABLED", default=True),
                    "storage": "postgresql",
                    "schema": os.getenv("POSTGRES_SCHEMA", "mop_execution"),
                    "dsn": "[REDACTED]",
                },
                "postgres": {
                    "enabled": _env_bool("POSTGRES_ENABLED", default=True),
                    "schema": os.getenv("POSTGRES_SCHEMA", "mop_execution"),
                    "dsn": "[REDACTED]",
                },
                "secrets": {
                    "database_url": "[REDACTED]",
                    "postgres_dsn": "[REDACTED]",
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
        try:
            bundle_source = BundleSource.model_validate(source)
            loaded = load_and_validate_bundle(
                bundle_source,
                str(bundle.get("target_namespace") or payload.get("target_namespace") or ""),
            )
            source_valid = True
            bundle["validation_status"] = "valid"
            bundle["root_path"] = str(loaded.root_path)
            bundle["phase_count"] = len(loaded.machine_plan.phases)
            bundle["step_count"] = sum(len(phase.steps) for phase in loaded.machine_plan.phases)
            bundle["manifest_count"] = len(loaded.manifests)
            self._bundle_plans[bundle_id] = loaded.machine_plan
            self._bundle_roots[bundle_id] = loaded.root_path
        except Exception as exc:
            source_valid = False
            bundle["validation_status"] = "invalid"
            bundle["validation_error"] = str(redact_value(str(exc)))
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
            mop_id=payload.get("mop_id"),
            run_id=payload.get("run_id"),
            correlation_id=payload.get("correlation_id"),
            trace_id=payload.get("trace_id"),
            source_namespace=payload.get("source_namespace"),
            execution_mode=_execution_mode(payload.get("execution_mode")),
        )
        self._save_job(job)
        plan_payload = payload.get("plan") or {}
        self._plans[job.job_id] = plan_payload
        self._seed_job_plan_if_available(job)
        self._add_observation(
            job.job_id,
            "Execution job created.",
            {"state": job.state.value, "bundle_id": job.bundle_id},
        )
        self._add_audit(job.job_id, "job_created", {"bundle_id": job.bundle_id})
        self._write_memory(
            MemoryLayer.DURABLE_JOB,
            job.job_id,
            "Execution job created.",
            {"job": _dump(job)},
            namespace=job.target_namespace,
            tenant=payload.get("tenant"),
            environment=payload.get("environment"),
        )
        self._write_memory(
            MemoryLayer.IN_RUN,
            job.job_id,
            "In-run execution context initialized.",
            {"state": job.state.value, "execution_mode": job.execution_mode.value},
            namespace=job.target_namespace,
            tenant=payload.get("tenant"),
            environment=payload.get("environment"),
        )
        return self._ok(
            "Execution job created.",
            data={"job": _dump(job)},
            job_id=job.job_id,
            state=job.state,
        )

    def list_jobs(self) -> dict[str, Any]:
        jobs = [
            _dump(job)
            for job in sorted(self._repository.list_jobs(), key=lambda item: item.created_at)
        ]
        return self._ok("Execution jobs returned.", data={"jobs": jobs})

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self._get_job(job_id)
        if job is None:
            return self._not_found("Job not found.", job_id=job_id)
        return self._ok(
            "Execution job returned.",
            data={"job": _dump(job)},
            job_id=job_id,
            state=job.state,
        )

    def start_job(self, job_id: str) -> dict[str, Any]:
        job = self._get_job(job_id)
        if job is None:
            return self._not_found("Job not found.", job_id=job_id)
        if (
            job.execution_mode
            in {ExecutionMode.DRY_RUN_ONLY, ExecutionMode.EXECUTE_AFTER_APPROVAL}
            and self._repository.get_steps(job_id)
        ):
            runtime = self._runtime_for_job(job)
            runtime.enqueue(job_id)
            last_action = "queued"
            max_ticks = _env_int("MOP_EXECUTION_START_MAX_TICKS", default=100)
            for _ in range(max_ticks):
                decision = runtime.run_once()
                last_action = decision.action
                if not decision.requeue:
                    break
            updated = self._get_job(job_id) or job
            return self._ok(
                "Job start processed.",
                data={"job": _dump(updated), "runtime_action": last_action},
                job_id=job_id,
                state=updated.state,
            )
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
        job = self._get_job(job_id)
        if job is None:
            return self._not_found("Job not found.", job_id=job_id)
        self._add_audit(
            job_id,
            "instruction_received",
            {
                "instruction_id": payload.get("instruction_id"),
                "issued_by": payload.get("issued_by"),
            },
        )
        data = {
            "instruction_id": payload.get("instruction_id") or new_id("instr"),
            "job_id": job_id,
            "instruction_type": payload.get("instruction_type") or InstructionType.CONTINUE.value,
            "controller_id": payload.get("controller_id") or "external-llm-controller",
            "issued_by": payload.get("issued_by") or "external_llm_controller",
            **{key: value for key, value in payload.items() if key not in {"job_id"}},
        }
        try:
            instruction = ExternalInstruction.model_validate(data)
        except Exception as exc:
            self._add_audit(
                job_id,
                "instruction_rejected",
                {"reason_code": "INSTRUCTION_SCHEMA_INVALID"},
            )
            return self._error(
                "Instruction input is invalid.",
                code="INSTRUCTION_SCHEMA_INVALID",
                data={"error": str(exc)},
                job_id=job_id,
                state=job.state,
            )
        unsafe_types = {
            InstructionType.PATCH_MANIFEST,
            InstructionType.REPLACE_MANIFEST,
            InstructionType.INVOKE_MCP_TOOL,
            InstructionType.ROLLBACK,
        }
        if instruction.instruction_type in unsafe_types:
            self._add_audit(
                job_id,
                "instruction_policy_blocked",
                {
                    "instruction_id": instruction.instruction_id,
                    "reason_code": "UNSAFE_INSTRUCTION_BLOCKED",
                },
            )
            return self._error(
                "Instruction "
                f"{instruction.instruction_type.value} requires a dedicated policy path.",
                code="UNSAFE_INSTRUCTION_BLOCKED",
                job_id=job_id,
                state=job.state,
            )
        if self._repository.get_steps(job_id):
            runtime_decision = self._runtime_for_job(job).submit_instruction(instruction)
            if not runtime_decision.accepted:
                self._add_audit(
                    job_id,
                    "instruction_rejected",
                    {"reason_code": runtime_decision.reason_code or "INSTRUCTION_REJECTED"},
                )
                return self._error(
                    "Instruction was rejected by the runtime gate.",
                    code=runtime_decision.reason_code or "INSTRUCTION_REJECTED",
                    job_id=job_id,
                    state=job.state,
                )
        else:
            self._repository.save_instruction(instruction)
        self._instructions[instruction.instruction_id] = instruction
        self._add_observation(
            job_id,
            "External instruction submitted.",
            {"instruction": _dump(instruction)},
        )
        self._add_audit(
            job_id,
            "instruction_accepted",
            {"instruction_id": instruction.instruction_id},
        )
        self._write_memory(
            MemoryLayer.EPISODIC_EXECUTION,
            job_id,
            "External instruction accepted.",
            {"instruction": _dump(instruction)},
            namespace=job.target_namespace,
        )
        return self._ok(
            "Instruction submitted.",
            data={"instruction": _dump(instruction)},
            job_id=job_id,
            state=(self._get_job(job_id) or job).state,
        )

    def submit_approval(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        if self._get_job(job_id) is None:
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
        self._repository.save_approval(approval)
        job = (self._get_job(job_id) or self._jobs[job_id]).model_copy(
            update={"approval_status": ApprovalStatus.ACTIVE}
        )
        self._save_job(job)
        self._add_observation(job_id, "Human approval submitted.", {"approval": _dump(approval)})
        self._add_audit(job_id, "approval_submitted", {"approval_id": approval.approval_id})
        self._write_memory(
            MemoryLayer.APPROVAL,
            job_id,
            "Human approval submitted.",
            {"approval": _dump(approval)},
            namespace=job.target_namespace,
        )
        return self._ok(
            "Approval submitted.",
            data={"approval": _dump(approval), "job": _dump(job)},
            job_id=job_id,
            state=job.state,
        )

    def get_plan(self, job_id: str) -> dict[str, Any]:
        if self._get_job(job_id) is None:
            return self._not_found("Job not found.", job_id=job_id)
        return self._ok("Plan returned.", data={"plan": self._plans.get(job_id, {})}, job_id=job_id)

    def list_observations(self, job_id: str) -> dict[str, Any]:
        if self._get_job(job_id) is None:
            return self._not_found("Job not found.", job_id=job_id)
        observations = self._repository.get_observations(job_id) or self._observations.get(
            job_id,
            [],
        )
        return self._ok(
            "Observations returned.",
            data={"observations": [_dump(item) for item in observations]},
            job_id=job_id,
        )

    def list_events(self, job_id: str) -> dict[str, Any]:
        if self._get_job(job_id) is None:
            return self._not_found("Job not found.", job_id=job_id)
        events = [_dump(item) for item in self._repository.get_observations(job_id)]
        events.extend(_dump(item) for item in self._repository.list_audit_events(job_id))
        return self._ok("Events returned.", data={"events": events}, job_id=job_id)

    def list_audit_events(self, job_id: str) -> dict[str, Any]:
        if self._get_job(job_id) is None:
            return self._not_found("Job not found.", job_id=job_id)
        audit_events = self._repository.list_audit_events(job_id) or self._audit_events.get(
            job_id,
            [],
        )
        return self._ok(
            "Audit events returned.",
            data={"audit_events": [_dump(item) for item in audit_events]},
            job_id=job_id,
        )

    def memory_context(
        self,
        job_id: str,
        filters: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._get_job(job_id) is None:
            return self._not_found("Job not found.", job_id=job_id)
        query = MemoryQuery(job_id=job_id, **_memory_filters(filters or {}))
        records = [
            record.model_dump(mode="json")
            for record in self._memory.query(query)
        ]
        return self._ok(
            "Memory context returned.",
            data={
                "memory_context": {
                    "job_id": job_id,
                    "authority": MEMORY_AUTHORITY,
                    "redaction_applied": True,
                    "filters": query.model_dump(mode="json", exclude_none=True),
                    "records": records,
                    "facts": records,
                }
            },
            job_id=job_id,
        )

    def next_required_decision(self, job_id: str) -> dict[str, Any]:
        job = self._get_job(job_id)
        if job is None:
            return self._not_found("Job not found.", job_id=job_id)
        latest_decision = None
        for observation in reversed(self._observations.get(job_id, [])):
            if observation.next_required_decision:
                latest_decision = observation.next_required_decision
                break
        if latest_decision is None and job.state == JobState.DECISION_REQUIRED:
            latest_decision = {
                "job_id": job_id,
                "target_namespace": job.target_namespace,
                "authority": MEMORY_AUTHORITY,
                "reason_code": "DECISION_REQUIRED",
                "summary": "External LLM instruction is required.",
                "required_from": "external_llm",
                "allowed_next_action_types": ["continue", "retry", "wait", "skip", "abort"],
                "memory": {
                    "authority": MEMORY_AUTHORITY,
                    "records": [
                        record.model_dump(mode="json")
                        for record in self._memory.query(MemoryQuery(job_id=job_id))
                    ],
                },
            }
        return self._ok(
            "Next required decision returned.",
            data={"next_required_decision": latest_decision},
            job_id=job_id,
            next_required_decision=latest_decision,
        )

    def list_reports(self, job_id: str) -> dict[str, Any]:
        if self._get_job(job_id) is None:
            return self._not_found("Job not found.", job_id=job_id)
        return self._ok(
            "Reports returned.",
            data={"reports": [_dump(item) for item in self._reports.get(job_id, [])]},
            job_id=job_id,
        )

    def get_report_metadata(self, job_id: str, report_id: str) -> dict[str, Any]:
        if self._get_job(job_id) is None:
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
        if self._get_job(job_id) is None:
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
        if context.job_id:
            self._write_memory(
                MemoryLayer.POLICY,
                context.job_id,
                "Policy evaluated.",
                decision.model_dump(mode="json"),
                namespace=context.target_namespace,
                error_code=decision.blocks[0].code if decision.blocks else None,
            )
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
        job = self._get_job(job_id)
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
        self._save_job(updated)
        self._observations.setdefault(job_id, []).append(result.observation)
        self._audit_events.setdefault(job_id, []).append(result.audit_event)
        self._repository.add_observation(result.observation)
        self._repository.append_audit_event(result.audit_event)
        self._write_memory(
            MemoryLayer.EPISODIC_EXECUTION,
            job_id,
            f"Job transitioned to {target_state.value}.",
            result.transition.model_dump(mode="json"),
            namespace=updated.target_namespace,
        )
        self._write_memory(
            MemoryLayer.AUDIT,
            job_id,
            "State transition audit event recorded.",
            result.audit_event.model_dump(mode="json"),
            namespace=updated.target_namespace,
        )
        return self._ok(
            reason,
            data={"job": _dump(updated), "transition": result.transition.model_dump(mode="json")},
            job_id=job_id,
            state=target_state,
        )

    def _add_observation(self, job_id: str, summary: str, result: dict[str, Any]) -> None:
        observation = Observation(
            observation_id=new_id("obs"),
            job_id=job_id,
            severity=ObservationSeverity.INFO,
            observation_type=ObservationType.POLICY_CHECK,
            summary=summary,
            result=cast("dict[str, Any]", redact_value(result)),
        )
        self._observations.setdefault(job_id, []).append(observation)
        self._repository.add_observation(observation)
        job = self._get_job(job_id)
        self._write_memory(
            MemoryLayer.OBSERVABILITY,
            job_id,
            summary,
            observation.model_dump(mode="json"),
            namespace=job.target_namespace if job else None,
        )

    def _add_audit(self, job_id: str, action: str, details: dict[str, Any]) -> None:
        event = AuditEvent(
            audit_event_id=new_id("audit"),
            actor_type=ActorType.WORKER,
            action=action,
            job_id=job_id,
            details=cast("dict[str, Any]", redact_value(details)),
            redacted=True,
        )
        self._audit_events.setdefault(job_id, []).append(event)
        self._repository.append_audit_event(event)
        job = self._get_job(job_id)
        self._write_memory(
            MemoryLayer.AUDIT,
            job_id,
            f"Audit event recorded: {action}.",
            event.model_dump(mode="json"),
            namespace=job.target_namespace if job else None,
        )

    def _write_memory(
        self,
        layer: MemoryLayer,
        job_id: str,
        summary: str,
        payload: dict[str, Any],
        *,
        namespace: str | None = None,
        chart: str | None = None,
        kind: str | None = None,
        resource_name: str | None = None,
        error_code: str | None = None,
        mcp_source: str | None = None,
        tenant: str | None = None,
        environment: str | None = None,
    ) -> None:
        self._memory.write(
            layer=layer,
            job_id=job_id,
            summary=summary,
            payload=payload,
            namespace=namespace,
            chart=chart,
            kind=kind,
            resource_name=resource_name,
            error_code=error_code,
            mcp_source=mcp_source,
            tenant=tenant,
            environment=environment,
        )

    def _save_job(self, job: ExecutionJob) -> None:
        self._jobs[job.job_id] = job
        self._repository.save_job(job)

    def _get_job(self, job_id: str) -> ExecutionJob | None:
        return self._repository.get_job(job_id) or self._jobs.get(job_id)

    def _seed_job_plan_if_available(self, job: ExecutionJob) -> None:
        plan = self._bundle_plans.get(job.bundle_id)
        bundle_root = self._bundle_roots.get(job.bundle_id)
        bundle = self._bundles.get(job.bundle_id)
        if plan is None and bundle is not None and bundle.get("validation_status") != "invalid":
            try:
                bundle_source = BundleSource.model_validate(bundle.get("source"))
                loaded = load_and_validate_bundle(bundle_source, job.target_namespace)
            except Exception:
                return
            plan = loaded.machine_plan
            bundle_root = loaded.root_path
            self._bundle_plans[job.bundle_id] = plan
            self._bundle_roots[job.bundle_id] = bundle_root
        if plan is None or bundle_root is None:
            return
        runtime = self._runtime_for_job(job, bundle_root=bundle_root)
        runtime.seed_plan(job.job_id, plan)
        self._job_bundle_roots[job.job_id] = bundle_root
        self._plans[job.job_id] = plan.raw_machine_execution_plan

    def _runtime_for_job(
        self,
        job: ExecutionJob,
        *,
        bundle_root: Path | None = None,
    ) -> WorkerRuntime:
        root = bundle_root or self._job_bundle_roots.get(job.job_id) or self._bundle_roots.get(
            job.bundle_id
        )
        dry_runs = None
        mutations = None
        if root is not None:
            k8s_client = KubernetesInspectorRestDryRunClient(
                base_url=os.getenv(
                    "K8S_INSPECTOR_MCP_ENDPOINT",
                    "http://bosgenesis-k8s-inspector-mcp:8080",
                ),
                api_key=os.getenv("K8S_INSPECTOR_API_KEY") or os.getenv("BOSGENESIS_API_KEY"),
                job_id=job.job_id,
                correlation_id=job.correlation_id,
                trace_id=job.trace_id,
            )
            helm_client = HelmManagerRestDryRunClient(
                base_url=os.getenv(
                    "HELM_MANAGER_MCP_ENDPOINT",
                    "http://bosgenesis-helm-manager-mcp:8080",
                ),
                api_key=os.getenv("HELM_MANAGER_API_KEY") or os.getenv("BOSGENESIS_API_KEY"),
                job_id=job.job_id,
                correlation_id=job.correlation_id,
                trace_id=job.trace_id,
            )
            dry_runs = DryRunExecutor(
                bundle_root=root,
                k8s_client=k8s_client,
                helm_client=helm_client,
            )
            mutations = MutationExecutor(
                bundle_root=root,
                k8s_client=k8s_client,
                helm_client=helm_client,
                audit_writer=AppendOnlyAuditWriter(self._repository),
            )
        return WorkerRuntime(
            repository=self._repository,
            queue=InMemoryJobQueue(self._repository),
            lock_service=NamespaceLockService(self._redis),
            heartbeat_service=WorkerHeartbeatService(self._redis),
            worker_id="api-inline-worker",
            dry_runs=dry_runs,
            mutations=mutations,
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


def _memory_filters(payload: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "namespace",
        "chart",
        "kind",
        "error_code",
        "mcp_source",
        "tenant",
        "environment",
    }
    return {key: value for key, value in payload.items() if key in allowed and value}


def _repository_path() -> Path:
    configured = os.getenv("MOP_EXECUTION_REPOSITORY_PATH")
    if configured:
        return Path(configured)
    artifact_root = os.getenv("ARTIFACT_ROOT_PATH")
    if artifact_root:
        return Path(artifact_root) / "repository.json"
    return Path(tempfile.mkdtemp(prefix="bosgenesis-mop-execution-agent-")) / "repository.json"


def _execution_mode(value: Any) -> ExecutionMode:
    if value is None:
        return ExecutionMode.EXTERNAL_LLM_CONTROLLED
    try:
        return ExecutionMode(str(value))
    except ValueError:
        return ExecutionMode.EXTERNAL_LLM_CONTROLLED


def _env_int(name: str, *, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
