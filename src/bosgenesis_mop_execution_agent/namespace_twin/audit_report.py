"""Deterministic audit timeline and report rendering for Namespace Twins."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from bosgenesis_mop_execution_agent.security import redact_value

AUDIT_REPORT_SCHEMA_VERSION = "1.0.0"
AUDIT_REPORT_RENDERER_VERSION = "namespace_twin_report_v1"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def sha256_value(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def build_audit_event(event: dict[str, Any], twin: dict[str, Any]) -> dict[str, Any]:
    payload = redact_value(dict(event.get("payload") or {}))
    event_type = str(event.get("event_type") or "twin_event")
    phase = _phase(event_type)
    status = _status(event_type, payload)
    actor_id = str(payload.get("actor_id") or twin.get("actor_id") or "execution-agent")
    actor_type = "operator" if payload.get("actor_id") else "execution_agent"
    evidence_refs = _evidence_refs(payload, event, twin)
    hashes = {
        key: str(value)
        for key, value in {
            "input_hash": payload.get("input_hash") or twin.get("input_hash"),
            "bundle_hash": payload.get("bundle_hash") or twin.get("bundle_hash"),
            "report_hash": payload.get("report_hash") or twin.get("report_hash"),
            "snapshot_hash": payload.get("snapshot_hash"),
        }.items()
        if value
    }
    versions = {
        key: value
        for key, value in {
            "decision_version": payload.get("decision_version") or twin.get("decision_version"),
            "policy_version": payload.get("policy_version") or twin.get("policy_version"),
            "risk_rule_version": payload.get("risk_rule_version") or twin.get("risk_rule_version"),
            "row_version": twin.get("row_version"),
        }.items()
        if value is not None
    }
    return {
        "event_id": str(event["event_id"]),
        "twin_id": str(event["twin_id"]),
        "sequence": int(event["sequence"]),
        "event_type": event_type,
        "phase": phase,
        "status": status,
        "actor": {"type": actor_type, "id": actor_id, "display_name": actor_id},
        "safe_summary": str(redact_value(event.get("message") or event_type))[:4000],
        "evidence_refs": evidence_refs,
        "hashes": hashes,
        "versions": versions,
        "safe_links": [item["href"] for item in evidence_refs if item.get("href")],
        "redacted": True,
        "created_at": str(event["created_at"]),
    }


def build_report(twin: dict[str, Any], events: list[dict[str, Any]]) -> dict[str, Any]:
    safe_twin = redact_value(twin)
    timeline = [build_audit_event(item, safe_twin) for item in events]
    facts = dict(safe_twin.get("facts") or {})
    policy = dict(facts.get("policy_twin") or {})
    dry_run = dict(facts.get("dry_run_evidence") or {})
    rollback = dict(facts.get("rollback_twin") or {})
    drift = dict(facts.get("drift_twin") or {})
    runtime = dict(facts.get("runtime_behavior_twin") or {})
    decision = str(safe_twin.get("decision") or "pending")
    report_core = {
        "schema_version": AUDIT_REPORT_SCHEMA_VERSION,
        "report_type": "namespace_digital_twin_audit",
        "renderer_version": AUDIT_REPORT_RENDERER_VERSION,
        "generated_at": safe_twin.get("updated_at") or safe_twin.get("created_at"),
        "twin": {
            "twin_id": safe_twin.get("twin_id"),
            "display_name": safe_twin.get("display_name"),
            "actor_id": safe_twin.get("actor_id"),
            "lifecycle_status": safe_twin.get("lifecycle_status"),
            "source_namespace": safe_twin.get("source_namespace"),
            "target_cluster": safe_twin.get("target_cluster"),
            "target_namespace": safe_twin.get("target_namespace"),
            "bundle_name": safe_twin.get("bundle_name"),
            "release_version": safe_twin.get("release_version"),
            "created_at": safe_twin.get("created_at"),
            "updated_at": safe_twin.get("updated_at"),
            "completed_at": safe_twin.get("completed_at"),
        },
        "decision": {
            "value": decision,
            "version": int(safe_twin.get("decision_version") or 0),
            "is_final": bool(safe_twin.get("decision_is_final")),
            "lifecycle_status": safe_twin.get("lifecycle_status"),
        },
        "versions": {
            "policy": safe_twin.get("policy_version"),
            "risk_rules": safe_twin.get("risk_rule_version"),
            "row": safe_twin.get("row_version"),
            "runtime_rules": runtime.get("rules_version"),
            "drift_rules": drift.get("rules_version"),
            "rollback_rules": rollback.get("rules_version"),
        },
        "hashes": {
            "input": safe_twin.get("input_hash"),
            "bundle": safe_twin.get("bundle_hash"),
            "terminal_report": safe_twin.get("report_hash"),
            "dry_run_input": dry_run.get("input_hash"),
        },
        "evidence_summary": {
            "resource_count": int(facts.get("resource_count") or 0),
            "edge_count": int(facts.get("edge_count") or 0),
            "finding_count": int(facts.get("finding_count") or 0),
            "policy_verdict": (policy.get("policy_axis") or {}).get("verdict")
            or "not_available",
            "dry_run_status": dry_run.get("qualification_status")
            or dry_run.get("status")
            or "not_run",
            "rollback_confidence": rollback.get("confidence") or "not_available",
            "drift_status": drift.get("status") or "not_available",
            "runtime_risk": runtime.get("risk") or "not_available",
            "runtime_health": (runtime.get("current_health") or {}).get("status")
            or "not_available",
        },
        "timeline": timeline,
        "safe_evidence_links": _report_links(str(safe_twin.get("twin_id") or "")),
        "safety": {
            "redacted": True,
            "secret_values_included": False,
            "chain_of_thought_included": False,
            "model_authority": False,
        },
    }
    report_hash = sha256_value(report_core)
    return {
        **report_core,
        "report_id": f"twinreport_{report_hash[:24]}",
        "report_hash": report_hash,
    }


def render_markdown(report: dict[str, Any]) -> str:
    twin = report["twin"]
    decision = report["decision"]
    evidence = report["evidence_summary"]
    lines = [
        f"# Namespace Digital Twin Audit Report: {twin['display_name']}",
        "",
        "## Document Control",
        "",
        f"- Report ID: `{report['report_id']}`",
        f"- Report Hash: `{report['report_hash']}`",
        f"- Generated At: `{report['generated_at']}`",
        f"- Renderer: `{report['renderer_version']}`",
        "",
        "## Twin Decision",
        "",
        f"- Decision: `{decision['value']}`",
        f"- Decision Version: `{decision['version']}`",
        f"- Final: `{'yes' if decision['is_final'] else 'no'}`",
        f"- Lifecycle: `{decision['lifecycle_status']}`",
        "",
        "## Target and Evidence",
        "",
        f"- Source Namespace: `{twin.get('source_namespace') or 'not_available'}`",
        f"- Target: `{twin.get('target_cluster')}/{twin.get('target_namespace')}`",
        f"- Bundle: `{twin.get('bundle_name')}`",
        f"- Resources: `{evidence['resource_count']}`",
        f"- Dependencies: `{evidence['edge_count']}`",
        f"- Findings: `{evidence['finding_count']}`",
        f"- Policy Verdict: `{evidence['policy_verdict']}`",
        f"- Dry-run: `{evidence['dry_run_status']}`",
        f"- Rollback Confidence: `{evidence['rollback_confidence']}`",
        f"- Drift: `{evidence['drift_status']}`",
        f"- Runtime Risk: `{evidence['runtime_risk']}`",
        "",
        "## Immutable Audit Timeline",
        "",
        "| Seq | Timestamp | Phase | Status | Actor | Event | Safe Summary |",
        "|---:|---|---|---|---|---|---|",
    ]
    for event in report["timeline"]:
        summary = str(event["safe_summary"]).replace("|", "\\|").replace("\n", " ")
        lines.append(
            f"| {event['sequence']} | {event['created_at']} | {event['phase']} | "
            f"{event['status']} | {event['actor']['display_name']} | "
            f"{event['event_type']} | {summary} |"
        )
    lines.extend(
        [
            "",
            "## Safety",
            "",
            "- Report values are redacted.",
            "- Secret values are not included.",
            "- Hidden model reasoning is not included.",
            "- Model output has no decision authority.",
            "",
        ]
    )
    return "\n".join(lines)


def _phase(event_type: str) -> str:
    for needle, phase in (
        ("runtime", "runtime_behavior"),
        ("drift", "drift"),
        ("rollback", "rollback"),
        ("dry_run", "dry_run"),
        ("decision", "decision"),
        ("policy", "policy"),
        ("dependency", "dependency_graph"),
        ("delta", "release_delta"),
        ("cancel", "lifecycle"),
        ("supersed", "lifecycle"),
        ("requested", "intake"),
        ("created", "intake"),
        ("generat", "generation"),
    ):
        if needle in event_type:
            return phase
    return "lifecycle"


def _status(event_type: str, payload: dict[str, Any]) -> str:
    explicit = str(payload.get("status") or payload.get("to_state") or "").lower()
    combined = f"{event_type} {explicit}"
    if "fail" in combined or "error" in combined:
        return "failed"
    if "cancel" in combined:
        return "cancelled"
    if "supersed" in combined:
        return "superseded"
    if "warn" in combined or "drift" in event_type:
        return "warning"
    if explicit in {"requested", "generating", "awaiting_dry_run", "decision_calculating"}:
        return "running"
    return "completed"


def _evidence_refs(
    payload: dict[str, Any], event: dict[str, Any], twin: dict[str, Any]
) -> list[dict[str, Any]]:
    refs = payload.get("evidence_refs") or []
    if not isinstance(refs, list):
        refs = [refs]
    result = []
    for index, item in enumerate(refs[:50]):
        value = str(item.get("evidence_id") if isinstance(item, dict) else item or "").strip()
        if not value:
            continue
        result.append(
            {
                "evidence_id": value[:300],
                "source_type": "report",
                "source_id": str(event.get("event_id")),
                "summary": f"Redacted evidence reference {index + 1}.",
                "captured_at": event.get("created_at"),
                "redacted": True,
                "href": f"/v1/namespace-twins/{twin['twin_id']}/audit?event={event['event_id']}",
            }
        )
    return result


def _report_links(twin_id: str) -> list[dict[str, str]]:
    return [
        {"format": "json", "href": f"/v1/namespace-twins/{twin_id}/reports/json"},
        {"format": "markdown", "href": f"/v1/namespace-twins/{twin_id}/reports/markdown"},
    ]
