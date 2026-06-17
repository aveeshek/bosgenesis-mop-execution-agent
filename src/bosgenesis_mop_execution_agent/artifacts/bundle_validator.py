"""Artifact bundle validation orchestration."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bosgenesis_mop_execution_agent.artifacts.bundle_reader import (
    read_json_file,
    resolve_bundle_source,
)
from bosgenesis_mop_execution_agent.artifacts.manifest_loader import load_manifest_file
from bosgenesis_mop_execution_agent.artifacts.models import ArtifactBundle, BundleSource
from bosgenesis_mop_execution_agent.artifacts.values_loader import load_values_file
from bosgenesis_mop_execution_agent.plans.machine_plan_parser import (
    parse_embedded_machine_plan,
    parse_machine_plan,
)


class BundleValidationError(ValueError):
    """Raised when a bundle fails closed validation."""


def load_and_validate_bundle(source: BundleSource, target_namespace: str) -> ArtifactBundle:
    """Resolve, load, and validate a MoP output bundle."""
    root = resolve_bundle_source(source)
    plan_path = root / "machine_execution_plan.yaml"
    installation_notes = _read_optional_text(root, "machine-readable-installation-notes.md")
    if plan_path.exists():
        plan = parse_machine_plan(plan_path)
    elif installation_notes:
        embedded_plan = parse_embedded_machine_plan(installation_notes)
        if embedded_plan is None:
            raise BundleValidationError("machine_execution_plan_missing")
        plan = embedded_plan
    else:
        raise BundleValidationError("machine_execution_plan_missing")

    if plan.target_namespace != target_namespace:
        msg = f"machine_plan_target_namespace_mismatch:{plan.target_namespace}"
        raise BundleValidationError(msg)

    human_mop = _read_optional_text(root, "human-readable-mop.md")
    artifact_json = _read_optional_json(root, "artifact.json")
    artifact_index_json = _read_optional_json(root, "artifact-index.json")
    response_json = _read_optional_json(root, "response.json")

    manifests = []
    for manifest_ref in sorted(plan.manifest_refs):
        manifests.extend(load_manifest_file(root, manifest_ref, target_namespace))

    values_files = [load_values_file(root, values_ref) for values_ref in sorted(plan.values_refs)]

    _validate_artifact_index_refs(root, artifact_index_json)

    return ArtifactBundle(
        root_path=root,
        source=source,
        target_namespace=target_namespace,
        machine_plan=plan,
        human_mop_markdown=human_mop,
        installation_notes_markdown=installation_notes,
        artifact_json=artifact_json,
        artifact_index_json=artifact_index_json,
        response_json=response_json,
        manifests=manifests,
        values_files=values_files,
    )


def _read_optional_text(root: Path, filename: str) -> str | None:
    path = root / filename
    if not path.exists():
        return None
    return path.read_text(encoding="utf-8")


def _read_optional_json(root: Path, filename: str) -> dict[str, Any] | None:
    path = root / filename
    if not path.exists():
        return None
    return read_json_file(path)


def _validate_artifact_index_refs(root: Path, artifact_index: dict[str, Any] | None) -> None:
    if artifact_index is None:
        return
    files = artifact_index.get("files")
    if not isinstance(files, list):
        raise BundleValidationError("artifact_index_files_not_list")
    for entry in files:
        if not isinstance(entry, dict):
            raise BundleValidationError("artifact_index_entry_not_object")
        path = entry.get("path")
        if isinstance(path, str) and _is_unsafe_relative_path(path):
            raise BundleValidationError(f"artifact_index_file_path_unsafe:{path}")
        if isinstance(path, str) and not (root / path).exists():
            raise BundleValidationError(f"artifact_index_file_missing:{path}")


def _is_unsafe_relative_path(path: str) -> bool:
    relative_path = Path(path)
    return relative_path.is_absolute() or ".." in relative_path.parts
