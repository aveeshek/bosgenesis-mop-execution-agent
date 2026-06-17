"""Parser for `machine_execution_plan.yaml`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from bosgenesis_mop_execution_agent.plans.dependency_graph import validate_dependency_graph
from bosgenesis_mop_execution_agent.plans.models import MachineExecutionPlan


class MachinePlanParseError(ValueError):
    """Raised when a machine execution plan cannot be parsed or validated."""


def parse_machine_plan(path: str | Path) -> MachineExecutionPlan:
    """Parse and validate a standalone machine execution plan."""
    plan_path = Path(path)
    try:
        loaded = yaml.safe_load(plan_path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MachinePlanParseError(f"machine_plan_yaml_invalid:{exc}") from exc
    if not isinstance(loaded, dict):
        raise MachinePlanParseError("machine_plan_not_mapping")
    try:
        plan = MachineExecutionPlan.model_validate(
            {**loaded, "raw_machine_execution_plan": loaded}
        )
    except ValidationError as exc:
        raise MachinePlanParseError(f"machine_plan_schema_invalid:{exc}") from exc
    except ValueError as exc:
        raise MachinePlanParseError(str(exc)) from exc
    validate_dependency_graph(plan)
    return plan


def parse_embedded_machine_plan(markdown_text: str) -> MachineExecutionPlan | None:
    """Extract an embedded machine plan YAML code fence from installation notes."""
    marker = "machine_execution_plan"
    if marker not in markdown_text:
        return None
    lines = markdown_text.splitlines()
    collecting = False
    block: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("```") and marker in stripped:
            collecting = True
            continue
        if collecting and stripped.startswith("```"):
            break
        if collecting:
            block.append(line)
    if not block:
        return None
    try:
        loaded: Any = yaml.safe_load("\n".join(block))
    except yaml.YAMLError as exc:
        raise MachinePlanParseError(f"embedded_machine_plan_yaml_invalid:{exc}") from exc
    if not isinstance(loaded, dict):
        raise MachinePlanParseError("embedded_machine_plan_not_mapping")
    try:
        plan = MachineExecutionPlan.model_validate(
            {**loaded, "raw_machine_execution_plan": loaded}
        )
    except ValidationError as exc:
        raise MachinePlanParseError(f"embedded_machine_plan_schema_invalid:{exc}") from exc
    validate_dependency_graph(plan)
    return plan
