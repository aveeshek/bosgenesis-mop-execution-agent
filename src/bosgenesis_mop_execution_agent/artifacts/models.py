"""Artifact bundle models."""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field

from bosgenesis_mop_execution_agent.models.base import StrictBaseModel
from bosgenesis_mop_execution_agent.plans.models import MachineExecutionPlan


class BundleSourceType(StrEnum):
    """Supported bundle source modes from the OpenAPI contract."""

    LOCAL_PATH = "local_path"
    UPLOADED_ZIP = "uploaded_zip"
    MOP_CREATION_RUN = "mop_creation_run"
    ARTIFACT_MANIFEST = "artifact_manifest"
    OBJECT_STORE = "object_store"


class BundleSource(StrictBaseModel):
    """Input source reference for a bundle."""

    type: BundleSourceType
    value: str


class BundleValidationFinding(StrictBaseModel):
    """Bundle validation finding."""

    severity: str
    code: str
    message: str
    path: str | None = None


class LoadedManifest(StrictBaseModel):
    """Parsed Kubernetes manifest document."""

    path: str
    document_index: int
    api_version: str
    kind: str
    name: str
    namespace: str | None = None
    scope: str = "namespaced"
    content: dict[str, Any]


class LoadedValuesFile(StrictBaseModel):
    """Parsed Helm values file."""

    path: str
    content: dict[str, Any]
    redaction_status: str = "clean"


class ArtifactBundle(StrictBaseModel):
    """Loaded and validated bundle."""

    root_path: Path
    source: BundleSource
    target_namespace: str
    machine_plan: MachineExecutionPlan
    human_mop_markdown: str | None = None
    installation_notes_markdown: str | None = None
    artifact_json: dict[str, Any] | None = None
    artifact_index_json: dict[str, Any] | None = None
    artifact_index_root_path: Path | None = None
    response_json: dict[str, Any] | None = None
    manifests: list[LoadedManifest] = Field(default_factory=list)
    values_files: list[LoadedValuesFile] = Field(default_factory=list)
    findings: list[BundleValidationFinding] = Field(default_factory=list)
