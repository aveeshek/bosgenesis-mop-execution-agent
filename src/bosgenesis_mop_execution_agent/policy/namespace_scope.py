"""Namespace and Kubernetes scope policy guards."""

from __future__ import annotations

from typing import Any

from bosgenesis_mop_execution_agent.models import PolicyBlock, PolicySeverity, ResourceRef

CLUSTER_SCOPED_KINDS = {
    "Namespace",
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "StorageClass",
    "PersistentVolume",
}


def namespace_scope_blocks(
    *,
    target_namespace: str,
    resource_refs: list[ResourceRef],
    manifests: list[dict[str, Any]],
) -> list[PolicyBlock]:
    """Reject cluster-scoped and out-of-namespace resources."""
    blocks: list[PolicyBlock] = []
    for resource in resource_refs:
        blocks.extend(_resource_scope_blocks(resource, target_namespace))
    for manifest in manifests:
        blocks.extend(_manifest_scope_blocks(manifest, target_namespace))
    return blocks


def _resource_scope_blocks(resource: ResourceRef, target_namespace: str) -> list[PolicyBlock]:
    blocks: list[PolicyBlock] = []
    if resource.kind in CLUSTER_SCOPED_KINDS:
        blocks.append(_block("CLUSTER_SCOPED_RESOURCE_BLOCKED", resource.kind or "unknown"))
    if resource.namespace not in (None, target_namespace):
        blocks.append(
            _block(
                "RESOURCE_NAMESPACE_OUT_OF_SCOPE",
                f"{resource.kind or 'resource'}/{resource.name or 'unknown'} targets "
                f"{resource.namespace}",
            )
        )
    return blocks


def _manifest_scope_blocks(manifest: dict[str, Any], target_namespace: str) -> list[PolicyBlock]:
    kind = manifest.get("kind")
    metadata = manifest.get("metadata")
    namespace = metadata.get("namespace") if isinstance(metadata, dict) else None
    blocks: list[PolicyBlock] = []
    if kind in CLUSTER_SCOPED_KINDS:
        blocks.append(_block("CLUSTER_SCOPED_RESOURCE_BLOCKED", str(kind)))
    if namespace not in (None, target_namespace):
        blocks.append(_block("RESOURCE_NAMESPACE_OUT_OF_SCOPE", str(namespace)))
    return blocks


def _block(code: str, detail: str) -> PolicyBlock:
    return PolicyBlock(
        code=code,
        message=detail,
        severity=PolicySeverity.BLOCK,
        guardrail="namespace_scope",
    )
