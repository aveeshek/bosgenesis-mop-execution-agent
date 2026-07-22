"""Real, provisional namespace twin lifecycle foundation."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import time
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

import yaml

from bosgenesis_mop_execution_agent.artifacts.bundle_validator import (
    artifact_index_file_entries,
    load_and_validate_bundle,
)
from bosgenesis_mop_execution_agent.artifacts.models import ArtifactBundle, BundleSource
from bosgenesis_mop_execution_agent.namespace_twin.audit_report import (
    build_audit_event,
    build_report,
    render_markdown,
)
from bosgenesis_mop_execution_agent.namespace_twin.canonicalization import (
    canonicalize_kubernetes_object,
)
from bosgenesis_mop_execution_agent.namespace_twin.delta import calculate_release_delta
from bosgenesis_mop_execution_agent.namespace_twin.dependency_graph import (
    EDGE_TYPES,
    build_dependency_graph,
)
from bosgenesis_mop_execution_agent.namespace_twin.drift_twin import (
    assess_drift,
    capture_baseline,
    initial_drift_assessment,
)
from bosgenesis_mop_execution_agent.namespace_twin.live_snapshot import (
    KubernetesLiveSnapshotCollector,
    LiveSnapshotCollector,
)
from bosgenesis_mop_execution_agent.namespace_twin.models import (
    POLICY_VERSION,
    RISK_RULE_VERSION,
)
from bosgenesis_mop_execution_agent.namespace_twin.mop_replay_twin import (
    ReplayEvidenceError,
    build_replay_result,
)
from bosgenesis_mop_execution_agent.namespace_twin.persistence import (
    NamespaceTwinPersistenceError,
    NamespaceTwinRepository,
)
from bosgenesis_mop_execution_agent.namespace_twin.policy_twin import (
    evaluate_policy_twin,
    finalize_policy_twin,
)
from bosgenesis_mop_execution_agent.namespace_twin.release_note_validation import (
    validate_release_note_claims,
)
from bosgenesis_mop_execution_agent.namespace_twin.rollback_twin import (
    assess_rollback_twin,
    enrich_rollback_proof,
)
from bosgenesis_mop_execution_agent.namespace_twin.runtime_behavior_twin import (
    assess_runtime_behavior,
)
from bosgenesis_mop_execution_agent.plans.models import SUPPORTED_MACHINE_PLAN_SCHEMA_VERSIONS
from bosgenesis_mop_execution_agent.security import redact_value

DEFAULT_TWIN_CONFIGMAP_EXCLUDE_NAMES = ("kube-root-ca.crt",)
DEFAULT_TWIN_CONFIGMAP_EXCLUDE_PREFIXES = ("istio-",)
TWIN_RENDERED_HELM_KINDS = {
    "ConfigMap",
    "CronJob",
    "DaemonSet",
    "Deployment",
    "Ingress",
    "Job",
    "NetworkPolicy",
    "PersistentVolumeClaim",
    "PodDisruptionBudget",
    "Role",
    "RoleBinding",
    "Service",
    "ServiceAccount",
    "StatefulSet",
}


def _csv_property(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


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

    def __init__(
        self,
        repository: NamespaceTwinRepository | None = None,
        live_collector: LiveSnapshotCollector | None = None,
        execution_service: Any | None = None,
        pvc_risk_enabled: bool | None = None,
        configmap_exclude_names: tuple[str, ...] | None = None,
        configmap_exclude_prefixes: tuple[str, ...] | None = None,
    ) -> None:
        self.repository = repository or NamespaceTwinRepository()
        self.live_collector = live_collector or KubernetesLiveSnapshotCollector.from_environment()
        self.execution_service = execution_service
        self.pvc_risk_enabled = (
            pvc_risk_enabled
            if pvc_risk_enabled is not None
            else os.getenv("NAMESPACE_TWIN_PVC_RISK_ENABLED", "false").strip().lower()
            in {"1", "true", "yes", "on"}
        )
        self.configmap_exclude_names = configmap_exclude_names or _csv_property(
            "NAMESPACE_TWIN_CONFIGMAP_EXCLUDE_NAMES",
            DEFAULT_TWIN_CONFIGMAP_EXCLUDE_NAMES,
        )
        self.configmap_exclude_prefixes = configmap_exclude_prefixes or _csv_property(
            "NAMESPACE_TWIN_CONFIGMAP_EXCLUDE_PREFIXES",
            DEFAULT_TWIN_CONFIGMAP_EXCLUDE_PREFIXES,
        )
        self._reconcile_lock = RLock()
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
            snapshot = self.live_collector.collect(
                target_namespace, correlation_id=f"twin-snapshot-{uuid4().hex}"
            )
            planned_helm_installs = self._planned_helm_install_releases(bundle)
            planned_resources = self._resources(
                bundle,
                snapshot=snapshot,
                planned_helm_installs=planned_helm_installs,
            )
            ignored_manifest_refs = self._ignored_manifest_refs(bundle)
            resources, edges, graph_findings, graph_summary = build_dependency_graph(
                bundle,
                planned_resources,
                ignored_manifest_refs=ignored_manifest_refs,
            )
            explicit_deletes = self._explicit_deletes(bundle)
            findings = self._findings(explicit_deletes) + graph_findings
            deltas = calculate_release_delta(
                planned_resources,
                snapshot,
                target_namespace=target_namespace,
                explicit_deletes=explicit_deletes,
                planned_helm_installs=planned_helm_installs,
            )
            policy_twin = evaluate_policy_twin(
                bundle=bundle,
                planned_resources=planned_resources,
                deltas=deltas,
                snapshot=snapshot,
                provenance=provenance,
                graph_summary=graph_summary,
                explicit_deletes=explicit_deletes,
                input_hash=input_hash,
                target_namespace=target_namespace,
                pvc_risk_enabled=self.pvc_risk_enabled,
            )
            rollback_twin = assess_rollback_twin(bundle)
            runtime_context = self._collect_runtime_context(
                target_namespace, correlation_id=f"twin-runtime-{uuid4().hex}"
            )
            runtime_behavior_twin = assess_runtime_behavior(
                snapshot,
                namespace_summary=runtime_context.get("namespace_summary") or {},
                events=(
                    list(runtime_context.get("events") or [])
                    if runtime_context.get("events_collected")
                    else None
                ),
                captured_at=datetime.now(UTC),
                target_namespace=target_namespace,
            )
            findings.extend(
                {
                    "finding_id": f"twinfinding_{uuid4().hex}",
                    "code": item["code"],
                    "severity": item["severity"],
                    "status": item["status"],
                    "message": item["message"],
                    "evidence_refs": item["evidence_refs"],
                }
                for item in policy_twin["findings"]
            )
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
        drift_baseline = capture_baseline(
            snapshot,
            captured_at=now,
            target_namespace=target_namespace,
        )
        drift_twin = initial_drift_assessment(drift_baseline)
        snapshot_hash = drift_baseline["hash"]
        twin_id = f"twin_{uuid4().hex}"
        bundle_name = self._bundle_name(source)
        display_source = source_namespace or Path(bundle_name).stem or "bundle"
        facts = {
            "provisional": True,
            "decision_authority": (
                "deterministic_policy_projection; final decision awaits authoritative dry-run"
            ),
            "schema_version": bundle.machine_plan.schema_version,
            "phase_count": len(bundle.machine_plan.phases),
            "step_count": sum(len(phase.steps) for phase in bundle.machine_plan.phases),
            "resource_count": len(resources),
            "edge_count": len(edges),
            "dependency_graph_summary": graph_summary,
            "finding_count": len(findings),
            "explicit_delete_count": len(explicit_deletes),
            "explicit_deletes": explicit_deletes,
            "provenance": provenance,
            "policy_twin": policy_twin,
            "rollback_twin": rollback_twin,
            "runtime_behavior_twin": runtime_behavior_twin,
            "drift_baseline": drift_baseline,
            "drift_twin": drift_twin,
            "module_modes": {
                "release_delta_count": len(deltas),
                "release_delta_summary": self._delta_summary(deltas),
                "twin_planning": {
                    "planned_helm_installs": sorted(planned_helm_installs),
                    "configmap_exclude_names": list(self.configmap_exclude_names),
                    "configmap_exclude_prefixes": list(self.configmap_exclude_prefixes),
                    "configmap_bundle_debt": (
                        "Platform-managed ConfigMaps remain in source bundles but are excluded "
                        "from Namespace Twin planning until bundle generation can classify them."
                    ),
                },
                "live_snapshot": {
                    "available": snapshot.available,
                    "snapshot_id": f"snapshot_{snapshot_hash[:24]}",
                    "captured_at": now.isoformat(),
                    "hash": snapshot_hash,
                    "complete_kinds": sorted(snapshot.complete_kinds),
                    "resource_count": len(snapshot.resources),
                    "helm_inventory_available": snapshot.helm_inventory_available,
                    "installed_helm_releases": sorted(snapshot.installed_helm_releases),
                    "ignored_helm_releases": sorted(snapshot.ignored_helm_releases),
                    "ignored_helm_prefixes": list(snapshot.ignored_helm_prefixes),
                    "evidence_refs": snapshot.evidence_refs,
                    "warning": snapshot.warning,
                },
                "overview": "real_core",
                "release-delta": "real_core",
                "dependency-graph": "real_core",
                "policy": "real_core",
                "dry-run": "real_core",
                "rollback": "real_core",
                "drift": "real_core",
                "mop-replay": "real_core",
                "runtime-behavior": "real_core",
                "release-note-validation": "real_core",
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
            deltas=deltas,
        )
        if not created:
            if payload.get("run_authoritative_dry_run"):
                run = self._ensure_authoritative_dry_run(run, source=source)
            return {**self._project(run), "idempotent_replay": True}
        try:
            self.repository.transition(
                twin_id,
                "generating",
                message="Validated bundle facts are being normalized.",
                payload={
                    "resource_count": len(resources),
                    "release_delta_count": len(deltas),
                    "input_hash": input_hash,
                },
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
        if payload.get("run_authoritative_dry_run"):
            run = self._ensure_authoritative_dry_run(run, source=source)
        return self._project(run)

    def _ensure_authoritative_dry_run(
        self,
        core: dict[str, Any],
        *,
        source: BundleSource,
    ) -> dict[str, Any]:
        """Create and start one shared, dry-run-only execution job for a twin."""
        if self.execution_service is None:
            raise NamespaceTwinError(
                "execution_service_unavailable",
                "The shared MoP Execution service is unavailable.",
                status_code=503,
            )
        twin_id = str(core["twin_id"])
        facts = dict(core.get("facts") or {})
        linked_job_id = str(facts.get("dry_run_job_id") or "").strip()
        if linked_job_id:
            return core

        job_id = f"twinjob_{twin_id.removeprefix('twin_')}"
        bundle_id = f"twinbundle_{str(core.get('bundle_hash') or '')[:24]}"
        existing_job = self.execution_service.get_job(job_id)
        if not existing_job.get("ok"):
            registered = self.execution_service.register_bundle(
                {
                    "bundle_id": bundle_id,
                    "source": source.model_dump(mode="json"),
                    "target_namespace": core["target_namespace"],
                }
            )
            if not registered.get("ok"):
                raise NamespaceTwinError(
                    "simulation_bundle_registration_failed",
                    "The authoritative simulation bundle could not be registered.",
                    status_code=502,
                    details={"twin_id": twin_id},
                )
            validated = self.execution_service.validate_bundle(bundle_id, {})
            validation_data = dict(validated.get("data") or {})
            if not validated.get("ok") or validation_data.get("valid") is not True:
                raise NamespaceTwinError(
                    "simulation_bundle_validation_failed",
                    "The authoritative simulation bundle did not pass execution validation.",
                    status_code=409,
                    details={
                        "twin_id": twin_id,
                        "validation": redact_value(validation_data),
                    },
                )
            created_job = self.execution_service.create_job(
                {
                    "job_id": job_id,
                    "bundle_id": bundle_id,
                    "target_namespace": core["target_namespace"],
                    "source_namespace": core.get("source_namespace"),
                    "execution_mode": "dry_run_only",
                    "job_name": f"Namespace Twin simulation {twin_id}",
                    "correlation_id": f"namespace-twin-{twin_id}",
                    "namespace_twin_input_hash": core.get("input_hash"),
                    "bundle_hash": core.get("bundle_hash"),
                    "snapshot_hash": (facts.get("drift_baseline") or {}).get("hash"),
                }
            )
            if not created_job.get("ok"):
                raise NamespaceTwinError(
                    "simulation_job_creation_failed",
                    "The authoritative dry-run job could not be created.",
                    status_code=502,
                    details={"twin_id": twin_id},
                )

        started = self.execution_service.start_job(job_id)
        if not started.get("ok"):
            raise NamespaceTwinError(
                "simulation_job_start_failed",
                "The authoritative dry-run job could not be started.",
                status_code=502,
                details={"twin_id": twin_id, "dry_run_job_id": job_id},
            )
        return self.repository.merge_facts(
            twin_id,
            {
                "dry_run_job_id": job_id,
                "dry_run_status": "queued",
                "simulation": {
                    "mode": "full_on_demand",
                    "state": "queued",
                    "bundle_id": bundle_id,
                    "dry_run_job_id": job_id,
                    "started_at": datetime.now(UTC).isoformat(),
                    "mutation_performed": False,
                },
            },
            event_type="authoritative_simulation_started",
            message="Authoritative namespace simulation dry-run was created and queued.",
            event_payload={
                "dry_run_job_id": job_id,
                "bundle_id": bundle_id,
                "execution_mode": "dry_run_only",
                "mutation_performed": False,
            },
        )

    def _reconcile_authoritative_dry_run(self, twin_id: str) -> dict[str, Any]:
        """Reconcile a linked durable dry-run into the twin's deterministic decision."""
        with self._reconcile_lock:
            core = self.repository.get_run(twin_id)
            if core.get("decision_is_final") or core.get("lifecycle_status") in {
                "failed",
                "cancelled",
                "superseded",
                "expired",
            }:
                return core
            facts = dict(core.get("facts") or {})
            job_id = str(facts.get("dry_run_job_id") or "").strip()
            if not job_id or self.execution_service is None:
                return core

            job_response = self.execution_service.get_job(job_id)
            if not job_response.get("ok"):
                observed_state = "unavailable"
                job_state = "unavailable"
            else:
                job = dict((job_response.get("data") or {}).get("job") or {})
                job_state = str(job.get("state") or "unknown")
                observed_state = job_state

            simulation = dict(facts.get("simulation") or {})
            prior_state = str(simulation.get("state") or "")
            terminal_job_states = {"completed", "failed", "cancelled", "decision_required"}
            if job_state not in terminal_job_states:
                if observed_state != prior_state:
                    core = self.repository.merge_facts(
                        twin_id,
                        {
                            "dry_run_status": observed_state,
                            "simulation": {
                                **simulation,
                                "state": observed_state,
                                "last_reconciled_at": datetime.now(UTC).isoformat(),
                            },
                        },
                        event_type="authoritative_simulation_progress",
                        message=(
                            f"Authoritative simulation job state changed to {observed_state}."
                        ),
                        event_payload={
                            "dry_run_job_id": job_id,
                            "job_state": observed_state,
                        },
                    )
                return core

            return self.attach_dry_run_evidence(
                twin_id,
                {"dry_run_job_id": job_id},
            )["twin"]

    def attach_dry_run_evidence(
        self,
        twin_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        """Attach one existing authoritative dry-run and finalize deterministically."""
        core = self.repository.get_run(twin_id)
        facts = dict(core.get("facts") or {})
        job_id = str(payload.get("dry_run_job_id") or "").strip()
        if not job_id:
            raise NamespaceTwinError(
                "dry_run_job_id_required",
                "dry_run_job_id is required to attach authoritative evidence.",
            )
        existing = facts.get("dry_run_evidence") or {}
        if core.get("decision_is_final"):
            if existing.get("dry_run_job_id") == job_id:
                return {
                    "twin": self._project(core),
                    "dry_run": self.dry_run(twin_id),
                    "idempotent_replay": True,
                }
            raise NamespaceTwinError(
                "terminal_twin_immutable",
                "A different dry-run cannot modify a terminal namespace twin.",
                status_code=409,
            )
        if core.get("lifecycle_status") in {"superseded", "expired", "cancelled", "failed"}:
            raise NamespaceTwinError(
                "twin_not_attachable",
                "Authoritative evidence cannot be attached to this twin lifecycle state.",
                status_code=409,
                details={"lifecycle_status": core.get("lifecycle_status")},
            )
        if self.execution_service is None:
            raise NamespaceTwinError(
                "execution_service_unavailable",
                "The shared MoP Execution service is unavailable.",
                status_code=503,
            )
        wait_seconds = max(0, min(int(payload.get("wait_seconds") or 0), 30))
        poll_interval_seconds = max(
            0.1,
            min(float(payload.get("poll_interval_ms") or 500) / 1000.0, 5.0),
        )
        deadline = time.monotonic() + wait_seconds
        while True:
            response = self.execution_service.namespace_twin_dry_run_evidence(job_id)
            if not response.get("ok"):
                raise NamespaceTwinError(
                    "dry_run_job_not_found",
                    "The requested authoritative dry-run job was not found.",
                    status_code=404,
                    details={"dry_run_job_id": job_id},
                )
            evidence = dict((response.get("data") or {}).get("dry_run_evidence") or {})
            polled_status = str(evidence.get("status") or "pending")
            if polled_status not in {"pending", "running"} or time.monotonic() >= deadline:
                break
            time.sleep(poll_interval_seconds)
        mismatches: list[str] = []
        for key, expected, observed in (
            ("target_namespace", core.get("target_namespace"), evidence.get("target_namespace")),
            ("bundle_hash", core.get("bundle_hash"), evidence.get("bundle_hash")),
            ("input_hash", core.get("input_hash"), evidence.get("input_hash")),
        ):
            if not observed or str(observed) != str(expected):
                mismatches.append(
                    f"{key}: expected {expected or 'missing'}, observed {observed or 'missing'}"
                )
        for key in ("bundle_hash", "input_hash", "command_fingerprint_hash"):
            supplied = str(payload.get(key) or "").strip()
            observed = str(evidence.get(key) or "").strip()
            expected = (
                str(core.get(key) or "").strip() if key != "command_fingerprint_hash" else observed
            )
            if supplied and supplied != expected:
                mismatches.append(
                    f"request {key}: expected {expected or 'missing'}, observed {supplied}"
                )
        if mismatches:
            raise NamespaceTwinError(
                "dry_run_evidence_mismatch",
                "The dry-run evidence does not match this twin input boundary.",
                status_code=409,
                details={"mismatches": mismatches, "dry_run_job_id": job_id},
            )

        evidence_time = self._parse_timestamp(
            evidence.get("completed_at") or evidence.get("updated_at")
        )
        max_age = max(
            60,
            int(os.getenv("NAMESPACE_TWIN_DRY_RUN_MAX_AGE_SECONDS", "86400")),
        )
        age_seconds = int((datetime.now(UTC) - evidence_time).total_seconds())
        if age_seconds < 0 or age_seconds > max_age:
            raise NamespaceTwinError(
                "stale_dry_run_evidence",
                "The dry-run evidence is outside the configured freshness window.",
                status_code=409,
                details={
                    "age_seconds": age_seconds,
                    "maximum_age_seconds": max_age,
                    "dry_run_job_id": job_id,
                },
            )
        evidence_status = str(evidence.get("status") or "pending")
        if evidence_status in {"pending", "running"}:
            raise NamespaceTwinError(
                "dry_run_not_terminal",
                "Only terminal authoritative dry-run evidence can be attached.",
                status_code=409,
                details={"status": evidence_status, "dry_run_job_id": job_id},
            )
        if evidence_status == "passed" and not evidence.get("command_fingerprint_hash"):
            evidence_status = "partial"
            evidence["status"] = "partial"
            evidence.setdefault("partial_steps", []).append("command_fingerprint_hash_missing")

        module_modes = dict(facts.get("module_modes") or {})
        module_modes["dry-run"] = "real_core"
        module_modes["rollback"] = "real_core"
        rollback_twin = enrich_rollback_proof(dict(facts.get("rollback_twin") or {}), evidence)
        simulation = dict(facts.get("simulation") or {})
        attached_facts = {
            "module_modes": module_modes,
            "dry_run_job_id": job_id,
            "dry_run_status": evidence_status,
            "dry_run_evidence": redact_value(evidence),
            "command_fingerprint_hash": evidence.get("command_fingerprint_hash"),
            "dry_run_attached_at": datetime.now(UTC).isoformat(),
            "rollback_twin": rollback_twin,
            "simulation": (
                {
                    **simulation,
                    "state": (
                        "completed"
                        if evidence_status == "passed"
                        else "completed_with_findings"
                    ),
                    "evidence_status": evidence_status,
                    "completed_at": datetime.now(UTC).isoformat(),
                    "mutation_performed": False,
                }
                if simulation
                else {}
            ),
        }
        updated = self.repository.merge_facts(
            twin_id,
            attached_facts,
            event_type="dry_run_evidence_verified",
            message="Authoritative dry-run evidence identity and freshness were verified.",
            event_payload={
                "dry_run_job_id": job_id,
                "status": evidence_status,
                "command_fingerprint_hash": evidence.get("command_fingerprint_hash"),
                "age_seconds": age_seconds,
            },
        )
        self.repository.transition(
            twin_id,
            "dry_run_evidence_attached",
            message="Authoritative dry-run evidence attached to the namespace twin.",
            payload={
                "dry_run_job_id": job_id,
                "status": evidence_status,
                "qualifies": evidence_status == "passed",
            },
        )
        self.repository.transition(
            twin_id,
            "decision_calculating",
            message="Deterministic decision axes are being combined with dry-run evidence.",
            payload={"model_authority": False},
        )
        merged_facts = dict(updated.get("facts") or {})
        merged_facts.update(attached_facts)
        merged_facts["policy_twin"] = finalize_policy_twin(
            dict(merged_facts.get("policy_twin") or {}),
            dry_run_evidence=evidence,
        )
        policy_projection = (merged_facts.get("policy_twin") or {}).get("decision_projection") or {}
        projected_decision = str(policy_projection.get("level") or "amber")
        if projected_decision not in {"green", "amber", "red"}:
            projected_decision = "amber"
        runtime_assessment = merged_facts.get("runtime_behavior_twin") or {}
        runtime_effect = str(runtime_assessment.get("execution_effect") or "no_uplift")
        decision = "red" if evidence_status != "passed" else projected_decision
        if runtime_effect == "force_red":
            decision = "red"
        elif runtime_effect in {"force_amber", "require_review"} and decision == "green":
            decision = "amber"
        decision_facts = {
            **merged_facts,
            "dry_run_qualification": {
                "status": evidence_status,
                "qualifies": evidence_status == "passed",
                "failed_steps": list(evidence.get("failed_steps") or []),
                "partial_steps": list(evidence.get("partial_steps") or []),
                "precedence": (
                    "authoritative_dry_run_failed_or_partial"
                    if evidence_status != "passed"
                    else policy_projection.get("precedence_rule")
                ),
            },
            "decision_authority": "deterministic_policy_and_authoritative_dry_run",
            "runtime_qualification": {
                "risk": runtime_assessment.get("risk") or "unknown",
                "health": (runtime_assessment.get("current_health") or {}).get("status")
                or "unknown",
                "execution_effect": runtime_effect,
                "may_independently_approve": False,
                "rules_version": runtime_assessment.get("rules_version"),
            },
            "provisional": False,
        }
        canonical = json.dumps(
            decision_facts,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        report_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        final = self.repository.persist_terminal_decision(
            twin_id,
            decision=decision,
            report_hash=report_hash,
            facts=decision_facts,
        )
        return {
            "twin": self._project(final),
            "dry_run": self.dry_run(twin_id),
            "idempotent_replay": False,
        }

    def dry_run(
        self,
        twin_id: str,
        *,
        phase: str | None = None,
        step: str | None = None,
        resource: str | None = None,
        tool: str | None = None,
        outcome: str | None = None,
    ) -> dict[str, Any]:
        """Return the persisted authoritative dry-run and structured diff projection."""
        core = self.repository.get_run(twin_id)
        facts = core.get("facts") or {}
        evidence = dict(facts.get("dry_run_evidence") or {})
        if not evidence:
            return {
                "schema_version": core["schema_version"],
                "twin_id": twin_id,
                "decision_version": core["decision_version"],
                "lifecycle_status": core["lifecycle_status"],
                "freshness": self._freshness(core),
                "availability": self._availability(
                    "not_run", "Authoritative dry-run evidence has not been attached."
                ),
                "data": None,
            }
        observations = list(evidence.get("observations") or [])
        filters = {
            "phase": phase,
            "step": step,
            "resource": resource,
            "tool": tool,
            "outcome": outcome,
        }
        for key, value in filters.items():
            if not value:
                continue
            needle = str(value).lower()
            field = "resource_identity" if key == "resource" else key
            observations = [
                item for item in observations if needle in str(item.get(field) or "").lower()
            ]
        deltas, delta_total, delta_summary = self.repository.list_release_deltas(
            twin_id,
            limit=500,
            offset=0,
        )
        if resource:
            needle = str(resource).lower()
            deltas = [
                item
                for item in deltas
                if needle
                in " ".join(
                    [
                        str(item.get("resource_identity") or ""),
                        str(item.get("kind") or ""),
                        str(item.get("name") or ""),
                    ]
                ).lower()
            ]
        snapshot = dict((facts.get("module_modes") or {}).get("live_snapshot") or {})
        evidence_status = str(evidence.get("status") or "failed")
        tab_status = {
            "passed": "passed",
            "partial": "failed",
            "failed": "failed",
        }.get(evidence_status, evidence_status)
        return {
            "schema_version": core["schema_version"],
            "twin_id": twin_id,
            "decision_version": core["decision_version"],
            "lifecycle_status": core["lifecycle_status"],
            "freshness": self._freshness(core),
            "availability": self._availability(
                "available", "Authoritative dry-run and structured diff evidence is available."
            ),
            "data": {
                "dry_run_job_id": evidence["dry_run_job_id"],
                "status": tab_status,
                "qualification_status": evidence_status,
                "authoritative": True,
                "bundle_hash": core["bundle_hash"],
                "input_hash": core["input_hash"],
                "target_namespace": core["target_namespace"],
                "snapshot": {
                    "snapshot_id": snapshot.get("snapshot_id")
                    or f"snapshot_{str(snapshot.get('hash') or '')[:24]}",
                    "captured_at": snapshot.get("captured_at"),
                    "hash": snapshot.get("hash"),
                },
                "command_fingerprint_hash": evidence.get("command_fingerprint_hash"),
                "command_fingerprints": list(evidence.get("command_fingerprints") or []),
                "validations": list(evidence.get("validations") or []),
                "observations": observations,
                "observation_counts": {
                    key: sum(item.get("outcome") == key for item in observations)
                    for key in ("accepted", "rejected", "warning", "skipped", "unknown")
                },
                "structured_diff": {
                    "rows": deltas,
                    "result_count": len(deltas),
                    "unfiltered_result_count": delta_total,
                    "summary": delta_summary,
                },
                "evidence_refs": list(evidence.get("evidence_refs") or []),
                "fidelity_limitations": list(evidence.get("fidelity_limitations") or []),
                "fidelity_contract": dict(evidence.get("fidelity_contract") or {}),
                "fidelity_demonstrations": list(
                    evidence.get("fidelity_demonstrations") or []
                ),
                "artifacts": list(evidence.get("reports") or []),
                "failed_steps": list(evidence.get("failed_steps") or []),
                "partial_steps": list(evidence.get("partial_steps") or []),
                "applied_filters": {key: value for key, value in filters.items() if value},
                "model_authority": False,
                "automatic_instruction_submission": False,
                "automatic_mutation_retry": False,
            },
        }

    def rollback(self, twin_id: str) -> dict[str, Any]:
        """Return the persisted deterministic rollback readiness projection."""
        twin = self.get(twin_id)
        facts = twin.get("foundation_facts") or {}
        assessment = dict(facts.get("rollback_twin") or {})
        if not assessment:
            return {
                "schema_version": "1.0.0",
                "twin_id": twin_id,
                "decision_version": twin["decision_version"],
                "lifecycle_status": twin["lifecycle_status"],
                "freshness": twin["freshness"],
                "availability": self._availability(
                    "not_available",
                    "This historical twin predates the deterministic Rollback Twin assessment.",
                ),
                "data": None,
            }
        return {
            "schema_version": "1.0.0",
            "twin_id": twin_id,
            "decision_version": twin["decision_version"],
            "lifecycle_status": twin["lifecycle_status"],
            "freshness": twin["freshness"],
            "availability": self._availability(
                "available", "Deterministic Rollback Twin facts are available."
            ),
            "data": assessment,
        }

    def drift(self, twin_id: str) -> dict[str, Any]:
        """Return the latest persisted deterministic drift assessment."""
        twin = self.get(twin_id)
        facts = twin.get("foundation_facts") or {}
        assessment = dict(facts.get("drift_twin") or {})
        if not assessment:
            return {
                "schema_version": "1.0.0",
                "twin_id": twin_id,
                "decision_version": twin["decision_version"],
                "lifecycle_status": twin["lifecycle_status"],
                "freshness": twin["freshness"],
                "availability": self._availability(
                    "not_available",
                    "This historical twin predates deterministic Drift Twin baselines.",
                ),
                "data": None,
            }
        return {
            "schema_version": "1.0.0",
            "twin_id": twin_id,
            "decision_version": twin["decision_version"],
            "lifecycle_status": twin["lifecycle_status"],
            "freshness": twin["freshness"],
            "availability": self._availability(
                "available", "Deterministic Drift Twin facts are available."
            ),
            "data": assessment,
        }

    def refresh_drift(self, twin_id: str, *, actor_id: str) -> dict[str, Any]:
        """Collect current namespace state read-only and persist new drift evidence."""
        core = self.repository.get_run(twin_id)
        facts = core.get("facts") or {}
        baseline = dict(facts.get("drift_baseline") or {})
        if not baseline:
            raise NamespaceTwinError(
                "drift_baseline_unavailable",
                "This twin has no deterministic drift baseline; regenerate it before refresh.",
                status_code=409,
            )
        snapshot = self.live_collector.collect(
            str(core["target_namespace"]),
            correlation_id=f"twin-drift-{uuid4().hex}",
        )
        assessment = assess_drift(
            baseline,
            snapshot,
            captured_at=datetime.now(UTC),
            target_namespace=str(core["target_namespace"]),
        )
        updated = self.repository.record_drift(
            twin_id,
            assessment=assessment,
            actor_id=actor_id,
        )
        return self.drift(updated["twin_id"])

    def runtime_behavior(self, twin_id: str) -> dict[str, Any]:
        """Return the latest persisted rules-first runtime behavior assessment."""
        twin = self.get(twin_id)
        facts = twin.get("foundation_facts") or {}
        assessment = dict(facts.get("runtime_behavior_twin") or {})
        if not assessment:
            return {
                "schema_version": "1.0.0",
                "twin_id": twin_id,
                "decision_version": twin["decision_version"],
                "lifecycle_status": twin["lifecycle_status"],
                "freshness": twin["freshness"],
                "availability": self._availability(
                    "not_available",
                    "This historical twin predates the rules-first Runtime Behavior Twin.",
                ),
                "data": None,
            }
        return {
            "schema_version": "1.0.0",
            "twin_id": twin_id,
            "decision_version": twin["decision_version"],
            "lifecycle_status": twin["lifecycle_status"],
            "freshness": twin["freshness"],
            "availability": self._availability(
                "available", "Rules-first current runtime evidence is available."
            ),
            "data": assessment,
        }

    def refresh_runtime_behavior(self, twin_id: str, *, actor_id: str) -> dict[str, Any]:
        """Refresh namespace-scoped runtime signals without approving execution."""
        core = self.repository.get_run(twin_id)
        namespace = str(core["target_namespace"])
        correlation_id = f"twin-runtime-{uuid4().hex}"
        snapshot = self.live_collector.collect(namespace, correlation_id=correlation_id)
        context = self._collect_runtime_context(namespace, correlation_id=correlation_id)
        assessment = assess_runtime_behavior(
            snapshot,
            namespace_summary=context.get("namespace_summary") or {},
            events=(list(context.get("events") or []) if context.get("events_collected") else None),
            captured_at=datetime.now(UTC),
            target_namespace=namespace,
        )
        updated = self.repository.record_runtime_behavior(
            twin_id,
            assessment=assessment,
            actor_id=actor_id,
        )
        return self.runtime_behavior(updated["twin_id"])

    def release_note_validation(self, twin_id: str) -> dict[str, Any]:
        """Return persisted editorial claim validation, or explicit Not Run state."""
        twin = self.get(twin_id)
        facts = twin.get("foundation_facts") or {}
        validation = dict(facts.get("release_note_validation") or {})
        if not validation:
            return {
                "schema_version": "1.0.0",
                "twin_id": twin_id,
                "decision_version": twin["decision_version"],
                "lifecycle_status": twin["lifecycle_status"],
                "freshness": twin["freshness"],
                "availability": self._availability(
                    "not_run",
                    "Link a release-note artifact to run deterministic claim validation.",
                ),
                "data": None,
            }
        return {
            "schema_version": "1.0.0",
            "twin_id": twin_id,
            "decision_version": twin["decision_version"],
            "lifecycle_status": twin["lifecycle_status"],
            "freshness": twin["freshness"],
            "availability": self._availability(
                "available", "Deterministic release-note claim validation is available."
            ),
            "data": validation,
        }

    def mop_replay(self, twin_id: str) -> dict[str, Any]:
        """Return approved isolated replay evidence, or explicit Not Run state."""
        twin = self.get(twin_id)
        facts = twin.get("foundation_facts") or {}
        replay = dict(facts.get("mop_replay_twin") or {})
        if not replay:
            return {
                "schema_version": "1.0.0",
                "twin_id": twin_id,
                "decision_version": twin["decision_version"],
                "lifecycle_status": twin["lifecycle_status"],
                "freshness": twin["freshness"],
                "availability": self._availability(
                    "not_run",
                    "MoP replay has not run; separately approved isolated replay "
                    "infrastructure is required.",
                ),
                "data": None,
            }
        return {
            "schema_version": "1.0.0",
            "twin_id": twin_id,
            "decision_version": twin["decision_version"],
            "lifecycle_status": twin["lifecycle_status"],
            "freshness": twin["freshness"],
            "availability": self._availability(
                "available", "Authoritative isolated MoP replay evidence is available."
            ),
            "data": replay,
        }

    def record_mop_replay(
        self,
        twin_id: str,
        payload: dict[str, Any],
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        """Accept terminal replay facts only after explicit infrastructure approval."""
        core = self.repository.get_run(twin_id)
        try:
            replay = build_replay_result(
                twin_id=twin_id,
                source_namespace=core.get("source_namespace"),
                target_namespace=str(core.get("target_namespace") or ""),
                target_cluster=str(core.get("target_cluster") or "configured-cluster"),
                payload=payload,
            )
        except ReplayEvidenceError as exc:
            raise NamespaceTwinError(exc.code, exc.message, status_code=409) from exc
        before_decision = core.get("decision")
        before_version = core.get("decision_version")
        updated = self.repository.record_mop_replay(
            twin_id,
            replay=replay,
            actor_id=actor_id,
        )
        if (
            updated.get("decision") != before_decision
            or updated.get("decision_version") != before_version
        ):
            raise NamespaceTwinError(
                "replay_changed_decision_authority",
                "Replay evidence must not rewrite the persisted baseline decision.",
                status_code=500,
            )
        return self.mop_replay(updated["twin_id"])

    def validate_release_note(
        self,
        twin_id: str,
        payload: dict[str, Any],
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        """Match bounded extracted claims against deterministic twin evidence."""
        core = self.repository.get_run(twin_id)
        artifact_id = str(payload.get("release_note_artifact_id") or "").strip()
        artifact_hash = str(payload.get("release_note_artifact_hash") or "").strip().lower()
        claims = list(payload.get("claims") or [])
        extraction = dict(payload.get("extraction") or {})
        if not artifact_id:
            raise NamespaceTwinError(
                "release_note_artifact_required", "release_note_artifact_id is required."
            )
        if len(artifact_hash) != 64 or any(
            char not in "0123456789abcdef" for char in artifact_hash
        ):
            raise NamespaceTwinError(
                "invalid_release_note_hash", "release_note_artifact_hash must be SHA-256."
            )
        if len(claims) > 100:
            raise NamespaceTwinError(
                "release_note_claim_limit", "At most 100 bounded release-note claims are allowed."
            )
        facts = dict(core.get("facts") or {})
        deltas, _, _ = self.repository.list_release_deltas(twin_id, limit=10000)
        validation = validate_release_note_claims(
            twin_id=twin_id,
            artifact_id=artifact_id,
            artifact_hash=artifact_hash,
            claims=claims,
            extraction=extraction,
            facts=facts,
            deltas=deltas,
        )
        updated = self.repository.record_release_note_validation(
            twin_id,
            validation=validation,
            actor_id=actor_id,
        )
        return self.release_note_validation(updated["twin_id"])

    def _collect_runtime_context(self, namespace: str, *, correlation_id: str) -> dict[str, Any]:
        collector = getattr(self.live_collector, "collect_runtime", None)
        if not callable(collector):
            return {
                "available": False,
                "namespace_summary": {},
                "events": [],
                "events_collected": False,
                "warning": "Runtime event collection is unavailable for this collector.",
            }
        result = collector(namespace, correlation_id=correlation_id)
        return result if isinstance(result, dict) else {}

    @staticmethod
    def _parse_timestamp(value: Any) -> datetime:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError) as exc:
            raise NamespaceTwinError(
                "invalid_dry_run_timestamp",
                "Authoritative dry-run evidence has no valid completion timestamp.",
                status_code=409,
            ) from exc
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    def _get_phase4(self, twin_id: str) -> dict[str, Any]:
        return self.repository.get_run(twin_id)

    def _list_phase4(self, query: dict[str, Any]) -> dict[str, Any]:
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
        self._reconcile_authoritative_dry_run(twin_id)
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

    def audit(
        self,
        twin_id: str,
        *,
        cursor: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        """Return the append-only event ledger as a cursor-paginated audit contract."""
        bounded_limit = self._bounded_int(limit, default=25, minimum=1, maximum=100)
        offset = self._decode_cursor(cursor, sort="audit_events", direction="asc") if cursor else 0
        core = self.repository.get_run(twin_id)
        rows, total = self.repository.list_events(twin_id, limit=bounded_limit, offset=offset)
        events = [build_audit_event(row, core) for row in rows]
        next_offset = offset + len(events)
        projected = self._project(core)
        return {
            "schema_version": "1.0.0",
            "twin_id": twin_id,
            "decision_version": int(core.get("decision_version") or 0),
            "lifecycle_status": core.get("lifecycle_status"),
            "freshness": projected.get("freshness"),
            "availability": {
                "state": "available",
                "message": "Append-only redacted namespace twin audit events are available.",
                "reason_code": None,
                "retryable": False,
                "last_attempt_at": core.get("updated_at"),
            },
            "events": events,
            "page": {
                "limit": bounded_limit,
                "offset": offset,
                "result_count": total,
                "has_more": next_offset < total,
                "next_cursor": (
                    self._encode_cursor(next_offset, sort="audit_events", direction="asc")
                    if next_offset < total
                    else None
                ),
            },
            "redacted": True,
        }

    def record_execution_link(
        self,
        twin_id: str,
        payload: dict[str, Any],
        *,
        actor_id: str,
    ) -> dict[str, Any]:
        """Link execution evidence after rechecking immutable twin identity facts."""
        core = self.repository.get_run(twin_id)
        facts = dict(core.get("facts") or {})
        errors: list[str] = []
        if core.get("decision_is_final") is not True:
            errors.append("Namespace Twin decision must be final before execution linkage.")
        if int(payload.get("decision_version") or 0) != int(core.get("decision_version") or 0):
            errors.append("Namespace Twin decision version changed before execution linkage.")
        if str(payload.get("bundle_hash") or "") != str(core.get("bundle_hash") or ""):
            errors.append("Execution bundle hash does not match the Namespace Twin.")
        if payload.get("input_hash") and str(payload.get("input_hash")) != str(
            core.get("input_hash") or ""
        ):
            errors.append("Execution input hash does not match the Namespace Twin.")
        if str(payload.get("target_namespace") or "") != str(core.get("target_namespace") or ""):
            errors.append("Execution target namespace does not match the Namespace Twin.")
        canonical_dry_run = str(facts.get("dry_run_job_id") or "")
        authoritative_dry_run = str(payload.get("authoritative_dry_run_job_id") or "")
        if canonical_dry_run and authoritative_dry_run != canonical_dry_run:
            errors.append("Authoritative dry-run identity does not match the Namespace Twin.")
        canonical_fingerprint = str(facts.get("command_fingerprint_hash") or "")
        if (
            canonical_fingerprint
            and payload.get("command_fingerprint_hash")
            and str(payload.get("command_fingerprint_hash")) != canonical_fingerprint
        ):
            errors.append("Execution command fingerprint does not match the Namespace Twin.")
        if errors:
            raise NamespaceTwinError(
                "execution_link_identity_mismatch",
                "Execution outcome cannot be linked because canonical twin facts changed.",
                status_code=409,
                details={"errors": errors, "twin_id": twin_id},
            )
        link = {
            **payload,
            "twin_id": twin_id,
            "pre_execution_decision": core.get("decision"),
            "pre_execution_decision_version": core.get("decision_version"),
            "linked_at": datetime.now(UTC).isoformat(),
        }
        updated = self.repository.record_execution_link(
            twin_id,
            link=link,
            actor_id=actor_id,
        )
        return {
            "schema_version": updated["schema_version"],
            "twin_id": twin_id,
            "decision": updated.get("decision"),
            "decision_version": updated.get("decision_version"),
            "decision_is_final": updated.get("decision_is_final"),
            "execution_link": link,
            "relationships": self._project(updated).get("relationships"),
        }

    def report(self, twin_id: str) -> dict[str, Any]:
        """Build a deterministic, side-effect-free JSON report from persisted facts."""
        core = self.repository.get_run(twin_id)
        rows, _ = self.repository.list_events(twin_id, limit=100000, offset=0)
        return build_report(core, rows)

    def report_markdown(self, twin_id: str) -> str:
        """Render Markdown from the exact same structured report model as JSON."""
        return render_markdown(self.report(twin_id))

    def _cancel_phase4(self, twin_id: str, *, actor_id: str) -> dict[str, Any]:
        return self.repository.cancel(twin_id, actor_id=actor_id)

    # Phase 5A authoritative projection starts here.

    def get(self, twin_id: str) -> dict[str, Any]:
        """Restore either an active or terminal twin with server-owned projections."""
        return self._project(self._reconcile_authoritative_dry_run(twin_id))

    def list(self, query: dict[str, Any]) -> dict[str, Any]:
        limit = self._bounded_int(query.get("limit"), default=25, minimum=1, maximum=100)
        sort = str(query.get("sort") or "created_at")
        direction = str(query.get("direction") or "desc").lower()
        if sort not in {
            "created_at",
            "updated_at",
            "display_name",
            "lifecycle_status",
            "decision",
            "risk_score",
            "target_namespace",
            "bundle_name",
        }:
            raise NamespaceTwinError("invalid_sort", f"Unsupported sort field: {sort}.")
        if direction not in {"asc", "desc"}:
            raise NamespaceTwinError("invalid_sort_direction", "direction must be asc or desc.")
        cursor = str(query.get("cursor") or "")
        offset = (
            self._decode_cursor(cursor, sort=sort, direction=direction)
            if cursor
            else self._bounded_int(query.get("offset"), default=0, minimum=0, maximum=10_000_000)
        )
        created_from = self._parse_datetime(query.get("created_from"), "created_from")
        created_to = self._parse_datetime(query.get("created_to"), "created_to")

        rows, total, metrics = self.repository.list_runs_v5(
            search=str(query.get("q") or query.get("search") or "").strip() or None,
            decision=str(query.get("decision") or "").strip() or None,
            lifecycle_status=str(
                query.get("lifecycle_status") or query.get("lifecycle") or ""
            ).strip()
            or None,
            target_namespace=str(
                query.get("target_namespace") or query.get("namespace") or ""
            ).strip()
            or None,
            bundle_name=str(query.get("bundle_name") or query.get("bundle") or "").strip() or None,
            actor_id=str(query.get("actor_id") or query.get("creator") or "").strip() or None,
            freshness=str(query.get("freshness") or "").strip() or None,
            created_from=created_from,
            created_to=created_to,
            linked_execution=str(query.get("linked_execution") or "").strip() or None,
            sort=sort,
            direction=direction,
            limit=limit,
            offset=offset,
        )
        items = [self._project(row) for row in rows]
        next_offset = offset + len(items)
        previous_offset = max(0, offset - limit)
        return {
            "schema_version": "1.0.0",
            "items": items,
            "metrics": metrics,
            "page": {
                "offset": offset,
                "limit": limit,
                "result_count": total,
                "has_more": next_offset < total,
                "next_cursor": (
                    self._encode_cursor(next_offset, sort=sort, direction=direction)
                    if next_offset < total
                    else None
                ),
                "previous_cursor": (
                    self._encode_cursor(previous_offset, sort=sort, direction=direction)
                    if offset
                    else None
                ),
            },
            "applied_query": {
                key: value for key, value in query.items() if value not in (None, "", "all")
            },
        }

    def overview(self, twin_id: str) -> dict[str, Any]:
        twin = self.get(twin_id)
        facts = twin.get("foundation_facts") or {}
        reason_rows = [
            {
                "code": reason["code"],
                "title": reason["summary"],
                "detail": reason["summary"],
                "severity": reason["severity"],
                "tab": reason["tab_slug"],
                "finding": None,
            }
            for reason in twin["top_reasons"]
        ]
        return {
            "schema_version": "1.0.0",
            "twin_id": twin_id,
            "decision_version": twin["decision_version"],
            "state": "available",
            "kind": "overview",
            "title": "Overview",
            "summary": ((twin.get("final_summary") or twin["preliminary_summary"])["headline"]),
            "metrics": [
                {
                    "label": "Resources",
                    "value": int(facts.get("resource_count") or 0),
                    "tone": "info",
                },
                {
                    "label": "Relationships",
                    "value": int(facts.get("edge_count") or 0),
                    "tone": "info",
                },
                {
                    "label": "Findings",
                    "value": int(facts.get("finding_count") or 0),
                    "tone": "amber" if facts.get("finding_count") else "green",
                },
                {
                    "label": "Decision",
                    "value": twin["decision"],
                    "tone": twin["decision"]
                    if twin["decision"] in {"green", "amber", "red"}
                    else "info",
                },
            ],
            "reasons": reason_rows,
            "recommended_action": twin["recommended_action"],
            "preliminary_summary": twin["preliminary_summary"],
            "final_summary": twin["final_summary"],
            "risk": twin["risk"],
            "freshness": twin["freshness"],
            "actions": twin["actions"],
            "relationships": twin["relationships"],
            "fact_envelope": {
                "lifecycle_status": twin["lifecycle_status"],
                "visible_lifecycle": twin["visible_lifecycle"],
                "decision": twin["decision"],
                "decision_is_final": twin["decision_is_final"],
                "autonomy_eligibility": twin["autonomy_eligibility"],
                "resource_count": int(facts.get("resource_count") or 0),
                "finding_count": int(facts.get("finding_count") or 0),
                "explicit_delete_count": int(facts.get("explicit_delete_count") or 0),
            },
        }

    def release_delta(
        self,
        twin_id: str,
        *,
        action: str | None = None,
        risk: str | None = None,
        kind: str | None = None,
        cursor: str | None = None,
        limit: int = 25,
    ) -> dict[str, Any]:
        allowed_actions = {
            "create",
            "update",
            "explicit_delete",
            "no_op",
            "unknown",
            "immutable_conflict",
            "namespace_rewrite",
        }
        allowed_risks = {"low", "medium", "high", "critical", "unknown"}
        if action and action not in allowed_actions:
            raise NamespaceTwinError("invalid_delta_action", f"Unsupported delta action: {action}.")
        if risk and risk not in allowed_risks:
            raise NamespaceTwinError("invalid_delta_risk", f"Unsupported delta risk: {risk}.")
        if limit not in {25, 50, 100}:
            raise NamespaceTwinError("invalid_delta_limit", "limit must be 25, 50, or 100.")
        offset = self._decode_cursor(cursor, sort="release_delta", direction="asc") if cursor else 0
        rows, result_count, stored_summary = self.repository.list_release_deltas(
            twin_id, action=action, risk=risk, kind=kind, limit=limit, offset=offset
        )
        twin = self.get(twin_id)
        for row in rows:
            row["evidence_refs"] = [
                self._delta_evidence_ref(ref, captured_at=twin.get("updated_at"))
                for ref in row.get("evidence_refs") or []
            ]
        next_offset = offset + len(rows)
        summary = {
            key: int(stored_summary.get(key) or 0)
            for key in (
                "total",
                "create",
                "update",
                "explicit_delete",
                "no_op",
                "unknown",
                "immutable_conflict",
                "namespace_rewrite",
            )
        }
        state = "available" if summary["total"] else "empty"
        return {
            "schema_version": "1.0.0",
            "twin_id": twin_id,
            "decision_version": twin["decision_version"],
            "lifecycle_status": twin["lifecycle_status"],
            "freshness": twin["freshness"],
            "availability": self._availability(
                state,
                (
                    "Authoritative canonical Release Delta facts are available."
                    if rows
                    else (
                        "No Release Delta rows match the selected filters."
                        if summary["total"]
                        else "No planned release delta facts were produced."
                    )
                ),
            ),
            "data": {
                "summary": summary,
                "changes": rows,
                "page": {
                    "limit": limit,
                    "has_more": next_offset < result_count,
                    "next_cursor": (
                        self._encode_cursor(next_offset, sort="release_delta", direction="asc")
                        if next_offset < result_count
                        else None
                    ),
                    "result_count": result_count,
                },
                "artifacts": [],
            },
        }

    def dependency_graph(
        self,
        twin_id: str,
        *,
        kind: str | None = None,
        risk: str | None = None,
        status: str | None = None,
        namespace: str | None = None,
        relationship: str | None = None,
        confidence: str | None = None,
        edge_status: str | None = None,
        search: str | None = None,
        missing_only: bool = False,
        resource: str | None = None,
        node_cursor: str | None = None,
        edge_cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        if risk and risk not in {"low", "medium", "high", "critical", "unknown"}:
            raise NamespaceTwinError("invalid_graph_risk", f"Unsupported graph risk: {risk}.")
        if status and status not in {"present", "missing", "uncertain"}:
            raise NamespaceTwinError("invalid_graph_status", f"Unsupported node status: {status}.")
        if edge_status and edge_status not in {"valid", "missing", "uncertain"}:
            raise NamespaceTwinError(
                "invalid_graph_edge_status", f"Unsupported edge status: {edge_status}."
            )
        if relationship and relationship not in EDGE_TYPES:
            raise NamespaceTwinError(
                "invalid_graph_relationship", f"Unsupported relationship: {relationship}."
            )
        if confidence and confidence not in {"deterministic", "high", "medium", "uncertain"}:
            raise NamespaceTwinError(
                "invalid_graph_confidence", f"Unsupported confidence: {confidence}."
            )
        if limit not in {25, 50, 100}:
            raise NamespaceTwinError("invalid_graph_limit", "limit must be 25, 50, or 100.")
        node_offset = (
            self._decode_cursor(node_cursor, sort="dependency_nodes", direction="asc")
            if node_cursor
            else 0
        )
        edge_offset = (
            self._decode_cursor(edge_cursor, sort="dependency_edges", direction="asc")
            if edge_cursor
            else 0
        )
        stored = self.repository.list_dependency_graph(
            twin_id,
            kind=kind,
            risk=risk,
            status=status,
            namespace=namespace,
            relationship=relationship,
            confidence=confidence,
            edge_status=edge_status,
            search=search,
            missing_only=missing_only,
            resource=resource,
            node_limit=limit,
            node_offset=node_offset,
            edge_limit=limit,
            edge_offset=edge_offset,
        )
        twin = self.get(twin_id)
        captured_at = twin.get("updated_at")

        def evidence_id(item: Any) -> str:
            while isinstance(item, dict):
                item = item.get("evidence_id")
            return str(item or "").strip()

        def project_evidence(items: list[Any]) -> list[dict[str, Any]]:
            identifiers = list(
                dict.fromkeys(identifier for item in items if (identifier := evidence_id(item)))
            )
            return [
                {
                    "evidence_id": identifier,
                    "source": "namespace_twin_dependency_graph",
                    "captured_at": captured_at,
                    "redacted": True,
                }
                for identifier in identifiers
            ]

        for node in stored["nodes"]:
            node["evidence_refs"] = project_evidence(node.get("evidence_refs") or [])
        for edge in [*stored["edges"], *stored["table_rows"]]:
            edge["evidence_refs"] = project_evidence(edge.get("evidence_refs") or [])
        selected = stored.get("selected_context")
        if isinstance(selected, dict) and selected.get("found"):
            selected_node = selected.get("node") or {}
            selected_node["evidence_refs"] = project_evidence(
                selected_node.get("evidence_refs") or []
            )
            for key in ("inbound_edges", "outbound_edges"):
                for edge in selected.get(key) or []:
                    edge["evidence_refs"] = project_evidence(edge.get("evidence_refs") or [])
            for path in selected.get("impact_paths") or []:
                path["evidence_refs"] = project_evidence(path.get("evidence_refs") or [])

        summary = dict(stored["summary"])
        persisted_summary = (twin.get("foundation_facts") or {}).get(
            "dependency_graph_summary"
        ) or {}
        summary["cycles"] = int(persisted_summary.get("cycles") or 0)
        summary["findings"] = int(persisted_summary.get("findings") or 0)
        state = "available" if summary.get("nodes") else "empty"
        return {
            "schema_version": "1.0.0",
            "twin_id": twin_id,
            "decision_version": twin["decision_version"],
            "lifecycle_status": twin["lifecycle_status"],
            "freshness": twin["freshness"],
            "availability": self._availability(
                state,
                (
                    "Authoritative dependency facts are available."
                    if summary.get("nodes")
                    else "No dependency graph facts were produced."
                ),
            ),
            "data": {
                "summary": summary,
                "nodes": stored["nodes"],
                "edges": stored["edges"],
                "table_rows": stored["table_rows"],
                "selected_context": selected,
                "node_page": {
                    "limit": limit,
                    "result_count": stored["node_result_count"],
                    "has_more": stored["node_has_more"],
                    "next_cursor": (
                        self._encode_cursor(
                            node_offset + len(stored["nodes"]),
                            sort="dependency_nodes",
                            direction="asc",
                        )
                        if stored["node_has_more"]
                        else None
                    ),
                },
                "edge_page": {
                    "limit": limit,
                    "result_count": stored["edge_result_count"],
                    "has_more": stored["edge_has_more"],
                    "next_cursor": (
                        self._encode_cursor(
                            edge_offset + len(stored["table_rows"]),
                            sort="dependency_edges",
                            direction="asc",
                        )
                        if stored["edge_has_more"]
                        else None
                    ),
                },
                "artifacts": [],
            },
        }

    def policy(
        self,
        twin_id: str,
        *,
        severity: str | None = None,
        category: str | None = None,
        effect: str | None = None,
    ) -> dict[str, Any]:
        """Return the persisted deterministic Policy Twin assessment."""
        if severity and severity not in {
            "info",
            "low",
            "medium",
            "high",
            "critical",
            "review",
            "block",
        }:
            raise NamespaceTwinError(
                "invalid_policy_severity", f"Unsupported policy severity: {severity}."
            )
        if effect and effect not in {"allow", "approval_required", "deny"}:
            raise NamespaceTwinError(
                "invalid_policy_effect", f"Unsupported policy effect: {effect}."
            )
        twin = self.get(twin_id)
        facts = twin.get("foundation_facts") or {}
        assessment = facts.get("policy_twin") or {}
        if not assessment:
            return {
                "schema_version": "1.0.0",
                "twin_id": twin_id,
                "decision_version": twin["decision_version"],
                "lifecycle_status": twin["lifecycle_status"],
                "freshness": twin["freshness"],
                "availability": self._availability(
                    "not_available",
                    "This historical twin predates the deterministic Policy Twin assessment.",
                ),
                "data": None,
            }
        raw_findings = list(assessment.get("findings") or [])
        if category:
            raw_findings = [item for item in raw_findings if str(item.get("category")) == category]
        if effect:
            raw_findings = [item for item in raw_findings if str(item.get("effect")) == effect]
        if severity:
            raw_findings = [
                item
                for item in raw_findings
                if severity
                in {
                    str(item.get("severity")),
                    self._policy_severity(str(item.get("severity"))),
                }
            ]
        captured_at = assessment.get("evaluated_at") or twin.get("updated_at")
        findings = [
            {
                "finding_id": item["finding_id"],
                "code": item["code"],
                "title": item["title"],
                "severity": self._policy_severity(str(item.get("severity"))),
                "status": ("denied" if item.get("effect") == "deny" else "approval_required"),
                "summary": item["detail"],
                "category": item["category"],
                "policy_version": item["policy_version"],
                "resource_identity": None,
                "evidence_refs": [
                    {
                        "evidence_id": (
                            "policy_" + hashlib.sha256(str(ref).encode("utf-8")).hexdigest()[:24]
                        ),
                        "source_type": "policy",
                        "source_id": None,
                        "summary": f"Deterministic policy evidence: {str(ref)[:500]}",
                        "captured_at": captured_at,
                        "redacted": True,
                        "href": None,
                    }
                    for ref in item.get("evidence_refs") or []
                ],
            }
            for item in raw_findings
        ]
        policy_axis = assessment.get("policy_axis") or {}
        evidence_axis = assessment.get("evidence_axis") or {}
        risk_axis = assessment.get("risk_axis") or {}
        projection = assessment.get("decision_projection") or {}
        return {
            "schema_version": "1.0.0",
            "twin_id": twin_id,
            "decision_version": twin["decision_version"],
            "lifecycle_status": twin["lifecycle_status"],
            "freshness": twin["freshness"],
            "availability": self._availability(
                "available", "Authoritative deterministic Policy Twin facts are available."
            ),
            "applied_query": {
                key: value
                for key, value in {
                    "severity": severity,
                    "category": category,
                    "effect": effect,
                }.items()
                if value
            },
            "data": {
                "verdict": {"approval_required": "allow_with_approval"}.get(
                    str(policy_axis.get("verdict")), str(policy_axis.get("verdict") or "unknown")
                ),
                "policy_version": str(policy_axis.get("version") or POLICY_VERSION),
                "policy_bundle_hash": str(policy_axis.get("bundle_hash") or ""),
                "input_hash": str(assessment.get("input_hash") or twin.get("input_hash")),
                "groups": list((assessment.get("policy_bundle") or {}).get("groups") or []),
                "findings": findings,
                "passed_groups": list(assessment.get("passed_groups") or []),
                "evidence_axis": evidence_axis,
                "risk_axis": risk_axis,
                "decision_projection": projection,
                "rule_contributions": list(assessment.get("rule_contributions") or []),
                "command_fingerprint_hash": assessment.get("command_fingerprint_hash"),
                "dry_run_job_id": assessment.get("dry_run_job_id"),
                "model_authority": False,
                "artifacts": [],
            },
        }

    @staticmethod
    def _policy_severity(value: str) -> str:
        return {
            "review": "medium",
            "warning": "medium",
            "block": "high",
            "critical": "critical",
        }.get(value, value if value in {"info", "low", "medium", "high"} else "medium")

    def actions(self, twin_id: str) -> list[dict[str, Any]]:
        return self.get(twin_id)["actions"]

    def cancel(self, twin_id: str, *, actor_id: str) -> dict[str, Any]:
        return self._project(self.repository.cancel(twin_id, actor_id=actor_id))

    def _project(self, core: dict[str, Any]) -> dict[str, Any]:
        status = str(core.get("lifecycle_status") or "requested")
        decision = str(core.get("decision") or "pending")
        facts = core.get("facts") or {}
        visible = self._visible_lifecycle(status)
        freshness = self._freshness(core)
        drift_assessment = facts.get("drift_twin") or {}
        drift_baseline = drift_assessment.get("baseline") or {}
        drift_current = drift_assessment.get("current_capture") or {}
        drift_comparison_performed = drift_baseline.get("captured_at") != drift_current.get(
            "captured_at"
        ) or drift_baseline.get("hash") != drift_current.get("hash")
        if (
            drift_comparison_performed
            and bool(drift_assessment.get("execution_disabled"))
            and freshness["status"] not in {"expired", "superseded"}
        ):
            freshness = {
                **freshness,
                "status": "drifted" if bool(drift_assessment.get("material")) else "stale",
                "message": (
                    "Execution eligibility is disabled because material namespace drift "
                    "was detected."
                    if bool(drift_assessment.get("material"))
                    else "Execution eligibility is disabled until namespace drift evidence "
                    "is refreshed."
                ),
            }
        policy_assessment = facts.get("policy_twin") or {}
        runtime_assessment = facts.get("runtime_behavior_twin") or {}
        runtime_effect = str(runtime_assessment.get("execution_effect") or "no_uplift")
        risk = dict(policy_assessment.get("risk_axis") or self._risk(status, decision))
        runtime_level = str(runtime_assessment.get("risk") or "unknown")
        risk_order = {"unknown": -1, "low": 0, "medium": 1, "high": 2, "critical": 3}
        if risk_order.get(runtime_level, -1) > risk_order.get(str(risk.get("level")), -1):
            risk = {
                "level": runtime_level,
                "score": runtime_assessment.get("risk_score"),
                "source": "runtime_behavior_twin",
            }
        reasons = self._top_reasons(status, decision, facts, freshness["status"])
        if runtime_effect != "no_uplift":
            reasons.append(
                {
                    "code": "RUNTIME_RISK_REVIEW",
                    "summary": str(
                        runtime_assessment.get("summary")
                        or "Current runtime evidence requires review."
                    ),
                    "severity": "high" if runtime_effect == "force_red" else "medium",
                    "tab_slug": "runtime-behavior",
                }
            )
            reasons = reasons[:5]
        recommendation = self._recommended_action(status, decision, freshness["status"])
        if runtime_effect == "force_red":
            recommendation = (
                "Resolve critical current runtime signals and refresh the twin before execution."
            )
        elif runtime_effect in {"force_amber", "require_review"} and decision == "green":
            recommendation = (
                "Review current runtime signals and obtain bounded approval before execution."
            )
        actions = self._actions(core, freshness["status"])
        autonomy_eligibility = self._autonomy_eligibility(status, decision, freshness["status"])
        if runtime_effect == "force_red":
            autonomy_eligibility = "ineligible"
        elif runtime_effect in {"force_amber", "require_review"} and decision == "green":
            autonomy_eligibility = "approval_required"
        preliminary = {
            "status": "preliminary",
            "headline": self._preliminary_headline(visible, facts),
            "observations": [reason["summary"] for reason in reasons],
            "generated_at": core.get("updated_at"),
            "deterministic": True,
        }
        final_summary = None
        if bool(core.get("decision_is_final")) or status in {
            "failed",
            "cancelled",
            "superseded",
            "expired",
        }:
            final_summary = {
                "status": "final",
                "headline": recommendation,
                "decision": decision if core.get("decision_is_final") else status,
                "observations": [reason["summary"] for reason in reasons],
                "generated_at": core.get("completed_at") or core.get("updated_at"),
                "deterministic": True,
            }

        return {
            **core,
            "visible_lifecycle": visible,
            "risk": risk,
            "autonomy_eligibility": autonomy_eligibility,
            "recommended_action": recommendation,
            "freshness": freshness,
            "target": {
                "cluster_id": core.get("target_cluster") or "configured-cluster",
                "cluster_name": core.get("target_cluster") or "Configured cluster",
                "namespace": core.get("target_namespace"),
            },
            "bundle": {
                "bundle_id": core.get("input_hash"),
                "bundle_name": core.get("bundle_name"),
                "bundle_hash": core.get("bundle_hash"),
                "release_version": core.get("release_version") or "not_available",
                "open_href": (
                    f"/mop-execution?bundle_hash={core.get('bundle_hash')}"
                    if core.get("bundle_hash")
                    else "/mop-execution"
                ),
            },
            "created_by": core.get("actor_id"),
            "created_by_display": core.get("actor_id"),
            "relationships": {
                "dry_run_job_id": facts.get("dry_run_job_id"),
                "approval_id": facts.get("approval_id"),
                "approval_status": facts.get("approval_status") or "not_required",
                "execution_id": facts.get("execution_id"),
                "execution_status": facts.get("execution_status") or "unlinked",
                "used_for_execution": bool(facts.get("execution_id")),
            },
            "top_reasons": reasons,
            "actions": actions,
            "tab_states": {
                "overview": self._availability(
                    "available", "Authoritative Overview facts are available."
                ),
                "release-delta": self._availability(
                    "available" if int(facts.get("release_delta_count") or 0) else "empty",
                    "Authoritative canonical Release Delta facts are available."
                    if int(facts.get("release_delta_count") or 0)
                    else "No release delta facts found.",
                ),
                "dependency-graph": self._availability(
                    (
                        "available"
                        if int((facts.get("dependency_graph_summary") or {}).get("nodes") or 0)
                        else "empty"
                    ),
                    (
                        "Authoritative Dependency Graph facts are available."
                        if int((facts.get("dependency_graph_summary") or {}).get("nodes") or 0)
                        else "No dependency graph facts were produced."
                    ),
                ),
                "policy": self._availability(
                    "available" if policy_assessment else "not_available",
                    (
                        "Authoritative deterministic Policy Twin facts are available."
                        if policy_assessment
                        else "This twin predates the deterministic Policy Twin assessment."
                    ),
                ),
                "dry-run": self._availability(
                    "available" if facts.get("dry_run_evidence") else "not_run",
                    (
                        "Authoritative dry-run and diff evidence is attached."
                        if facts.get("dry_run_evidence")
                        else "Authoritative dry-run evidence has not been attached."
                    ),
                ),
                "rollback": self._availability(
                    "available" if facts.get("rollback_twin") else "not_available",
                    (
                        "Deterministic Rollback Twin facts are available."
                        if facts.get("rollback_twin")
                        else "This twin predates the deterministic Rollback Twin assessment."
                    ),
                ),
                "drift": self._availability(
                    "available" if facts.get("drift_twin") else "not_available",
                    (
                        "Deterministic Drift Twin facts are available."
                        if facts.get("drift_twin")
                        else "This twin predates deterministic Drift Twin baselines."
                    ),
                ),
                "mop-replay": self._availability(
                    "available" if facts.get("mop_replay_twin") else "not_run",
                    (
                        "Authoritative isolated MoP replay evidence is available."
                        if facts.get("mop_replay_twin")
                        else "MoP replay requires separately approved isolated infrastructure."
                    ),
                ),
                "runtime-behavior": self._availability(
                    "available" if runtime_assessment else "not_available",
                    (
                        "Rules-first current Runtime Behavior Twin facts are available."
                        if runtime_assessment
                        else "This twin predates the rules-first Runtime Behavior Twin."
                    ),
                ),
                "release-note-validation": self._availability(
                    "available" if facts.get("release_note_validation") else "not_run",
                    (
                        "Deterministic release-note claim validation is available."
                        if facts.get("release_note_validation")
                        else "Link a release-note artifact to run deterministic claim validation."
                    ),
                ),
                "audit": self._availability(
                    "available", "Authoritative lifecycle events are available."
                ),
            },
            "optional_states": {},
            "prior_decision": facts.get("prior_decision"),
            "progress_index": {
                "requested": 0,
                "generating": 1,
                "awaiting_dry_run": 2,
                "dry_run_evidence_attached": 3,
                "decision_calculating": 4,
            }.get(status, 4),
            "preliminary_summary": preliminary,
            "final_summary": final_summary,
            "foundation_facts": facts,
        }

    @staticmethod
    def _visible_lifecycle(status: str) -> str:
        return {
            "requested": "preparing",
            "bundle_validating": "preparing",
            "normalizing": "generating",
            "snapshot_collecting": "generating",
            "rendering": "generating",
            "graph_building": "analyzing",
            "diffing": "analyzing",
            "policy_checking": "analyzing",
            "generating": "generating",
            "awaiting_dry_run": "awaiting_dry_run",
            "dry_run_evidence_attached": "finalizing",
            "decision_calculating": "finalizing",
            "green": "ready",
            "amber": "review_required",
            "red": "blocked",
            "superseded": "blocked",
            "expired": "blocked",
        }.get(status, status)

    @staticmethod
    def _delta_summary(rows: list[dict[str, Any]]) -> dict[str, int]:
        summary = {
            key: 0
            for key in (
                "create",
                "update",
                "explicit_delete",
                "no_op",
                "unknown",
                "immutable_conflict",
                "namespace_rewrite",
            )
        }
        for row in rows:
            action = str(row.get("action") or "unknown")
            summary[action if action in summary else "unknown"] += 1
        return {"total": len(rows), **summary}

    @staticmethod
    def _delta_evidence_ref(reference: str, *, captured_at: Any) -> dict[str, Any]:
        source_type = "kubernetes" if reference.startswith("bosgenesis-k8s") else "bundle"
        digest = hashlib.sha256(reference.encode("utf-8")).hexdigest()[:24]
        return {
            "evidence_id": f"evidence_{digest}",
            "source_type": source_type,
            "source_id": None,
            "summary": (
                "Namespace-scoped Kubernetes snapshot evidence."
                if source_type == "kubernetes"
                else f"Bundle manifest evidence: {reference[:300]}"
            ),
            "captured_at": captured_at,
            "redacted": True,
            "href": None,
        }

    @staticmethod
    def _availability(state: str, message: str) -> dict[str, Any]:
        return {
            "state": state,
            "message": message,
            "reason_code": None,
            "retryable": state == "failed",
            "last_attempt_at": None,
        }

    @staticmethod
    def _freshness(core: dict[str, Any]) -> dict[str, Any]:
        status = str(core.get("lifecycle_status") or "")
        expires_at = NamespaceTwinService._parse_datetime_value(core.get("expires_at"))
        now = datetime.now(UTC)
        if status == "superseded":
            freshness_status = "superseded"
            message = "A newer twin generation superseded this version."
        elif status == "expired" or (expires_at and expires_at <= now):
            freshness_status = "expired"
            message = "This twin is outside its execution freshness window."
        elif expires_at and expires_at <= now + timedelta(minutes=30):
            freshness_status = "approaching_expiry"
            message = "This twin is approaching the end of its freshness window."
        else:
            freshness_status = "fresh"
            message = "The persisted twin facts are inside their configured freshness window."
        return {
            "status": freshness_status,
            "captured_at": core.get("updated_at"),
            "expires_at": core.get("expires_at"),
            "superseded_by": core.get("superseded_by"),
            "message": message,
        }

    @staticmethod
    def _risk(status: str, decision: str) -> dict[str, Any]:
        if decision == "green":
            return {"level": "low", "score": 15}
        if decision == "amber":
            return {"level": "medium", "score": 55}
        if decision == "red":
            return {"level": "high", "score": 90}
        if status == "failed":
            return {"level": "high", "score": 85}
        return {"level": "unknown", "score": None}

    @staticmethod
    def _top_reasons(
        status: str,
        decision: str,
        facts: dict[str, Any],
        freshness: str,
    ) -> list[dict[str, Any]]:
        if decision in {"green", "amber", "red"}:
            reasons = [
                {
                    "code": f"DECISION_{decision.upper()}",
                    "summary": f"The deterministic policy decision is {decision}.",
                    "severity": "info"
                    if decision == "green"
                    else "medium"
                    if decision == "amber"
                    else "high",
                    "tab_slug": "overview",
                }
            ]
        elif status == "failed":
            reasons = [
                {
                    "code": "GENERATION_FAILED",
                    "summary": "Twin generation failed safely before a final policy decision.",
                    "severity": "high",
                    "tab_slug": "audit",
                }
            ]
        elif status in {"cancelled", "superseded", "expired"}:
            reasons = [
                {
                    "code": status.upper(),
                    "summary": f"The twin lifecycle is {status}; it is historical only.",
                    "severity": "medium",
                    "tab_slug": "overview",
                }
            ]
        else:
            reasons = [
                {
                    "code": "REAL_FACTS_PERSISTED",
                    "summary": "Bundle facts and lifecycle are persisted by the execution agent.",
                    "severity": "info",
                    "tab_slug": "overview",
                },
                {
                    "code": "AUTHORITATIVE_DRY_RUN_PENDING",
                    "summary": "A final decision requires authoritative dry-run evidence.",
                    "severity": "medium",
                    "tab_slug": "dry-run",
                },
            ]
        if int(facts.get("explicit_delete_count") or 0):
            reasons.append(
                {
                    "code": "EXPLICIT_DELETE_REVIEW",
                    "summary": (
                        f"{int(facts.get('explicit_delete_count') or 0)} explicit "
                        "delete step(s) require review."
                    ),
                    "severity": "medium",
                    "tab_slug": "policy",
                }
            )
        if freshness in {"expired", "superseded"}:
            reasons.append(
                {
                    "code": f"FRESHNESS_{freshness.upper()}",
                    "summary": f"Freshness state {freshness} blocks execution eligibility.",
                    "severity": "high",
                    "tab_slug": "drift",
                }
            )
        return reasons[:5]

    @staticmethod
    def _recommended_action(status: str, decision: str, freshness: str) -> str:
        if freshness in {"expired", "superseded", "stale", "drifted"}:
            return "Regenerate the namespace twin before requesting approval or execution."
        if decision == "green":
            return (
                "Review the final evidence, then start execution within the current "
                "freshness window."
            )
        if decision == "amber":
            return "Review the findings and request bounded human approval before execution."
        if decision == "red":
            return (
                "Open the blocking findings and regenerate after remediation; "
                "execution is disabled."
            )
        if status == "failed":
            return "Review the failure event and regenerate from a valid bundle source."
        if status == "cancelled":
            return "Generation was cancelled. Regenerate when the operator is ready."
        if status == "awaiting_dry_run":
            return "Run or attach the authoritative dry-run to calculate a final decision."
        return "Wait for deterministic twin generation to reach the dry-run gate."

    @staticmethod
    def _autonomy_eligibility(status: str, decision: str, freshness: str) -> str:
        if freshness in {"expired", "superseded", "stale", "drifted"}:
            return "ineligible"
        if decision == "green":
            return "eligible"
        if decision == "amber":
            return "approval_required"
        if decision == "red" or status in {"failed", "cancelled"}:
            return "ineligible"
        return "pending"

    @staticmethod
    def _preliminary_headline(visible: str, facts: dict[str, Any]) -> str:
        return (
            f"Twin is {visible}; {int(facts.get('resource_count') or 0)} resource fact(s) "
            f"and {int(facts.get('finding_count') or 0)} finding(s) are persisted."
        )

    def _actions(self, core: dict[str, Any], freshness: str) -> list[dict[str, Any]]:
        status = str(core.get("lifecycle_status") or "")
        decision = str(core.get("decision") or "pending")
        active = status in {"requested", "generating", "awaiting_dry_run", "decision_calculating"}
        historical = status in {"superseded", "expired"}
        stale = freshness in {"expired", "superseded", "stale", "drifted"}
        report_ready = bool(core.get("report_hash"))
        runtime_assessment = (core.get("facts") or {}).get("runtime_behavior_twin") or {}
        runtime_effect = str(runtime_assessment.get("execution_effect") or "no_uplift")
        runtime_blocks_execution = runtime_effect in {
            "force_red",
            "force_amber",
            "require_review",
        }
        twin_id = core["twin_id"]
        actions = [
            self._eligibility_action(
                "open_twin", "Open Twin", True, "eligible", f"/digital-twins/{twin_id}"
            ),
            self._eligibility_action(
                "open_bundle",
                "Open Bundle",
                True,
                "eligible",
                f"/mop-execution?bundle_hash={core.get('bundle_hash')}",
            ),
            self._eligibility_action(
                "cancel_generation",
                "Cancel Generation",
                active,
                "eligible" if active else "running",
                f"/api/digital-twins/{twin_id}/cancel",
                method="POST",
                confirmation=True,
            ),
            self._eligibility_action(
                "download_report",
                "Download Report",
                report_ready,
                "eligible" if report_ready else "not_created",
                f"/api/digital-twins/{twin_id}/report",
            ),
            self._eligibility_action(
                "export_evidence",
                "Export Evidence",
                True,
                "eligible",
                f"/api/digital-twins/{twin_id}/events",
            ),
            self._eligibility_action(
                "refresh_drift",
                "Refresh Drift",
                bool((core.get("facts") or {}).get("drift_baseline")),
                (
                    "eligible"
                    if bool((core.get("facts") or {}).get("drift_baseline"))
                    else "not_available"
                ),
                f"/api/digital-twins/{twin_id}/drift/refresh",
                method="POST",
            ),
            self._eligibility_action(
                "refresh_runtime_behavior",
                "Refresh Runtime Behavior",
                bool(runtime_assessment),
                "eligible" if runtime_assessment else "not_available",
                f"/api/digital-twins/{twin_id}/runtime-behavior/refresh",
                method="POST",
            ),
            self._eligibility_action(
                "request_approval",
                "Request Approval",
                (decision == "amber" or runtime_effect in {"force_amber", "require_review"})
                and not stale,
                (
                    "eligible"
                    if (decision == "amber" or runtime_effect in {"force_amber", "require_review"})
                    and not stale
                    else "approval_required"
                ),
                f"/approvals?twin_id={twin_id}",
            ),
            self._eligibility_action(
                "start_execution",
                "Start Execution",
                decision == "green"
                and not stale
                and not historical
                and not runtime_blocks_execution,
                "eligible"
                if decision == "green" and not stale and not runtime_blocks_execution
                else (
                    "runtime_review_required"
                    if runtime_blocks_execution
                    else ("decision_red" if decision == "red" else "approval_required")
                ),
                f"/mop-execution?twin_id={twin_id}",
            ),
            self._eligibility_action(
                "regenerate_twin",
                "Regenerate Twin",
                stale or status in {"failed", "cancelled"},
                "eligible" if stale or status in {"failed", "cancelled"} else "unsupported",
                f"/digital-twins?regenerate={twin_id}",
            ),
        ]
        return actions

    @staticmethod
    def _eligibility_action(
        code: str,
        label: str,
        enabled: bool,
        reason_code: str,
        href: str,
        *,
        method: str = "GET",
        confirmation: bool = False,
    ) -> dict[str, Any]:
        return {
            "code": code,
            "label": label,
            "enabled": enabled,
            "visible": True,
            "method": method,
            "href": href,
            "reason_code": reason_code,
            "disabled_reason": None if enabled else reason_code.replace("_", " "),
            "requires_confirmation": confirmation,
        }

    @staticmethod
    def _encode_cursor(offset: int, *, sort: str, direction: str) -> str:
        raw = json.dumps(
            {"offset": offset, "sort": sort, "direction": direction},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str, *, sort: str, direction: str) -> int:
        try:
            padded = cursor + ("=" * (-len(cursor) % 4))
            payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
            if payload.get("sort") != sort or payload.get("direction") != direction:
                raise ValueError("cursor query mismatch")
            return max(0, int(payload["offset"]))
        except Exception as exc:
            raise NamespaceTwinError(
                "invalid_cursor",
                "cursor is invalid or belongs to a different sort order.",
            ) from exc

    @staticmethod
    def _bounded_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
        try:
            parsed = int(value if value not in (None, "") else default)
        except (TypeError, ValueError) as exc:
            raise NamespaceTwinError("invalid_pagination", "limit must be an integer.") from exc
        return max(minimum, min(parsed, maximum))

    @staticmethod
    def _parse_datetime(value: Any, field: str) -> datetime | None:
        if value in (None, ""):
            return None
        parsed = NamespaceTwinService._parse_datetime_value(value)
        if parsed is None:
            raise NamespaceTwinError(
                "invalid_date_filter", f"{field} must be an ISO-8601 timestamp."
            )
        if field == "created_to" and isinstance(value, str) and len(value.strip()) == 10:
            return parsed + timedelta(days=1) - timedelta(microseconds=1)
        return parsed

    @staticmethod
    def _parse_datetime_value(value: Any) -> datetime | None:
        if not isinstance(value, str) or not value.strip():
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except ValueError:
            return None

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
        files = artifact_index_file_entries(index)
        artifact_root = bundle.artifact_index_root_path or bundle.root_path
        for entry in files:
            path_value = entry.get("path") if isinstance(entry, dict) else None
            if not isinstance(path_value, str):
                raise NamespaceTwinError(
                    "artifact_index_invalid", "Every artifact index entry needs a path."
                )
            path = artifact_root / path_value
            if not path.exists() or not path.is_file():
                raise NamespaceTwinError(
                    "artifact_index_file_missing",
                    f"Artifact index file is missing: {path_value}.",
                )
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
            "checksum_coverage": (
                "none" if verified == 0 else "complete" if checksummed == verified else "partial"
            ),
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

    def _ignored_manifest_refs(self, bundle: ArtifactBundle) -> set[str]:
        ignored: set[str] = set()
        for manifest in bundle.manifests:
            resource = self._resource_record(
                manifest.content,
                path=manifest.path,
                document_index=manifest.document_index,
                target_namespace=bundle.target_namespace,
                source="bundle_manifest",
            )
            if self._excluded_configmap(resource):
                ignored.add(Path(manifest.path).as_posix())
        return ignored

    def _resources(
        self,
        bundle: ArtifactBundle,
        *,
        snapshot: Any,
        planned_helm_installs: set[str],
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        rollback_evidence_paths = self._rollback_evidence_manifest_paths(bundle)
        for manifest in bundle.manifests:
            if Path(manifest.path).as_posix() in rollback_evidence_paths:
                continue
            candidates.append(
                self._resource_record(
                    manifest.content,
                    path=manifest.path,
                    document_index=manifest.document_index,
                    target_namespace=bundle.target_namespace,
                    source="bundle_manifest",
                )
            )

        candidates.extend(
            self._rendered_helm_resources(
                bundle,
                planned_helm_installs=planned_helm_installs,
            )
        )

        selected: dict[str, dict[str, Any]] = {}
        for resource in candidates:
            manifest = (resource.get("payload_redacted") or {}).get("manifest") or {}
            if self._excluded_configmap(resource):
                continue
            if self._excluded_helm_resource(
                manifest,
                snapshot=snapshot,
                planned_helm_installs=planned_helm_installs,
            ):
                continue
            selected.setdefault(str(resource["stable_identity"]), resource)
        return list(selected.values())

    @staticmethod
    def _resource_record(
        manifest: dict[str, Any],
        *,
        path: str,
        document_index: int,
        target_namespace: str,
        source: str,
    ) -> dict[str, Any]:
        content = deepcopy(manifest)
        metadata = content.setdefault("metadata", {})
        api_version = str(content.get("apiVersion") or "")
        kind = str(content.get("kind") or "")
        name = str(metadata.get("name") or "")
        namespace = str(metadata.get("namespace") or target_namespace)
        metadata["namespace"] = namespace
        identity = f"{api_version}:{kind}:{namespace}:{name}"
        return {
            "resource_id": f"twinres_{uuid4().hex}",
            "stable_identity": identity,
            "api_version": api_version,
            "kind": kind,
            "name": name,
            "namespace": namespace,
            "payload_redacted": {
                "path": path,
                "document_index": document_index,
                "manifest": canonicalize_kubernetes_object(content),
                "source": source,
                "status": "present",
                "risk": "low",
                "evidence_refs": [path],
            },
        }

    @classmethod
    def _rendered_helm_resources(
        cls,
        bundle: ArtifactBundle,
        *,
        planned_helm_installs: set[str],
    ) -> list[dict[str, Any]]:
        if not planned_helm_installs or not bundle.artifact_index_json:
            return []
        paths = [
            item
            for item in bundle.artifact_index_json.get("rendered_manifests", [])
            if isinstance(item, str)
        ]
        for entry in bundle.artifact_index_json.get("files", []) or []:
            if not isinstance(entry, dict):
                continue
            role = str(entry.get("role") or "").casefold()
            path = entry.get("path")
            if isinstance(path, str) and "rendered" in role and path not in paths:
                paths.append(path)

        root = bundle.artifact_index_root_path or bundle.root_path
        root_resolved = root.resolve()
        planned = {item.casefold() for item in planned_helm_installs}
        resources: list[dict[str, Any]] = []
        for relative_path in paths:
            candidate = (root / relative_path).resolve()
            try:
                candidate.relative_to(root_resolved)
            except ValueError:
                continue
            if not candidate.is_file():
                continue
            try:
                documents = list(yaml.safe_load_all(candidate.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, yaml.YAMLError):
                continue
            display_path = cls._bundle_relative_path(bundle, candidate)
            document_index = 0
            for document in documents:
                expanded = (
                    document.get("items") or []
                    if isinstance(document, dict) and document.get("kind") == "List"
                    else [document]
                )
                for item in expanded:
                    if not isinstance(item, dict):
                        continue
                    kind = str(item.get("kind") or "")
                    metadata = item.get("metadata")
                    if (
                        kind not in TWIN_RENDERED_HELM_KINDS
                        or not isinstance(metadata, dict)
                        or not metadata.get("name")
                    ):
                        continue
                    release = cls._resource_helm_release(item)
                    if not release or release.casefold() not in planned:
                        continue
                    resources.append(
                        cls._resource_record(
                            item,
                            path=display_path,
                            document_index=document_index,
                            target_namespace=bundle.target_namespace,
                            source="helm_rendered_manifest",
                        )
                    )
                    document_index += 1
        return resources

    @staticmethod
    def _bundle_relative_path(bundle: ArtifactBundle, path: Path) -> str:
        try:
            return path.relative_to(bundle.root_path.resolve()).as_posix()
        except ValueError:
            return path.name

    def _excluded_configmap(self, resource: dict[str, Any]) -> bool:
        if str(resource.get("kind") or "") != "ConfigMap":
            return False
        name = str(resource.get("name") or "").casefold()
        names = {item.casefold() for item in self.configmap_exclude_names}
        prefixes = tuple(item.casefold() for item in self.configmap_exclude_prefixes)
        return name in names or any(name.startswith(prefix) for prefix in prefixes)

    @classmethod
    def _excluded_helm_resource(
        cls,
        manifest: dict[str, Any],
        *,
        snapshot: Any,
        planned_helm_installs: set[str],
    ) -> bool:
        release = cls._resource_helm_release(manifest)
        if not release:
            return False
        folded = release.casefold()
        if any(
            folded.startswith(prefix.casefold())
            for prefix in getattr(snapshot, "ignored_helm_prefixes", ())
        ):
            return True
        if not getattr(snapshot, "helm_inventory_available", False):
            return False
        installed = {
            item.casefold() for item in getattr(snapshot, "installed_helm_releases", set())
        }
        planned = {item.casefold() for item in planned_helm_installs}
        return folded not in installed and folded not in planned

    @staticmethod
    def _resource_helm_release(manifest: dict[str, Any]) -> str | None:
        metadata = manifest.get("metadata") if isinstance(manifest.get("metadata"), dict) else {}
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

    @staticmethod
    def _planned_helm_install_releases(bundle: ArtifactBundle) -> set[str]:
        releases: set[str] = set()
        pattern = re.compile(r"\bhelm\s+(?:upgrade\s+--install|install)\s+([^\s]+)", re.I)
        for phase in bundle.machine_plan.phases:
            for step in phase.steps:
                commands = "\n".join(command.command for command in step.commands)
                intentional_install = step.type == "helm_install" or bool(pattern.search(commands))
                if not intentional_install:
                    continue
                release = str(step.metadata.get("release_name") or "").strip()
                if not release:
                    match = pattern.search(commands)
                    release = match.group(1).strip("'\"") if match else ""
                if release:
                    releases.add(release)
        return releases

    @staticmethod
    def _rollback_evidence_manifest_paths(bundle: ArtifactBundle) -> set[str]:
        """Keep previous-state evidence out of the desired resource projection."""
        paths: set[str] = set()
        for entry in artifact_index_file_entries(bundle.artifact_index_json or {}):
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            role = str(entry.get("role") or "").lower()
            if isinstance(path, str) and (
                "previous" in role or "rollback" in role or "baseline" in role
            ):
                paths.add(Path(path).as_posix())

        for phase in bundle.machine_plan.phases:
            phase_text = " ".join(
                [str(phase.phase_id), str(phase.title), str(phase.objective)]
            ).lower()
            for step in phase.steps:
                step_text = " ".join(
                    [phase_text, str(step.step_id), str(step.title), str(step.type)]
                ).lower()
                if "rollback" in step_text or "revert" in step_text or "restore" in step_text:
                    paths.update(Path(path).as_posix() for path in step.manifest_refs)
        return paths

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
