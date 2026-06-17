"""Helm values file loading and safety checks."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from bosgenesis_mop_execution_agent.artifacts.models import LoadedValuesFile

SENSITIVE_KEY_PATTERN = re.compile(
    r"(password|passwd|pwd|token|api[_-]?key|secret|client_secret|private[_-]?key)",
    re.IGNORECASE,
)


class ValuesValidationError(ValueError):
    """Raised when Helm values appear unsafe."""


def load_values_file(root: Path, relative_path: str) -> LoadedValuesFile:
    """Load and validate a Helm values file."""
    path = _resolve_values_path(root, relative_path)
    if not path.exists():
        raise ValuesValidationError(f"values_file_missing:{relative_path}")
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ValuesValidationError(f"values_yaml_invalid:{relative_path}:{exc}") from exc
    if not isinstance(loaded, dict):
        raise ValuesValidationError(f"values_not_mapping:{relative_path}")
    sensitive_paths = list(_find_sensitive_paths(loaded))
    if sensitive_paths:
        joined = ",".join(sensitive_paths)
        raise ValuesValidationError(f"values_secret_like_key_detected:{relative_path}:{joined}")
    return LoadedValuesFile(path=relative_path, content=loaded)


def _resolve_values_path(root: Path, relative_path: str) -> Path:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValuesValidationError(f"values_file_path_unsafe:{relative_path}")
    return root / path


def _find_sensitive_paths(value: Any, prefix: str = "") -> list[str]:
    findings: list[str] = []
    if isinstance(value, dict):
        for key, nested in value.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            if SENSITIVE_KEY_PATTERN.search(key_text) and nested not in (None, "", []):
                findings.append(path)
            findings.extend(_find_sensitive_paths(nested, path))
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            findings.extend(_find_sensitive_paths(nested, f"{prefix}[{index}]"))
    return findings
