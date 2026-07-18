"""Deterministic, read-only Namespace Drift Twin assessment."""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

from bosgenesis_mop_execution_agent.namespace_twin.canonicalization import (
    canonicalize_kubernetes_object,
    resource_identity,
)
from bosgenesis_mop_execution_agent.namespace_twin.delta import LiveSnapshot

DRIFT_RULES_VERSION = "namespace-twin-drift-1.0.0"
DEFAULT_FRESHNESS_THRESHOLD_SECONDS = 900

_CLUSTER_SCOPED_KINDS = {
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "Namespace",
    "Node",
    "PersistentVolume",
    "StorageClass",
    "MutatingWebhookConfiguration",
    "ValidatingWebhookConfiguration",
}
_WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob", "Pod"}
_SAFETY_KEYS = {
    "hostNetwork",
    "hostPID",
    "hostIPC",
    "hostPath",
    "privileged",
    "runAsUser",
    "runAsNonRoot",
    "serviceAccountName",
    "allowPrivilegeEscalation",
    "capabilities",
}
_LEVEL = {"none": 0, "minor": 1, "major": 2, "critical": 3, "unknown": 4}


def capture_baseline(
    snapshot: LiveSnapshot,
    *,
    captured_at: datetime | str,
    target_namespace: str,
    freshness_threshold_seconds: int = DEFAULT_FRESHNESS_THRESHOLD_SECONDS,
) -> dict[str, Any]:
    """Persist a mandatory, redacted baseline representation for later comparison."""
    captured = _timestamp(captured_at)
    resources = _resource_map(snapshot.resources, target_namespace)
    digest = _snapshot_hash(resources)
    return {
        "captured_at": captured,
        "hash": digest,
        "available": bool(snapshot.available),
        "complete_kinds": sorted(snapshot.complete_kinds),
        "resource_count": len(resources),
        "resources": resources,
        "evidence_refs": list(snapshot.evidence_refs),
        "warning": snapshot.warning,
        "freshness_threshold_seconds": max(1, int(freshness_threshold_seconds)),
        "redacted": True,
    }


def initial_drift_assessment(baseline: dict[str, Any]) -> dict[str, Any]:
    status = "none" if baseline.get("available") else "unknown"
    return _assessment(
        baseline=baseline,
        current=deepcopy(baseline),
        changes=[],
        status=status,
        freshness_status="fresh",
        freshness_age_seconds=0,
        decision_invalidated=False,
    )


def assess_drift(
    baseline: dict[str, Any],
    current_snapshot: LiveSnapshot,
    *,
    captured_at: datetime | str,
    target_namespace: str,
) -> dict[str, Any]:
    current = capture_baseline(
        current_snapshot,
        captured_at=captured_at,
        target_namespace=target_namespace,
        freshness_threshold_seconds=int(
            baseline.get("freshness_threshold_seconds") or DEFAULT_FRESHNESS_THRESHOLD_SECONDS
        ),
    )
    age = max(
        0,
        int(
            (
                _parse_time(current["captured_at"]) - _parse_time(baseline["captured_at"])
            ).total_seconds()
        ),
    )
    freshness = (
        "stale"
        if age
        > int(baseline.get("freshness_threshold_seconds") or DEFAULT_FRESHNESS_THRESHOLD_SECONDS)
        else "fresh"
    )
    if not baseline.get("available") or not current.get("available"):
        return _assessment(
            baseline=baseline,
            current=current,
            changes=[],
            status="unknown",
            freshness_status=freshness,
            freshness_age_seconds=age,
            decision_invalidated=False,
        )

    before = dict(baseline.get("resources") or {})
    after = dict(current.get("resources") or {})
    changes: list[dict[str, Any]] = []
    for identity in sorted(set(before) | set(after)):
        old = before.get(identity)
        new = after.get(identity)
        if old is None:
            changes.append(
                _change(identity, None, new, "created", target_namespace, current["captured_at"])
            )
        elif new is None:
            changes.append(
                _change(identity, old, None, "deleted", target_namespace, current["captured_at"])
            )
        elif old.get("canonical_hash") != new.get("canonical_hash"):
            changes.append(
                _change(identity, old, new, "updated", target_namespace, current["captured_at"])
            )
        elif old.get("health_hash") != new.get("health_hash"):
            changes.append(
                _change(
                    identity,
                    old,
                    new,
                    "status_only",
                    target_namespace,
                    current["captured_at"],
                )
            )

    status = "none"
    for item in changes:
        if _LEVEL[item["classification"]] > _LEVEL[status]:
            status = item["classification"]
    if freshness == "stale" and status == "none":
        status = "minor"
    material = status in {"major", "critical"}
    return _assessment(
        baseline=baseline,
        current=current,
        changes=changes,
        status=status,
        freshness_status=freshness,
        freshness_age_seconds=age,
        decision_invalidated=material,
    )


