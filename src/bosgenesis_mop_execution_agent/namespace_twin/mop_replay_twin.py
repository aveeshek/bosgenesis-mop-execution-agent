"""Deterministic validation for isolated MoP replay evidence."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any

REPLAY_RULES_VERSION = "namespace-twin-mop-replay-1.0.0"
REPLAY_MODES = {"mimic_namespace", "ephemeral_cluster"}
TIMELINE_STATUSES = {"pending", "running", "passed", "warning", "failed", "skipped"}
CHECK_TYPES = {
    "helm_hook",
    "readiness",
    "init_container",
    "pvc_binding",
    "service",
    "smoke_test",
    "log",
    "cleanup",
}
CHECK_STATUSES = {"passed", "warning", "failed", "skipped", "not_available"}
REQUIRED_CHECKS = {"readiness", "smoke_test", "cleanup"}
_DNS_LABEL = re.compile(r"^[a-z0-9](?:[-a-z0-9]{0,61}[a-z0-9])?$")


class ReplayEvidenceError(ValueError):
    """Raised when submitted replay evidence violates the isolated-replay contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def build_replay_result(
    *,
    twin_id: str,
    source_namespace: str | None,
    target_namespace: str,
    target_cluster: str,
    payload: dict[str, Any],
    recorded_at: datetime | None = None,
) -> dict[str, Any]:
    """Validate and normalize explicit isolated replay facts without granting authority."""
    if payload.get("infrastructure_approved") is not True:
        raise ReplayEvidenceError(
            "replay_infrastructure_not_approved",
            "Replay infrastructure must be explicitly approved before evidence is accepted.",
        )
    approval_id = str(payload.get("approval_id") or "").strip()
    if not approval_id:
        raise ReplayEvidenceError(
            "replay_approval_id_required",
            "An explicit replay infrastructure approval ID is required.",
        )
    mode = str(payload.get("mode") or "").strip()
    if mode not in REPLAY_MODES:
        raise ReplayEvidenceError(
            "invalid_replay_mode", "Replay mode must be mimic_namespace or ephemeral_cluster."
        )
    isolation_target = str(payload.get("isolation_target") or "").strip()
    if not isolation_target:
        raise ReplayEvidenceError(
            "replay_isolation_target_required", "An isolated replay target is required."
        )
    if mode == "mimic_namespace":
        if not _DNS_LABEL.fullmatch(isolation_target) or not isolation_target.startswith(
            "esda-twin-"
        ):
            raise ReplayEvidenceError(
                "unsafe_replay_namespace",
                "Mimic replay must use a DNS-safe namespace prefixed with esda-twin-.",
            )
        if isolation_target in {target_namespace, str(source_namespace or "").strip()}:
            raise ReplayEvidenceError(
                "replay_target_not_isolated",
                "Mimic replay cannot use the source or intended target namespace.",
            )
    elif isolation_target == target_cluster:
        raise ReplayEvidenceError(
            "replay_cluster_not_isolated",
            "Ephemeral replay cannot identify the configured target cluster as its "
            "isolation target.",
        )
    if payload.get("production_secret_values_copied") is not False:
        raise ReplayEvidenceError(
            "production_secret_copy_forbidden",
            "Production Secret values must never be copied into replay infrastructure.",
        )
    if payload.get("production_data_copied") is not False:
        raise ReplayEvidenceError(
            "production_data_copy_forbidden",
            "Production data must never be copied into replay infrastructure.",
        )
    secret_strategy = str(payload.get("synthetic_secret_strategy") or "").strip()
    strategy_kind = next(
        (
            token
            for token in ("synthetic", "placeholder", "redacted")
            if token in secret_strategy.lower()
        ),
        None,
    )
    if strategy_kind is None:
        raise ReplayEvidenceError(
            "synthetic_secret_strategy_required",
            "Replay evidence must describe a synthetic, placeholder, or redacted Secret strategy.",
        )

    timeline = _timeline(payload.get("timeline"))
    checks = _checks(payload.get("checks"))
    missing_checks = sorted(REQUIRED_CHECKS - {item["type"] for item in checks})
    if missing_checks:
        raise ReplayEvidenceError(
            "replay_checks_incomplete",
            "Replay evidence is missing required checks: " + ", ".join(missing_checks) + ".",
        )
    cleanup_status = str(payload.get("cleanup_status") or "").strip()
    if cleanup_status not in {"completed", "failed"}:
        raise ReplayEvidenceError(
            "replay_cleanup_terminal_required",
            "Replay evidence is accepted only after cleanup reaches completed or failed.",
        )
    evidence_refs = _evidence_refs(payload.get("evidence_refs"))
    limitations = [
        str(item).strip()[:2000]
        for item in list(payload.get("limitations") or [])[:50]
        if str(item).strip()
    ]
    production_limit = (
        "Isolated replay is additional evidence only and does not prove production success."
    )
    if production_limit not in limitations:
        limitations.append(production_limit)

    failed = cleanup_status == "failed" or any(
        item["status"] == "failed" for item in [*timeline, *checks]
    )
    status = "failed" if failed else "passed"
    replay_id = str(payload.get("replay_id") or "").strip() or (
        "replay_"
        + hashlib.sha256(f"{twin_id}:{mode}:{isolation_target}:{approval_id}".encode()).hexdigest()[
            :24
        ]
    )
    result = {
        "replay_id": replay_id[:300],
        "status": status,
        "isolation": f"{mode}: {isolation_target}",
        "synthetic_secret_strategy": f"{strategy_kind} values only",
        "retention_seconds": max(0, int(payload.get("retention_seconds") or 0)),
        "timeline": timeline,
        "checks": checks,
        "cleanup_status": cleanup_status,
        "evidence_refs": evidence_refs,
        "limitations": limitations,
        "authoritative": True,
        "additional_evidence_only": True,
        "production_secret_values_copied": False,
        "production_data_copied": False,
        "model_authority": False,
        "execution_eligibility_effect": "none",
        "approval_id": approval_id[:300],
        "rules_version": REPLAY_RULES_VERSION,
        "recorded_at": (recorded_at or datetime.now(UTC)).isoformat(),
    }
    canonical = json.dumps(result, sort_keys=True, separators=(",", ":"), default=str)
    result["replay_hash"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return result


def _timeline(value: Any) -> list[dict[str, Any]]:
    rows = list(value or [])
    if not rows:
        raise ReplayEvidenceError(
            "replay_timeline_required", "Replay timeline evidence is required."
        )
    result: list[dict[str, Any]] = []
    for index, raw in enumerate(rows[:1000], start=1):
        item = dict(raw or {})
        sequence = int(item.get("sequence") or 0)
        status = str(item.get("status") or "").strip()
        if sequence != index:
            raise ReplayEvidenceError(
                "invalid_replay_sequence", "Replay timeline sequence must be contiguous from 1."
            )
        if status not in TIMELINE_STATUSES:
            raise ReplayEvidenceError(
                "invalid_replay_timeline_status", f"Invalid status: {status}."
            )
        phase = str(item.get("phase") or "").strip()
        summary = str(item.get("summary") or "").strip()
        created_at = str(item.get("created_at") or "").strip()
        if not phase or not summary or not created_at:
            raise ReplayEvidenceError(
                "invalid_replay_timeline_item",
                "Replay timeline items need phase, summary, and time.",
            )
        result.append(
            {
                "sequence": sequence,
                "phase": phase[:200],
                "status": status,
                "summary": summary[:4000],
                "created_at": created_at,
            }
        )
    return result


def _checks(value: Any) -> list[dict[str, Any]]:
    rows = list(value or [])
    result: list[dict[str, Any]] = []
    for raw in rows[:1000]:
        item = dict(raw or {})
        check_type = str(item.get("type") or "").strip()
        status = str(item.get("status") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if check_type not in CHECK_TYPES or status not in CHECK_STATUSES or not summary:
            raise ReplayEvidenceError(
                "invalid_replay_check",
                "Replay checks require a supported type, status, and summary.",
            )
        result.append({"type": check_type, "status": status, "summary": summary[:4000]})
    return result


def _evidence_refs(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for raw in list(value or [])[:200]:
        item = dict(raw or {})
        evidence_id = str(item.get("evidence_id") or "").strip()
        summary = str(item.get("summary") or "").strip()
        if not evidence_id or not summary:
            continue
        result.append(
            {
                "evidence_id": evidence_id[:300],
                "source_type": str(item.get("source_type") or "report")[:80],
                "source_id": str(item.get("source_id") or "").strip()[:300] or None,
                "summary": summary[:4000],
                "captured_at": item.get("captured_at"),
                "redacted": True,
                "href": str(item.get("href") or "").strip()[:2000] or None,
            }
        )
    return result
