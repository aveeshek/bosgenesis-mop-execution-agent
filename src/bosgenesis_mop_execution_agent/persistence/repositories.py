"""Durable repository implementations.

The JSON repository is intentionally small and local-development friendly. It gives
the worker restart-safe persistence semantics in tests while PostgreSQL migration
DDL defines the production table contract.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from pydantic import Field

from bosgenesis_mop_execution_agent.models import (
    AuditEvent,
    ExecutionJob,
    ExecutionPhase,
    ExecutionStep,
    ExternalInstruction,
    HumanApproval,
    MemoryQuery,
    MemoryRecord,
    Observation,
    ReportArtifact,
)
from bosgenesis_mop_execution_agent.models.base import StrictBaseModel


class RepositorySnapshot(StrictBaseModel):
    """Serialized repository state."""

    jobs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    phases: dict[str, dict[str, Any]] = Field(default_factory=dict)
    steps: dict[str, dict[str, Any]] = Field(default_factory=dict)
    observations: dict[str, dict[str, Any]] = Field(default_factory=dict)
    instructions: dict[str, dict[str, Any]] = Field(default_factory=dict)
    approvals: dict[str, dict[str, Any]] = Field(default_factory=dict)
    audit_events: list[dict[str, Any]] = Field(default_factory=list)
    memory_records: dict[str, dict[str, Any]] = Field(default_factory=dict)
    reports: dict[str, dict[str, Any]] = Field(default_factory=dict)


class JsonExecutionRepository:
    """File-backed repository for restart-safe local and test persistence."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._lock = threading.RLock()
        self._snapshot = self._load()

    @property
    def path(self) -> Path:
        return self._path

    def save_job(self, job: ExecutionJob) -> None:
        with self._lock:
            self._snapshot.jobs[job.job_id] = job.model_dump(mode="json")
            self._flush()

    def get_job(self, job_id: str) -> ExecutionJob | None:
        with self._lock:
            payload = self._snapshot.jobs.get(job_id)
            return ExecutionJob.model_validate(payload) if payload else None

    def list_jobs(self) -> list[ExecutionJob]:
        with self._lock:
            return [
                ExecutionJob.model_validate(payload)
                for payload in self._snapshot.jobs.values()
            ]

    def save_phase(self, phase: ExecutionPhase) -> None:
        with self._lock:
            self._snapshot.phases[_compound_key(phase.job_id, phase.phase_id)] = (
                phase.model_dump(mode="json")
            )
            self._flush()

    def get_phases(self, job_id: str) -> list[ExecutionPhase]:
        with self._lock:
            return [
                ExecutionPhase.model_validate(payload)
                for key, payload in self._snapshot.phases.items()
                if key.startswith(f"{job_id}:")
            ]

    def save_step(self, step: ExecutionStep) -> None:
        with self._lock:
            self._snapshot.steps[_compound_key(step.job_id, step.step_id)] = (
                step.model_dump(mode="json")
            )
            self._flush()

    def get_steps(self, job_id: str) -> list[ExecutionStep]:
        with self._lock:
            return [
                ExecutionStep.model_validate(payload)
                for key, payload in self._snapshot.steps.items()
                if key.startswith(f"{job_id}:")
            ]

    def add_observation(self, observation: Observation) -> None:
        with self._lock:
            self._snapshot.observations[observation.observation_id] = (
                observation.model_dump(mode="json")
            )
            self._flush()

    def get_observations(self, job_id: str) -> list[Observation]:
        with self._lock:
            return [
                Observation.model_validate(payload)
                for payload in self._snapshot.observations.values()
                if payload.get("job_id") == job_id
            ]

    def save_instruction(self, instruction: ExternalInstruction) -> None:
        with self._lock:
            self._snapshot.instructions[instruction.instruction_id] = (
                instruction.model_dump(mode="json")
            )
            self._flush()

    def get_instruction(self, instruction_id: str) -> ExternalInstruction | None:
        with self._lock:
            payload = self._snapshot.instructions.get(instruction_id)
            return ExternalInstruction.model_validate(payload) if payload else None

    def get_instructions(self, job_id: str) -> list[ExternalInstruction]:
        with self._lock:
            return [
                ExternalInstruction.model_validate(payload)
                for payload in self._snapshot.instructions.values()
                if payload.get("job_id") == job_id
            ]

    def save_approval(self, approval: HumanApproval) -> None:
        with self._lock:
            self._snapshot.approvals[approval.approval_id] = approval.model_dump(mode="json")
            self._flush()

    def get_approval(self, approval_id: str) -> HumanApproval | None:
        with self._lock:
            payload = self._snapshot.approvals.get(approval_id)
            return HumanApproval.model_validate(payload) if payload else None

    def get_approvals(self, job_id: str) -> list[HumanApproval]:
        with self._lock:
            return [
                HumanApproval.model_validate(payload)
                for payload in self._snapshot.approvals.values()
                if payload.get("job_id") == job_id
            ]

    def append_audit_event(self, audit_event: AuditEvent) -> None:
        with self._lock:
            if any(
                event.get("audit_event_id") == audit_event.audit_event_id
                for event in self._snapshot.audit_events
            ):
                msg = f"audit_event_already_exists:{audit_event.audit_event_id}"
                raise ValueError(msg)
            self._snapshot.audit_events.append(audit_event.model_dump(mode="json"))
            self._flush()

    def list_audit_events(self, job_id: str | None = None) -> list[AuditEvent]:
        with self._lock:
            return [
                AuditEvent.model_validate(payload)
                for payload in self._snapshot.audit_events
                if job_id is None or payload.get("job_id") == job_id
            ]

    def save_memory_record(self, record: MemoryRecord) -> None:
        with self._lock:
            self._snapshot.memory_records[record.memory_id] = record.model_dump(mode="json")
            self._flush()

    def list_memory_records(self, query: MemoryQuery | None = None) -> list[MemoryRecord]:
        with self._lock:
            memory_query = query or MemoryQuery()
            records = [
                MemoryRecord.model_validate(payload)
                for payload in self._snapshot.memory_records.values()
            ]
            return [record for record in records if _matches_memory(record, memory_query)]

    def save_report(self, report: ReportArtifact) -> None:
        with self._lock:
            self._snapshot.reports[report.report_id] = report.model_dump(mode="json")
            self._flush()

    def get_reports(self, job_id: str) -> list[ReportArtifact]:
        with self._lock:
            return [
                ReportArtifact.model_validate(payload)
                for payload in self._snapshot.reports.values()
                if payload.get("job_id") == job_id
            ]

    def _load(self) -> RepositorySnapshot:
        if not self._path.exists():
            return RepositorySnapshot()
        return RepositorySnapshot.model_validate_json(self._path.read_text(encoding="utf-8"))

    def _flush(self) -> None:
        with self._lock:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                self._snapshot.model_dump(mode="json"),
                indent=2,
                sort_keys=True,
            )
            self._path.write_text(f"{payload}\n", encoding="utf-8")


def _compound_key(left: str, right: str) -> str:
    return f"{left}:{right}"


def _matches_memory(record: MemoryRecord, query: MemoryQuery) -> bool:
    for field_name, expected in query.model_dump(exclude_none=True).items():
        if getattr(record, field_name) != expected:
            return False
    return True