def _assessment(
    *,
    baseline: dict[str, Any],
    current: dict[str, Any],
    changes: list[dict[str, Any]],
    status: str,
    freshness_status: str,
    freshness_age_seconds: int,
    decision_invalidated: bool,
) -> dict[str, Any]:
    counts = {key: 0 for key in ("minor", "major", "critical", "unknown")}
    for item in changes:
        counts[item["classification"]] = counts.get(item["classification"], 0) + 1
    helm = any(item.get("axes", {}).get("helm_revision") for item in changes)
    manual = [
        item["resource_identity"] for item in changes if item.get("axes", {}).get("manual_patch")
    ]
    health = [item["summary"] for item in changes if item["change_type"] == "status_only"]
    material = status in {"major", "critical"}
    execution_disabled = material or status == "unknown" or freshness_status == "stale"
    return {
        "status": status,
        "snapshot": {key: baseline.get(key) for key in ("captured_at", "hash")},
        "baseline": {
            key: baseline.get(key)
            for key in (
                "captured_at",
                "hash",
                "available",
                "resource_count",
                "complete_kinds",
                "warning",
            )
        },
        "current_capture": {
            key: current.get(key)
            for key in (
                "captured_at",
                "hash",
                "available",
                "resource_count",
                "complete_kinds",
                "warning",
            )
        },
        "freshness_threshold_seconds": int(
            baseline.get("freshness_threshold_seconds") or DEFAULT_FRESHNESS_THRESHOLD_SECONDS
        ),
        "freshness": {"status": freshness_status, "age_seconds": freshness_age_seconds},
        "changes": changes,
        "change_counts": {"total": len(changes), **counts},
        "helm_revision_drift": helm,
        "manual_patch_indicators": manual,
        "health_changes": health,
        "material": material,
        "execution_disabled": execution_disabled,
        "decision_invalidated": decision_invalidated,
        "rules_version": DRIFT_RULES_VERSION,
        "model_authority": False,
        "summary": _summary(status, len(changes), decision_invalidated),
        "evidence_refs": sorted(
            set(
                list(baseline.get("evidence_refs") or []) + list(current.get("evidence_refs") or [])
            )
        ),
    }


