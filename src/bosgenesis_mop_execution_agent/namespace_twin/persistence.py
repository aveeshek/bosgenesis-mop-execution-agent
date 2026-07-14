"""SQLAlchemy persistence for durable namespace twin runs."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from bosgenesis_mop_execution_agent.namespace_twin.models import (
    ACTIVE_STATES,
    ALLOWED_TRANSITIONS,
    TERMINAL_STATES,
    NamespaceTwinBase,
    NamespaceTwinDecisionRow,
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
