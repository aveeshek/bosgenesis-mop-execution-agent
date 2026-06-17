"""Artifact bundle source resolution and file loading."""

from __future__ import annotations

import json
import tempfile
import zipfile
from pathlib import Path
from typing import Any

from bosgenesis_mop_execution_agent.artifacts.models import BundleSource, BundleSourceType


class BundleSourceResolutionError(ValueError):
    """Raised when a bundle source cannot be resolved."""


def resolve_bundle_source(source: BundleSource) -> Path:
    """Resolve a bundle source to a local directory path."""
    if source.type == BundleSourceType.LOCAL_PATH:
        path = Path(source.value)
        if not path.exists() or not path.is_dir():
            raise BundleSourceResolutionError(f"bundle_local_path_missing:{source.value}")
        return path
    if source.type == BundleSourceType.UPLOADED_ZIP:
        archive = Path(source.value)
        if not archive.exists() or not archive.is_file():
            raise BundleSourceResolutionError(f"bundle_archive_missing:{source.value}")
        return _extract_zip_safely(archive)
    if source.type == BundleSourceType.ARTIFACT_MANIFEST:
        manifest_path = Path(source.value)
        if not manifest_path.exists():
            raise BundleSourceResolutionError(f"artifact_manifest_missing:{source.value}")
        manifest = read_json_file(manifest_path)
        root = manifest.get("root_path") or manifest.get("artifact_root")
        if not isinstance(root, str):
            raise BundleSourceResolutionError("artifact_manifest_root_path_missing")
        path = Path(root)
        if not path.exists() or not path.is_dir():
            raise BundleSourceResolutionError(f"artifact_manifest_root_missing:{root}")
        return path
    if source.type in {BundleSourceType.MOP_CREATION_RUN, BundleSourceType.OBJECT_STORE}:
        msg = f"bundle_source_not_locally_resolvable:{source.type.value}"
        raise BundleSourceResolutionError(msg)
    raise BundleSourceResolutionError(f"unsupported_bundle_source:{source.type.value}")


def read_json_file(path: str | Path) -> dict[str, Any]:
    """Read a JSON file as an object."""
    try:
        loaded = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise BundleSourceResolutionError(f"json_file_invalid:{path}:{exc.msg}") from exc
    if not isinstance(loaded, dict):
        raise BundleSourceResolutionError(f"json_file_not_object:{path}")
    return loaded


def _extract_zip_safely(archive: Path) -> Path:
    target = Path(tempfile.mkdtemp(prefix="mop-exec-bundle-"))
    with zipfile.ZipFile(archive) as zip_file:
        for member in zip_file.infolist():
            member_path = Path(member.filename)
            if member_path.is_absolute() or ".." in member_path.parts:
                raise BundleSourceResolutionError(f"unsafe_zip_member:{member.filename}")
            if member.is_dir():
                continue
            destination = target / member_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            with zip_file.open(member) as source, destination.open("wb") as output:
                output.write(source.read())
    return target
