"""Append-only audit writer."""

from __future__ import annotations

from bosgenesis_mop_execution_agent.models import AuditEvent
from bosgenesis_mop_execution_agent.persistence.repositories import JsonExecutionRepository


class AppendOnlyAuditWriter:
    """Writes audit events without update/delete operations."""

    def __init__(self, repository: JsonExecutionRepository) -> None:
        self._repository = repository

    def write(self, audit_event: AuditEvent) -> None:
        """Append one audit event."""
        self._repository.append_audit_event(audit_event)

    def list_for_job(self, job_id: str) -> list[AuditEvent]:
        """Return audit events for a job."""
        return self._repository.list_audit_events(job_id)
