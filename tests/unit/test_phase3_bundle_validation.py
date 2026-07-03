from __future__ import annotations

import json
import shutil
import zipfile
from pathlib import Path

import pytest
import yaml

from bosgenesis_mop_execution_agent.artifacts.bundle_reader import BundleSourceResolutionError
from bosgenesis_mop_execution_agent.artifacts.bundle_validator import (
    BundleValidationError,
    load_and_validate_bundle,
)
from bosgenesis_mop_execution_agent.artifacts.manifest_loader import ManifestValidationError
from bosgenesis_mop_execution_agent.artifacts.models import BundleSource, BundleSourceType
from bosgenesis_mop_execution_agent.artifacts.values_loader import ValuesValidationError
from bosgenesis_mop_execution_agent.plans.dependency_graph import DependencyGraphError
from bosgenesis_mop_execution_agent.plans.machine_plan_parser import MachinePlanParseError

FIXTURE_ROOT = Path("tests/fixtures/sample_mop_bundle")


def test_sample_bundle_parses_machine_plan_first_and_loads_supporting_context(
    tmp_path: Path,
) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    (bundle_root / "human-readable-mop.md").write_text(
        "# Human MoP\n\nTarget namespace: markdown-should-not-win\n",
        encoding="utf-8",
    )

    bundle = load_and_validate_bundle(
        BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
        target_namespace="sample-target",
    )

    assert bundle.machine_plan.target_namespace == "sample-target"
    assert "markdown-should-not-win" in (bundle.human_mop_markdown or "")
    assert bundle.manifests[0].kind == "ConfigMap"
    assert bundle.artifact_json is not None
    assert bundle.artifact_index_json is not None
    assert bundle.response_json is not None


