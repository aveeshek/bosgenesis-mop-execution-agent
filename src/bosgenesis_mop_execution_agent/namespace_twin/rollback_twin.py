"""Deterministic rollback readiness assessment for Namespace Digital Twins."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bosgenesis_mop_execution_agent.artifacts.models import ArtifactBundle

ROLLBACK_RULE_VERSION = "namespace-twin-rollback-1.0.0"

_FORWARD_TYPES = {"k8s_apply", "k8s_delete", "helm_install", "helm_upgrade"}
_ROLLBACK_WORDS = ("rollback", "revert", "restore", "uninstall")
_DATA_DESTRUCTIVE_WORDS = (
    "drop database",
    "drop table",
    "truncate",
    "delete database",
    "wipe data",
    "purge data",
    "persistentvolumeclaim --all",
)


def assess_rollback_twin(
    bundle: ArtifactBundle,
    *,
    captured_at: datetime | None = None,
    dry_run_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Assess rollback definition, evidence, and proof without model inference."""
    captured = (captured_at or datetime.now(UTC)).astimezone(UTC).isoformat()
    forward_steps = _forward_steps(bundle)
    rollback_steps = _rollback_steps(bundle)
    links = _link_rollback_steps(forward_steps, rollback_steps)
    linked_forward_ids = {forward_id for item in links for forward_id in item["forward_step_ids"]}
    previous = _previous_artifacts(bundle)
    helm = _helm_evidence(bundle, forward_steps, rollback_steps)
    pvc_status, pvc_findings = _pvc_reversibility(bundle, forward_steps, rollback_steps)
    destructive_findings = _non_reversible_changes(bundle, rollback_steps)
    non_reversible = pvc_findings + destructive_findings
    proof = _rollback_proof(dry_run_evidence or {}, links)

    mutating_count = len(forward_steps)
    linked_count = len(linked_forward_ids)
    coverage = 100 if mutating_count == 0 else int((linked_count / mutating_count) * 100)
    rollback_defined = bool(rollback_steps) and (
        mutating_count == 0 or linked_count == mutating_count
    )
    rollback_proven = rollback_defined and proof["status"] == "passed"

    score = 100
    gaps: list[dict[str, Any]] = []
    if not rollback_steps:
        score -= 30
        gaps.append(
            _gap(
                "ROLLBACK_STEP_MISSING", "No rollback step is defined in the machine plan.", "high"
            )
        )
    elif coverage < 100:
        score -= 30
        gaps.append(
            _gap(
                "ROLLBACK_LINK_INCOMPLETE",
                f"Rollback links cover {linked_count} of {mutating_count} forward operation(s).",
                "high",
            )
        )
    if helm["required"] and not helm["history_available"]:
        score -= 20
        gaps.append(
            _gap("HELM_HISTORY_MISSING", "A previous Helm revision is not evidenced.", "medium")
        )
    if not previous["manifests_available"] and not previous["values_available"]:
        score -= 15
        gaps.append(
            _gap(
                "PREVIOUS_ARTIFACTS_MISSING",
                "No previous manifests or previous Helm values are included in the bundle.",
                "medium",
            )
        )
    if pvc_status == "conditional":
        score -= 20
        gaps.append(
            _gap("PVC_ROLLBACK_CONDITIONAL", "PVC/data restoration needs operator review.", "high")
        )
    elif pvc_status == "not_reversible":
        score = 0
        gaps.append(
            _gap(
                "PVC_ROLLBACK_NOT_REVERSIBLE",
                "PVC/data rollback is not safely reversible.",
                "critical",
            )
        )
    if destructive_findings:
        score = 0
        gaps.append(
            _gap(
                "DATA_DESTRUCTIVE_ROLLBACK",
                "A data-destructive rollback action is present.",
                "critical",
            )
        )
    if proof["status"] != "passed":
        score -= 10
        gaps.append(
            _gap(
                "ROLLBACK_NOT_PROVEN",
                "Rollback is not proven by authoritative rollback-specific "
                "dry-run or execution evidence.",
                "medium",
            )
        )
    score = max(0, min(score, 100))
    confidence = _confidence(
        score, rollback_steps=rollback_steps, destructive=bool(destructive_findings)
    )
    if rollback_defined and not rollback_proven and confidence == "high":
        score = min(score, 79)
        confidence = "medium"

    evidence_refs = _evidence_refs(
        bundle,
        captured_at=captured,
        previous=previous,
        helm=helm,
        proof=proof,
    )
    validation_checks = _validation_checks(bundle, rollback_steps)
    manual_steps = _manual_steps(rollback_steps, gaps)
    return {
        "rule_version": ROLLBACK_RULE_VERSION,
        "evaluated_at": captured,
        "confidence": confidence,
        "confidence_score": score,
        "summary": _summary(confidence, rollback_defined, rollback_proven, coverage),
        "rollback_defined": rollback_defined,
        "rollback_proven": rollback_proven,
        "coverage": {
            "mutating_operations": mutating_count,
            "linked_operations": linked_count,
            "coverage_percent": coverage,
            "unlinked_forward_step_ids": sorted(
                item["step_id"]
                for item in forward_steps
                if item["step_id"] not in linked_forward_ids
            ),
        },
        "helm": helm,
        "previous_helm_revision": helm["previous_revision"],
        "previous_artifacts_available": bool(
            previous["manifests_available"] or previous["values_available"]
        ),
        "previous_artifacts": previous,
        "machine_plan_steps": links,
        "pvc_data_reversibility": pvc_status,
        "non_reversible_changes": non_reversible,
        "runtime_rollback_proven": rollback_proven,
        "proof": proof,
        "gaps": gaps,
        "manual_steps": manual_steps,
        "validation_checks": validation_checks,
        "evidence_refs": evidence_refs,
        "artifacts": [],
        "model_authority": False,
    }