def _change(
    identity: str,
    old: dict[str, Any] | None,
    new: dict[str, Any] | None,
    change_type: str,
    target_namespace: str,
    captured_at: str,
) -> dict[str, Any]:
    sample = new or old or {}
    kind = str(sample.get("kind") or "Unknown")
    namespace = str(sample.get("namespace") or "")
    axes = {
        "spec": change_type in {"created", "updated", "deleted"},
        "policy_boundary": kind in _CLUSTER_SCOPED_KINDS
        or namespace not in {"", target_namespace}
        or kind == "Secret",
        "target": namespace not in {"", target_namespace},
        "helm_revision": bool(old and new and old.get("helm_revision") != new.get("helm_revision")),
        "safety_control": bool(old and new and old.get("safety_hash") != new.get("safety_hash"))
        or kind in {"Role", "RoleBinding", "ClusterRole", "ClusterRoleBinding"},
        "manual_patch": change_type in {"created", "updated"}
        and not bool(sample.get("helm_release")),
    }
    if axes["policy_boundary"] or axes["target"] or axes["safety_control"]:
        classification = "critical"
    elif change_type == "deleted" or axes["helm_revision"] or kind in _WORKLOAD_KINDS:
        classification = "major"
    else:
        classification = "minor"
    change_id = hashlib.sha256(f"{identity}:{change_type}".encode()).hexdigest()[:24]
    return {
        "change_id": f"drift_{change_id}",
        "resource_identity": identity,
        "kind": kind,
        "namespace": namespace or None,
        "name": sample.get("name"),
        "change_type": "helm_revision" if axes["helm_revision"] else change_type,
        "classification": classification,
        "summary": f"{kind}/{sample.get('name') or 'unknown'} was {change_type.replace('_', ' ')}.",
        "axes": axes,
        "before_hash": old.get("canonical_hash") if old else None,
        "after_hash": new.get("canonical_hash") if new else None,
        "evidence_refs": [
            {
                "evidence_id": f"evidence_{change_id}",
                "source_type": "live_snapshot",
                "summary": f"Read-only drift comparison for {identity}.",
                "captured_at": captured_at,
                "redacted": True,
            }
        ],
    }


def _resource_map(
    resources: list[dict[str, Any]], target_namespace: str
) -> dict[str, dict[str, Any]]:
    rows: dict[str, dict[str, Any]] = {}
    for resource in resources:
        declarative = deepcopy(resource)
        declarative.pop("summary", None)
        canonical = canonicalize_kubernetes_object(declarative)
        metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
        summary = resource.get("summary") if isinstance(resource.get("summary"), dict) else {}
        identity = resource_identity(resource, target_namespace)
        helm_release = (
            (metadata.get("annotations") or {}).get("meta.helm.sh/release-name")
            or (metadata.get("labels") or {}).get("app.kubernetes.io/instance")
            or summary.get("release")
        )
        helm_revision = (
            (metadata.get("annotations") or {}).get("meta.helm.sh/release-revision")
            or summary.get("revision")
            or summary.get("helm_revision")
        )
        health = _health_signature(resource, summary)
        safety = _extract_safety(canonical)
        rows[identity] = {
            "resource_identity": identity,
            "api_version": resource.get("apiVersion"),
            "kind": resource.get("kind"),
            "namespace": metadata.get("namespace") or target_namespace,
            "name": metadata.get("name"),
            "canonical": canonical,
            "canonical_hash": _hash(canonical),
            "health": health,
            "health_hash": _hash(health),
            "safety_hash": _hash(safety),
            "helm_release": helm_release,
            "helm_revision": str(helm_revision) if helm_revision is not None else None,
            "redacted": True,
        }
    return rows


def _health_signature(resource: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    status = resource.get("status") if isinstance(resource.get("status"), dict) else {}
    return {
        "phase": status.get("phase") or summary.get("phase") or summary.get("status"),
        "ready": summary.get("ready")
        or summary.get("ready_replicas")
        or status.get("readyReplicas"),
        "replicas": summary.get("replicas") or status.get("replicas"),
        "restarts": summary.get("restarts") or summary.get("restart_count"),
    }


def _extract_safety(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, raw in value.items():
            nested = _extract_safety(raw)
            if key in _SAFETY_KEYS:
                result[key] = raw
            elif nested not in ({}, [], None):
                result[key] = nested
        return result
    if isinstance(value, list):
        result_list = []
        for raw in value:
            nested = _extract_safety(raw)
            if nested not in ({}, [], None):
                result_list.append(nested)
        return result_list
    return None


def _snapshot_hash(resources: dict[str, Any]) -> str:
    return _hash(resources)


def _hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _timestamp(value: datetime | str) -> str:
    return _parse_time(value).isoformat()


def _parse_time(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _summary(status: str, count: int, invalidated: bool) -> str:
    if status == "none":
        return "No drift was detected against the persisted namespace baseline."
    if status == "unknown":
        return "Drift could not be determined from complete read-only snapshot evidence."
    suffix = " The prior execution decision is superseded." if invalidated else ""
    return f"Detected {count} changed resource(s); overall drift is {status}.{suffix}"
