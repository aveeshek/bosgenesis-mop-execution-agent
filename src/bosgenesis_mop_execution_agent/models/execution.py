"""Execution job, phase, and step models."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from bosgenesis_mop_execution_agent.common.time import utc_now
from bosgenesis_mop_execution_agent.models.base import StrictBaseModel
from bosgenesis_mop_execution_agent.models.enums import (
    ApprovalStatus,
    ExecutionMode,
    JobState,
    PhaseStatus,
    StepState,
    StepType,
)
from bosgenesis_mop_execution_agent.models.resources import ResourceRef


class ExecutionProgress(StrictBaseModel):
    """Job progress counters."""

    total_phases: int = 0
    completed_phases: int = 0
    total_steps: int = 0
    completed_steps: int = 0
    failed_steps: int = 0


class ExecutionJob(StrictBaseModel):
    """Top-level execution job."""

    job_id: str
    bundle_id: str
    target_namespace: str
    state: JobState = JobState.CREATED
    job_name: str | None = None
    mop_id: str | None = None
    run_id: str | None = None
    correlation_id: str | None = None
    trace_id: str | None = None
    source_namespace: str | None = None
    execution_mode: ExecutionMode = ExecutionMode.EXTERNAL_LLM_CONTROLLED
    current_phase_id: str | None = None
    current_step_id: str | None = None
    dry_run_satisfied: bool = False
    approval_status: ApprovalStatus = ApprovalStatus.MISSING
    decision_required: bool = False
    blocked: bool = False
    progress: ExecutionProgress = Field(default_factory=ExecutionProgress)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime | None = None
    links: dict[str, str] = {}


class ExecutionPhase(StrictBaseModel):
    """A phase instance within an execution job."""

    phase_id: str
    job_id: str
    sequence_index: int
    status: PhaseStatus = PhaseStatus.PENDING
    title: str | None = None
    objective: str | None = None
    depends_on: list[str] = []
    correlation_id: str | None = None
    trace_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None


class ExecutionStep(StrictBaseModel):
    """A step instance within an execution phase."""

    step_id: str
    job_id: str
    phase_id: str
    sequence_index: int
    title: str | None = None
    type: StepType = StepType.UNKNOWN
    state: StepState = StepState.PENDING
    depends_on: list[str] = []
    resource_refs: list[ResourceRef] = []
    command_fingerprint: str | None = None
    dry_run_status: StepState | None = None
    mutation_status: StepState | None = None
    validation_status: StepState | None = None
    attempt_number: int = 0
    correlation_id: str | None = None
    trace_id: str | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
