"""Machine execution plan models."""

from __future__ import annotations

from typing import Any

from pydantic import Field, model_validator

from bosgenesis_mop_execution_agent.models.base import StrictBaseModel

SUPPORTED_MACHINE_PLAN_SCHEMA_VERSIONS = {"0.1.0", "1.0"}
SUPPORTED_MACHINE_PLAN_SCHEMA_VERSION = "0.1.0"


class ExecutorContract(StrictBaseModel):
    """Executor guardrail contract embedded in the machine plan."""

    parse_this_block_first: bool = True
    dry_run_before_mutation: bool = True
    human_approval_before_mutation: bool = True
    never_copy_secret_values: bool = True
    target_namespace_only: bool | str = True
    llm_suggestions_are_not_authority: bool | None = None


class DependencyGraphEntry(StrictBaseModel):
    """Phase dependency graph entry."""

    phase_id: str
    depends_on: list[str] = Field(default_factory=list)


class MachinePlanCommand(StrictBaseModel):
    """Command metadata from a plan step."""

    kind: str
    command: str
    dry_run: bool | None = None
    mutating: bool | None = None


class MachinePlanStep(StrictBaseModel):
    """A step from the machine execution plan."""

    step_id: str
    title: str
    type: str
    depends_on: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    manifest_refs: list[str] = Field(default_factory=list)
    values_refs: list[str] = Field(default_factory=list)
    commands: list[MachinePlanCommand] = Field(default_factory=list)
    expected_outcomes: list[str] = Field(default_factory=list)
    required_human_inputs: list[str] = Field(default_factory=list)
    inference: dict[str, Any] | None = None


class MachinePlanPhase(StrictBaseModel):
    """A phase from the machine execution plan."""

    phase_id: str
    title: str | None = None
    objective: str
    depends_on: list[str] = Field(default_factory=list)
    steps: list[MachinePlanStep] = Field(default_factory=list)


class MachineExecutionPlan(StrictBaseModel):
    """Canonical machine-readable execution plan."""

    schema_version: str
    target_namespace: str
    authority_order: str | None = None
    executor_contract: ExecutorContract = Field(default_factory=ExecutorContract)
    dependency_graph: list[DependencyGraphEntry] = Field(default_factory=list)
    phases: list[MachinePlanPhase]
    raw_machine_execution_plan: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_schema_version(self) -> MachineExecutionPlan:
        if self.schema_version not in SUPPORTED_MACHINE_PLAN_SCHEMA_VERSIONS:
            msg = f"unsupported_machine_plan_schema:{self.schema_version}"
            raise ValueError(msg)
        return self

    @property
    def phase_ids(self) -> set[str]:
        return {phase.phase_id for phase in self.phases}

    @property
    def manifest_refs(self) -> set[str]:
        return {ref for phase in self.phases for step in phase.steps for ref in step.manifest_refs}

    @property
    def values_refs(self) -> set[str]:
        return {ref for phase in self.phases for step in phase.steps for ref in step.values_refs}