def test_uploaded_zip_source_resolves_and_validates(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    archive = tmp_path / "bundle.zip"
    with zipfile.ZipFile(archive, mode="w") as zip_file:
        for path in bundle_root.rglob("*"):
            if path.is_file():
                zip_file.write(path, path.relative_to(bundle_root))

    bundle = load_and_validate_bundle(
        BundleSource(type=BundleSourceType.UPLOADED_ZIP, value=str(archive)),
        target_namespace="sample-target",
    )

    assert bundle.machine_plan.phase_ids == {"apply_configmaps"}


def test_object_store_zip_source_resolves_and_validates(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    archive = tmp_path / "object-store-bundle.zip"
    with zipfile.ZipFile(archive, mode="w") as zip_file:
        for path in bundle_root.rglob("*"):
            if path.is_file():
                zip_file.write(path, path.relative_to(bundle_root))

    bundle = load_and_validate_bundle(
        BundleSource(type=BundleSourceType.OBJECT_STORE, value=str(archive)),
        target_namespace="sample-target",
    )

    assert bundle.machine_plan.phase_ids == {"apply_configmaps"}

def test_artifact_manifest_source_resolves(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    manifest = tmp_path / "artifact-manifest.json"
    manifest.write_text(json.dumps({"root_path": str(bundle_root)}), encoding="utf-8")

    bundle = load_and_validate_bundle(
        BundleSource(type=BundleSourceType.ARTIFACT_MANIFEST, value=str(manifest)),
        target_namespace="sample-target",
    )

    assert bundle.root_path == bundle_root


@pytest.mark.parametrize(
    "source_type",
    [BundleSourceType.MOP_CREATION_RUN, BundleSourceType.OBJECT_STORE],
)
def test_placeholder_sources_fail_closed(source_type: BundleSourceType) -> None:
    with pytest.raises(BundleSourceResolutionError, match="bundle_source_not_locally_resolvable"):
        load_and_validate_bundle(
            BundleSource(type=source_type, value="placeholder"),
            target_namespace="sample-target",
        )


def test_missing_machine_plan_fails_closed(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    (bundle_root / "machine_execution_plan.yaml").unlink()
    (bundle_root / "machine-readable-installation-notes.md").unlink()

    with pytest.raises(BundleValidationError, match="machine_execution_plan_missing"):
        load_and_validate_bundle(
            BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
            target_namespace="sample-target",
        )


def test_embedded_installation_notes_plan_is_fallback(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    plan_text = (bundle_root / "machine_execution_plan.yaml").read_text(encoding="utf-8")
    (bundle_root / "machine_execution_plan.yaml").unlink()
    (bundle_root / "machine-readable-installation-notes.md").write_text(
        f"# Notes\n\n```machine_execution_plan\n{plan_text}```\n",
        encoding="utf-8",
    )
    artifact_index = _read_json(bundle_root / "artifact-index.json")
    artifact_index["files"] = [
        entry
        for entry in artifact_index["files"]
        if isinstance(entry, dict) and entry.get("path") != "machine_execution_plan.yaml"
    ]
    (bundle_root / "artifact-index.json").write_text(
        json.dumps(artifact_index),
        encoding="utf-8",
    )

    bundle = load_and_validate_bundle(
        BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
        target_namespace="sample-target",
    )

    assert bundle.machine_plan.target_namespace == "sample-target"


def test_machine_plan_preserves_helm_metadata_fields(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    plan = _read_yaml(bundle_root / "machine_execution_plan.yaml")
    step = plan["phases"][0]["steps"][0]
    step.update(
        {
            "type": "helm_upgrade",
            "manifest_refs": [],
            "values_refs": ["values/values-signoz.yaml"],
            "release_name": "signoz",
            "chart_ref": "signoz/signoz",
            "chart_version": "0.129.0",
            "repo_name": "signoz",
            "repo_url": "https://charts.signoz.io",
            "commands": [
                {
                    "kind": "upgrade",
                    "command": "helm upgrade --install signoz signoz/signoz -n sample-target",
                }
            ],
        }
    )
    values_dir = bundle_root / "values"
    values_dir.mkdir(exist_ok=True)
    (values_dir / "values-signoz.yaml").write_text("global: {}\n", encoding="utf-8")
    _write_yaml(bundle_root / "machine_execution_plan.yaml", plan)

    bundle = load_and_validate_bundle(
        BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
        target_namespace="sample-target",
    )

    metadata = bundle.machine_plan.phases[0].steps[0].metadata
    assert metadata["release_name"] == "signoz"
    assert metadata["chart_ref"] == "signoz/signoz"
    assert metadata["chart_version"] == "0.129.0"
    assert metadata["repo_url"] == "https://charts.signoz.io"


def test_unsupported_schema_fails_closed(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    plan = _read_yaml(bundle_root / "machine_execution_plan.yaml")
    plan["schema_version"] = "9.9.9"
    _write_yaml(bundle_root / "machine_execution_plan.yaml", plan)

    with pytest.raises(MachinePlanParseError, match="machine_plan_schema_invalid"):
        load_and_validate_bundle(
            BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
            target_namespace="sample-target",
        )


def test_dependency_cycle_fails_closed(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    plan = _read_yaml(bundle_root / "machine_execution_plan.yaml")
    plan["dependency_graph"] = [
        {"phase_id": "apply_configmaps", "depends_on": ["apply_configmaps"]}
    ]
    _write_yaml(bundle_root / "machine_execution_plan.yaml", plan)

    with pytest.raises(DependencyGraphError, match="phase_dependency_cycle"):
        load_and_validate_bundle(
            BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
            target_namespace="sample-target",
        )


def test_unknown_dependency_reference_fails_closed(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    plan = _read_yaml(bundle_root / "machine_execution_plan.yaml")
    plan["dependency_graph"] = [{"phase_id": "apply_configmaps", "depends_on": ["missing"]}]
    _write_yaml(bundle_root / "machine_execution_plan.yaml", plan)

    with pytest.raises(DependencyGraphError, match="unknown_phase_dependency"):
        load_and_validate_bundle(
            BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
            target_namespace="sample-target",
        )


def test_missing_manifest_reference_fails_closed(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    (bundle_root / "generated/configmap-sample-app.yaml").unlink()

    with pytest.raises(ManifestValidationError, match="manifest_missing"):
        load_and_validate_bundle(
            BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
            target_namespace="sample-target",
        )


def test_unsafe_manifest_reference_fails_closed(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    plan = _read_yaml(bundle_root / "machine_execution_plan.yaml")
    plan["phases"][0]["steps"][0]["manifest_refs"] = ["../outside.yaml"]
    _write_yaml(bundle_root / "machine_execution_plan.yaml", plan)

    with pytest.raises(ManifestValidationError, match="manifest_path_unsafe"):
        load_and_validate_bundle(
            BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
            target_namespace="sample-target",
        )


def test_invalid_manifest_yaml_fails_closed(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    (bundle_root / "generated/configmap-sample-app.yaml").write_text(
        "apiVersion: v1\nkind: [\n",
        encoding="utf-8",
    )

    with pytest.raises(ManifestValidationError, match="manifest_yaml_invalid"):
        load_and_validate_bundle(
            BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
            target_namespace="sample-target",
        )


def test_cluster_scoped_manifest_fails_closed(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    _write_yaml(
        bundle_root / "generated/configmap-sample-app.yaml",
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "blocked"}},
    )

    with pytest.raises(ManifestValidationError, match="cluster_scoped_resource_blocked"):
        load_and_validate_bundle(
            BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
            target_namespace="sample-target",
        )


def test_unknown_manifest_kind_fails_closed(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    _write_yaml(
        bundle_root / "generated/configmap-sample-app.yaml",
        {
            "apiVersion": "example.com/v1",
            "kind": "Mystery",
            "metadata": {"name": "unknown", "namespace": "sample-target"},
        },
    )

    with pytest.raises(ManifestValidationError, match="manifest_kind_unsupported"):
        load_and_validate_bundle(
            BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
            target_namespace="sample-target",
        )


def test_secret_manifest_values_fail_closed(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    _write_yaml(
        bundle_root / "generated/configmap-sample-app.yaml",
        {
            "apiVersion": "v1",
            "kind": "Secret",
            "metadata": {"name": "unsafe", "namespace": "sample-target"},
            "stringData": {"password": "do-not-copy"},
        },
    )

    with pytest.raises(ManifestValidationError, match="secret_value_detected"):
        load_and_validate_bundle(
            BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
            target_namespace="sample-target",
        )


def test_values_file_sensitive_key_fails_closed(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    (bundle_root / "generated/values.yaml").write_text("password: unsafe\n", encoding="utf-8")
    plan = _read_yaml(bundle_root / "machine_execution_plan.yaml")
    plan["phases"][0]["steps"][0]["values_refs"] = ["generated/values.yaml"]
    _write_yaml(bundle_root / "machine_execution_plan.yaml", plan)

    with pytest.raises(ValuesValidationError, match="values_secret_like_key_detected"):
        load_and_validate_bundle(
            BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
            target_namespace="sample-target",
        )


def test_helm_values_flag_is_not_loaded_as_kubernetes_manifest(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    (bundle_root / "generated/values.yaml").write_text(
        "_note: placeholder values are supporting Helm input\n",
        encoding="utf-8",
    )
    plan = _read_yaml(bundle_root / "machine_execution_plan.yaml")
    plan["dependency_graph"].append({"phase_id": "install_helm", "depends_on": []})
    plan["phases"].append(
        {
            "phase_id": "install_helm",
            "objective": "Dry-run a Helm release with a values file.",
            "depends_on": [],
            "steps": [
                {
                    "step_id": "helm-dry-run",
                    "title": "Dry-run Helm release",
                    "type": "helm",
                    "commands": [
                        {
                            "kind": "dry_run",
                            "command": (
                                "helm upgrade --install sample sample/chart "
                                "-n sample-target -f generated/values.yaml --dry-run"
                            ),
                            "dry_run": True,
                            "mutating": False,
                        }
                    ],
                }
            ],
        }
    )
    _write_yaml(bundle_root / "machine_execution_plan.yaml", plan)

    bundle = load_and_validate_bundle(
        BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
        target_namespace="sample-target",
    )

    assert {manifest.path for manifest in bundle.manifests} == {
        "generated/configmap-sample-app.yaml"
    }
    assert [values.path for values in bundle.values_files] == ["generated/values.yaml"]
    assert bundle.machine_plan.phases[1].steps[0].type == "helm_upgrade"


def test_artifact_index_missing_file_fails_closed(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    artifact_index = _read_json(bundle_root / "artifact-index.json")
    artifact_index["files"].append({"path": "missing.yaml", "role": "generated_manifest"})
    (bundle_root / "artifact-index.json").write_text(
        json.dumps(artifact_index),
        encoding="utf-8",
    )

    with pytest.raises(BundleValidationError, match="artifact_index_file_missing"):
        load_and_validate_bundle(
            BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
            target_namespace="sample-target",
        )


def test_artifact_index_unsafe_file_path_fails_closed(tmp_path: Path) -> None:
    bundle_root = _copy_sample_bundle(tmp_path)
    artifact_index = _read_json(bundle_root / "artifact-index.json")
    artifact_index["files"].append({"path": "../outside.yaml", "role": "generated_manifest"})
    (bundle_root / "artifact-index.json").write_text(
        json.dumps(artifact_index),
        encoding="utf-8",
    )

    with pytest.raises(BundleValidationError, match="artifact_index_file_path_unsafe"):
        load_and_validate_bundle(
            BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(bundle_root)),
            target_namespace="sample-target",
        )


def _copy_sample_bundle(tmp_path: Path) -> Path:
    destination = tmp_path / "bundle"
    shutil.copytree(FIXTURE_ROOT, destination)
    return destination


def _read_yaml(path: Path) -> dict[str, object]:
    loaded = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded


def _write_yaml(path: Path, content: dict[str, object]) -> None:
    path.write_text(yaml.safe_dump(content, sort_keys=False), encoding="utf-8")


def _read_json(path: Path) -> dict[str, object]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return loaded
