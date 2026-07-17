"""Kubernetes-aware canonicalization for deterministic release deltas."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from decimal import Decimal, InvalidOperation
from typing import Any

RUNTIME_METADATA_FIELDS = {
    "creationTimestamp",
    "deletionGracePeriodSeconds",
    "deletionTimestamp",
    "generation",
    "managedFields",
    "resourceVersion",
    "selfLink",
    "uid",
}

ORDER_INSENSITIVE_LIST_KEYS: dict[str, tuple[str, ...]] = {
    "containers": ("name",),
    "env": ("name",),
    "finalizers": (),
    "imagePullSecrets": ("name",),
    "initContainers": ("name",),
    "ports": ("name", "containerPort", "port"),
    "rules": (),
    "subjects": ("kind", "namespace", "name"),
    "tolerations": ("key", "operator", "effect", "value"),
    "volumeMounts": ("name", "mountPath"),
    "volumes": ("name",),
}

QUANTITY_PATH_PARTS = {"limits", "requests", "capacity"}
QUANTITY_SUFFIXES = {
    "n": Decimal("0.000000001"),
    "u": Decimal("0.000001"),
    "m": Decimal("0.001"),
    "k": Decimal("1000"),
    "K": Decimal("1000"),
    "M": Decimal("1000000"),
    "G": Decimal("1000000000"),
    "T": Decimal("1000000000000"),
    "P": Decimal("1000000000000000"),
    "E": Decimal("1000000000000000000"),
    "Ki": Decimal(1024),
    "Mi": Decimal(1024**2),
    "Gi": Decimal(1024**3),
    "Ti": Decimal(1024**4),
    "Pi": Decimal(1024**5),
    "Ei": Decimal(1024**6),
}
QUANTITY_PATTERN = re.compile(r"^([+-]?(?:\d+(?:\.\d*)?|\.\d+))([a-zA-Z]{0,2})$")


def resource_identity(resource: dict[str, Any], default_namespace: str | None = None) -> str:
    """Return the stable GVK/namespace/name identity used by twin facts."""
    metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
    namespace = metadata.get("namespace") or default_namespace or ""
    return ":".join(
        (
            str(resource.get("apiVersion") or "unknown"),
            str(resource.get("kind") or "Unknown"),
            str(namespace),
            str(metadata.get("name") or "unknown"),
        )
    )


def canonicalize_kubernetes_object(resource: dict[str, Any]) -> dict[str, Any]:
    """Remove runtime noise while preserving declarative intent and provenance."""
    document = deepcopy(resource)
    document.pop("status", None)
    metadata = document.get("metadata")
    if isinstance(metadata, dict):
        for field in RUNTIME_METADATA_FIELDS:
            metadata.pop(field, None)
        if not metadata.get("annotations"):
            metadata.pop("annotations", None)
        if not metadata.get("labels"):
            metadata.pop("labels", None)

    if str(document.get("kind") or "").lower() == "secret":
        for field in ("data", "stringData"):
            values = document.get(field)
            if isinstance(values, dict):
                document[field] = {str(key): "<redacted>" for key in sorted(values)}

    return _canonical_value(document, ())


def canonical_json(resource: dict[str, Any]) -> str:
    return json.dumps(
        canonicalize_kubernetes_object(resource),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def _canonical_value(value: Any, path: tuple[str, ...]) -> Any:
    if isinstance(value, dict):
        return {str(key): _canonical_value(value[key], (*path, str(key))) for key in sorted(value)}
    if isinstance(value, list):
        items = [_canonical_value(item, path) for item in value]
        list_key = path[-1] if path else ""
        if list_key in ORDER_INSENSITIVE_LIST_KEYS:
            keys = ORDER_INSENSITIVE_LIST_KEYS[list_key]
            return sorted(items, key=lambda item: _list_sort_key(item, keys))
        return items
    if isinstance(value, str) and _is_quantity_path(path):
        return _normalize_quantity(value)
    return value


def _list_sort_key(value: Any, keys: tuple[str, ...]) -> str:
    if isinstance(value, dict) and keys:
        selected = [value.get(key) for key in keys]
        if any(item is not None for item in selected):
            return json.dumps(selected, sort_keys=True, default=str)
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _is_quantity_path(path: tuple[str, ...]) -> bool:
    if not path:
        return False
    if any(part in QUANTITY_PATH_PARTS for part in path):
        return True
    return path[-2:] == ("resources", "storage") or path[-1] == "storage"


def _normalize_quantity(value: str) -> str:
    match = QUANTITY_PATTERN.match(value.strip())
    if not match:
        return value
    number, suffix = match.groups()
    if suffix not in QUANTITY_SUFFIXES and suffix:
        return value
    try:
        normalized = Decimal(number) * QUANTITY_SUFFIXES.get(suffix, Decimal(1))
    except InvalidOperation:
        return value
    text = format(normalized.normalize(), "f")
    return "0" if text in {"-0", ""} else text
