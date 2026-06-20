"""Append-only execution memory store."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from bosgenesis_mop_execution_agent.models.memory import (
    MEMORY_AUTHORITY,
    MemoryLayer,
    MemoryQuery,
    MemoryRecord,
)
from bosgenesis_mop_execution_agent.security import redact_value


class ExecutionMemoryStore:
    """Small append-only memory store for factual execution context."""

    def __init__(self, records: Iterable[MemoryRecord] | None = None) -> None:
        self._records = list(records or [])

    def write(
        self,
        *,
        layer: MemoryLayer,
        job_id: str,
        summary: str,
        payload: dict[str, Any] | None = None,
        namespace: str | None = None,
        chart: str | None = None,
        kind: str | None = None,
        resource_name: str | None = None,
        error_code: str | None = None,
        mcp_source: str | None = None,
        tenant: str | None = None,
        environment: str | None = None,
        tags: list[str] | None = None,
    ) -> MemoryRecord:
        """Append a redacted memory record."""
        record = MemoryRecord(
            layer=layer,
            job_id=job_id,
            namespace=namespace,
            chart=chart,
            kind=kind,
            resource_name=resource_name,
            error_code=error_code,
            mcp_source=mcp_source,
            tenant=tenant,
            environment=environment,
            summary=str(redact_value(summary)),
            payload_redacted=redact_value(payload or {}),
            tags=tags or [],
            authority=MEMORY_AUTHORITY,
            redaction_applied=True,
        )
        self._records.append(record)
        return record

    def append(self, record: MemoryRecord) -> None:
        """Append an already validated record after rehydration."""
        self._records.append(record)

    def query(self, query: MemoryQuery) -> list[MemoryRecord]:
        """Return records matching every populated filter."""
        return [record for record in self._records if _matches(record, query)]


def _matches(record: MemoryRecord, query: MemoryQuery) -> bool:
    for field_name, expected in query.model_dump(exclude_none=True).items():
        if getattr(record, field_name) != expected:
            return False
    return True
