"""MCP tool registry and handlers for the MoP Execution Agent."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from bosgenesis_mop_execution_agent import __version__
from bosgenesis_mop_execution_agent.policy import PolicyEvaluationContext, evaluate_policy

SERVER_NAME = "bosgenesis-mop-execution-agent"
PROTOCOL_VERSION = "2025-03-26"


TOOL_NAMES = [
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
]


READ_ONLY_TOOLS = {
    "mop_execution_health",
    "mop_execution_get_capabilities",
    "mop_execution_get_job",
    "mop_execution_list_jobs",
    "mop_execution_get_plan",
    "mop_execution_get_next_required_decision",
    "mop_execution_list_observations",
    "mop_execution_list_audit_events",
    "mop_execution_get_memory_context",
    "mop_execution_evaluate_policy",
    "mop_execution_generate_release_notes",
}


def list_tools() -> list[dict[str, Any]]:
    """Return MCP tool definitions."""
    return [_tool_definition(name) for name in TOOL_NAMES]


def call_tool(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute a tool and return a standard MCP tool result."""
    args = arguments or {}
    if name == "mop_execution_health":
        envelope = _ok(
            "MoP Execution Agent is healthy.",
            data={
                "status": "ok",
                "service": SERVER_NAME,
                "version": __version__,
                "timestamp": datetime.now(UTC).isoformat(),
            },
        )
    elif name == "mop_execution_get_capabilities":
        envelope = _ok("Capabilities returned.", data=capabilities())
    elif name == "mop_execution_evaluate_policy":
        envelope = _evaluate_policy(args)
    elif name in TOOL_NAMES:
        envelope = _not_implemented(name)
    else:
        envelope = _error(f"Unknown tool: {name}", code="UNKNOWN_TOOL")

    return {
        "content": [{"type": "text", "text": _json_dumps(envelope)}],
        "structuredContent": envelope,
        "isError": not bool(envelope["ok"]),
    }


def capabilities() -> dict[str, Any]:
    """Return static server capabilities and guardrails."""
    return {
        "server_name": SERVER_NAME,
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
        "instruction_types": [
            "continue",
            "retry",
            "wait",
            "skip",
            "patch_manifest",
            "replace_manifest",
            "run_validation",
            "request_human_approval",
            "rollback",
            "abort",
            "refresh_observation",
            "invoke_mcp_tool",
        ],
        "tools": TOOL_NAMES,
        "implemented_tools": [
            "mop_execution_health",
            "mop_execution_get_capabilities",
            "mop_execution_evaluate_policy",
        ],
        "not_implemented_until_runtime_phases": [
            name
            for name in TOOL_NAMES
            if name
            not in {
                "mop_execution_health",
                "mop_execution_get_capabilities",
                "mop_execution_evaluate_policy",
            }
        ],
        "mcp_adapters": [
            "bosgenesis_k8s",
            "bosgenesis_helm",
            "data_ingestion_agent",
            "bosgenesis_release_note_agent",
        ],
    }


def _tool_definition(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "description": _tool_description(name),
        "inputSchema": {
            "type": "object",
            "additionalProperties": True,
            "properties": _tool_properties(name),
        },
        "annotations": {
            "readOnlyHint": name in READ_ONLY_TOOLS,
            "destructiveHint": name in {"mop_execution_request_rollback"},
            "idempotentHint": name in READ_ONLY_TOOLS,
            "openWorldHint": False,
        },
    }


