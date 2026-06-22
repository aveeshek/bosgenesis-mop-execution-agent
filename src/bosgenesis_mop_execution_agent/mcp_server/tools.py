"""MCP tool registry and handlers for the MoP Execution Agent."""

from __future__ import annotations

from typing import Any

from bosgenesis_mop_execution_agent.api.service import MopExecutionApiService, capabilities_payload

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
    "mop_execution_execute_rollback",
    "mop_execution_revert_namespace",
    "mop_execution_run_validation",
    "mop_execution_generate_execution_report",
    "mop_execution_generate_validation_report",
    "mop_execution_generate_rollback_report",
    "mop_execution_generate_change_report",
    "mop_execution_generate_release_notes",
    "mop_execution_download_report",
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
    "mop_execution_download_report",
}


def list_tools() -> list[dict[str, Any]]:
    """Return MCP tool definitions."""
    return [_tool_definition(name) for name in TOOL_NAMES]


def call_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    service: MopExecutionApiService | None = None,
) -> dict[str, Any]:
    """Execute a tool and return a standard MCP tool result."""
    args = arguments or {}
    effective_service = service or MopExecutionApiService()
    if name == "mop_execution_health":
        envelope = effective_service.health()
    elif name == "mop_execution_get_capabilities":
        envelope = effective_service.capabilities()
    elif name == "mop_execution_evaluate_policy":
        envelope = effective_service.evaluate_policy(args)
    elif name == "mop_execution_register_bundle":
        envelope = effective_service.register_bundle(args)
    elif name == "mop_execution_validate_bundle":
        envelope = effective_service.validate_bundle(args.get("bundle_id"), args)
    elif name == "mop_execution_create_job":
        envelope = effective_service.create_job(args)
    elif name == "mop_execution_get_job":
        envelope = effective_service.get_job(str(args.get("job_id", "")))
    elif name == "mop_execution_list_jobs":
        envelope = effective_service.list_jobs()
    elif name == "mop_execution_start_job":
        envelope = effective_service.start_job(str(args.get("job_id", "")))
    elif name == "mop_execution_pause_job":
        envelope = effective_service.pause_job(str(args.get("job_id", "")))
    elif name == "mop_execution_resume_job":
        envelope = effective_service.resume_job(str(args.get("job_id", "")))
    elif name == "mop_execution_cancel_job":
        envelope = effective_service.cancel_job(str(args.get("job_id", "")))
    elif name == "mop_execution_submit_instruction":
        envelope = effective_service.submit_instruction(
            str(args.get("job_id", "")),
            _nested_or_self(args, "instruction"),
        )
    elif name == "mop_execution_submit_approval":
        envelope = effective_service.submit_approval(
            str(args.get("job_id", "")),
            _nested_or_self(args, "approval"),
        )
    elif name == "mop_execution_get_plan":
        envelope = effective_service.get_plan(str(args.get("job_id", "")))
    elif name == "mop_execution_get_next_required_decision":
        envelope = effective_service.next_required_decision(str(args.get("job_id", "")))
    elif name == "mop_execution_list_observations":
        envelope = effective_service.list_observations(str(args.get("job_id", "")))
    elif name == "mop_execution_list_audit_events":
        envelope = effective_service.list_audit_events(str(args.get("job_id", "")))
    elif name == "mop_execution_get_memory_context":
        envelope = effective_service.memory_context(str(args.get("job_id", "")), args)
    elif name == "mop_execution_request_rollback":
        envelope = effective_service.request_rollback(str(args.get("job_id", "")), args)
    elif name == "mop_execution_execute_rollback":
        envelope = effective_service.execute_rollback(str(args.get("job_id", "")), args)
    elif name == "mop_execution_revert_namespace":
        envelope = effective_service.revert_namespace(args)
    elif name == "mop_execution_run_validation":
        envelope = effective_service.run_validation(str(args.get("job_id", "")))
    elif name == "mop_execution_generate_execution_report":
        envelope = effective_service.generate_execution_report(str(args.get("job_id", "")))
    elif name == "mop_execution_generate_validation_report":
        envelope = effective_service.generate_validation_report(str(args.get("job_id", "")))
    elif name == "mop_execution_generate_rollback_report":
        envelope = effective_service.generate_rollback_report(str(args.get("job_id", "")))
    elif name == "mop_execution_generate_change_report":
        envelope = effective_service.generate_change_report(str(args.get("job_id", "")))
    elif name == "mop_execution_generate_release_notes":
        envelope = effective_service.generate_release_notes(str(args.get("job_id", "")))
    elif name == "mop_execution_download_report":
        envelope = effective_service.report_download_metadata(
            str(args.get("job_id", "")),
            str(args.get("report_id", "")),
            str(args.get("artifact") or "pdf"),
        )
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
    payload = capabilities_payload()
    payload["implemented_tools"] = TOOL_NAMES
    payload["not_implemented_until_runtime_phases"] = []
    return payload


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
            "destructiveHint": name
            in {
                "mop_execution_request_rollback",
                "mop_execution_execute_rollback",
                "mop_execution_revert_namespace",
            },
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
        "mop_execution_execute_rollback": (
            "Execute an approved rollback through governed Helm and Kubernetes MCP clients."
        ),
        "mop_execution_revert_namespace": (
            "Revert a target namespace by deleting Helm releases and namespaced resources."
        ),
        "mop_execution_run_validation": (
            "Run post-execution validation for resources, rollout, PVCs, ingress, and Helm status."
        ),
        "mop_execution_generate_execution_report": (
            "Generate Markdown, HTML, PDF, and zip execution report artifacts."
        ),
        "mop_execution_generate_validation_report": (
            "Generate Markdown, HTML, PDF, and zip validation report artifacts."
        ),
        "mop_execution_generate_rollback_report": (
            "Generate Markdown, HTML, PDF, and zip rollback report artifacts."
        ),
        "mop_execution_generate_change_report": (
            "Generate a target-namespace change report in Markdown, HTML, PDF, and zip."
        ),
        "mop_execution_generate_release_notes": (
            "Generate release notes for reportable execution state."
        ),
        "mop_execution_download_report": (
            "Return REST download metadata and link for a report artifact without streaming bytes."
        ),
    }
    return descriptions[name]


