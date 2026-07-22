"""Deterministic Namespace Twin policy, evidence, and risk assessment."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from bosgenesis_mop_execution_agent.artifacts.models import ArtifactBundle
from bosgenesis_mop_execution_agent.models import ResourceRef
from bosgenesis_mop_execution_agent.namespace_twin.delta import LiveSnapshot
from bosgenesis_mop_execution_agent.namespace_twin.models import (
    POLICY_VERSION,
    RISK_RULE_VERSION,
)
from bosgenesis_mop_execution_agent.policy.engine import (
    PolicyEvaluationContext,
    evaluate_policy,
)

POLICY_GROUPS = (
    "namespace_boundary",
    "cluster_scope",
    "rbac",
    "secret_handling",
    "privileged_host_access",
    "pvc_data_safety",
    "resource_quotas",
    "image_policy",
    "approval_policy",
    "bundle_dry_run_freshness_integrity",
)

RISK_RULES: tuple[tuple[str, int], ...] = (
    ("pvc_create_or_explicit_delete", 30),
    ("statefulset_change", 25),
    ("helm_release_upgrade", 20),
    ("image_change", 15),
    ("configmap_change", 15),
    ("ingress_change", 15),
    ("service_selector_change", 20),
    ("large_replica_change", 10),
    ("missing_rollback_step", 30),
    ("inferred_chart_or_value", 20),
    ("partial_or_stale_live_evidence", 20),
    ("previous_similar_failure", 20),
    ("drift_detected", 25),
)

POLICY_BUNDLE = {
    "version": POLICY_VERSION,
    "engine": "existing_phase4_policy_engine",
    "risk_rules_version": RISK_RULE_VERSION,
    "groups": list(POLICY_GROUPS),
    "evidence_policy": {
        "authoritative_dry_run_required_for_green": True,
        "required_evidence_unavailable_before_dry_run": "amber",
        "freshness_threshold_seconds": 900,
    },
    "decision_precedence": [
        "policy_deny_or_hard_block",
        "authoritative_dry_run_failed",
        "critical_unmitigated_risk",
        "approval_required",
        "partial_stale_or_unavailable_evidence",
        "risk_above_green_band",
        "otherwise_green",
    ],
}
POLICY_BUNDLE_HASH = hashlib.sha256(
    json.dumps(POLICY_BUNDLE, sort_keys=True, separators=(",", ":")).encode("utf-8")
).hexdigest()


def evaluate_policy_twin(
    *,
    bundle: ArtifactBundle,
    planned_resources: list[dict[str, Any]],
    deltas: list[dict[str, Any]],
    snapshot: LiveSnapshot,
    provenance: dict[str, Any],
    graph_summary: dict[str, Any],
    explicit_deletes: list[dict[str, Any]],
    input_hash: str,
    target_namespace: str,
    pvc_risk_enabled: bool = False,
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    """Return a versioned, non-LLM preliminary decision projection."""
    now = evaluated_at or datetime.now(UTC)
    manifests = [manifest.content for manifest in bundle.manifests]
    resource_refs = [
        ResourceRef(
            api_version=manifest.api_version,
            kind=manifest.kind,
            namespace=manifest.namespace,
            name=manifest.name,
            file_path=manifest.path,
        )
        for manifest in bundle.manifests
    ]
    command = (
        "\n".join(
            command.command
            for phase in bundle.machine_plan.phases
            for step in phase.steps
            for command in step.commands
            if command.mutating is True
        )
        or None
    )
    decision = evaluate_policy(
        PolicyEvaluationContext(
            job_id=f"namespace-twin-policy-{input_hash[:16]}",
            target_namespace=target_namespace,
            mutating=False,
            command=command,
            resource_refs=resource_refs,
            manifests=manifests,
            values_files=[item.content for item in bundle.values_files],
        )
    )

    findings = _policy_findings(decision.blocks)
    findings.extend(_privileged_findings(bundle))
    if bundle.machine_plan.executor_contract.human_approval_before_mutation:
        findings.append(
            _finding(
                "HUMAN_APPROVAL_REQUIRED",
                "approval_policy",
                "review",
                "approval_required",
                "The machine execution plan requires human approval before mutation.",
                ["machine_execution_plan.yaml#executor_contract"],
            )
        )
    if explicit_deletes:
        findings.append(
            _finding(
                "EXPLICIT_DELETE_APPROVAL_REQUIRED",
                "pvc_data_safety",
                "review",
                "approval_required",
                f"{len(explicit_deletes)} explicit delete step(s) require bounded approval.",
                [
                    reference
                    for item in explicit_deletes
                    for reference in item.get("manifest_refs") or []
                ],
            )
        )
    findings = _dedupe_findings(findings)

    evidence_axis = _evidence_axis(
        snapshot=snapshot,
        provenance=provenance,
        graph_summary=graph_summary,
        bundle=bundle,
        now=now,
    )
    risk_axis = _risk_axis(
        deltas=deltas,
        bundle=bundle,
        planned_resources=planned_resources,
        explicit_deletes=explicit_deletes,
        evidence_axis=evidence_axis,
        pvc_risk_enabled=pvc_risk_enabled,
    )
    policy_axis = _policy_axis(findings)
    decision_projection = _decision_projection(
        policy_axis=policy_axis,
        evidence_axis=evidence_axis,
        risk_axis=risk_axis,
    )
    rule_contributions = [
        *_policy_contributions(findings),
        *evidence_axis["contributions"],
        *risk_axis["contributions"],
        *decision_projection["contributions"],
    ]
    axes_hash = _hash(
        {
            "policy": policy_axis,
            "evidence": evidence_axis,
            "risk": risk_axis,
            "decision": decision_projection,
        }
    )
    decision_projection["axes_hash"] = axes_hash
    return {
        "schema_version": "1.0.0",
        "evaluated_at": now.isoformat(),
        "input_hash": input_hash,
        "policy_bundle": {**POLICY_BUNDLE, "hash": POLICY_BUNDLE_HASH},
        "policy_axis": policy_axis,
        "evidence_axis": evidence_axis,
        "risk_axis": risk_axis,
        "decision_projection": decision_projection,
        "rule_contributions": rule_contributions,
        "findings": findings,
        "passed_groups": [
            group
            for group in POLICY_GROUPS
            if not any(item["category"] == group for item in findings)
        ],
        "command_fingerprint_hash": decision.command_fingerprint,
        "dry_run_job_id": None,
        "model_authority": False,
    }


def finalize_policy_twin(
    policy_twin: dict[str, Any],
    *,
    dry_run_evidence: dict[str, Any],
    evaluated_at: datetime | None = None,
) -> dict[str, Any]:
    """Recalculate all deterministic axes after authoritative dry-run attachment."""
    finalized = deepcopy(policy_twin)
    status = str(dry_run_evidence.get("status") or "partial").lower()
    dry_run_passed = status == "passed"

    evidence_axis = deepcopy(finalized.get("evidence_axis") or {})
    checks = list(evidence_axis.get("checks") or [])
    dry_run_refs = [
        str(value)
        for value in (
            dry_run_evidence.get("dry_run_job_id"),
            dry_run_evidence.get("command_fingerprint_hash"),
        )
        if value
    ]
    replacement = _evidence_check(
        "authoritative_dry_run",
        dry_run_passed,
        (
            "Authoritative dry-run evidence completed successfully and was verified."
            if dry_run_passed
            else f"Authoritative dry-run evidence completed with status {status}."
        ),
        dry_run_refs,
    )
    replaced = False
    for index, check in enumerate(checks):
        if check.get("code") == "authoritative_dry_run":
            checks[index] = replacement
            replaced = True
            break
    if not replaced:
        checks.append(replacement)

    missing = [str(item.get("code")) for item in checks if not item.get("satisfied")]
    essential_missing = "bundle_integrity" in missing
    completeness = "unavailable" if essential_missing else "complete" if not missing else "partial"
    freshness = str(evidence_axis.get("freshness") or "unavailable")
    classification = "stale" if freshness == "stale" else completeness
    evidence_axis.update(
        {
            "classification": classification,
            "completeness": completeness,
            "required_count": len(checks),
            "present_count": len(checks) - len(missing),
            "missing": missing,
            "checks": checks,
            "contributions": [
                {
                    "axis": "evidence",
                    "rule": item["code"],
                    "matched": not item["satisfied"],
                    "effect": "degrade" if not item["satisfied"] else "satisfied",
                    "contribution": 0,
                    "reason": item["summary"],
                    "evidence_refs": item["evidence_refs"],
                }
                for item in checks
            ],
        }
    )

    risk_axis = deepcopy(finalized.get("risk_axis") or {})
    risk_contributions = list(risk_axis.get("contributions") or [])
    for contribution in risk_contributions:
        if contribution.get("rule") != "partial_or_stale_live_evidence":
            continue
        matched = classification in {"partial", "stale", "unavailable"}
        weight = int(contribution.get("weight") or 20)
        contribution.update(
            {
                "matched": matched,
                "effect": "increase" if matched else "none",
                "contribution": weight if matched else 0,
                "reason": _risk_reason("partial_or_stale_live_evidence", matched),
                "evidence_refs": [f"evidence:{item}" for item in missing],
            }
        )
        break
    raw_score = sum(int(item.get("contribution") or 0) for item in risk_contributions)
    score = min(raw_score, 100)
    level = (
        "low" if score <= 29 else "medium" if score <= 69 else "high" if score < 90 else "critical"
    )
    risk_axis.update(
        {
            "level": level,
            "score": score,
            "raw_score": raw_score,
            "contributions": risk_contributions,
        }
    )

    policy_axis = deepcopy(finalized.get("policy_axis") or {})
    decision_projection = _decision_projection(
        policy_axis=policy_axis,
        evidence_axis=evidence_axis,
        risk_axis=risk_axis,
        authoritative_dry_run_failed=not dry_run_passed,
    )
    decision_projection.update({"preliminary": False, "decision_is_final": True})
    axes_hash = _hash(
        {
            "policy": policy_axis,
            "evidence": evidence_axis,
            "risk": risk_axis,
            "decision": decision_projection,
        }
    )
    decision_projection["axes_hash"] = axes_hash
    findings = list(finalized.get("findings") or [])
    finalized.update(
        {
            "evaluated_at": (evaluated_at or datetime.now(UTC)).isoformat(),
            "evidence_axis": evidence_axis,
            "risk_axis": risk_axis,
            "decision_projection": decision_projection,
            "rule_contributions": [
                *_policy_contributions(findings),
                *evidence_axis["contributions"],
                *risk_axis["contributions"],
                *decision_projection["contributions"],
            ],
            "dry_run_job_id": dry_run_evidence.get("dry_run_job_id"),
            "command_fingerprint_hash": dry_run_evidence.get("command_fingerprint_hash")
            or finalized.get("command_fingerprint_hash"),
        }
    )
    return finalized

def _policy_axis(findings: list[dict[str, Any]]) -> dict[str, Any]:
    hard_blocks = [item for item in findings if item["effect"] == "deny"]
    approvals = [item for item in findings if item["effect"] == "approval_required"]
    verdict = "deny" if hard_blocks else "approval_required" if approvals else "allow"
    return {
        "verdict": verdict,
        "version": POLICY_VERSION,
        "bundle_hash": POLICY_BUNDLE_HASH,
        "hard_block_count": len(hard_blocks),
        "approval_required_count": len(approvals),
        "hard_blocks": [item["code"] for item in hard_blocks],
        "approval_requirements": [item["code"] for item in approvals],
    }


def _evidence_axis(
    *,
    snapshot: LiveSnapshot,
    provenance: dict[str, Any],
    graph_summary: dict[str, Any],
    bundle: ArtifactBundle,
    now: datetime,
) -> dict[str, Any]:
    has_rollback = _has_rollback(bundle)
    checks = [
        _evidence_check(
            "bundle_integrity",
            bool(provenance.get("artifact_index_present")),
            "Artifact index and referenced bundle files were verified.",
            ["artifact-index.json"],
        ),
        _evidence_check(
            "live_namespace_snapshot",
            snapshot.available,
            snapshot.warning or "Namespace-scoped live evidence was collected.",
            snapshot.evidence_refs,
        ),
        _evidence_check(
            "dependency_integrity",
            int(graph_summary.get("missing_nodes") or graph_summary.get("missing") or 0) == 0,
            "Dependency graph contains no missing nodes.",
            ["namespace-twin-dependency-graph"],
        ),
        _evidence_check(
            "rollback_evidence",
            has_rollback,
            (
                "The machine plan includes a rollback or cleanup step."
                if has_rollback
                else "The machine plan does not include a rollback or cleanup step."
            ),
            ["machine_execution_plan.yaml"],
        ),
        _evidence_check(
            "authoritative_dry_run",
            False,
            "Authoritative dry-run evidence is not attached until Slice 5E.",
            [],
        ),
    ]
    missing = [item["code"] for item in checks if not item["satisfied"]]
    essential_missing = "bundle_integrity" in missing
    completeness = "unavailable" if essential_missing else "complete" if not missing else "partial"
    freshness = "fresh" if snapshot.available else "unavailable"
    classification = "stale" if freshness == "stale" else completeness
    return {
        "classification": classification,
        "completeness": completeness,
        "freshness": freshness,
        "captured_at": now.isoformat() if snapshot.available else None,
        "age_seconds": 0 if snapshot.available else None,
        "freshness_threshold_seconds": 900,
        "required_count": len(checks),
        "present_count": len(checks) - len(missing),
        "missing": missing,
        "stale": [],
        "checks": checks,
        "contributions": [
            {
                "axis": "evidence",
                "rule": item["code"],
                "matched": not item["satisfied"],
                "effect": "degrade" if not item["satisfied"] else "satisfied",
                "contribution": 0,
                "reason": item["summary"],
                "evidence_refs": item["evidence_refs"],
            }
            for item in checks
        ],
    }


def _risk_axis(
    *,
    deltas: list[dict[str, Any]],
    bundle: ArtifactBundle,
    planned_resources: list[dict[str, Any]],
    explicit_deletes: list[dict[str, Any]],
    evidence_axis: dict[str, Any],
    pvc_risk_enabled: bool,
) -> dict[str, Any]:
    active = [row for row in deltas if row.get("action") not in {"no_op", None}]
    diffs = [_diff(row) for row in active]
    facts = {
        "pvc_create_or_explicit_delete": pvc_risk_enabled
        and (
            bool(explicit_deletes)
            or any(
                row.get("kind") == "PersistentVolumeClaim" and row.get("action") == "create"
                for row in active
            )
        ),
        "statefulset_change": any(row.get("kind") == "StatefulSet" for row in active),
        "helm_release_upgrade": any(
            row.get("helm_release") and row.get("action") == "update" for row in active
        ),
        "image_change": any(
            any(str(change.get("path") or "").endswith("image") for change in diff)
            for diff in diffs
        ),
        "configmap_change": any(row.get("kind") == "ConfigMap" for row in active),
        "ingress_change": any(row.get("kind") == "Ingress" for row in active),
        "service_selector_change": any(
            row.get("kind") == "Service"
            and any("spec.selector" in str(change.get("path") or "") for change in diff)
            for row, diff in zip(active, diffs, strict=True)
        ),
        "large_replica_change": any(_large_replica_change(diff) for diff in diffs),
        "missing_rollback_step": bool(active or explicit_deletes) and not _has_rollback(bundle),
        "inferred_chart_or_value": _has_inferred_helm_change(
            active=active,
            bundle=bundle,
            planned_resources=planned_resources,
        ),
        "partial_or_stale_live_evidence": evidence_axis["classification"]
        in {"partial", "stale", "unavailable"},
        "previous_similar_failure": False,
        "drift_detected": False,
    }
    contributions = []
    raw_score = 0
    for rule, weight in RISK_RULES:
        matched = bool(facts[rule])
        value = weight if matched else 0
        raw_score += value
        reason = _risk_reason(rule, matched)
        if rule == "pvc_create_or_explicit_delete" and not pvc_risk_enabled:
            reason = "PVC risk evaluation is disabled by configuration for this MVP."
        contributions.append(
            {
                "axis": "risk",
                "rule": rule,
                "matched": matched,
                "effect": "increase" if matched else "none",
                "contribution": value,
                "weight": weight,
                "reason": reason,
                "evidence_refs": _risk_evidence(rule, active, evidence_axis),
            }
        )
    score = min(raw_score, 100)
    level = (
        "low" if score <= 29 else "medium" if score <= 69 else "high" if score < 90 else "critical"
    )
    return {
        "level": level,
        "score": score,
        "raw_score": raw_score,
        "rules_version": RISK_RULE_VERSION,
        "thresholds": {"green_max": 29, "amber_min": 30, "amber_max": 69, "red_min": 70},
        "feature_toggles": {"pvc_risk_enabled": pvc_risk_enabled},
        "contributions": contributions,
    }


def _has_inferred_helm_change(
    *,
    active: list[dict[str, Any]],
    bundle: ArtifactBundle,
    planned_resources: list[dict[str, Any]],
) -> bool:
    changed_releases = {
        str(row.get("helm_release") or "").casefold()
        for row in active
        if row.get("helm_release")
    }
    if not changed_releases:
        return False

    inferred_helm_steps = []
    for phase in bundle.machine_plan.phases:
        for step in phase.steps:
            command_text = " ".join(
                f"{command.kind} {command.command}" for command in step.commands
            ).casefold()
            is_helm_step = "helm" in str(step.type).casefold() or "helm" in command_text
            if step.inference and is_helm_step:
                inferred_helm_steps.append(step)
    if not inferred_helm_steps:
        return False

    rendered_releases: set[str] = set()
    for resource in planned_resources:
        payload = resource.get("payload_redacted") or {}
        if payload.get("source") != "helm_rendered_manifest":
            continue
        manifest = payload.get("manifest") or {}
        metadata = manifest.get("metadata") or {}
        annotations = metadata.get("annotations") or {}
        labels = metadata.get("labels") or {}
        release = annotations.get("meta.helm.sh/release-name") or labels.get(
            "app.kubernetes.io/instance"
        )
        if release:
            rendered_releases.add(str(release).casefold())

    explicit_values = bool(bundle.values_files) and all(
        bool(step.values_refs) for step in inferred_helm_steps
    )
    rendered_evidence = bool(changed_releases & rendered_releases)
    return not (explicit_values and rendered_evidence)


def _decision_projection(
    *,
    policy_axis: dict[str, Any],
    evidence_axis: dict[str, Any],
    risk_axis: dict[str, Any],
    authoritative_dry_run_failed: bool = False,
) -> dict[str, Any]:
    rules = [
        ("policy_deny_or_hard_block", policy_axis["verdict"] == "deny", "red"),
        ("authoritative_dry_run_failed", authoritative_dry_run_failed, "red"),
        ("critical_unmitigated_risk", risk_axis["score"] >= 70, "red"),
        ("approval_required", policy_axis["verdict"] == "approval_required", "amber"),
        (
            "partial_stale_or_unavailable_evidence",
            evidence_axis["classification"] in {"partial", "stale", "unavailable"},
            "amber",
        ),
        ("risk_above_green_band", risk_axis["score"] >= 30, "amber"),
        ("otherwise_green", True, "green"),
    ]
    selected = next(item for item in rules if item[1])
    level = selected[2]
    return {
        "level": level,
        "label": level.title(),
        "preliminary": True,
        "decision_is_final": False,
        "precedence_rule": selected[0],
        "summary": _decision_summary(level, selected[0]),
        "hard_blocks": policy_axis["hard_blocks"],
        "approval_required": policy_axis["verdict"] == "approval_required",
        "model_authority": False,
        "contributions": [
            {
                "axis": "decision",
                "rule": rule,
                "matched": matched,
                "effect": effect,
                "selected": rule == selected[0],
                "contribution": 0,
                "reason": _decision_summary(effect, rule),
                "evidence_refs": [],
            }
            for rule, matched, effect in rules
        ],
    }


def _policy_findings(blocks: list[Any]) -> list[dict[str, Any]]:
    return [
        _finding(
            str(block.code),
            _category(str(block.guardrail or ""), str(block.code)),
            "critical" if str(block.severity) == "critical" else "block",
            "deny",
            str(block.message),
            [],
        )
        for block in blocks
    ]


def _privileged_findings(bundle: ArtifactBundle) -> list[dict[str, Any]]:
    findings = []
    for manifest in bundle.manifests:
        pod_spec = _pod_spec(manifest.content)
        privileged = pod_spec.get("hostNetwork") is True or pod_spec.get("hostPID") is True
        privileged = privileged or any(
            isinstance(container, dict)
            and isinstance(container.get("securityContext"), dict)
            and (
                container["securityContext"].get("privileged") is True
                or container["securityContext"].get("allowPrivilegeEscalation") is True
            )
            for container in [
                *(pod_spec.get("containers") or []),
                *(pod_spec.get("initContainers") or []),
            ]
        )
        if privileged:
            findings.append(
                _finding(
                    "PRIVILEGED_HOST_ACCESS_BLOCKED",
                    "privileged_host_access",
                    "critical",
                    "deny",
                    f"{manifest.kind}/{manifest.name} requests privileged or host access.",
                    [manifest.path],
                )
            )
    return findings


def _finding(
    code: str,
    category: str,
    severity: str,
    effect: str,
    message: str,
    evidence_refs: list[str],
) -> dict[str, Any]:
    digest = _hash({"code": code, "category": category, "message": message})[:24]
    return {
        "id": f"policyfinding_{digest}",
        "finding_id": f"policyfinding_{digest}",
        "code": code,
        "category": category,
        "severity": severity,
        "effect": effect,
        "status": "active",
        "title": code.replace("_", " ").title(),
        "detail": message,
        "message": message,
        "policy_version": POLICY_VERSION,
        "evidence_refs": list(dict.fromkeys(str(item) for item in evidence_refs if item)),
    }


def _policy_contributions(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    contributions = []
    for group in POLICY_GROUPS:
        matched = [item for item in findings if item["category"] == group]
        effect = (
            "deny"
            if any(item["effect"] == "deny" for item in matched)
            else "approval_required"
            if matched
            else "allow"
        )
        contributions.append(
            {
                "axis": "policy",
                "rule": group,
                "matched": bool(matched),
                "effect": effect,
                "contribution": 0,
                "reason": (
                    "; ".join(item["message"] for item in matched)
                    if matched
                    else f"Policy group {group} passed deterministic evaluation."
                ),
                "evidence_refs": list(
                    dict.fromkeys(
                        reference
                        for item in matched
                        for reference in item.get("evidence_refs") or []
                    )
                ),
            }
        )
    return contributions


def _evidence_check(
    code: str, satisfied: bool, summary: str, evidence_refs: list[str]
) -> dict[str, Any]:
    return {
        "code": code,
        "satisfied": bool(satisfied),
        "summary": summary,
        "evidence_refs": list(dict.fromkeys(str(item) for item in evidence_refs if item)),
    }


def _category(guardrail: str, code: str) -> str:
    if guardrail == "namespace_scope":
        return "cluster_scope" if "CLUSTER" in code else "namespace_boundary"
    return {
        "secret_guard": "secret_handling",
        "production_data_guard": "pvc_data_safety",
        "approval_gate": "approval_policy",
        "dry_run_gate": "bundle_dry_run_freshness_integrity",
        "limits": "resource_quotas",
    }.get(guardrail, "bundle_dry_run_freshness_integrity")


def _has_rollback(bundle: ArtifactBundle) -> bool:
    def describes_rollback(phase: Any, step: Any) -> bool:
        text = (
            f"{phase.phase_id} {phase.title or ''} {step.step_id} {step.title} {step.type}"
        ).lower()
        return bool(step.rollback_commands) or any(
            token in text for token in ("rollback", "cleanup", "revert")
        )

    return any(
        describes_rollback(phase, step)
        for phase in bundle.machine_plan.phases
        for step in phase.steps
    )


def _diff(row: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        value = json.loads(str(row.get("canonical_diff") or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value.get("field_changes") or [] if isinstance(value, dict) else []


def _large_replica_change(changes: list[dict[str, Any]]) -> bool:
    for change in changes:
        if not str(change.get("path") or "").endswith("spec.replicas"):
            continue
        try:
            if abs(int(change.get("planned") or 0) - int(change.get("current") or 0)) >= 3:
                return True
        except (TypeError, ValueError):
            continue
    return False


def _risk_reason(rule: str, matched: bool) -> str:
    return (
        f"Risk rule {rule} contributed its configured weight."
        if matched
        else f"Risk rule {rule} did not match the current facts."
    )


def _risk_evidence(
    rule: str, rows: list[dict[str, Any]], evidence_axis: dict[str, Any]
) -> list[str]:
    if rule == "partial_or_stale_live_evidence":
        return [f"evidence:{item}" for item in evidence_axis.get("missing") or []]
    return list(
        dict.fromkeys(
            str(reference) for row in rows for reference in row.get("evidence_refs") or []
        )
    )[:30]


def _decision_summary(level: str, rule: str) -> str:
    return {
        "red": f"Red preliminary projection selected by precedence rule {rule}.",
        "amber": f"Amber preliminary projection selected by precedence rule {rule}.",
        "green": (
            "Green preliminary projection; authoritative dry-run is still required to finalize."
        ),
    }.get(level, f"Decision contribution {rule} evaluated.")


def _pod_spec(resource: dict[str, Any]) -> dict[str, Any]:
    spec = resource.get("spec") if isinstance(resource.get("spec"), dict) else {}
    template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
    return template.get("spec") if isinstance(template.get("spec"), dict) else spec


def _dedupe_findings(findings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[tuple[str, str], dict[str, Any]] = {}
    for finding in findings:
        selected[(finding["code"], finding["detail"])] = finding
    return sorted(selected.values(), key=lambda item: (item["effect"], item["code"]))


def _hash(value: Any) -> str:
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
