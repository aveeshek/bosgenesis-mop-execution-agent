"""Real, provisional namespace twin lifecycle foundation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from bosgenesis_mop_execution_agent.artifacts.bundle_validator import load_and_validate_bundle
from bosgenesis_mop_execution_agent.artifacts.models import ArtifactBundle, BundleSource
from bosgenesis_mop_execution_agent.namespace_twin.models import (
    POLICY_VERSION,
    RISK_RULE_VERSION,
)
from bosgenesis_mop_execution_agent.namespace_twin.persistence import (
    NamespaceTwinPersistenceError,
    NamespaceTwinRepository,
)
from bosgenesis_mop_execution_agent.plans.models import SUPPORTED_MACHINE_PLAN_SCHEMA_VERSIONS
from bosgenesis_mop_execution_agent.security import redact_value


class NamespaceTwinError(RuntimeError):
    """Typed service error exposed through REST and the ESDA gateway."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 422,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


class NamespaceTwinService:
    """Owns durable lifecycle facts without calculating a Phase 5 decision."""

    def __init__(self, repository: NamespaceTwinRepository | None = None) -> None:
        self.repository = repository or NamespaceTwinRepository()
        self.recovered_twin_ids = self.repository.recover_non_terminal()

    def create(self, payload: dict[str, Any], *, actor_id: str) -> dict[str, Any]:
        try:
            source = BundleSource.model_validate(payload.get("source") or {})
        except Exception as exc:
            raise NamespaceTwinError(
                "invalid_bundle_source", "A supported bundle source is required."
            ) from exc
        target_namespace = str(payload.get("target_namespace") or "").strip()
        if not target_namespace:
            raise NamespaceTwinError("target_namespace_required", "target_namespace is required.")
        target_cluster = str(payload.get("target_cluster") or "configured-cluster").strip()
        idempotency_key = str(payload.get("idempotency_key") or "").strip()
        if not idempotency_key:
            idempotency_key = self._default_idempotency_key(
                source, target_cluster, target_namespace
            )

        try:
            bundle = load_and_validate_bundle(source, target_namespace)
            provenance = self._verify_provenance(bundle)
            bundle_hash = self._directory_hash(bundle.root_path)
            input_hash = self._input_hash(bundle, provenance, target_cluster, target_namespace)
            resources = self._resources(bundle)
            edges = self._owner_edges(bundle)
            explicit_deletes = self._explicit_deletes(bundle)
            findings = self._findings(explicit_deletes)
            source_namespace = self._source_namespace(bundle)
            self._validate_source_residue(bundle, source_namespace, target_namespace)
        except NamespaceTwinError:
            raise
        except Exception as exc:
            raise NamespaceTwinError(
                "bundle_validation_failed",
                "The bundle failed namespace twin validation.",
                details={"reason": str(redact_value(str(exc)))},
            ) from exc

        now = datetime.now(UTC)
        twin_id = f"twin_{uuid4().hex}"
        bundle_name = self._bundle_name(source)
        display_source = source_namespace or Path(bundle_name).stem or "bundle"
        facts = {
            "provisional": True,
            "decision_authority": "not_available_until_phase5",
            "schema_version": bundle.machine_plan.schema_version,
            "phase_count": len(bundle.machine_plan.phases),
            "step_count": sum(len(phase.steps) for phase in bundle.machine_plan.phases),
            "resource_count": len(resources),
            "edge_count": len(edges),
            "finding_count": len(findings),
            "explicit_delete_count": len(explicit_deletes),
            "explicit_deletes": explicit_deletes,
            "provenance": provenance,
            "module_modes": {
                "overview": "real_core",
                "release-delta": "mock_non_authoritative",
                "dependency-graph": "mock_non_authoritative",
                "policy": "mock_non_authoritative",
                "dry-run": "mock_non_authoritative",
                "rollback": "mock_non_authoritative",
                "drift": "mock_non_authoritative",
                "runtime-behavior": "mock_non_authoritative",
                "audit": "real_core",
            },
        }
        actions = [
            self._action("open_twin", "Open Twin", method="GET"),
            self._action(
                "cancel_generation",
                "Cancel Generation",
                method="POST",
                confirmation=True,
            ),
        ]
        run, created = self.repository.create_run(
            {
                "twin_id": twin_id,
                "actor_id": actor_id,
                "idempotency_key": idempotency_key,
                "display_name": f"Provisional twin - {display_source} to {target_namespace}",
                "lifecycle_status": "requested",
                "decision": "pending",
                "decision_version": 1,
                "decision_is_final": False,
                "source_type": source.type.value,
                "source_value_redacted": str(redact_value(source.value)),
                "source_namespace": source_namespace,
                "target_cluster": target_cluster,
                "target_namespace": target_namespace,
                "bundle_name": bundle_name,
                "bundle_hash": bundle_hash,
                "release_version": self._release_version(bundle),
                "input_hash": input_hash,
                "report_hash": None,
                "policy_version": POLICY_VERSION,
                "risk_rule_version": RISK_RULE_VERSION,
                "facts_redacted": redact_value(facts),
                "actions_redacted": actions,
                "row_version": 1,
                "created_at": now,
                "updated_at": now,
                "completed_at": None,
                "expires_at": now + timedelta(hours=24),
                "superseded_by": None,
            },
            resources=resources,
            edges=edges,
            findings=findings,
        )
        if not created:
            return {**run, "idempotent_replay": True}
        try:
            self.repository.transition(
                twin_id,
                "generating",
                message="Validated bundle facts are being normalized.",
                payload={"resource_count": len(resources), "input_hash": input_hash},
            )
            run = self.repository.transition(
                twin_id,
                "awaiting_dry_run",
                message="Provisional twin is awaiting the existing authoritative dry-run.",
                payload={"dry_run_reused": True, "decision_is_final": False},
            )
        except Exception as exc:
            try:
                self.repository.transition(
                    twin_id,
                    "failed",
                    message="Namespace twin foundation failed safely.",
                    payload={"reason": str(redact_value(str(exc)))},
                )
            except Exception:
                pass
            raise

        supersedes_twin_id = str(payload.get("supersedes_twin_id") or "").strip()
        if supersedes_twin_id:
            self.repository.supersede(supersedes_twin_id, superseded_by=twin_id)
        return run

    def get(self, twin_id: str) -> dict[str, Any]:
        return self.repository.get_run(twin_id)

    def list(self, query: dict[str, Any]) -> dict[str, Any]:
        try:
            limit = max(1, min(int(query.get("limit") or 25), 100))
            offset = max(0, int(query.get("offset") or 0))
        except (TypeError, ValueError) as exc:
            raise NamespaceTwinError(
                "invalid_pagination", "limit and offset must be integers."
            ) from exc
        rows, total = self.repository.list_runs(
            lifecycle_status=str(query.get("lifecycle_status") or "") or None,
            target_namespace=str(query.get("target_namespace") or "") or None,
            limit=limit,
            offset=offset,
        )
        return {
            "items": rows,
            "page": {
                "limit": limit,
                "offset": offset,
                "result_count": total,
                "has_more": offset + len(rows) < total,
                "next_offset": offset + len(rows) if offset + len(rows) < total else None,
            },
        }

    def events(self, twin_id: str, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        rows, total = self.repository.list_events(twin_id, limit=limit, offset=offset)
        return {
            "twin_id": twin_id,
            "events": rows,
            "page": {
                "limit": limit,
                "offset": offset,
                "result_count": total,
                "has_more": offset + len(rows) < total,
            },
        }

    def cancel(self, twin_id: str, *, actor_id: str) -> dict[str, Any]:
        return self.repository.cancel(twin_id, actor_id=actor_id)

    @staticmethod
    def _default_idempotency_key(
        source: BundleSource, target_cluster: str, target_namespace: str
    ) -> str:
        raw = f"{source.type.value}:{source.value}:{target_cluster}:{target_namespace}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    @staticmethod
    def _bundle_name(source: BundleSource) -> str:
        return Path(source.value).name or f"{source.type.value}-bundle"

    @staticmethod
    def _directory_hash(root: Path) -> str:
        digest = hashlib.sha256()
        for path in sorted(item for item in root.rglob("*") if item.is_file()):
            relative = path.relative_to(root).as_posix()
            digest.update(relative.encode("utf-8"))
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()

    @staticmethod
    def _verify_provenance(bundle: ArtifactBundle) -> dict[str, Any]:
        if bundle.machine_plan.schema_version not in SUPPORTED_MACHINE_PLAN_SCHEMA_VERSIONS:
            raise NamespaceTwinError(
                "unsupported_machine_plan_schema",
                f"Unsupported machine plan schema {bundle.machine_plan.schema_version}.",
            )
        index = bundle.artifact_index_json
        if not index:
            raise NamespaceTwinError(
                "artifact_index_required",
                "artifact-index.json is required for a real namespace twin.",
            )
        verified = 0
        checksummed = 0
        for entry in index.get("files", []):
            path_value = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(path_value, str):
                raise NamespaceTwinError(
                    "artifact_index_invalid", "Every artifact index entry needs a path."
                )
            path = bundle.root_path / path_value
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            expected = entry.get("sha256") or entry.get("checksum")
            if isinstance(expected, str):
                normalized = expected.removeprefix("sha256:")
                if normalized.lower() != actual:
                    raise NamespaceTwinError(
                        "artifact_checksum_mismatch",
                        f"Artifact checksum mismatch for {path_value}.",
                    )
                checksummed += 1
            verified += 1
        return {
            "artifact_index_present": True,
            "referenced_files_verified": verified,
            "checksums_verified": checksummed,
            "checksum_coverage": "complete" if checksummed == verified else "partial",
        }

    @staticmethod
    def _input_hash(
        bundle: ArtifactBundle,
        provenance: dict[str, Any],
        target_cluster: str,
        target_namespace: str,
    ) -> str:
        envelope = {
            "machine_plan": bundle.machine_plan.model_dump(mode="json"),
            "artifact_index": bundle.artifact_index_json,
            "provenance": provenance,
            "target_cluster": target_cluster,
            "target_namespace": target_namespace,
        }
        canonical = json.dumps(envelope, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    @staticmethod
    def _resources(bundle: ArtifactBundle) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = []
        for manifest in bundle.manifests:
            namespace = manifest.namespace or bundle.target_namespace
            identity = f"{manifest.api_version}:{manifest.kind}:{namespace}:{manifest.name}"
            resources.append(
                {
                    "resource_id": f"twinres_{uuid4().hex}",
                    "stable_identity": identity,
                    "api_version": manifest.api_version,
                    "kind": manifest.kind,
                    "name": manifest.name,
                    "namespace": namespace,
                    "payload_redacted": redact_value(
                        {
                            "path": manifest.path,
                            "document_index": manifest.document_index,
                            "metadata": manifest.content.get("metadata", {}),
                        }
                    ),
                }
            )
        return resources

    @staticmethod
    def _owner_edges(bundle: ArtifactBundle) -> list[dict[str, Any]]:
        edges: list[dict[str, Any]] = []
        for manifest in bundle.manifests:
            namespace = manifest.namespace or bundle.target_namespace
            source = f"{manifest.api_version}:{manifest.kind}:{namespace}:{manifest.name}"
            metadata = manifest.content.get("metadata") or {}
            for owner in metadata.get("ownerReferences") or []:
                if not isinstance(owner, dict) or not owner.get("kind") or not owner.get("name"):
                    continue
                target = f"owner:{owner['kind']}:{namespace}:{owner['name']}"
                edges.append(
                    {
                        "edge_id": f"twinedge_{uuid4().hex}",
                        "source_identity": source,
                        "target_identity": target,
                        "edge_type": "owner_reference",
                        "confidence": "deterministic",
                        "evidence_refs": [manifest.path],
                    }
                )
        return edges

    @staticmethod
    def _explicit_deletes(bundle: ArtifactBundle) -> list[dict[str, Any]]:
        deletes: list[dict[str, Any]] = []
        for phase in bundle.machine_plan.phases:
            for step in phase.steps:
                delete_commands = [
                    command.command
                    for command in step.commands
                    if command.mutating is True and "delete" in command.command.lower()
                ]
                if "delete" in step.type.lower() or delete_commands:
                    deletes.append(
                        {
                            "phase_id": phase.phase_id,
                            "step_id": step.step_id,
                            "step_type": step.type,
                            "manifest_refs": list(step.manifest_refs),
                            "commands": delete_commands,
                        }
                    )
        return deletes

    @staticmethod
    def _findings(explicit_deletes: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [
            {
                "finding_id": f"twinfinding_{uuid4().hex}",
                "code": "EXPLICIT_DELETE_OPERATION",
                "severity": "review",
                "status": "provisional",
                "message": f"Plan step {item['step_id']} explicitly declares deletion.",
                "evidence_refs": item["manifest_refs"],
            }
            for item in explicit_deletes
        ]

    @staticmethod
    def _source_namespace(bundle: ArtifactBundle) -> str | None:
        artifact = bundle.artifact_json or {}
        for key in ("source_namespace", "namespace"):
            value = artifact.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        source = artifact.get("source")
        if isinstance(source, dict):
            value = source.get("namespace")
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _release_version(bundle: ArtifactBundle) -> str | None:
        artifact = bundle.artifact_json or {}
        for key in ("release_version", "chart_version", "version"):
            value = artifact.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return None

    @staticmethod
    def _validate_source_residue(
        bundle: ArtifactBundle, source_namespace: str | None, target_namespace: str
    ) -> None:
        if not source_namespace or source_namespace == target_namespace:
            return
        for manifest in bundle.manifests:
            namespace = manifest.namespace
            if namespace == source_namespace:
                raise NamespaceTwinError(
                    "source_namespace_residue",
                    f"Deployable manifest {manifest.path} still targets the source namespace.",
                )

    @staticmethod
    def _action(
        code: str,
        label: str,
        *,
        method: str,
        confirmation: bool = False,
    ) -> dict[str, Any]:
        return {
            "code": code,
            "label": label,
            "enabled": True,
            "visible": True,
            "method": method,
            "href": None,
            "reason_code": "real_core_eligible",
            "disabled_reason": None,
            "requires_confirmation": confirmation,
        }


def translate_persistence_error(exc: NamespaceTwinPersistenceError) -> NamespaceTwinError:
    return NamespaceTwinError(exc.code, exc.message, status_code=exc.status_code)
