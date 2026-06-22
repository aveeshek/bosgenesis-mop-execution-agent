"""Deterministic REST/MCP application service for Phase 5."""

from __future__ import annotations

import os
import tempfile
import threading
import time
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, cast

from bosgenesis_mop_execution_agent import __version__
from bosgenesis_mop_execution_agent.artifacts.bundle_validator import load_and_validate_bundle
from bosgenesis_mop_execution_agent.artifacts.models import BundleSource
from bosgenesis_mop_execution_agent.common.ids import new_id
from bosgenesis_mop_execution_agent.common.time import utc_now
from bosgenesis_mop_execution_agent.mcp_clients.release_notes import ReleaseNoteMcpClient
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
    InMemoryRedisLikeClient,
)
from bosgenesis_mop_execution_agent.persistence.repositories import JsonExecutionRepository
from bosgenesis_mop_execution_agent.plans.models import MachineExecutionPlan
from bosgenesis_mop_execution_agent.policy import PolicyEvaluationContext, evaluate_policy
from bosgenesis_mop_execution_agent.reports import ReportGenerator
from bosgenesis_mop_execution_agent.runtime.factory import (
    create_rollback_executor,
    create_validation_executor,
    create_worker_runtime,
    with_bundle_root_link,
)
from bosgenesis_mop_execution_agent.runtime.mcp_rest_adapters import (
    HttpMcpCompatibilityTransport,
)
from bosgenesis_mop_execution_agent.runtime.queue import InMemoryJobQueue
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
        self._queue = InMemoryJobQueue(self._repository)
        self._runtime = create_worker_runtime(
            repository=self._repository,
            queue=self._queue,
            redis_client=self._redis,
            worker_id="api-background-worker",
        )
        self._worker_stop = threading.Event()
        self._worker_lock = threading.Lock()
        self._worker_thread: threading.Thread | None = None
        if _env_bool("MOP_EXECUTION_RECOVER_ON_STARTUP", default=True):
            recovered = self._runtime.recover_restartable_jobs()
            if recovered:
                self._ensure_background_worker()

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
            self._runtime.enqueue(job_id)
            self._ensure_background_worker()
            updated = self._get_job(job_id) or job
            return self._ok(
                "Job queued for asynchronous execution.",
                data={"job": _dump(updated), "runtime_action": "queued"},
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

    def run_validation(self, job_id: str) -> dict[str, Any]:
        job = self._get_job(job_id)
        if job is None:
            return self._not_found("Job not found.", job_id=job_id)
        steps = self._repository.get_steps(job_id)
        result = create_validation_executor(job).execute(job=job, steps=steps)
        payload = _dataclass_payload(result)
        self._record_observation(
            Observation(
                observation_id=new_id("obs"),
                job_id=job_id,
                severity=ObservationSeverity.INFO if result.success else ObservationSeverity.ERROR,
                observation_type=ObservationType.VALIDATION_RESULT,
                summary="Post-execution validation completed.",
                correlation_id=job.correlation_id,
                trace_id=job.trace_id,
                result=payload,
                redaction_applied=True,
            )
        )
        self._add_audit(
            job_id,
            "validation_completed",
            {"success": str(result.success), "check_count": str(len(result.checks))},
        )
        report = self._record_report(
            job,
            ReportType.VALIDATION_REPORT,
            "BOS Genesis Validation Report",
            sections={"validation_result": payload},
            warnings=result.warnings,
        )
        return self._ok(
            "Validation completed.",
            data={"validation": payload, "report": self._report_payload(report)},
            job_id=job_id,
            state=job.state,
        )

    def execute_rollback(self, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        job = self._get_job(job_id)
        if job is None:
            return self._not_found("Job not found.", job_id=job_id)
        if payload.get("confirm") is not True:
            return self._error(
                "Rollback execution requires confirm=true.",
                code="ROLLBACK_CONFIRMATION_REQUIRED",
                job_id=job_id,
                state=job.state,
            )
        if job.state not in {JobState.ROLLBACK_REQUESTED, JobState.ROLLING_BACK}:
            return self._error(
                "Rollback execution requires a rollback-requested job.",
                code="ROLLBACK_REQUEST_REQUIRED",
                job_id=job_id,
                state=job.state,
            )
        if job.state == JobState.ROLLBACK_REQUESTED:
            transitioned = self._transition(
                job_id,
                JobState.ROLLING_BACK,
                "Rollback execution started.",
                {"mode": str(payload.get("mode") or "namespace_revert")},
            )
            if not transitioned["ok"]:
                return transitioned
            job = self._get_job(job_id) or job

        result = create_rollback_executor(job).execute(
            job=job,
            approvals=self._repository.get_approvals(job_id),
            instructions=self._repository.get_instructions(job_id),
            mode=str(payload.get("mode") or "namespace_revert"),
            dry_run=bool(payload.get("dry_run", False)),
            release_name=payload.get("release_name"),
            revision=payload.get("revision"),
            force_purge_release_storage=bool(
                payload.get("force_purge_release_storage", True)
            ),
        )
        payload_data = _dataclass_payload(result)
        self._record_observation(
            Observation(
                observation_id=new_id("obs"),
                job_id=job_id,
                severity=ObservationSeverity.INFO if result.success else ObservationSeverity.ERROR,
                observation_type=ObservationType.MUTATION_RESULT,
                summary="Rollback execution completed.",
                correlation_id=job.correlation_id,
                trace_id=job.trace_id,
                result=payload_data,
                redaction_applied=True,
            )
        )
        self._add_audit(
            job_id,
            "rollback_executed",
            {"success": str(result.success), "step_count": str(len(result.steps))},
        )
        report = self._record_report(
            job,
            ReportType.ROLLBACK_REPORT,
            "BOS Genesis Rollback Report",
            sections={"rollback_result": payload_data},
            warnings=result.warnings,
        )
        latest = self._get_job(job_id) or job
        if result.success and JobState.COMPLETED in DEFAULT_STATE_MACHINE.allowed_targets(
            latest.state
        ):
            self._transition(job_id, JobState.COMPLETED, "Rollback completed.", payload_data)
        elif (
            not result.success
            and JobState.DECISION_REQUIRED
            in DEFAULT_STATE_MACHINE.allowed_targets(latest.state)
        ):
            failed_transition = self._transition(
                job_id,
                JobState.DECISION_REQUIRED,
                "Rollback requires external decision.",
                {"warnings": result.warnings},
            )
            failed_state = (
                self._get_job(job_id) or latest
            ).state
            return self._error(
                "Rollback execution failed and requires external decision.",
                code="ROLLBACK_FAILED",
                data={
                    "rollback": payload_data,
                    "report": self._report_payload(report),
                    "transition": failed_transition.get("data", {}),
                },
                job_id=job_id,
                state=failed_state,
            )
        return self._ok(
            "Rollback execution completed.",
            data={"rollback": payload_data, "report": self._report_payload(report)},
            job_id=job_id,
            state=(self._get_job(job_id) or latest).state,
        )

    def revert_namespace(self, payload: dict[str, Any]) -> dict[str, Any]:
        namespace = str(payload.get("target_namespace") or payload.get("namespace") or "")
        if not namespace:
            return self._error(
                "Namespace revert requires target_namespace.",
                code="TARGET_NAMESPACE_REQUIRED",
            )
        if payload.get("confirm") is not True:
            return self._error(
                "Namespace revert requires confirm=true.",
                code="NAMESPACE_REVERT_CONFIRMATION_REQUIRED",
            )
        job_id = str(payload.get("job_id") or new_id("job"))
        job = self._get_job(job_id)
        if job is None:
            job = ExecutionJob(
                job_id=job_id,
                bundle_id="namespace-revert",
                target_namespace=namespace,
                job_name="agent-ai-namespace-revert",
                correlation_id=payload.get("correlation_id"),
                trace_id=payload.get("trace_id"),
                state=JobState.ROLLING_BACK,
                execution_mode=ExecutionMode.EXTERNAL_LLM_CONTROLLED,
            )
            self._save_job(job)
            self._add_audit(
                job_id,
                "namespace_revert_job_created",
                {"target_namespace": namespace},
            )
        result = create_rollback_executor(job).revert_namespace(
            job=job,
            mode=str(payload.get("mode") or "namespace_revert"),
            dry_run=bool(payload.get("dry_run", False)),
            release_name=payload.get("release_name"),
            revision=payload.get("revision"),
            force_purge_release_storage=bool(
                payload.get("force_purge_release_storage", True)
            ),
        )
        result_payload = _dataclass_payload(result)
        self._record_observation(
            Observation(
                observation_id=new_id("obs"),
                job_id=job.job_id,
                severity=ObservationSeverity.INFO if result.success else ObservationSeverity.ERROR,
                observation_type=ObservationType.MUTATION_RESULT,
                summary=f"Namespace revert completed for {namespace}.",
                correlation_id=job.correlation_id,
                trace_id=job.trace_id,
                result=result_payload,
                redaction_applied=True,
            )
        )
        self._add_audit(
            job.job_id,
            "namespace_reverted",
            {"target_namespace": namespace, "success": str(result.success)},
        )
        report = self._record_report(
            job,
            ReportType.ROLLBACK_REPORT,
            "BOS Genesis Namespace Revert Report",
            sections={"namespace_revert_result": result_payload},
            warnings=result.warnings,
        )
        latest = self._get_job(job.job_id) or job
        if result.success and latest.state == JobState.ROLLING_BACK:
            self._transition(job.job_id, JobState.COMPLETED, "Namespace revert completed.")
        elif not result.success and latest.state == JobState.ROLLING_BACK:
            failed_transition = self._transition(
                job.job_id,
                JobState.DECISION_REQUIRED,
                "Namespace revert requires external decision.",
                {"warnings": result.warnings},
            )
            failed_state = (
                self._get_job(job.job_id) or latest
            ).state
            return self._error(
                "Namespace revert failed and requires external decision.",
                code="NAMESPACE_REVERT_FAILED",
                data={
                    "rollback": result_payload,
                    "report": self._report_payload(report),
                    "transition": failed_transition.get("data", {}),
                },
                job_id=job.job_id,
                state=failed_state,
            )
        return self._ok(
            "Namespace revert completed.",
            data={"rollback": result_payload, "report": self._report_payload(report)},
            job_id=job.job_id,
            state=(self._get_job(job.job_id) or latest).state,
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
        if (
            instruction.instruction_type == InstructionType.ROLLBACK
            and job.state != JobState.ROLLBACK_REQUESTED
        ):
            self._add_audit(
                job_id,
                "instruction_policy_blocked",
                {
                    "instruction_id": instruction.instruction_id,
                    "reason_code": "ROLLBACK_STATE_REQUIRED",
                },
            )
            return self._error(
                "Rollback instructions require a rollback-requested job.",
                code="ROLLBACK_STATE_REQUIRED",
                job_id=job_id,
                state=job.state,
            )
        if self._repository.get_steps(job_id):
            runtime_decision = self._runtime.submit_instruction(instruction)
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
        if self._repository.get_steps(job_id):
            self._ensure_background_worker()
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
        reports_by_id = {
            report.report_id: report
            for report in self._repository.get_reports(job_id)
        }
        reports_by_id.update(
            {report.report_id: report for report in self._reports.get(job_id, [])}
        )
        return self._ok(
            "Reports returned.",
            data={
                "reports": [
                    self._report_payload(item)
                    for item in sorted(
                        reports_by_id.values(),
                        key=lambda report: report.created_at,
                    )
                ]
            },
            job_id=job_id,
        )

    def get_report_metadata(self, job_id: str, report_id: str) -> dict[str, Any]:
        if self._get_job(job_id) is None:
            return self._not_found("Job not found.", job_id=job_id)
        report = self._find_report(job_id, report_id)
        if report is None:
            return self._not_found("Report not found.", job_id=job_id)
        download_ready = self.resolve_report_download(job_id, report_id, "pdf")["ok"] is True
        return self._ok(
            "Report metadata returned.",
            data={"report": self._report_payload(report), "download_ready": download_ready},
            job_id=job_id,
        )

    def report_download_metadata(
        self,
        job_id: str,
        report_id: str,
        artifact: str = "pdf",
    ) -> dict[str, Any]:
        resolved = self.resolve_report_download(job_id, report_id, artifact)
        if not resolved["ok"]:
            return resolved
        download = dict(resolved["data"]["download"])
        download.pop("path", None)
        return self._ok(
            "Report download metadata returned.",
            data={"download": download},
            job_id=job_id,
            redact_data=False,
        )

    def resolve_report_download(
        self,
        job_id: str,
        report_id: str,
        artifact: str = "pdf",
    ) -> dict[str, Any]:
        if self._get_job(job_id) is None:
            return self._not_found("Job not found.", job_id=job_id)
        report = self._find_report(job_id, report_id)
        if report is None:
            return self._not_found("Report not found.", job_id=job_id)
        artifact_key = (artifact or "pdf").strip().lower()
        artifact_fields = {
            "markdown": ("path", "text/markdown; charset=utf-8"),
            "md": ("path", "text/markdown; charset=utf-8"),
            "html": ("html_path", "text/html; charset=utf-8"),
            "pdf": ("pdf_path", "application/pdf"),
            "archive": ("archive_path", "application/zip"),
            "zip": ("archive_path", "application/zip"),
        }
        field_and_media = artifact_fields.get(artifact_key)
        if field_and_media is None:
            return self._error(
                "Unsupported report artifact requested.",
                code="REPORT_ARTIFACT_UNSUPPORTED",
                data={"artifact": artifact, "supported_artifacts": sorted(artifact_fields)},
                job_id=job_id,
            )
        field_name, media_type = field_and_media
        raw_path = getattr(report, field_name)
        if not raw_path:
            return self._error(
                "Report artifact is not available for this report.",
                code="REPORT_ARTIFACT_UNAVAILABLE",
                data={"artifact": artifact_key, "field": field_name},
                job_id=job_id,
            )
        artifact_path = Path(raw_path)
        if not artifact_path.is_absolute():
            artifact_path = _artifact_root_path(self._repository.path) / artifact_path
        artifact_path = artifact_path.resolve()
        artifact_root = _artifact_root_path(self._repository.path).resolve()
        try:
            artifact_path.relative_to(artifact_root)
        except ValueError:
            return self._error(
                "Report artifact path is outside the configured artifact root.",
                code="REPORT_ARTIFACT_PATH_OUTSIDE_ROOT",
                job_id=job_id,
            )
        if not artifact_path.exists() or not artifact_path.is_file():
            return self._not_found("Report artifact file not found.", job_id=job_id)
        normalized_artifact = (
            "archive"
            if artifact_key == "zip"
            else "markdown"
            if artifact_key == "md"
            else artifact_key
        )
        return self._ok(
            "Report artifact resolved.",
            data={
                "download": {
                    "job_id": job_id,
                    "report_id": report_id,
                    "report_type": report.report_type.value,
                    "artifact": normalized_artifact,
                    "filename": artifact_path.name,
                    "media_type": media_type,
                    "size_bytes": artifact_path.stat().st_size,
                    "download_url": _report_download_url(job_id, report_id, normalized_artifact),
                    "path": str(artifact_path),
                }
            },
            job_id=job_id,
            redact_data=False,
        )

    def generate_execution_report(self, job_id: str) -> dict[str, Any]:
        job = self._get_job(job_id)
        if job is None:
            return self._not_found("Job not found.", job_id=job_id)
        report = self._record_report(
            job,
            ReportType.EXECUTION_SUMMARY,
            "BOS Genesis Execution Report",
            sections=self._change_sections(job),
        )
        return self._ok(
            "Execution report generated.",
            data={"report": self._report_payload(report)},
            job_id=job_id,
        )

    def generate_validation_report(self, job_id: str) -> dict[str, Any]:
        job = self._get_job(job_id)
        if job is None:
            return self._not_found("Job not found.", job_id=job_id)
        report = self._record_report(
            job,
            ReportType.VALIDATION_REPORT,
            "BOS Genesis Validation Report",
            sections=self._change_sections(job),
        )
        return self._ok(
            "Validation report generated.",
            data={"report": self._report_payload(report)},
            job_id=job_id,
        )

    def generate_rollback_report(self, job_id: str) -> dict[str, Any]:
        job = self._get_job(job_id)
        if job is None:
            return self._not_found("Job not found.", job_id=job_id)
        report = self._record_report(
            job,
            ReportType.ROLLBACK_REPORT,
            "BOS Genesis Rollback Report",
            sections=self._change_sections(job),
        )
        return self._ok(
            "Rollback report generated.",
            data={"report": self._report_payload(report)},
            job_id=job_id,
        )

    def generate_change_report(self, job_id: str) -> dict[str, Any]:
        job = self._get_job(job_id)
        if job is None:
            return self._not_found("Job not found.", job_id=job_id)
        report = self._record_report(
            job,
            ReportType.CHANGE_REPORT,
            "BOS Genesis Target Namespace Change Report",
            sections={
                **self._change_sections(job),
                "document_purpose": (
                    "Operator-facing record of the resources and actions performed in "
                    f"target namespace {job.target_namespace}."
                ),
            },
        )
        return self._ok(
            "Change report generated.",
            data={"report": self._report_payload(report)},
            job_id=job_id,
        )

    def generate_release_notes(self, job_id: str) -> dict[str, Any]:
        job = self._get_job(job_id)
        if job is None:
            return self._not_found("Job not found.", job_id=job_id)
        release_note_result = self._call_release_note_agent(job)
        report = self._record_report(
            job,
            ReportType.RELEASE_NOTES,
            "BOS Genesis Release Notes",
            sections={
                **self._change_sections(job),
                "release_note_mcp_integration": release_note_result,
            },
        )
        self._add_audit(job_id, "release_notes_requested", {"report_id": report.report_id})
        return self._ok(
            "Release notes generated.",
            data={"report": self._report_payload(report), "release_note_mcp": release_note_result},
            job_id=job_id,
        )

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
        updated = job.model_copy(
            update={
                "state": target_state,
                "updated_at": utc_now(),
                "decision_required": target_state == JobState.DECISION_REQUIRED,
                "blocked": target_state == JobState.DECISION_REQUIRED,
                "completed_at": utc_now()
                if target_state in {JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED}
                else job.completed_at,
            }
        )
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
        self._record_observation(observation)

    def _record_observation(self, observation: Observation) -> None:
        self._observations.setdefault(observation.job_id, []).append(observation)
        self._repository.add_observation(observation)
        job = self._get_job(observation.job_id)
        self._write_memory(
            MemoryLayer.OBSERVABILITY,
            observation.job_id,
            observation.summary,
            observation.model_dump(mode="json"),
            namespace=job.target_namespace if job else None,
        )

    def _record_report(
        self,
        job: ExecutionJob,
        report_type: ReportType,
        title: str,
        *,
        sections: dict[str, Any] | None = None,
        warnings: list[str] | None = None,
    ) -> ReportArtifact:
        generator = ReportGenerator(_artifact_root_path(self._repository.path))
        report_set = generator.generate(
            job=job,
            report_type=report_type,
            title=title,
            steps=self._repository.get_steps(job.job_id),
            observations=self._repository.get_observations(job.job_id),
            audit_events=self._repository.list_audit_events(job.job_id),
            sections=sections or {},
            warnings=warnings or [],
        )
        report = generator.artifact(job=job, report_type=report_type, report_set=report_set)
        reports = [
            item
            for item in self._reports.get(job.job_id, [])
            if item.report_id != report.report_id
        ]
        reports.append(report)
        self._reports[job.job_id] = reports
        self._repository.save_report(report)
        self._add_audit(
            job.job_id,
            "report_generated",
            {"report_id": report.report_id, "report_type": report.report_type.value},
        )
        return report

    def _find_report(self, job_id: str, report_id: str) -> ReportArtifact | None:
        reports_by_id = {
            report.report_id: report
            for report in self._repository.get_reports(job_id)
        }
        reports_by_id.update(
            {report.report_id: report for report in self._reports.get(job_id, [])}
        )
        return reports_by_id.get(report_id)

    def _report_payload(self, report: ReportArtifact) -> dict[str, Any]:
        payload = _dump(report)
        if report.job_id:
            payload["download_url"] = _report_download_url(
                report.job_id,
                report.report_id,
                "pdf",
            )
        return payload

    def _change_sections(self, job: ExecutionJob) -> dict[str, Any]:
        steps = self._repository.get_steps(job.job_id)
        observations = self._repository.get_observations(job.job_id)
        mutation_observations = [
            observation.model_dump(mode="json")
            for observation in observations
            if observation.observation_type == ObservationType.MUTATION_RESULT
        ]
        validation_observations = [
            observation.model_dump(mode="json")
            for observation in observations
            if observation.observation_type == ObservationType.VALIDATION_RESULT
        ]
        return {
            "target_namespace": job.target_namespace,
            "source_namespace": job.source_namespace,
            "resource_change_table": [
                {
                    "step_id": step.step_id,
                    "phase_id": step.phase_id,
                    "type": step.type.value,
                    "state": step.state.value,
                    "manifest_refs": step.manifest_refs,
                    "values_refs": step.values_refs,
                }
                for step in steps
            ],
            "mutation_observations": mutation_observations,
            "validation_observations": validation_observations,
        }

    def _call_release_note_agent(self, job: ExecutionJob) -> dict[str, Any]:
        endpoint = os.getenv("RELEASE_NOTE_MCP_ENDPOINT")
        if not endpoint:
            return {
                "adapter": "bosgenesis_release_note_agent",
                "configured": False,
                "status": "not_configured_local_artifacts_generated",
                "formats": ["markdown", "html", "pdf", "zip"],
            }
        client = ReleaseNoteMcpClient(
            server_name="bosgenesis_release_note_agent",
            transport=HttpMcpCompatibilityTransport(
                base_url=endpoint,
                api_key=os.getenv("RELEASE_NOTE_API_KEY") or os.getenv("BOSGENESIS_API_KEY"),
            ),
            job_id=job.job_id,
            timeout_seconds=_env_float("RELEASE_NOTE_MCP_TIMEOUT_SECONDS", default=30.0),
            correlation_id=job.correlation_id,
            trace_id=job.trace_id,
        )
        result = client.create_execution_notes(
            job_id=job.job_id,
            executed_steps=[
                step.model_dump(mode="json")
                for step in self._repository.get_steps(job.job_id)
            ],
            warnings=[
                observation.summary
                for observation in self._repository.get_observations(job.job_id)
                if observation.severity
                in {ObservationSeverity.WARNING, ObservationSeverity.ERROR}
            ],
            trace_id=job.trace_id,
        )
        self._record_observation(result.observation)
        if result.audit_event is not None:
            self._repository.append_audit_event(result.audit_event)
        if result.success:
            return {
                "adapter": "bosgenesis_release_note_agent",
                "configured": True,
                "status": "generated_by_release_note_mcp",
                "data": result.data or {},
            }
        return {
            "adapter": "bosgenesis_release_note_agent",
            "configured": True,
            "status": "release_note_mcp_failed_local_artifacts_generated",
            "error": result.error.model_dump(mode="json") if result.error else {},
        }

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
        linked_job = with_bundle_root_link(job, bundle_root)
        self._save_job(linked_job)
        self._runtime.seed_plan(job.job_id, plan)
        self._job_bundle_roots[job.job_id] = bundle_root
        self._plans[job.job_id] = plan.raw_machine_execution_plan

    def _ensure_background_worker(self) -> None:
        if not _env_bool("MOP_EXECUTION_API_BACKGROUND_WORKER_ENABLED", default=True):
            return
        with self._worker_lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                return
            self._worker_stop.clear()
            self._worker_thread = threading.Thread(
                target=self._background_worker_loop,
                name="mop-execution-api-worker",
                daemon=True,
            )
            self._worker_thread.start()

    def _background_worker_loop(self) -> None:
        idle_sleep = _env_float("MOP_EXECUTION_API_WORKER_IDLE_SLEEP_SECONDS", default=0.25)
        while not self._worker_stop.is_set():
            decision = self._runtime.run_once()
            if decision.action == "idle":
                time.sleep(idle_sleep)

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
            "mop_execution_execute_rollback",
            "mop_execution_revert_namespace",
            "mop_execution_run_validation",
            "mop_execution_generate_execution_report",
            "mop_execution_generate_validation_report",
            "mop_execution_generate_rollback_report",
            "mop_execution_generate_change_report",
            "mop_execution_generate_release_notes",
            "mop_execution_download_report",
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


def _dataclass_payload(value: Any) -> dict[str, Any]:
    if is_dataclass(value):
        payload = asdict(value)
    elif isinstance(value, dict):
        payload = value
    else:
        payload = {"value": value}
    redacted = redact_value(payload)
    return redacted if isinstance(redacted, dict) else {"value": redacted}


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


def _report_download_url(job_id: str, report_id: str, artifact: str = "pdf") -> str:
    return f"/v1/execution-jobs/{job_id}/reports/{report_id}/download?artifact={artifact}"


def _repository_path() -> Path:
    configured = os.getenv("MOP_EXECUTION_REPOSITORY_PATH")
    if configured:
        return Path(configured)
    artifact_root = os.getenv("ARTIFACT_ROOT_PATH")
    if artifact_root:
        return Path(artifact_root) / "repository.json"
    return Path(tempfile.mkdtemp(prefix="bosgenesis-mop-execution-agent-")) / "repository.json"


def _artifact_root_path(repository_path: Path) -> Path:
    configured = os.getenv("MOP_EXECUTION_REPORT_ROOT") or os.getenv("ARTIFACT_ROOT_PATH")
    if configured:
        return Path(configured)
    return repository_path.parent


def _execution_mode(value: Any) -> ExecutionMode:
    if value is None:
        return ExecutionMode.EXTERNAL_LLM_CONTROLLED
    try:
        return ExecutionMode(str(value))
    except ValueError:
        return ExecutionMode.EXTERNAL_LLM_CONTROLLED


def _env_float(name: str, *, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}