def _tool_description(name: str) -> str:
    descriptions = {
        "mop_execution_health": "Return MoP Execution Agent health.",
        "mop_execution_get_capabilities": "Return guardrails, instruction types, and tools.",
        "mop_execution_register_bundle": "Register an artifact bundle reference.",
        "mop_execution_validate_bundle": "Validate a MoP artifact bundle.",
        "mop_execution_create_job": "Create an execution job from a validated bundle.",
        "mop_execution_get_job": "Fetch execution job status.",
        "mop_execution_list_jobs": "List execution jobs.",
        "mop_execution_start_job": "Start or enqueue an execution job.",
        "mop_execution_pause_job": "Pause a job at the next safe point.",
        "mop_execution_resume_job": "Resume a paused or decision-required job.",
        "mop_execution_cancel_job": "Cancel a job at a safe point.",
        "mop_execution_submit_instruction": "Submit an external LLM instruction envelope.",
        "mop_execution_submit_approval": "Submit a scoped human approval reference.",
        "mop_execution_get_plan": "Fetch parsed machine execution plan.",
        "mop_execution_get_next_required_decision": "Fetch the next decision-required context.",
        "mop_execution_list_observations": "List factual observations for a job.",
        "mop_execution_list_audit_events": "List append-only audit events for a job.",
        "mop_execution_get_memory_context": "Fetch redacted memory context for a job.",
        "mop_execution_evaluate_policy": "Evaluate Phase 4 policy guardrails.",
        "mop_execution_request_rollback": (
            "Request rollback with explicit instruction and approval context."
        ),
        "mop_execution_generate_release_notes": (
            "Generate release notes for reportable execution state."
        ),
    }
    return descriptions[name]


def _tool_properties(name: str) -> dict[str, Any]:
    if name in {
        "mop_execution_get_job",
        "mop_execution_start_job",
        "mop_execution_pause_job",
        "mop_execution_resume_job",
        "mop_execution_cancel_job",
        "mop_execution_get_plan",
        "mop_execution_get_next_required_decision",
        "mop_execution_list_observations",
        "mop_execution_list_audit_events",
        "mop_execution_get_memory_context",
        "mop_execution_request_rollback",
        "mop_execution_generate_release_notes",
    }:
        return {"job_id": {"type": "string"}}
    if name in {"mop_execution_register_bundle", "mop_execution_validate_bundle"}:
        return {
            "source": {"type": "object"},
            "target_namespace": {"type": "string"},
        }
    if name == "mop_execution_create_job":
        return {
            "bundle_id": {"type": "string"},
            "target_namespace": {"type": "string"},
            "job_name": {"type": "string"},
        }
    if name == "mop_execution_submit_instruction":
        return {
            "job_id": {"type": "string"},
            "instruction": {"type": "object"},
        }
    if name == "mop_execution_submit_approval":
        return {
            "job_id": {"type": "string"},
            "approval": {"type": "object"},
        }
    if name == "mop_execution_evaluate_policy":
        return {
            "job_id": {"type": "string"},
            "target_namespace": {"type": "string"},
            "mutating": {"type": "boolean"},
            "command": {"type": "string"},
            "dry_run_satisfied": {"type": "boolean"},
            "audit_written": {"type": "boolean"},
        }
    return {}


def _evaluate_policy(arguments: dict[str, Any]) -> dict[str, Any]:
    try:
        context = PolicyEvaluationContext.model_validate(arguments)
    except Exception as exc:
        return _error(
            "Policy evaluation input is invalid.",
            code="POLICY_INPUT_INVALID",
            data={"error": str(exc)},
        )
    decision = evaluate_policy(context)
    return _ok(
        "Policy evaluated.",
        data=decision.model_dump(mode="json"),
        policy_blocks=[block.model_dump(mode="json") for block in decision.blocks],
    )


def _not_implemented(name: str) -> dict[str, Any]:
    return _error(
        f"{name} is registered but not implemented until the REST/job runtime phases are complete.",
        code="TOOL_NOT_IMPLEMENTED",
        data={"tool": name, "runtime_available": False},
    )


def _ok(
    message: str,
    *,
    data: dict[str, Any] | None = None,
    policy_blocks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "ok": True,
        "message": message,
        "data": data or {},
        "observations": [],
        "policy_blocks": policy_blocks or [],
        "next_required_decision": None,
        "redaction_applied": True,
    }


def _error(
    message: str,
    *,
    code: str,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "ok": False,
        "message": message,
        "data": data or {},
        "observations": [],
        "policy_blocks": [{"code": code, "message": message, "severity": "block"}],
        "next_required_decision": None,
        "redaction_applied": True,
    }


def _json_dumps(payload: dict[str, Any]) -> str:
    import json

    return json.dumps(payload, sort_keys=True)
