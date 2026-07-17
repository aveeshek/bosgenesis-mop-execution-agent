"""SQLAlchemy persistence for durable namespace twin runs."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from sqlalchemy import and_, case, create_engine, func, or_, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from bosgenesis_mop_execution_agent.namespace_twin.dependency_graph import (
    stable_edge_id,
    stable_node_id,
)
from bosgenesis_mop_execution_agent.namespace_twin.models import (
    ACTIVE_STATES,
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    NamespaceTwinBase,
    NamespaceTwinDecisionRow,
    NamespaceTwinDeltaRow,
    NamespaceTwinEdgeRow,
    NamespaceTwinEventRow,
    NamespaceTwinFindingRow,
    NamespaceTwinResourceRow,
    NamespaceTwinRunRow,
)
from bosgenesis_mop_execution_agent.security import redact_value


class NamespaceTwinPersistenceError(RuntimeError):
    """Typed repository failure used by the API boundary."""

    def __init__(self, code: str, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def namespace_twin_database_url() -> str:
    configured = os.getenv("NAMESPACE_TWIN_DATABASE_URL") or os.getenv("DATABASE_URL")
    if configured:
        if configured.startswith("postgresql+asyncpg://"):
            return configured.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
        if configured.startswith("postgresql://"):
            return configured.replace("postgresql://", "postgresql+psycopg://", 1)
        return configured
    path = Path(os.getenv("MOP_EXECUTION_STATE_DIR", "var")) / "namespace-twins.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return f"sqlite+pysqlite:///{path.resolve().as_posix()}"


class NamespaceTwinRepository:
    """Transactional twin repository with ordered events and scoped idempotency."""

    def __init__(self, database_url: str | None = None) -> None:
        url = database_url or namespace_twin_database_url()
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(url, pool_pre_ping=True, connect_args=connect_args)
        self.sessions = sessionmaker(self.engine, expire_on_commit=False)
        self._event_lock = RLock()
        NamespaceTwinBase.metadata.create_all(self.engine)

    def create_run(
        self,
        run_values: dict[str, Any],
        *,
        resources: list[dict[str, Any]],
        edges: list[dict[str, Any]],
        findings: list[dict[str, Any]],
        deltas: list[dict[str, Any]] | None = None,
    ) -> tuple[dict[str, Any], bool]:
        with self.sessions.begin() as session:
            existing = session.scalar(
                select(NamespaceTwinRunRow).where(
                    NamespaceTwinRunRow.actor_id == run_values["actor_id"],
                    NamespaceTwinRunRow.target_namespace == run_values["target_namespace"],
                    NamespaceTwinRunRow.idempotency_key == run_values["idempotency_key"],
                )
            )
            if existing:
                return self._run_dict(existing), False
            row = NamespaceTwinRunRow(**run_values)
            session.add(row)
            try:
                session.flush()
            except IntegrityError as exc:
                raise NamespaceTwinPersistenceError(
                    "idempotency_conflict",
                    "A twin already exists for this actor, target, and idempotency key.",
                ) from exc
            for item in resources:
                session.add(NamespaceTwinResourceRow(twin_id=row.twin_id, **item))
            for item in edges:
                session.add(NamespaceTwinEdgeRow(twin_id=row.twin_id, **item))
            for item in findings:
                session.add(NamespaceTwinFindingRow(twin_id=row.twin_id, **item))
            for item in deltas or []:
                session.add(NamespaceTwinDeltaRow(twin_id=row.twin_id, **item))
            self._append_event_in_session(
                session,
                row.twin_id,
                "twin_requested",
                "Namespace twin generation requested.",
                {
                    "lifecycle_status": row.lifecycle_status,
                    "input_hash": row.input_hash,
                    "policy_version": row.policy_version,
                    "risk_rule_version": row.risk_rule_version,
                },
            )
            return self._run_dict(row), True

    def list_release_deltas(
        self,
        twin_id: str,
        *,
        action: str | None = None,
        risk: str | None = None,
        kind: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
        with self.sessions() as session:
            if session.get(NamespaceTwinRunRow, twin_id) is None:
                raise NamespaceTwinPersistenceError(
                    "twin_not_found", f"Namespace twin {twin_id} was not found.", status_code=404
                )
            statement = select(NamespaceTwinDeltaRow).where(
                NamespaceTwinDeltaRow.twin_id == twin_id
            )
            if action:
                statement = statement.where(NamespaceTwinDeltaRow.action == action)
            if risk:
                statement = statement.where(NamespaceTwinDeltaRow.risk == risk)
            if kind:
                statement = statement.where(NamespaceTwinDeltaRow.kind == kind)
            filtered = statement.subquery()
            total = int(session.scalar(select(func.count()).select_from(filtered)) or 0)
            rows = session.scalars(
                statement.order_by(
                    NamespaceTwinDeltaRow.kind,
                    NamespaceTwinDeltaRow.namespace,
                    NamespaceTwinDeltaRow.name,
                    NamespaceTwinDeltaRow.action,
                )
                .offset(offset)
                .limit(limit)
            ).all()
            summary_rows = session.execute(
                select(NamespaceTwinDeltaRow.action, func.count())
                .where(NamespaceTwinDeltaRow.twin_id == twin_id)
                .group_by(NamespaceTwinDeltaRow.action)
            ).all()
            summary = {str(row[0]): int(row[1]) for row in summary_rows}
            summary["total"] = sum(summary.values())
            return [self._delta_dict(row) for row in rows], total, summary

    def list_dependency_graph(
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
        node_limit: int = 100,
        node_offset: int = 0,
        edge_limit: int = 100,
        edge_offset: int = 0,
    ) -> dict[str, Any]:
        with self.sessions() as session:
            if session.get(NamespaceTwinRunRow, twin_id) is None:
                raise NamespaceTwinPersistenceError(
                    "twin_not_found", f"Namespace twin {twin_id} was not found.", status_code=404
                )
            node_rows = session.scalars(
                select(NamespaceTwinResourceRow)
                .where(NamespaceTwinResourceRow.twin_id == twin_id)
                .order_by(
                    NamespaceTwinResourceRow.kind,
                    NamespaceTwinResourceRow.namespace,
                    NamespaceTwinResourceRow.name,
                )
            ).all()
            edge_rows = session.scalars(
                select(NamespaceTwinEdgeRow)
                .where(NamespaceTwinEdgeRow.twin_id == twin_id)
                .order_by(
                    NamespaceTwinEdgeRow.edge_type,
                    NamespaceTwinEdgeRow.source_identity,
                    NamespaceTwinEdgeRow.target_identity,
                )
            ).all()
            nodes = [self._resource_dict(row) for row in node_rows]
            by_identity = {node["resource_identity"]: node for node in nodes}
            edges = [self._graph_edge_dict(row, by_identity) for row in edge_rows]
            summary = self._graph_summary(nodes, edges)

            needle = str(search or "").strip().lower()
            filtered_nodes = [
                node
                for node in nodes
                if (not kind or node["kind"] == kind)
                and (not risk or node["risk"] == risk)
                and (not status or node["status"] == status)
                and (not namespace or (node.get("namespace") or "_cluster") == namespace)
                and (not missing_only or node["status"] in {"missing", "uncertain"})
                and (
                    not needle
                    or needle
                    in " ".join(
                        [
                            node["node_id"],
                            node["resource_identity"],
                            node["kind"],
                            node["name"],
                            str(node.get("namespace") or "cluster-scoped"),
                        ]
                    ).lower()
                )
            ]
            node_filter_active = bool(kind or risk or status or namespace or missing_only or needle)
            node_identities = {node["resource_identity"] for node in filtered_nodes}
            filtered_edges = [
                edge
                for edge in edges
                if (not relationship or edge["relationship"] == relationship)
                and (not confidence or edge["confidence"] == confidence)
                and (not edge_status or edge["status"] == edge_status)
                and (not missing_only or edge["status"] in {"missing", "uncertain"})
                and (
                    not needle
                    or needle
                    in " ".join(
                        [
                            edge["source"],
                            edge["target"],
                            edge["source_identity"],
                            edge["target_identity"],
                            edge["source_label"],
                            edge["target_label"],
                            edge["relationship"],
                            edge["status"],
                            edge["risk"],
                            edge["confidence"],
                        ]
                    ).lower()
                )
                and (
                    not node_filter_active
                    or edge["source_identity"] in node_identities
                    or edge["target_identity"] in node_identities
                )
            ]
            edge_filter_active = bool(relationship or confidence or edge_status)
            if edge_filter_active:
                connected = {
                    identity
                    for edge in filtered_edges
                    for identity in (edge["source_identity"], edge["target_identity"])
                }
                filtered_nodes = [
                    node for node in filtered_nodes if node["resource_identity"] in connected
                ]

            node_total = len(filtered_nodes)
            edge_total = len(filtered_edges)
            node_page = filtered_nodes[node_offset : node_offset + node_limit]
            edge_page = filtered_edges[edge_offset : edge_offset + edge_limit]
            page_identities = {node["resource_identity"] for node in node_page}
            graph_edges = [
                edge
                for edge in filtered_edges
                if edge["source_identity"] in page_identities
                and edge["target_identity"] in page_identities
            ]
            selected_context = self._selected_graph_context(resource, nodes=nodes, edges=edges)
            return {
                "summary": summary,
                "nodes": node_page,
                "edges": graph_edges,
                "table_rows": edge_page,
                "node_result_count": node_total,
                "edge_result_count": edge_total,
                "node_has_more": node_offset + len(node_page) < node_total,
                "edge_has_more": edge_offset + len(edge_page) < edge_total,
                "selected_context": selected_context,
            }

    def get_run(self, twin_id: str) -> dict[str, Any]:
        with self.sessions() as session:
            row = session.get(NamespaceTwinRunRow, twin_id)
            if not row:
                raise NamespaceTwinPersistenceError(
                    "twin_not_found", f"Namespace twin {twin_id} was not found.", status_code=404
                )
            result = self._run_dict(row)
            result["resource_count"] = (
                session.scalar(
                    select(func.count())
                    .select_from(NamespaceTwinResourceRow)
                    .where(NamespaceTwinResourceRow.twin_id == twin_id)
                )
                or 0
            )
            result["finding_count"] = (
                session.scalar(
                    select(func.count())
                    .select_from(NamespaceTwinFindingRow)
                    .where(NamespaceTwinFindingRow.twin_id == twin_id)
                )
                or 0
            )
            return result

    def list_runs(
        self,
        *,
        lifecycle_status: str | None = None,
        target_namespace: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        with self.sessions() as session:
            statement = select(NamespaceTwinRunRow)
            count_statement = select(func.count()).select_from(NamespaceTwinRunRow)
            if lifecycle_status:
                statement = statement.where(
                    NamespaceTwinRunRow.lifecycle_status == lifecycle_status
                )
                count_statement = count_statement.where(
                    NamespaceTwinRunRow.lifecycle_status == lifecycle_status
                )
            if target_namespace:
                statement = statement.where(
                    NamespaceTwinRunRow.target_namespace == target_namespace
                )
                count_statement = count_statement.where(
                    NamespaceTwinRunRow.target_namespace == target_namespace
                )
            total = int(session.scalar(count_statement) or 0)
            rows = session.scalars(
                statement.order_by(NamespaceTwinRunRow.created_at.desc())
                .offset(offset)
                .limit(limit)
            ).all()
            return [self._run_dict(row) for row in rows], total

    def list_runs_v5(
        self,
        *,
        search: str | None = None,
        decision: str | None = None,
        lifecycle_status: str | None = None,
        target_namespace: str | None = None,
        bundle_name: str | None = None,
        actor_id: str | None = None,
        freshness: str | None = None,
        created_from: datetime | None = None,
        created_to: datetime | None = None,
        linked_execution: str | None = None,
        sort: str = "created_at",
        direction: str = "desc",
        limit: int = 25,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int, dict[str, int]]:
        """Return a filtered page plus metrics from the same authoritative query."""
        with self.sessions() as session:
            statement = select(NamespaceTwinRunRow)
            count_statement = select(func.count()).select_from(NamespaceTwinRunRow)
            clauses: list[Any] = []
            if search:
                pattern = f"%{search.strip()}%"
                clauses.append(
                    or_(
                        NamespaceTwinRunRow.twin_id.ilike(pattern),
                        NamespaceTwinRunRow.display_name.ilike(pattern),
                        NamespaceTwinRunRow.target_cluster.ilike(pattern),
                        NamespaceTwinRunRow.target_namespace.ilike(pattern),
                        NamespaceTwinRunRow.bundle_name.ilike(pattern),
                    )
                )
            if decision and decision != "all":
                clauses.append(NamespaceTwinRunRow.decision == decision)
            if lifecycle_status and lifecycle_status != "all":
                clauses.append(NamespaceTwinRunRow.lifecycle_status == lifecycle_status)
            if target_namespace:
                clauses.append(NamespaceTwinRunRow.target_namespace == target_namespace)
            if bundle_name:
                clauses.append(NamespaceTwinRunRow.bundle_name.ilike(f"%{bundle_name.strip()}%"))
            if actor_id:
                clauses.append(NamespaceTwinRunRow.actor_id == actor_id)
            if created_from:
                clauses.append(NamespaceTwinRunRow.created_at >= created_from)
            if created_to:
                clauses.append(NamespaceTwinRunRow.created_at <= created_to)

            now = datetime.now(UTC)
            threshold = now + timedelta(minutes=30)
            if freshness == "expired":
                clauses.append(
                    or_(
                        NamespaceTwinRunRow.lifecycle_status == "expired",
                        NamespaceTwinRunRow.expires_at <= now,
                    )
                )
            elif freshness == "superseded":
                clauses.append(NamespaceTwinRunRow.lifecycle_status == "superseded")
            elif freshness == "approaching_expiry":
                clauses.append(
                    and_(
                        NamespaceTwinRunRow.expires_at > now,
                        NamespaceTwinRunRow.expires_at <= threshold,
                    )
                )
            elif freshness == "fresh":
                clauses.append(
                    or_(
                        NamespaceTwinRunRow.expires_at.is_(None),
                        NamespaceTwinRunRow.expires_at > threshold,
                    )
                )
            elif freshness in {"stale", "drifted"}:
                clauses.append(NamespaceTwinRunRow.twin_id == "__unsupported_freshness_state__")
            if linked_execution == "linked":
                clauses.append(NamespaceTwinRunRow.twin_id == "__no_execution_relationship_yet__")

            if clauses:
                statement = statement.where(*clauses)
                count_statement = count_statement.where(*clauses)
            total = int(session.scalar(count_statement) or 0)

            metrics_statement = select(
                func.count().label("total"),
                func.sum(case((NamespaceTwinRunRow.decision == "green", 1), else_=0)).label(
                    "green"
                ),
                func.sum(case((NamespaceTwinRunRow.decision == "amber", 1), else_=0)).label(
                    "amber"
                ),
                func.sum(case((NamespaceTwinRunRow.decision == "red", 1), else_=0)).label("red"),
                func.sum(
                    case((NamespaceTwinRunRow.lifecycle_status.in_(ACTIVE_STATES), 1), else_=0)
                ).label("generating"),
                func.sum(
                    case(
                        (
                            or_(
                                NamespaceTwinRunRow.lifecycle_status == "expired",
                                NamespaceTwinRunRow.expires_at <= now,
                            ),
                            1,
                        ),
                        else_=0,
                    )
                ).label("stale"),
            ).select_from(NamespaceTwinRunRow)
            if clauses:
                metrics_statement = metrics_statement.where(*clauses)
            metric_row = session.execute(metrics_statement).one()
            metrics = {
                "total": int(metric_row.total or 0),
                "green": int(metric_row.green or 0),
                "amber": int(metric_row.amber or 0),
                "red": int(metric_row.red or 0),
                "generating": int(metric_row.generating or 0),
                "stale": int(metric_row.stale or 0),
                "linked": 0,
            }

            sort_columns = {
                "created_at": NamespaceTwinRunRow.created_at,
                "updated_at": NamespaceTwinRunRow.updated_at,
                "display_name": NamespaceTwinRunRow.display_name,
                "lifecycle_status": NamespaceTwinRunRow.lifecycle_status,
                "decision": NamespaceTwinRunRow.decision,
                "target_namespace": NamespaceTwinRunRow.target_namespace,
                "bundle_name": NamespaceTwinRunRow.bundle_name,
            }
            sort_column = sort_columns.get(sort, NamespaceTwinRunRow.created_at)
            primary_order = sort_column.asc() if direction == "asc" else sort_column.desc()
            secondary_order = (
                NamespaceTwinRunRow.twin_id.asc()
                if direction == "asc"
                else NamespaceTwinRunRow.twin_id.desc()
            )
            rows = session.scalars(
                statement.order_by(primary_order, secondary_order).offset(offset).limit(limit)
            ).all()
            return [self._run_dict(row) for row in rows], total, metrics

    def transition(
        self,
        twin_id: str,
        next_state: str,
        *,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._event_lock, self.sessions.begin() as session:
            row = session.scalar(
                select(NamespaceTwinRunRow)
                .where(NamespaceTwinRunRow.twin_id == twin_id)
                .with_for_update()
            )
            if not row:
                raise NamespaceTwinPersistenceError(
                    "twin_not_found", f"Namespace twin {twin_id} was not found.", status_code=404
                )
            current = row.lifecycle_status
            if current == next_state:
                return self._run_dict(row)
            if current in TERMINAL_STATES or next_state not in ALLOWED_TRANSITIONS.get(
                current, set()
            ):
                raise NamespaceTwinPersistenceError(
                    "invalid_lifecycle_transition",
                    f"Namespace twin cannot transition from {current} to {next_state}.",
                )
            now = datetime.now(UTC)
            row.lifecycle_status = next_state
            row.updated_at = now
            row.row_version += 1
            if next_state in TERMINAL_STATES:
                row.completed_at = now
            self._append_event_in_session(
                session,
                twin_id,
                "lifecycle_transitioned",
                message,
                {"from_state": current, "to_state": next_state, **(payload or {})},
            )
            return self._run_dict(row)

    def cancel(self, twin_id: str, *, actor_id: str) -> dict[str, Any]:
        row = self.get_run(twin_id)
        if row["lifecycle_status"] == "cancelled":
            return row
        if row["lifecycle_status"] not in ACTIVE_STATES:
            raise NamespaceTwinPersistenceError(
                "twin_not_cancellable", "Only a non-terminal namespace twin can be cancelled."
            )
        return self.transition(
            twin_id,
            "cancelled",
            message="Namespace twin generation cancelled.",
            payload={"actor_id": actor_id},
        )

    def append_event(
        self,
        twin_id: str,
        event_type: str,
        message: str,
        payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with self._event_lock, self.sessions.begin() as session:
            if session.get(NamespaceTwinRunRow, twin_id) is None:
                raise NamespaceTwinPersistenceError(
                    "twin_not_found", f"Namespace twin {twin_id} was not found.", status_code=404
                )
            event = self._append_event_in_session(
                session, twin_id, event_type, message, payload or {}
            )
            return self._event_dict(event)

    def list_events(
        self, twin_id: str, *, limit: int = 100, offset: int = 0
    ) -> tuple[list[dict[str, Any]], int]:
        with self.sessions() as session:
            if session.get(NamespaceTwinRunRow, twin_id) is None:
                raise NamespaceTwinPersistenceError(
                    "twin_not_found", f"Namespace twin {twin_id} was not found.", status_code=404
                )
            total = int(
                session.scalar(
                    select(func.count())
                    .select_from(NamespaceTwinEventRow)
                    .where(NamespaceTwinEventRow.twin_id == twin_id)
                )
                or 0
            )
            rows = session.scalars(
                select(NamespaceTwinEventRow)
                .where(NamespaceTwinEventRow.twin_id == twin_id)
                .order_by(NamespaceTwinEventRow.sequence)
                .offset(offset)
                .limit(limit)
            ).all()
            return [self._event_dict(row) for row in rows], total

    def recover_non_terminal(self) -> list[str]:
        recovered: list[str] = []
        with self.sessions() as session:
            ids = list(
                session.scalars(
                    select(NamespaceTwinRunRow.twin_id).where(
                        NamespaceTwinRunRow.lifecycle_status.in_(ACTIVE_STATES)
                    )
                ).all()
            )
        for twin_id in ids:
            self.append_event(
                twin_id,
                "twin_recovered",
                "Non-terminal namespace twin restored after service restart.",
                {"recovered": True},
            )
            recovered.append(twin_id)
        return recovered

    def expire_due(self, *, now: datetime | None = None) -> list[str]:
        current = now or datetime.now(UTC)
        with self.sessions() as session:
            ids = list(
                session.scalars(
                    select(NamespaceTwinRunRow.twin_id).where(
                        NamespaceTwinRunRow.lifecycle_status.in_(ACTIVE_STATES),
                        NamespaceTwinRunRow.expires_at.is_not(None),
                        NamespaceTwinRunRow.expires_at <= current,
                    )
                ).all()
            )
        for twin_id in ids:
            self.transition(
                twin_id,
                "expired",
                message="Namespace twin generation expired before a final decision.",
            )
        return ids

    def supersede(self, twin_id: str, *, superseded_by: str) -> dict[str, Any]:
        with self._event_lock, self.sessions.begin() as session:
            row = session.scalar(
                select(NamespaceTwinRunRow)
                .where(NamespaceTwinRunRow.twin_id == twin_id)
                .with_for_update()
            )
            if not row:
                raise NamespaceTwinPersistenceError(
                    "twin_not_found", f"Namespace twin {twin_id} was not found.", status_code=404
                )
            if row.lifecycle_status == "superseded" and row.superseded_by == superseded_by:
                return self._run_dict(row)
            if row.lifecycle_status not in ACTIVE_STATES:
                raise NamespaceTwinPersistenceError(
                    "terminal_twin_not_supersedable",
                    "Only a non-terminal provisional twin can be superseded in Phase 4.",
                )
            current = row.lifecycle_status
            now = datetime.now(UTC)
            row.lifecycle_status = "superseded"
            row.superseded_by = superseded_by
            row.completed_at = now
            row.updated_at = now
            row.row_version += 1
            self._append_event_in_session(
                session,
                twin_id,
                "twin_superseded",
                "Namespace twin was superseded by a newer generation.",
                {
                    "from_state": current,
                    "to_state": "superseded",
                    "superseded_by": superseded_by,
                },
            )
            return self._run_dict(row)

    def persist_terminal_decision(
        self,
        twin_id: str,
        *,
        decision: str,
        report_hash: str,
        facts: dict[str, Any],
    ) -> dict[str, Any]:
        if decision not in {"green", "amber", "red"}:
            raise NamespaceTwinPersistenceError(
                "invalid_decision", "Decision must be green, amber, or red."
            )
        with self._event_lock, self.sessions.begin() as session:
            row = session.scalar(
                select(NamespaceTwinRunRow)
                .where(NamespaceTwinRunRow.twin_id == twin_id)
                .with_for_update()
            )
            if not row:
                raise NamespaceTwinPersistenceError(
                    "twin_not_found", f"Namespace twin {twin_id} was not found.", status_code=404
                )
            if row.decision_is_final:
                raise NamespaceTwinPersistenceError(
                    "terminal_decision_immutable", "A terminal decision version cannot be modified."
                )
            if row.lifecycle_status != "decision_calculating":
                raise NamespaceTwinPersistenceError(
                    "invalid_lifecycle_transition",
                    "A terminal decision requires decision_calculating state.",
                )
            now = datetime.now(UTC)
            decision_row = NamespaceTwinDecisionRow(
                decision_id=f"decision_{uuid4().hex}",
                twin_id=twin_id,
                decision_version=row.decision_version,
                decision=decision,
                input_hash=row.input_hash,
                report_hash=report_hash,
                policy_version=row.policy_version,
                risk_rule_version=row.risk_rule_version,
                facts_redacted=redact_value(facts),
                created_at=now,
            )
            session.add(decision_row)
            row.decision = decision
            row.decision_is_final = True
            row.report_hash = report_hash
            row.lifecycle_status = decision
            row.completed_at = now
            row.updated_at = now
            row.row_version += 1
            self._append_event_in_session(
                session,
                twin_id,
                "terminal_decision_persisted",
                "Immutable namespace twin decision persisted.",
                {
                    "decision": decision,
                    "decision_version": row.decision_version,
                    "report_hash": report_hash,
                },
            )
            return self._run_dict(row)

    def _append_event_in_session(
        self,
        session: Session,
        twin_id: str,
        event_type: str,
        message: str,
        payload: dict[str, Any],
    ) -> NamespaceTwinEventRow:
        sequence = (
            int(
                session.scalar(
                    select(func.coalesce(func.max(NamespaceTwinEventRow.sequence), 0)).where(
                        NamespaceTwinEventRow.twin_id == twin_id
                    )
                )
                or 0
            )
            + 1
        )
        event = NamespaceTwinEventRow(
            event_id=f"twinevt_{uuid4().hex}",
            twin_id=twin_id,
            sequence=sequence,
            event_type=event_type,
            message=str(redact_value(message)),
            payload_redacted=redact_value(payload),
            created_at=datetime.now(UTC),
        )
        session.add(event)
        session.flush()
        return event

    @staticmethod
    def _resource_dict(row: NamespaceTwinResourceRow) -> dict[str, Any]:
        payload = dict(row.payload_redacted or {})
        evidence_refs = [str(item) for item in payload.get("evidence_refs") or []]
        return {
            "node_id": str(payload.get("node_id") or stable_node_id(row.stable_identity)),
            "resource_identity": row.stable_identity,
            "api_version": row.api_version,
            "kind": row.kind,
            "name": row.name,
            "namespace": row.namespace,
            "source": str(payload.get("source") or "rendered_manifest"),
            "status": str(payload.get("status") or "present"),
            "risk": str(payload.get("risk") or "low"),
            "synthetic": bool(payload.get("synthetic")),
            "evidence_refs": evidence_refs or [str(payload.get("path") or "bundle")],
            "details": {
                "path": payload.get("path"),
                "document_index": payload.get("document_index"),
                **(payload.get("details") if isinstance(payload.get("details"), dict) else {}),
            },
        }

    @staticmethod
    def _graph_edge_dict(
        row: NamespaceTwinEdgeRow, by_identity: dict[str, dict[str, Any]]
    ) -> dict[str, Any]:
        source_node = by_identity.get(row.source_identity) or {}
        target_node = by_identity.get(row.target_identity) or {}
        statuses = {
            str(source_node.get("status") or "missing"),
            str(target_node.get("status") or "missing"),
        }
        status = (
            "missing"
            if "missing" in statuses
            else "uncertain"
            if "uncertain" in statuses
            else "valid"
        )
        risks = [
            str(source_node.get("risk") or "unknown"),
            str(target_node.get("risk") or "unknown"),
        ]
        risk = next(
            (item for item in ("critical", "high", "medium", "unknown", "low") if item in risks),
            "low",
        )
        return {
            "edge_id": stable_edge_id(row.source_identity, row.target_identity, row.edge_type),
            "source": source_node.get("node_id") or stable_node_id(row.source_identity),
            "target": target_node.get("node_id") or stable_node_id(row.target_identity),
            "source_identity": row.source_identity,
            "target_identity": row.target_identity,
            "source_label": (
                f"{source_node.get('kind') or 'Unknown'}/"
                f"{source_node.get('name') or row.source_identity}"
            ),
            "target_label": (
                f"{target_node.get('kind') or 'Unknown'}/"
                f"{target_node.get('name') or row.target_identity}"
            ),
            "relationship": row.edge_type,
            "status": status,
            "risk": risk,
            "confidence": row.confidence,
            "evidence_refs": [str(item) for item in row.evidence_refs or []],
        }

    @staticmethod
    def _graph_summary(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> dict[str, Any]:
        status_counts: dict[str, int] = {}
        risk_counts: dict[str, int] = {}
        relationship_counts: dict[str, int] = {}
        for node in nodes:
            status_counts[node["status"]] = status_counts.get(node["status"], 0) + 1
            risk_counts[node["risk"]] = risk_counts.get(node["risk"], 0) + 1
        for edge in edges:
            relationship_counts[edge["relationship"]] = (
                relationship_counts.get(edge["relationship"], 0) + 1
            )
        return {
            "nodes": len(nodes),
            "edges": len(edges),
            "present": int(status_counts.get("present") or 0),
            "missing": int(status_counts.get("missing") or 0),
            "uncertain": int(status_counts.get("uncertain") or 0),
            "valid_edges": sum(edge["status"] == "valid" for edge in edges),
            "missing_edges": sum(edge["status"] == "missing" for edge in edges),
            "uncertain_edges": sum(edge["status"] == "uncertain" for edge in edges),
            "high_risk_nodes": int(risk_counts.get("high") or 0)
            + int(risk_counts.get("critical") or 0),
            "relationship_counts": relationship_counts,
        }

    @staticmethod
    def _selected_graph_context(
        resource: str | None,
        *,
        nodes: list[dict[str, Any]],
        edges: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        requested = str(resource or "").strip()
        if not requested:
            return None
        selected = next(
            (node for node in nodes if requested in {node["node_id"], node["resource_identity"]}),
            None,
        )
        if selected is None:
            return {"requested": requested, "found": False, "impact_paths": []}
        node_by_id = {node["node_id"]: node for node in nodes}
        inbound = [edge for edge in edges if edge["target"] == selected["node_id"]]
        outbound = [edge for edge in edges if edge["source"] == selected["node_id"]]
        paths: list[dict[str, Any]] = []
        queue: list[tuple[str, list[str], list[dict[str, Any]]]] = [
            (selected["node_id"], [selected["node_id"]], [])
        ]
        while queue and len(paths) < 12:
            current, path, path_edges = queue.pop(0)
            for edge in edges:
                if edge["source"] != current or edge["target"] in path:
                    continue
                next_path = [*path, edge["target"]]
                next_edges = [*path_edges, edge]
                target = node_by_id.get(edge["target"])
                if target:
                    statuses = [item["status"] for item in next_edges]
                    risks = [item["risk"] for item in next_edges]
                    confidences = [str(item["confidence"] or "unknown") for item in next_edges]
                    confidence = next(
                        (
                            item
                            for item in ("uncertain", "medium", "high", "deterministic")
                            if item in confidences
                        ),
                        "uncertain",
                    )
                    paths.append(
                        {
                            "nodes": [
                                node_by_id[item]["resource_identity"]
                                for item in next_path
                                if item in node_by_id
                            ],
                            "relationships": [item["relationship"] for item in next_edges],
                            "status": (
                                "missing"
                                if "missing" in statuses
                                else "uncertain"
                                if "uncertain" in statuses
                                else "valid"
                            ),
                            "risk": next(
                                (
                                    item
                                    for item in (
                                        "critical",
                                        "high",
                                        "medium",
                                        "unknown",
                                        "low",
                                    )
                                    if item in risks
                                ),
                                "low",
                            ),
                            "confidence": confidence,
                            "evidence_refs": list(
                                dict.fromkeys(
                                    ref
                                    for path_edge in next_edges
                                    for ref in path_edge["evidence_refs"]
                                )
                            ),
                        }
                    )
                    if len(next_path) < 4:
                        queue.append((edge["target"], next_path, next_edges))
                if len(paths) >= 12:
                    break
        return {
            "requested": requested,
            "found": True,
            "node": selected,
            "inbound_edges": inbound[:25],
            "outbound_edges": outbound[:25],
            "impact_paths": paths,
        }

    @staticmethod
    def _delta_dict(row: NamespaceTwinDeltaRow) -> dict[str, Any]:
        return {
            "change_id": row.change_id,
            "resource_identity": row.resource_identity,
            "api_version": row.api_version,
            "kind": row.kind,
            "namespace": row.namespace,
            "name": row.name,
            "helm_release": row.helm_release,
            "action": row.action,
            "current_summary": row.current_summary,
            "planned_summary": row.planned_summary,
            "risk": row.risk,
            "reason": row.reason,
            "canonical_diff": row.canonical_diff,
            "evidence_refs": list(row.evidence_refs or []),
            "redacted": bool(row.redacted),
        }

    @staticmethod
    def _run_dict(row: NamespaceTwinRunRow) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "twin_id": row.twin_id,
            "actor_id": row.actor_id,
            "display_name": row.display_name,
            "lifecycle_status": row.lifecycle_status,
            "decision": row.decision,
            "decision_version": row.decision_version,
            "decision_is_final": row.decision_is_final,
            "source_type": row.source_type,
            "source_namespace": row.source_namespace,
            "target_cluster": row.target_cluster,
            "target_namespace": row.target_namespace,
            "bundle_name": row.bundle_name,
            "bundle_hash": row.bundle_hash,
            "release_version": row.release_version,
            "input_hash": row.input_hash,
            "report_hash": row.report_hash,
            "policy_version": row.policy_version,
            "risk_rule_version": row.risk_rule_version,
            "facts": row.facts_redacted,
            "actions": row.actions_redacted,
            "row_version": row.row_version,
            "created_at": row.created_at.isoformat(),
            "updated_at": row.updated_at.isoformat(),
            "completed_at": row.completed_at.isoformat() if row.completed_at else None,
            "expires_at": row.expires_at.isoformat() if row.expires_at else None,
            "superseded_by": row.superseded_by,
        }

    @staticmethod
    def _event_dict(row: NamespaceTwinEventRow) -> dict[str, Any]:
        return {
            "event_id": row.event_id,
            "twin_id": row.twin_id,
            "sequence": row.sequence,
            "event_type": row.event_type,
            "message": row.message,
            "payload": row.payload_redacted,
            "created_at": row.created_at.isoformat(),
        }
