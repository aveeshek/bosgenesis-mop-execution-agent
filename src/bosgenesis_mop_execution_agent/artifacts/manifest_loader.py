"""Kubernetes manifest loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from bosgenesis_mop_execution_agent.artifacts.models import LoadedManifest

NAMESPACED_KINDS = {
    "ConfigMap",
    "Secret",
    "Service",
    "Deployment",
    "StatefulSet",
    "DaemonSet",
    "Job",
    "CronJob",
    "PersistentVolumeClaim",
    "Ingress",
    "Role",
    "RoleBinding",
    "ServiceAccount",
}

CLUSTER_SCOPED_KINDS = {
    "Namespace",
    "ClusterRole",
    "ClusterRoleBinding",
    "CustomResourceDefinition",
    "StorageClass",
    "PersistentVolume",
}


class ManifestValidationError(ValueError):
    """Raised when a manifest is invalid or unsafe."""


def load_manifest_file(
    root: Path,
    relative_path: str,
    target_namespace: str,
) -> list[LoadedManifest]:
    """Load and validate a YAML manifest file."""
    path = _resolve_manifest_path(root, relative_path)
    if not path.exists():
        raise ManifestValidationError(f"manifest_missing:{relative_path}")
    try:
        documents = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    except yaml.YAMLError as exc:
        raise ManifestValidationError(f"manifest_yaml_invalid:{relative_path}:{exc}") from exc

    manifests: list[LoadedManifest] = []
    for index, document in enumerate(documents):
        if document is None:
            continue
        if not isinstance(document, dict):
            raise ManifestValidationError(f"manifest_document_not_mapping:{relative_path}:{index}")
        manifests.append(_validate_manifest(document, relative_path, index, target_namespace))
    if not manifests:
        raise ManifestValidationError(f"manifest_empty:{relative_path}")
    return manifests


def _validate_manifest(
    document: dict[str, Any],
    relative_path: str,
    document_index: int,
    target_namespace: str,
) -> LoadedManifest:
    api_version = document.get("apiVersion")
    kind = document.get("kind")
    metadata = document.get("metadata")
    if not isinstance(api_version, str) or not api_version:
        msg = f"manifest_missing_apiVersion:{relative_path}:{document_index}"
        raise ManifestValidationError(msg)
    if not isinstance(kind, str) or not kind:
        raise ManifestValidationError(f"manifest_missing_kind:{relative_path}:{document_index}")
    if not isinstance(metadata, dict):
        raise ManifestValidationError(f"manifest_missing_metadata:{relative_path}:{document_index}")
    name = metadata.get("name")
    if not isinstance(name, str) or not name:
        msg = f"manifest_missing_metadata_name:{relative_path}:{document_index}"
        raise ManifestValidationError(msg)
    if kind in CLUSTER_SCOPED_KINDS:
        raise ManifestValidationError(f"cluster_scoped_resource_blocked:{relative_path}:{kind}")
    namespace = metadata.get("namespace")
    if namespace is not None and namespace != target_namespace:
        raise ManifestValidationError(f"manifest_namespace_mismatch:{relative_path}:{namespace}")
    if kind == "Secret" and (document.get("data") or document.get("stringData")):
        raise ManifestValidationError(f"secret_value_detected:{relative_path}:{name}")
    if kind not in NAMESPACED_KINDS:
        raise ManifestValidationError(f"manifest_kind_unsupported:{relative_path}:{kind}")
    return LoadedManifest(
        path=relative_path,
        document_index=document_index,
        api_version=api_version,
        kind=kind,
        name=name,
        namespace=namespace,
        scope="namespaced",
        content=document,
    )


def _resolve_manifest_path(root: Path, relative_path: str) -> Path:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ManifestValidationError(f"manifest_path_unsafe:{relative_path}")
    return root / path
