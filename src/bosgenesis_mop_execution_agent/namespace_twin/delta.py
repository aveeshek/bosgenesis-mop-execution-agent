"""Typed release-delta calculation from canonical Kubernetes facts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

from bosgenesis_mop_execution_agent.namespace_twin.canonicalization import (
    canonicalize_kubernetes_object,
    resource_identity,
)

CLUSTER_SCOPED_KINDS = {
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "Namespace",
    "Node",
    "PersistentVolume",
    "StorageClass",
}
IMMUTABLE_PATHS: dict[str, tuple[tuple[str, ...], ...]] = {
    "Deployment": (("spec", "selector"),),
    "StatefulSet": (("spec", "selector"), ("spec", "serviceName")),
    "Service": (
        ("spec", "clusterIP"),
        ("spec", "clusterIPs"),
        ("spec", "ipFamilies"),
        ("spec", "ipFamilyPolicy"),
    ),
    "PersistentVolumeClaim": (
        ("spec", "storageClassName"),
        ("spec", "volumeMode"),
        ("spec", "volumeName"),
        ("spec", "dataSource"),
    ),
}


@dataclass(frozen=True)
class LiveSnapshot:
    resources: list[dict[str, Any]] = field(default_factory=list)
    available: bool = False
    complete_kinds: set[str] = field(default_factory=set)
    evidence_refs: list[str] = field(default_factory=list)
    warning: str | None = None
    helm_inventory_available: bool = False
    installed_helm_releases: set[str] = field(default_factory=set)
    ignored_helm_releases: set[str] = field(default_factory=set)
    ignored_helm_prefixes: tuple[str, ...] = ()


def calculate_release_delta(
    planned_resources: list[dict[str, Any]],
    snapshot: LiveSnapshot,
    *,
    planned_helm_installs: set[str] | None = None,
    target_namespace: str,
    explicit_deletes: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Calculate planned changes; omission is deliberately never deletion."""
    live_map = {
        resource_identity(item, target_namespace): canonicalize_kubernetes_object(item)
        for item in snapshot.resources
        if isinstance(item, dict)
    }
    rows: list[dict[str, Any]] = []
    planned_paths: dict[str, dict[str, Any]] = {}
    for item in planned_resources:
        payload = item.get("payload_redacted")
        payload = payload if isinstance(payload, dict) else {}
        manifest = payload.get("manifest")
        manifest = manifest if isinstance(manifest, dict) else {}
        if _ignored_helm_resource(manifest, snapshot.ignored_helm_prefixes):
            continue
        if _uninstalled_helm_resource(
            manifest,
            inventory_available=snapshot.helm_inventory_available,
            planned_install_releases=planned_helm_installs or set(),
            installed_releases=snapshot.installed_helm_releases,
        ):
            continue
        identity = str(item.get("stable_identity") or resource_identity(manifest, target_namespace))
        planned = canonicalize_kubernetes_object(manifest)
        planned_paths[str(payload.get("path") or "")] = item
        kind = str(item.get("kind") or planned.get("kind") or "Unknown")
        current = live_map.get(identity)
        evidence_refs = [str(payload.get("path") or identity), *snapshot.evidence_refs]
        if current is None:
            if snapshot.available and kind in snapshot.complete_kinds:
                action = "create"
                reason = (
                    "The resource is declared by the bundle and absent from a complete "
                    "live-kind snapshot."
                )
                risk = _risk_for(kind, planned, action, [])
            else:
                action = "unknown"
                reason = "Live evidence is unavailable or incomplete for this resource kind."
                risk = "unknown"
            changes: list[dict[str, Any]] = []
        elif not _is_full_manifest(current):
            action = "unknown"
            reason = "The live inspector returned summary evidence, not a canonical manifest."
            risk = "unknown"
            changes = []
        else:
            changes = _field_changes(current, planned)
            if not changes:
                action = "no_op"
                reason = "Canonical live and planned intent are equivalent."
            elif _immutable_conflict(kind, changes):
                action = "immutable_conflict"
                reason = "The planned change modifies an immutable Kubernetes field."
            else:
                action = "update"
                reason = _change_reason(changes)
            risk = _risk_for(kind, planned, action, changes)
        rows.append(
            _row(
                identity=identity,
                api_version=str(item.get("api_version") or planned.get("apiVersion") or ""),
                kind=kind,
                namespace=item.get("namespace") or target_namespace,
                name=str(
                    item.get("name") or ((planned.get("metadata") or {}).get("name")) or "unknown"
                ),
                action=action,
                risk=risk,
                reason=reason,
                current=current,
                planned=planned,
                changes=changes,
                evidence_refs=evidence_refs,
                helm_inventory_available=snapshot.helm_inventory_available,
                installed_helm_releases=snapshot.installed_helm_releases,
            )
        )

    for deletion in explicit_deletes or []:
        for manifest_ref in deletion.get("manifest_refs") or []:
            item = planned_paths.get(str(manifest_ref))
            if not item:
                continue
            identity = str(item["stable_identity"])
            current = live_map.get(identity)
            if current is None:
                continue
            rows.append(
                _row(
                    identity=identity,
                    api_version=str(item.get("api_version") or ""),
                    kind=str(item.get("kind") or "Unknown"),
                    namespace=item.get("namespace") or target_namespace,
                    name=str(item.get("name") or "unknown"),
                    action="explicit_delete",
                    risk="high",
                    reason=(
                        "The machine plan contains an explicit delete step for this live resource."
                    ),
                    current=current,
                    planned=None,
                    changes=[],
                    evidence_refs=[str(manifest_ref)],
                    helm_inventory_available=snapshot.helm_inventory_available,
                    installed_helm_releases=snapshot.installed_helm_releases,
                )
            )
    return _deduplicate(rows)