def enrich_rollback_proof(
    assessment: dict[str, Any], dry_run_evidence: dict[str, Any]
) -> dict[str, Any]:
    """Attach rollback-specific proof facts to an existing deterministic assessment."""
    updated = dict(assessment)
    proof = _rollback_proof(dry_run_evidence, list(assessment.get("machine_plan_steps") or []))
    defined = bool(assessment.get("rollback_defined"))
    proven = defined and proof["status"] == "passed"
    updated["proof"] = proof
    updated["rollback_proven"] = proven
    updated["runtime_rollback_proven"] = proven
    updated["evaluated_at"] = datetime.now(UTC).isoformat()
    gaps = [
        item
        for item in list(updated.get("gaps") or [])
        if item.get("code") != "ROLLBACK_NOT_PROVEN"
    ]
    score = int(updated.get("confidence_score") or 0)
    if proven:
        score = min(100, score + 10)
    else:
        gaps.append(
            _gap(
                "ROLLBACK_NOT_PROVEN",
                "Rollback is not proven by authoritative rollback-specific "
                "dry-run or execution evidence.",
                "medium",
            )
        )
    updated["gaps"] = gaps
    updated["confidence_score"] = score
    updated["confidence"] = _confidence(
        score,
        rollback_steps=list(updated.get("machine_plan_steps") or []),
        destructive=any(
            item.get("severity") == "critical"
            for item in list(updated.get("non_reversible_changes") or [])
        ),
    )
    updated["summary"] = _summary(
        updated["confidence"],
        defined,
        proven,
        int((updated.get("coverage") or {}).get("coverage_percent") or 0),
    )
    return updated


def _forward_steps(bundle: ArtifactBundle) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for phase in bundle.machine_plan.phases:
        for step in phase.steps:
            mutating = step.type in _FORWARD_TYPES or any(
                command.mutating is True for command in step.commands
            )
            if mutating and not _is_rollback(
                phase.phase_id, phase.title, step.step_id, step.title, step.type
            ):
                rows.append(
                    {
                        "phase_id": phase.phase_id,
                        "step_id": step.step_id,
                        "title": step.title,
                        "type": step.type,
                        "manifest_refs": list(step.manifest_refs),
                        "values_refs": list(step.values_refs),
                        "metadata": dict(step.metadata),
                    }
                )
    return rows


def _rollback_steps(bundle: ArtifactBundle) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for phase in bundle.machine_plan.phases:
        for step in phase.steps:
            command_text = "\n".join(command.command for command in step.commands)
            if not _is_rollback(
                phase.phase_id, phase.title, step.step_id, step.title, step.type, command_text
            ):
                continue
            rows.append(
                {
                    "phase_id": phase.phase_id,
                    "step_id": step.step_id,
                    "title": step.title,
                    "type": step.type,
                    "depends_on": list(step.depends_on),
                    "manifest_refs": list(step.manifest_refs),
                    "values_refs": list(step.values_refs),
                    "metadata": dict(step.metadata),
                    "commands": [command.model_dump(mode="json") for command in step.commands],
                    "expected_outcomes": list(step.expected_outcomes),
                    "required_human_inputs": list(step.required_human_inputs),
                }
            )
    return rows


