"""Deterministic release-note claim validation for Namespace Digital Twins."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

from bosgenesis_mop_execution_agent.security import redact_value

RULES_VERSION = "namespace-twin-release-note-validation-1.0.0"
CATEGORIES = {
    "image",
    "configuration",
    "migration",
    "pvc_storage",
    "rbac",
    "route",
    "rollback",
    "breaking_change",
    "known_risk",
    "other",
}

_NEGATION = re.compile(
    r"\b(no|none|without|not|never|unchanged|no[- ]?op|does not|did not)\b",
    re.IGNORECASE,
)


def validate_release_note_claims(
    *,
    twin_id: str,
    artifact_id: str,
    artifact_hash: str,
    claims: list[dict[str, Any]],
    extraction: dict[str, Any],
    facts: dict[str, Any],
    deltas: list[dict[str, Any]],
    captured_at: datetime | None = None,
) -> dict[str, Any]:
    """Classify model-extracted claims against deterministic persisted evidence."""
    now = captured_at or datetime.now(UTC)
    evidence = _evidence_by_category(facts, deltas, captured_at=now)
    classified: list[dict[str, Any]] = []
    claimed_categories: set[str] = set()

    for index, raw in enumerate(claims[:100], start=1):
        category = str(raw.get("category") or "other").strip().lower()
        if category not in CATEGORIES:
            category = "other"
        claim = _bounded_text(raw.get("claim"), 4000)
        if not claim:
            continue
        claimed_categories.add(category)
        refs = list(evidence.get(category) or [])
        negated = bool(_NEGATION.search(claim))
        if refs and negated:
            status = "contradicted"
            summary = (
                f"Deterministic {category.replace('_', ' ')} evidence contradicts the "
                "negative release-note claim."
            )
        elif refs:
            status = "supported"
            summary = (
                f"Deterministic {category.replace('_', ' ')} evidence supports this claim."
            )
        else:
            status = "unsupported"
            summary = (
                f"No deterministic {category.replace('_', ' ')} evidence supports this claim."
            )
        classified.append(
            {
                "claim_id": _identifier("rnclaim", artifact_hash, str(index), category, claim),
                "category": category,
                "claim": claim,
                "status": status,
                "summary": summary,
                "evidence_refs": refs[:20],
            }
        )

    missing_notes: list[str] = []
    for category in sorted(key for key, refs in evidence.items() if refs and key != "other"):
        if category in claimed_categories:
            continue
        label = category.replace("_", " ")
        note = f"Release notes do not document deterministic {label} evidence."
        missing_notes.append(note)
        classified.append(
            {
                "claim_id": _identifier("rnclaim", artifact_hash, "missing", category),
                "category": category,
                "claim": note,
                "status": "missing",
                "summary": f"Operational {label} evidence exists but is absent from the artifact.",
                "evidence_refs": list(evidence[category])[:20],
            }
        )

    counts = {
        status: sum(item["status"] == status for item in classified)
        for status in ("supported", "unsupported", "contradicted", "missing")
    }
    status = (
        "failed"
        if counts["contradicted"]
        else "warning"
        if counts["unsupported"] or counts["missing"]
        else "passed"
    )
    corrections = _suggested_corrections(classified)
    all_refs = _unique_refs(
        reference
        for item in classified
        for reference in item.get("evidence_refs") or []
    )
    safe_extraction = {
        "method": str(extraction.get("method") or "bounded_model_with_fallback"),
        "model_profile": _bounded_text(extraction.get("model_profile"), 200),
        "prompt_version": _bounded_text(extraction.get("prompt_version"), 200),
        "prompt_hash": _hash_value(extraction.get("prompt_hash")),
        "input_hash": _hash_value(extraction.get("input_hash")),
        "fallback_used": bool(extraction.get("fallback_used")),
        "safe_summary": _bounded_text(
            extraction.get("safe_summary")
            or f"Extracted {len(claims)} bounded claim(s) for deterministic validation.",
            1000,
        ),
        "chain_of_thought_included": False,
        "model_authority": False,
    }
    result = {
        "status": status,
        "release_note_artifact_id": artifact_id,
        "release_note_artifact_hash": artifact_hash,
        "validated_at": now.isoformat(),
        "rules_version": RULES_VERSION,
        "claims": classified,
        "claim_counts": counts,
        "missing_operational_notes": missing_notes,
        "suggested_corrections": corrections,
        "automatic_overwrite_allowed": False,
        "execution_eligibility_effect": "none",
        "editorial_only": True,
        "extraction": safe_extraction,
        "evidence_refs": all_refs,
    }
    hash_material = _without_volatile_timestamps(result)
    result["validation_hash"] = hashlib.sha256(
        json.dumps(hash_material, sort_keys=True, separators=(",", ":"), default=str).encode(
            "utf-8"
        )
    ).hexdigest()
    return redact_value(result)


def _without_volatile_timestamps(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_volatile_timestamps(item)
            for key, item in value.items()
            if key not in {"validated_at", "captured_at"}
        }
    if isinstance(value, list):
        return [_without_volatile_timestamps(item) for item in value]
    return value


def _evidence_by_category(
    facts: dict[str, Any],
    deltas: list[dict[str, Any]],
    *,
    captured_at: datetime,
) -> dict[str, list[dict[str, Any]]]:
    evidence: dict[str, list[dict[str, Any]]] = {category: [] for category in CATEGORIES}
    for delta in deltas:
        category = _delta_category(delta)
        if category:
            evidence[category].append(
                _evidence_ref(
                    "release_delta",
                    str(delta.get("change_id") or delta.get("resource_identity") or "delta"),
                    (
                        f"{delta.get('action', 'unknown')} {delta.get('kind', 'resource')} "
                        f"{delta.get('name', 'unknown')} ({delta.get('risk', 'unknown')} risk)."
                    ),
                    captured_at,
                )
            )
        if str(delta.get("action")) in {"explicit_delete", "immutable_conflict"}:
            evidence["breaking_change"].append(
                _evidence_ref(
                    "release_delta",
                    str(delta.get("change_id") or "breaking-change"),
                    f"{delta.get('action')} affects {delta.get('kind')} {delta.get('name')}.",
                    captured_at,
                )
            )

    rollback = facts.get("rollback_twin") or {}
    if rollback.get("rollback_defined") or rollback.get("rollback_steps"):
        evidence["rollback"].append(
            _evidence_ref(
                "rollback_twin",
                "rollback",
                str(rollback.get("summary") or "Deterministic rollback evidence is available."),
                captured_at,
                source_type="report",
            )
        )
    policy = facts.get("policy_twin") or {}
    for finding in policy.get("findings") or []:
        if str(finding.get("effect")) in {"deny", "approval_required"} or str(
            finding.get("severity")
        ) in {"high", "critical"}:
            evidence["known_risk"].append(
                _evidence_ref(
                    "policy_finding",
                    str(finding.get("finding_id") or finding.get("code") or "policy"),
                    str(finding.get("detail") or finding.get("title") or "Policy risk."),
                    captured_at,
                    source_type="policy",
                )
            )
    dry_run = facts.get("dry_run_evidence") or {}
    observations = dry_run.get("observations") or []
    if any(str(item.get("outcome")) in {"rejected", "warning", "unknown"} for item in observations):
        evidence["known_risk"].append(
            _evidence_ref(
                "dry_run_observation",
                str(dry_run.get("dry_run_job_id") or "dry-run"),
                "Authoritative dry-run evidence contains warnings, rejection, or unknown outcomes.",
                captured_at,
                source_type="dry_run",
            )
        )
    drift = facts.get("drift_twin") or {}
    runtime = facts.get("runtime_behavior_twin") or {}
    if drift.get("material"):
        evidence["known_risk"].append(
            _evidence_ref(
                "drift_twin",
                "material-drift",
                "Material namespace drift is present.",
                captured_at,
                source_type="live_snapshot",
            )
        )
    if str(runtime.get("risk")) in {"medium", "high", "critical", "unknown"}:
        evidence["known_risk"].append(
            _evidence_ref(
                "runtime_behavior",
                "runtime-risk",
                str(runtime.get("summary") or "Current runtime behavior requires review."),
                captured_at,
                source_type="live_snapshot",
            )
        )
    if deltas:
        evidence["other"].append(
            _evidence_ref(
                "release_delta_summary",
                "release-delta",
                f"{len(deltas)} canonical release delta row(s) are available.",
                captured_at,
            )
        )
    return {key: _unique_refs(value) for key, value in evidence.items()}


def _delta_category(delta: dict[str, Any]) -> str | None:
    kind = str(delta.get("kind") or "").lower()
    text = " ".join(
        str(delta.get(key) or "")
        for key in (
            "kind", "name", "reason", "canonical_diff", "current_summary", "planned_summary"
        )
    ).lower()
    if "migrat" in text:
        return "migration"
    if kind in {"persistentvolume", "persistentvolumeclaim", "storageclass"} or any(
        token in text for token in ("storage", "pvc", "volumeclaim")
    ):
        return "pvc_storage"
    if kind in {"configmap"} or any(token in text for token in ("config", "value", "environment")):
        return "configuration"
    if kind in {"role", "rolebinding", "clusterrole", "clusterrolebinding", "serviceaccount"}:
        return "rbac"
    if kind in {"ingress", "route", "service", "gateway", "httproute"}:
        return "route"
    if "image" in text:
        return "image"
    return None


def _evidence_ref(
    prefix: str,
    source_id: str,
    summary: str,
    captured_at: datetime,
    *,
    source_type: str = "bundle",
) -> dict[str, Any]:
    return {
        "evidence_id": _identifier("rnevidence", prefix, source_id),
        "source_type": source_type,
        "source_id": _identifier("source", source_id),
        "summary": _bounded_text(summary, 4000) or "Deterministic evidence is available.",
        "captured_at": captured_at.isoformat(),
        "redacted": True,
        "href": None,
    }


def _suggested_corrections(claims: list[dict[str, Any]]) -> list[str]:
    suggestions: list[str] = []
    for item in claims:
        if item["status"] == "contradicted":
            suggestions.append(
                f"Correct or qualify the contradicted {item['category'].replace('_', ' ')} claim: "
                f"{item['claim']}"
            )
        elif item["status"] == "unsupported":
            suggestions.append(
                f"Add deterministic evidence or remove the unsupported claim: {item['claim']}"
            )
        elif item["status"] == "missing":
            suggestions.append(f"Add an operational note for {item['category'].replace('_', ' ')}.")
    return suggestions[:100]


def _unique_refs(values: Any) -> list[dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    for value in values:
        if isinstance(value, dict) and value.get("evidence_id"):
            found[str(value["evidence_id"])] = value
    return [found[key] for key in sorted(found)]


def _identifier(prefix: str, *values: str) -> str:
    digest = hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def _hash_value(value: Any) -> str | None:
    text = str(value or "").lower()
    return text if re.fullmatch(r"[a-f0-9]{64}", text) else None


def _bounded_text(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]