def _tool_properties(name: str) -> dict[str, Any]:
    if name == "mop_execution_execute_rollback":
        return {
            "job_id": {"type": "string"},
            "confirm": {"type": "boolean"},
            "dry_run": {"type": "boolean"},
            "mode": {"type": "string"},
            "release_name": {"type": "string"},
            "revision": {"type": "integer"},
            "force_purge_release_storage": {"type": "boolean"},
        }
    if name == "mop_execution_revert_namespace":
        return {
            "target_namespace": {"type": "string"},
            "confirm": {"type": "boolean"},
            "dry_run": {"type": "boolean"},
            "job_id": {"type": "string"},
            "release_name": {"type": "string"},
            "force_purge_release_storage": {"type": "boolean"},
        }
    if name == "mop_execution_download_report":
        return {
            "job_id": {"type": "string"},
            "report_id": {"type": "string"},
            "artifact": {
                "type": "string",
                "enum": ["markdown", "html", "pdf", "archive"],
            },
        }
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
        "mop_execution_execute_rollback",
        "mop_execution_run_validation",
        "mop_execution_generate_execution_report",
        "mop_execution_generate_validation_report",
        "mop_execution_generate_rollback_report",
        "mop_execution_generate_change_report",
        "mop_execution_generate_release_notes",
    }:
        if name == "mop_execution_get_memory_context":
            return {
                "job_id": {"type": "string"},
                "namespace": {"type": "string"},
                "chart": {"type": "string"},
                "kind": {"type": "string"},
                "error_code": {"type": "string"},
                "mcp_source": {"type": "string"},
                "tenant": {"type": "string"},
                "environment": {"type": "string"},
            }
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
    return MopExecutionApiService().evaluate_policy(arguments)


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


def _nested_or_self(arguments: dict[str, Any], key: str) -> dict[str, Any]:
    nested = arguments.get(key)
    return nested if isinstance(nested, dict) else arguments
