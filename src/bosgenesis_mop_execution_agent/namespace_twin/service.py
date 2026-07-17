"""Real, provisional namespace twin lifecycle foundation."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from bosgenesis_mop_execution_agent.artifacts.bundle_validator import (
    artifact_index_file_entries,
    load_and_validate_bundle,
)
from bosgenesis_mop_execution_agent.artifacts.models import ArtifactBundle, BundleSource
from bosgenesis_mop_execution_agent.namespace_twin.canonicalization import (
    canonicalize_kubernetes_object,
)
from bosgenesis_mop_execution_agent.namespace_twin.delta import calculate_release_delta
from bosgenesis_mop_execution_agent.namespace_twin.dependency_graph import (
    EDGE_TYPES,
    build_dependency_graph,
)
from bosgenesis_mop_execution_agent.namespace_twin.live_snapshot import (
    KubernetesLiveSnapshotCollector,
    LiveSnapshotCollector,
)
from bosgenesis_mop_execution_agent.namespace_twin.models import (
    POLICY_VERSION,
    RISK_RULE_VERSION,
)
from bosgenesis_mop_execution_agent.namespace_twin.persistence import (
    NamespaceTwinPersistenceError,
    NamespaceTwinRepository,
)
from bosgenesis_mop_execution_agent.namespace_twin.policy_twin import evaluate_policy_twin
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

    def __init__(
        self,
        repository: NamespaceTwinRepository | None = None,
        live_collector: LiveSnapshotCollector | None = None,
    ) -> None:
        self.repository = repository or NamespaceTwinRepository()
        self.live_collector = live_collector or KubernetesLiveSnapshotCollector.from_environment()
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
            planned_resources = self._resources(bundle)
            resources, edges, graph_findings, graph_summary = build_dependency_graph(
                bundle, planned_resources
            )
            explicit_deletes = self._explicit_deletes(bundle)
            findings = self._findings(explicit_deletes) + graph_findings
            snapshot = self.live_collector.collect(
                target_namespace, correlation_id=f"twin-snapshot-{uuid4().hex}"
            )
            deltas = calculate_release_delta(
                planned_resources,
                snapshot,
                target_namespace=target_namespace,
                explicit_deletes=explicit_deletes,
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
            "module_modes": {
                "release_delta_count": len(deltas),
                "release_delta_summary": self._delta_summary(deltas),
                "live_snapshot": {
                    "available": snapshot.available,
                    "complete_kinds": sorted(snapshot.complete_kinds),
                    "resource_count": len(snapshot.resources),
                    "evidence_refs": snapshot.evidence_refs,
                    "warning": snapshot.warning,
                },
                "overview": "real_core",
                "release-delta": "real_core",
                "dependency-graph": "real_core",
                "policy": "real_core",
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
            deltas=deltas,
        )
        if not created:
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
        return self._project(run)

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

    def _cancel_phase4(self, twin_id: str, *, actor_id: str) -> dict[str, Any]:
        return self.repository.cancel(twin_id, actor_id=actor_id)

    # Phase 5A authoritative projection starts here.

    def get(self, twin_id: str) -> dict[str, Any]:
        """Restore either an active or terminal twin with server-owned projections."""
        return self._project(self.repository.get_run(twin_id))

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
        policy_assessment = facts.get("policy_twin") or {}
        risk = (
            self._risk(status, decision)
            if bool(core.get("decision_is_final"))
            else dict(policy_assessment.get("risk_axis") or self._risk(status, decision))
        )
        reasons = self._top_reasons(status, decision, facts, freshness["status"])
        recommendation = self._recommended_action(status, decision, freshness["status"])
        actions = self._actions(core, freshness["status"])
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
            "autonomy_eligibility": self._autonomy_eligibility(
                status, decision, freshness["status"]
            ),
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
                    "not_run", "Authoritative dry-run evidence is not attached to this slice."
                ),
                "rollback": self._availability(
                    "not_available", "Rollback evidence is not implemented in Slice 5A."
                ),
                "drift": self._availability(
                    "not_run", "Drift evaluation is not implemented in Slice 5A."
                ),
                "mop-replay": self._availability(
                    "not_run", "MoP replay is not implemented in Slice 5A."
                ),
                "runtime-behavior": self._availability(
                    "not_available", "Runtime behavior is not implemented in Slice 5A."
                ),
                "release-note-validation": self._availability(
                    "not_run", "Release-note validation is not implemented in Slice 5A."
                ),
                "audit": self._availability(
                    "available", "Authoritative lifecycle events are available."
                ),
            },
            "optional_states": {
                slug: "mock_non_authoritative"
                for slug in (
                    "dry-run",
                    "rollback",
                    "drift",
                    "mop-replay",
                    "runtime-behavior",
                    "release-note-validation",
                )
            },
            "prior_decision": None,
            "progress_index": {
                "requested": 0,
                "generating": 1,
                "awaiting_dry_run": 2,
                "decision_calculating": 3,
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
                "request_approval",
                "Request Approval",
                decision == "amber" and not stale,
                "eligible" if decision == "amber" and not stale else "approval_required",
                f"/approvals?twin_id={twin_id}",
            ),
            self._eligibility_action(
                "start_execution",
                "Start Execution",
                decision == "green" and not stale and not historical,
                "eligible"
                if decision == "green" and not stale
                else ("decision_red" if decision == "red" else "approval_required"),
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

    @staticmethod
    def _resources(bundle: ArtifactBundle) -> list[dict[str, Any]]:
        resources: list[dict[str, Any]] = []
        for manifest in bundle.manifests:
            namespace = (
                None
                if manifest.scope == "cluster"
                else manifest.namespace or bundle.target_namespace
            )
            identity = (
                f"{manifest.api_version}:{manifest.kind}:{namespace or '_cluster'}:{manifest.name}"
            )
            resources.append(
                {
                    "resource_id": f"twinres_{uuid4().hex}",
                    "stable_identity": identity,
                    "api_version": manifest.api_version,
                    "kind": manifest.kind,
                    "name": manifest.name,
                    "namespace": namespace,
                    "payload_redacted": {
                        "path": manifest.path,
                        "document_index": manifest.document_index,
                        "manifest": canonicalize_kubernetes_object(manifest.content),
                        "source": "rendered_manifest",
                        "status": "present",
                        "risk": "low",
                        "evidence_refs": [manifest.path],
                    },
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