def _row(
    *,
    identity: str,
    api_version: str,
    kind: str,
    namespace: str | None,
    name: str,
    action: str,
    risk: str,
    reason: str,
    current: dict[str, Any] | None,
    planned: dict[str, Any] | None,
    changes: list[dict[str, Any]],
    evidence_refs: list[str],
    helm_inventory_available: bool,
    installed_helm_releases: set[str],
) -> dict[str, Any]:
    diff = {"current": current, "planned": planned, "field_changes": changes}
    helm_release = _helm_release(planned or current or {})
    if (
        helm_inventory_available
        and action != "create"
        and helm_release not in installed_helm_releases
    ):
        helm_release = None
    return {
        "change_id": f"delta_{uuid4().hex}",
        "resource_identity": identity,
        "api_version": api_version or None,
        "kind": kind,
        "namespace": namespace,
        "name": name,
        "helm_release": helm_release,
        "action": action,
        "current_summary": _summary(current),
        "planned_summary": _summary(planned),
        "risk": risk,
        "reason": reason,
        "canonical_diff": json.dumps(diff, sort_keys=True, indent=2, default=str)[:50000],
        "evidence_refs": list(dict.fromkeys(ref for ref in evidence_refs if ref)),
        "redacted": True,
    }


def _field_changes(current: Any, planned: Any, path: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    if isinstance(current, dict) and isinstance(planned, dict):
        rows: list[dict[str, Any]] = []
        for key in sorted(set(current) | set(planned)):
            rows.extend(_field_changes(current.get(key), planned.get(key), (*path, str(key))))
        return rows
    if current != planned:
        return [{"path": ".".join(path), "current": current, "planned": planned}]
    return []


def _immutable_conflict(kind: str, changes: list[dict[str, Any]]) -> bool:
    paths = IMMUTABLE_PATHS.get(kind, ())
    return any(
        tuple(str(change.get("path") or "").split("."))[: len(path)] == path
        for change in changes
        for path in paths
    )


def _risk_for(
    kind: str, planned: dict[str, Any], action: str, changes: list[dict[str, Any]]
) -> str:
    if action == "unknown":
        return "unknown"
    if action == "immutable_conflict" or kind in CLUSTER_SCOPED_KINDS or kind == "Secret":
        return "critical"
    if kind in {"PersistentVolumeClaim", "Role", "RoleBinding", "ServiceAccount"}:
        return "high"
    pod_spec = _pod_spec(planned)
    containers = [
        *(pod_spec.get("containers") or []),
        *(pod_spec.get("initContainers") or []),
    ]
    if any(
        isinstance(container, dict)
        and isinstance(container.get("securityContext"), dict)
        and (
            container["securityContext"].get("privileged") is True
            or container["securityContext"].get("allowPrivilegeEscalation") is True
        )
        for container in containers
    ):
        return "critical"
    changed_paths = {str(item.get("path") or "") for item in changes}
    if any(
        token in path
        for path in changed_paths
        for token in (
            "selector",
            "hostNetwork",
            "hostPID",
            "serviceAccountName",
            "persistentVolumeClaim",
        )
    ):
        return "high"
    if kind in {"Ingress", "Service", "NetworkPolicy"} or action == "explicit_delete":
        return "high" if action == "explicit_delete" else "medium"
    return "low"


def _pod_spec(resource: dict[str, Any]) -> dict[str, Any]:
    spec = resource.get("spec") if isinstance(resource.get("spec"), dict) else {}
    template = spec.get("template") if isinstance(spec.get("template"), dict) else {}
    return template.get("spec") if isinstance(template.get("spec"), dict) else spec


def _change_reason(changes: list[dict[str, Any]]) -> str:
    paths = [str(item.get("path") or "") for item in changes[:4]]
    suffix = " and more" if len(changes) > 4 else ""
    return "Canonical intent differs at " + ", ".join(paths) + suffix + "."


def _summary(resource: dict[str, Any] | None) -> str | None:
    if resource is None:
        return None
    spec = resource.get("spec") if isinstance(resource.get("spec"), dict) else {}
    parts = [f"kind={resource.get('kind') or 'unknown'}"]
    if "replicas" in spec:
        parts.append(f"replicas={spec['replicas']}")
    containers = _pod_spec(resource).get("containers") or []
    images = [
        item.get("image") for item in containers if isinstance(item, dict) and item.get("image")
    ]
    if images:
        parts.append("images=" + ",".join(images[:4]))
    return "; ".join(parts)


def _helm_release(resource: dict[str, Any]) -> str | None:
    metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
    annotations = (
        metadata.get("annotations") if isinstance(metadata.get("annotations"), dict) else {}
    )
    labels = metadata.get("labels") if isinstance(metadata.get("labels"), dict) else {}
    value = (
        annotations.get("meta.helm.sh/release-name")
        or labels.get("app.kubernetes.io/instance")
        or labels.get("release")
    )
    return str(value).strip() if value else None


def _ignored_helm_resource(resource: dict[str, Any], prefixes: tuple[str, ...]) -> bool:
    release = (_helm_release(resource) or "").casefold()
    return bool(release and any(release.startswith(prefix.casefold()) for prefix in prefixes))


def _uninstalled_helm_resource(
    resource: dict[str, Any],
    *,
    inventory_available: bool,
    installed_releases: set[str],
    planned_install_releases: set[str],
) -> bool:
    if not inventory_available:
        return False
    release = (_helm_release(resource) or "").casefold()
    installed = {item.casefold() for item in installed_releases}
    planned_installs = {item.casefold() for item in planned_install_releases}
    return bool(release and release not in installed and release not in planned_installs)


def _is_full_manifest(resource: dict[str, Any]) -> bool:
    return bool(resource.get("apiVersion") and resource.get("kind") and resource.get("metadata"))


def _deduplicate(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row["resource_identity"])
        existing = selected.get(key)
        if existing and existing.get("action") == "explicit_delete":
            continue
        if row.get("action") == "explicit_delete" or existing is None:
            selected[key] = row
        else:
            selected[key] = row
    return sorted(
        selected.values(),
        key=lambda row: (row["kind"], row["namespace"] or "", row["name"], row["action"]),
    )
