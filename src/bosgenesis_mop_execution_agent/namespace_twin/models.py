"""Namespace Digital Twin lifecycle and persistence models."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

SCHEMA_VERSION = "1.0.0"
POLICY_VERSION = "namespace-twin-policy-2026.07.2"
RISK_RULE_VERSION = "namespace-twin-risk-1.1.0"

ACTIVE_STATES = {
    "requested",
    "generating",
    "awaiting_dry_run",
    "dry_run_evidence_attached",
    "decision_calculating",
}
TERMINAL_STATES = {
    "green",
    "amber",
    "red",
    "failed",
    "cancelled",
    "superseded",
    "expired",
}
ALLOWED_TRANSITIONS = {
    "requested": {"generating", "failed", "cancelled", "superseded", "expired"},
    "generating": {"awaiting_dry_run", "failed", "cancelled", "superseded", "expired"},
    "awaiting_dry_run": {
        "dry_run_evidence_attached",
        "decision_calculating",
        "failed",
        "cancelled",
        "superseded",
        "expired",
    },
    "dry_run_evidence_attached": {
        "decision_calculating",
        "failed",
        "cancelled",
        "superseded",
        "expired",
    },
    "decision_calculating": TERMINAL_STATES,
}


class NamespaceTwinBase(DeclarativeBase):
    """Declarative base kept separate from execution-job persistence."""


class NamespaceTwinRunRow(NamespaceTwinBase):
    __tablename__ = "namespace_twin_runs"
    __table_args__ = (
        UniqueConstraint(
            "actor_id",
            "target_namespace",
            "idempotency_key",
            name="uq_namespace_twin_scoped_idempotency",
        ),
        Index("idx_namespace_twin_status_updated", "lifecycle_status", "updated_at"),
        Index("idx_namespace_twin_target_created", "target_namespace", "created_at"),
    )

    twin_id: Mapped[str] = mapped_column(Text, primary_key=True)
    actor_id: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(40), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    decision_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    decision_is_final: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    source_value_redacted: Mapped[str] = mapped_column(Text, nullable=False)
    source_namespace: Mapped[str | None] = mapped_column(Text)
    target_cluster: Mapped[str] = mapped_column(Text, nullable=False, default="configured-cluster")
    target_namespace: Mapped[str] = mapped_column(Text, nullable=False)
    bundle_name: Mapped[str] = mapped_column(Text, nullable=False)
    bundle_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    release_version: Mapped[str | None] = mapped_column(Text)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    report_hash: Mapped[str | None] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    risk_rule_version: Mapped[str] = mapped_column(Text, nullable=False)
    facts_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    actions_redacted: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    row_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    superseded_by: Mapped[str | None] = mapped_column(Text)


class NamespaceTwinEventRow(NamespaceTwinBase):
    __tablename__ = "namespace_twin_events"
    __table_args__ = (
        UniqueConstraint("twin_id", "sequence", name="uq_namespace_twin_event_sequence"),
        Index("idx_namespace_twin_events_ordered", "twin_id", "sequence"),
    )

    event_id: Mapped[str] = mapped_column(Text, primary_key=True)
    twin_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("namespace_twin_runs.twin_id", ondelete="CASCADE"),
        nullable=False,
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    payload_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class NamespaceTwinResourceRow(NamespaceTwinBase):
    __tablename__ = "namespace_twin_resources"
    __table_args__ = (
        UniqueConstraint("twin_id", "stable_identity", name="uq_namespace_twin_resource_identity"),
        Index("idx_namespace_twin_resource_kind", "twin_id", "kind"),
    )

    resource_id: Mapped[str] = mapped_column(Text, primary_key=True)
    twin_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("namespace_twin_runs.twin_id", ondelete="CASCADE"),
        nullable=False,
    )
    stable_identity: Mapped[str] = mapped_column(Text, nullable=False)
    api_version: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str | None] = mapped_column(Text)
    payload_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class NamespaceTwinDeltaRow(NamespaceTwinBase):
    __tablename__ = "namespace_twin_release_deltas"
    __table_args__ = (
        UniqueConstraint("twin_id", "change_id", name="uq_namespace_twin_delta_change"),
        Index("idx_namespace_twin_delta_action", "twin_id", "action"),
        Index("idx_namespace_twin_delta_risk", "twin_id", "risk"),
        Index("idx_namespace_twin_delta_kind", "twin_id", "kind"),
    )

    change_id: Mapped[str] = mapped_column(Text, primary_key=True)
    twin_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("namespace_twin_runs.twin_id", ondelete="CASCADE"),
        nullable=False,
    )
    resource_identity: Mapped[str] = mapped_column(Text, nullable=False)
    api_version: Mapped[str | None] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    namespace: Mapped[str | None] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    helm_release: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    current_summary: Mapped[str | None] = mapped_column(Text)
    planned_summary: Mapped[str | None] = mapped_column(Text)
    risk: Mapped[str] = mapped_column(String(24), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    canonical_diff: Mapped[str | None] = mapped_column(Text)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    redacted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class NamespaceTwinEdgeRow(NamespaceTwinBase):
    __tablename__ = "namespace_twin_edges"
    __table_args__ = (
        UniqueConstraint(
            "twin_id",
            "source_identity",
            "target_identity",
            "edge_type",
            name="uq_namespace_twin_edge",
        ),
        Index("idx_namespace_twin_edge_source", "twin_id", "source_identity"),
    )

    edge_id: Mapped[str] = mapped_column(Text, primary_key=True)
    twin_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("namespace_twin_runs.twin_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_identity: Mapped[str] = mapped_column(Text, nullable=False)
    target_identity: Mapped[str] = mapped_column(Text, nullable=False)
    edge_type: Mapped[str] = mapped_column(String(80), nullable=False)
    confidence: Mapped[str] = mapped_column(String(24), nullable=False, default="deterministic")
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class NamespaceTwinFindingRow(NamespaceTwinBase):
    __tablename__ = "namespace_twin_findings"
    __table_args__ = (Index("idx_namespace_twin_finding_severity", "twin_id", "severity"),)

    finding_id: Mapped[str] = mapped_column(Text, primary_key=True)
    twin_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("namespace_twin_runs.twin_id", ondelete="CASCADE"),
        nullable=False,
    )
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(String(24), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_refs: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)


class NamespaceTwinDecisionRow(NamespaceTwinBase):
    __tablename__ = "namespace_twin_decisions"
    __table_args__ = (
        UniqueConstraint("twin_id", "decision_version", name="uq_namespace_twin_decision_version"),
        Index("idx_namespace_twin_decision_created", "twin_id", "created_at"),
    )

    decision_id: Mapped[str] = mapped_column(Text, primary_key=True)
    twin_id: Mapped[str] = mapped_column(
        Text,
        ForeignKey("namespace_twin_runs.twin_id", ondelete="CASCADE"),
        nullable=False,
    )
    decision_version: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    report_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(Text, nullable=False)
    risk_rule_version: Mapped[str] = mapped_column(Text, nullable=False)
    facts_redacted: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
