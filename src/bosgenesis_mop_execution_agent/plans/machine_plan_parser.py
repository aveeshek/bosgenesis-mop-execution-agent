"""Parser for `machine_execution_plan.yaml`."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from bosgenesis_mop_execution_agent.plans.dependency_graph import validate_dependency_graph
from bosgenesis_mop_execution_agent.plans.models import MachineExecutionPlan

STEP_METADATA_KEYS = {
    "release_name",
    "chart_ref",
    "chart_version",
    "chart_source",
    "repo_name",
    "repo_url",
    "credential_secret_ref",
    "generated_by",
    "timeout",
    "helm_timeout",
    "install_timeout",
    "wait",
    "atomic",
    "current_revision",
    "previous_revision",
    "revision",
    "forward_step_id",
    "forward_step_ids",
    "reverts_step_id",
    "rollback_for",
}


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
    loaded = _normalize_machine_plan_payload(loaded)
    try:
        plan = MachineExecutionPlan.model_validate({**loaded, "raw_machine_execution_plan": loaded})
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
    loaded = _normalize_machine_plan_payload(loaded)
    try:
        plan = MachineExecutionPlan.model_validate({**loaded, "raw_machine_execution_plan": loaded})
    except ValidationError as exc:
        raise MachinePlanParseError(f"embedded_machine_plan_schema_invalid:{exc}") from exc
    validate_dependency_graph(plan)
    return plan


def _normalize_machine_plan_payload(loaded: dict[str, Any]) -> dict[str, Any]:
    """Normalize MoP Creation Agent output into the execution-agent plan schema."""
    payload = loaded.get("machine_execution_plan", loaded)
    if not isinstance(payload, dict):
        raise MachinePlanParseError("machine_plan_not_mapping")

    executor_contract = _normalize_executor_contract(payload.get("executor_contract"))
    target_namespace = payload.get("target_namespace")
    if not isinstance(target_namespace, str) or not target_namespace:
        target_namespace_only = executor_contract.get("target_namespace_only")
        if isinstance(target_namespace_only, str) and target_namespace_only:
            target_namespace = target_namespace_only
    if not isinstance(target_namespace, str) or not target_namespace:
        raise MachinePlanParseError("machine_plan_target_namespace_missing")

    return {
        "schema_version": str(payload.get("schema_version") or ""),
        "target_namespace": target_namespace,
        "authority_order": payload.get("authority_order"),
        "executor_contract": executor_contract,
        "dependency_graph": _normalize_dependency_graph(payload.get("dependency_graph")),
        "phases": _normalize_phases(payload.get("phases")),
    }


def _normalize_executor_contract(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    allowed = {
        "parse_this_block_first",
        "dry_run_before_mutation",
        "human_approval_before_mutation",
        "never_copy_secret_values",
        "target_namespace_only",
        "llm_suggestions_are_not_authority",
    }
    return {key: item for key, item in value.items() if key in allowed}


def _normalize_dependency_graph(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    graph: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        phase_id = item.get("phase_id")
        if not isinstance(phase_id, str) or not phase_id:
            continue
        depends_on = item.get("depends_on")
        graph.append(
            {
                "phase_id": phase_id,
                "depends_on": depends_on if isinstance(depends_on, list) else [],
            }
        )
    return graph


def _normalize_phases(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise MachinePlanParseError("machine_plan_phases_not_list")
    phases: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        phase_id = item.get("phase_id")
        if not isinstance(phase_id, str) or not phase_id:
            continue
        depends_on = item.get("depends_on")
        phases.append(
            {
                "phase_id": phase_id,
                "title": item.get("title"),
                "objective": item.get("objective") or item.get("title") or phase_id,
                "depends_on": depends_on if isinstance(depends_on, list) else [],
                "steps": _normalize_steps(item.get("steps")),
            }
        )
    return phases


def _normalize_steps(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    steps: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        step_id = item.get("step_id")
        if not isinstance(step_id, str) or not step_id:
            continue
        commands = _normalize_commands(item.get("commands"))
        manifest_refs = _string_list(item.get("manifest_refs")) or _manifest_refs_from_commands(
            commands
        )
        values_refs = _string_list(item.get("values_refs")) or _values_refs_from_commands(commands)
        depends_on = item.get("depends_on")
        inference = item.get("inference")
        steps.append(
            {
                "step_id": step_id,
                "title": item.get("title") or step_id,
                "type": _normalize_step_type(str(item.get("type") or ""), manifest_refs, commands),
                "depends_on": depends_on if isinstance(depends_on, list) else [],
                "evidence_refs": _string_list(item.get("evidence_refs")),
                "manifest_refs": manifest_refs,
                "values_refs": values_refs,
                "commands": commands,
                "rollback_commands": _string_list(item.get("rollback_commands")),
                "metadata": _normalize_step_metadata(item),
                "expected_outcomes": _string_list(item.get("expected_outcomes")),
                "required_human_inputs": _string_list(item.get("required_human_inputs")),
                "inference": inference if isinstance(inference, dict) else None,
            }
        )
    return steps


def _normalize_commands(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    commands: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        if not isinstance(command, str):
            continue
        commands.append(
            {
                "kind": str(item.get("kind") or "command"),
                "command": command,
                "dry_run": item.get("dry_run") if isinstance(item.get("dry_run"), bool) else None,
                "mutating": item.get("mutating")
                if isinstance(item.get("mutating"), bool)
                else None,
            }
        )
    return commands


def _normalize_step_metadata(item: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    raw_metadata = item.get("metadata")
    if isinstance(raw_metadata, dict):
        metadata.update(raw_metadata)
    for key in STEP_METADATA_KEYS:
        value = item.get(key)
        if value is not None:
            metadata[key] = value
    return metadata


def _normalize_step_type(
    raw_type: str,
    manifest_refs: list[str],
    commands: list[dict[str, Any]],
) -> str:
    if raw_type in {
        "context_check",
        "k8s_apply",
        "k8s_delete",
        "k8s_get",
        "k8s_validate",
        "helm_install",
        "helm_upgrade",
        "helm_validate",
        "wait",
        "manual_input",
        "rollback",
        "release_notes",
    }:
        return raw_type
    command_text = "\n".join(str(command.get("command", "")) for command in commands)
    if manifest_refs or "kubectl apply" in command_text:
        return "k8s_apply"
    if "kubectl get" in command_text:
        return "k8s_get"
    if raw_type == "helm" and "helm upgrade" in command_text:
        return "helm_upgrade"
    if raw_type == "helm" and "helm install" in command_text:
        return "helm_install"
    if "helm template" in command_text or "helm list" in command_text:
        return "helm_validate"
    if raw_type in {"validation", "validate"}:
        return "k8s_validate"
    if raw_type in {"human_input", "namespace"}:
        return "context_check"
    return "unknown"


def _manifest_refs_from_commands(commands: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for command in commands:
        command_text = str(command.get("command") or "")
        if "kubectl apply" not in command_text:
            continue
        parts = command_text.split()
        for index, part in enumerate(parts):
            if part in {"-f", "--filename"} and index + 1 < len(parts):
                candidate = parts[index + 1].strip("'\"")
                if candidate.endswith((".yaml", ".yml")) and candidate not in refs:
                    refs.append(candidate)
    return refs


def _values_refs_from_commands(commands: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for command in commands:
        raw_command = str(command.get("command") or "")
        if not raw_command.startswith("helm "):
            continue
        parts = raw_command.split()
        for index, part in enumerate(parts):
            if part in {"-f", "--values", "--value"} and index + 1 < len(parts):
                candidate = parts[index + 1].strip("'\"")
                if candidate.endswith((".yaml", ".yml")) and candidate not in refs:
                    refs.append(candidate)
    return refs


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, str)]