def _is_rollback(*values: Any) -> bool:
    text = " ".join(str(value or "") for value in values).lower()
    return any(word in text for word in _ROLLBACK_WORDS)


def _link_rollback_steps(
    forward_steps: list[dict[str, Any]], rollback_steps: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    forward_ids = {item["step_id"] for item in forward_steps}
    rows: list[dict[str, Any]] = []
    for step in rollback_steps:
        metadata = step.get("metadata") or {}
        explicit: list[str] = []
        for key in ("forward_step_id", "reverts_step_id", "rollback_for", "forward_step_ids"):
            value = metadata.get(key)
            if isinstance(value, str):
                explicit.append(value)
            elif isinstance(value, list):
                explicit.extend(str(item) for item in value)
        explicit.extend(str(item) for item in step.get("depends_on") or [])
        linked = {item for item in explicit if item in forward_ids}
        if not linked:
            rollback_refs = set(step.get("manifest_refs") or []) | set(
                step.get("values_refs") or []
            )
            for forward in forward_steps:
                forward_refs = set(forward.get("manifest_refs") or []) | set(
                    forward.get("values_refs") or []
                )
                if rollback_refs and rollback_refs.intersection(forward_refs):
                    linked.add(forward["step_id"])
                elif metadata.get("release_name") and metadata.get("release_name") == (
                    forward.get("metadata") or {}
                ).get("release_name"):
                    linked.add(forward["step_id"])
        if not linked and len(forward_steps) == 1 and len(rollback_steps) == 1:
            linked.add(forward_steps[0]["step_id"])
        commands = step.get("commands") or []
        rows.append(
            {
                "step_id": step["step_id"],
                "summary": step["title"],
                "reversible": not any(
                    word in str(command.get("command") or "").lower()
                    for command in commands
                    for word in _DATA_DESTRUCTIVE_WORDS
                ),
                "forward_step_ids": sorted(linked),
                "mechanism": _rollback_mechanism(step),
                "command_available": bool(commands),
                "dry_run_capable": any(command.get("dry_run") is True for command in commands),
                "evidence_refs": sorted(
                    set(step.get("manifest_refs") or []) | set(step.get("values_refs") or [])
                ),
            }
        )
    return rows


def _rollback_mechanism(step: dict[str, Any]) -> str:
    text = " ".join(
        [step.get("type", "")]
        + [str(item.get("command") or "") for item in step.get("commands") or []]
    ).lower()
    if "helm rollback" in text:
        return "helm_revision_rollback"
    if "helm uninstall" in text or "uninstall" in text:
        return "helm_uninstall"
    if "kubectl apply" in text or "apply" in text:
        return "previous_manifest_reapply"
    if "kubectl delete" in text or "delete" in text:
        return "resource_delete"
    return "manual_or_agent_rollback"


def _previous_artifacts(bundle: ArtifactBundle) -> dict[str, Any]:
    paths = _artifact_paths(bundle)
    previous_paths = [
        path
        for path in paths
        if any(token in path.lower() for token in ("previous", "prior", "baseline"))
        and "machine_execution_plan" not in path.lower()
    ]
    manifest_paths = [
        path for path in previous_paths if Path(path).suffix.lower() in {".yaml", ".yml", ".json"}
    ]
    value_paths = [path for path in previous_paths if "value" in path.lower()]
    return {
        "manifests_available": bool(manifest_paths),
        "values_available": bool(value_paths),
        "manifest_paths": manifest_paths,
        "values_paths": value_paths,
        "provenance": "bundle_artifact_index" if previous_paths else "not_available",
    }


def _artifact_paths(bundle: ArtifactBundle) -> list[str]:
    index = bundle.artifact_index_json or {}
    paths: list[str] = []
    for item in index.get("files") or []:
        if isinstance(item, dict) and isinstance(item.get("path"), str):
            paths.append(item["path"])
    return sorted(set(paths))


def _helm_evidence(
    bundle: ArtifactBundle,
    forward_steps: list[dict[str, Any]],
    rollback_steps: list[dict[str, Any]],
) -> dict[str, Any]:
    helm_steps = [item for item in forward_steps if item["type"].startswith("helm_")]
    metadata = [item.get("metadata") or {} for item in helm_steps + rollback_steps]
    artifact = bundle.artifact_json or {}
    previous_revision = _first_int(
        [
            artifact.get("previous_helm_revision"),
            artifact.get("helm_previous_revision"),
            artifact.get("previous_revision"),
        ]
        + [item.get("previous_revision") for item in metadata]
        + [item.get("revision") for item in metadata if item.get("rollback_for")]
    )
    current_revision = _first_int(
        [artifact.get("helm_revision"), artifact.get("current_helm_revision")]
        + [item.get("current_revision") for item in metadata]
    )
    release_name = next(
        (str(item.get("release_name")) for item in metadata if item.get("release_name")),
        None,
    )
    refs = [
        path
        for path in _artifact_paths(bundle)
        if "helm" in path.lower()
        and any(word in path.lower() for word in ("history", "revision", "provenance"))
    ]
    return {
        "required": bool(helm_steps),
        "release_name": release_name,
        "current_revision": current_revision,
        "previous_revision": previous_revision,
        "history_available": previous_revision is not None or bool(refs),
        "provenance": "bundle_metadata"
        if previous_revision is not None
        else "artifact_index"
        if refs
        else "not_available",
        "evidence_refs": refs,
    }


def _first_int(values: list[Any]) -> int | None:
    for value in values:
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            continue
        if parsed > 0:
            return parsed
    return None


def _pvc_reversibility(
    bundle: ArtifactBundle,
    forward_steps: list[dict[str, Any]],
    rollback_steps: list[dict[str, Any]],
) -> tuple[str, list[dict[str, Any]]]:
    pvc_manifests = [item for item in bundle.manifests if item.kind == "PersistentVolumeClaim"]
    stateful = [item for item in bundle.manifests if item.kind == "StatefulSet"]
    if not pvc_manifests and not stateful:
        return "not_applicable", []
    rollback_text = " ".join(
        str(command.get("command") or "")
        for step in rollback_steps
        for command in step.get("commands") or []
    ).lower()
    deletes_pvc = "persistentvolumeclaim" in rollback_text and "delete" in rollback_text
    finding = _finding(
        "PVC_DATA_REVERSIBILITY",
        "PVC and data rollback requires operator review",
        "critical" if deletes_pvc else "high",
        "denied" if deletes_pvc else "approval_required",
        (
            "Rollback deletes a PersistentVolumeClaim and can cause data loss."
            if deletes_pvc
            else "The bundle changes stateful/PVC-backed resources but contains "
            "no proven data restore evidence."
        ),
        [item.path for item in pvc_manifests + stateful],
    )
    return ("not_reversible" if deletes_pvc else "conditional"), [finding]


def _non_reversible_changes(
    bundle: ArtifactBundle, rollback_steps: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for step in rollback_steps:
        for command in step.get("commands") or []:
            text = str(command.get("command") or "").lower()
            matches = [word for word in _DATA_DESTRUCTIVE_WORDS if word in text]
            if matches:
                findings.append(
                    _finding(
                        "DATA_DESTRUCTIVE_ROLLBACK",
                        "Data-destructive rollback action",
                        "critical",
                        "denied",
                        f"Rollback step {step['step_id']} contains a data-destructive action.",
                        list(step.get("manifest_refs") or []),
                    )
                )
    return findings


def _rollback_proof(
    evidence: dict[str, Any], rollback_steps: list[dict[str, Any]]
) -> dict[str, Any]:
    step_ids = {item.get("step_id") for item in rollback_steps}
    observations = [
        item
        for item in evidence.get("observations") or []
        if item.get("step") in step_ids
        or _is_rollback(item.get("phase"), item.get("step"), item.get("tool"), item.get("summary"))
    ]
    rejected = [
        item for item in observations if item.get("outcome") in {"rejected", "warning", "unknown"}
    ]
    accepted = [item for item in observations if item.get("outcome") == "accepted"]
    status = "passed" if accepted and not rejected else "failed" if rejected else "not_run"
    return {
        "status": status,
        "dry_run_job_id": evidence.get("dry_run_job_id"),
        "validated_step_ids": sorted(
            {str(item.get("step")) for item in accepted if item.get("step")}
        ),
        "evidence_refs": sorted(
            {str(ref) for item in observations for ref in item.get("evidence_refs") or []}
        ),
        "summary": (
            "Rollback-specific authoritative evidence passed."
            if status == "passed"
            else "Rollback-specific authoritative evidence failed."
            if status == "failed"
            else "No rollback-specific authoritative dry-run or execution evidence is attached."
        ),
    }


def _validation_checks(bundle: ArtifactBundle, rollback_steps: list[dict[str, Any]]) -> list[str]:
    checks = [outcome for step in rollback_steps for outcome in step.get("expected_outcomes") or []]
    if not checks:
        checks = [
            "Verify target resources return to the expected previous state.",
            "Verify workload readiness and service endpoints after rollback.",
        ]
    if any(item.kind == "PersistentVolumeClaim" for item in bundle.manifests):
        checks.append("Verify PVC binding and application data integrity after rollback.")
    return list(dict.fromkeys(checks))


def _manual_steps(rollback_steps: list[dict[str, Any]], gaps: list[dict[str, Any]]) -> list[str]:
    steps = [value for item in rollback_steps for value in item.get("required_human_inputs") or []]
    if gaps:
        steps.append("Review and resolve every rollback evidence gap before autonomous execution.")
    if not rollback_steps:
        steps.append("Define and link rollback steps for each mutating operation.")
    return list(dict.fromkeys(steps))


def _evidence_refs(
    bundle: ArtifactBundle,
    *,
    captured_at: str,
    previous: dict[str, Any],
    helm: dict[str, Any],
    proof: dict[str, Any],
) -> list[dict[str, Any]]:
    rows = [
        _evidence(
            "bundle:machine_execution_plan",
            "bundle",
            "machine_execution_plan.yaml",
            "Machine plan rollback and forward operations were parsed.",
            captured_at,
        )
    ]
    for path in previous["manifest_paths"] + previous["values_paths"]:
        rows.append(
            _evidence(f"bundle:{path}", "bundle", path, f"Previous artifact: {path}", captured_at)
        )
    for path in helm["evidence_refs"]:
        rows.append(
            _evidence(
                f"helm:{path}", "helm", path, f"Helm revision provenance: {path}", captured_at
            )
        )
    for ref in proof["evidence_refs"]:
        rows.append(
            _evidence(
                f"dry-run:{ref}", "dry_run", str(ref), "Rollback proof evidence.", captured_at
            )
        )
    return rows


def _evidence(
    seed: str,
    source_type: str,
    source_id: str | None,
    summary: str,
    captured_at: str,
) -> dict[str, Any]:
    return {
        "evidence_id": "rollback_" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24],
        "source_type": source_type,
        "source_id": source_id,
        "summary": summary,
        "captured_at": captured_at,
        "redacted": True,
        "href": None,
    }


def _finding(
    code: str,
    title: str,
    severity: str,
    status: str,
    summary: str,
    refs: list[str],
) -> dict[str, Any]:
    captured = datetime.now(UTC).isoformat()
    return {
        "finding_id": "rollbackfinding_"
        + hashlib.sha256((code + summary).encode("utf-8")).hexdigest()[:24],
        "code": code,
        "title": title,
        "severity": severity,
        "status": status,
        "summary": summary,
        "category": "rollback",
        "policy_version": ROLLBACK_RULE_VERSION,
        "resource_identity": None,
        "evidence_refs": [
            _evidence(
                f"finding:{code}:{ref}", "bundle", ref, f"Rollback finding source: {ref}", captured
            )
            for ref in refs
        ],
    }


def _gap(code: str, summary: str, severity: str) -> dict[str, Any]:
    return {"code": code, "summary": summary, "severity": severity}


def _confidence(score: int, *, rollback_steps: list[Any], destructive: bool) -> str:
    if destructive:
        return "low"
    if not rollback_steps:
        return "unavailable"
    if score >= 80:
        return "high"
    if score >= 55:
        return "medium"
    return "low"


def _summary(confidence: str, defined: bool, proven: bool, coverage: int) -> str:
    return (
        f"Rollback confidence is {confidence}. Plan coverage is {coverage}%. "
        f"Rollback is {'defined' if defined else 'not fully defined'} and "
        f"{'proven by authoritative evidence' if proven else 'not proven at runtime'}."
    )
