"""Idempotency storage and replay helpers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field

from bosgenesis_mop_execution_agent.common.time import utc_now
from bosgenesis_mop_execution_agent.models.base import StrictBaseModel


class IdempotencyStatus(StrEnum):
    """Stored idempotency record state."""

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class IdempotencyRecord(StrictBaseModel):
    """Durable idempotency key record."""

    idempotency_key: str
    scope: str
    request_hash: str
    state: IdempotencyStatus = IdempotencyStatus.IN_PROGRESS
    result_hash: str | None = None
    result_payload_redacted: dict[str, Any] | None = None
    correlation_id: str | None = None
    trace_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime | None = None


class IdempotencyConflictError(ValueError):
    """Raised when a key is reused for a different request."""


class IdempotencyStore:
    """File-backed idempotency store."""

    def __init__(self, path: str | Path) -> None:
        self._path = Path(path)
        self._records = self._load()

    def begin(
        self,
        *,
        idempotency_key: str,
        scope: str,
        request_payload: dict[str, Any],
        correlation_id: str | None = None,
        trace_id: str | None = None,
    ) -> tuple[IdempotencyRecord, bool]:
        """Create or replay an idempotency record.

        Returns `(record, created)` where `created=False` means the caller should
        replay the existing result or observe the in-progress state.
        """
        request_hash = stable_hash(request_payload)
        existing = self._records.get(idempotency_key)
        if existing is not None:
            if existing.request_hash != request_hash or existing.scope != scope:
                raise IdempotencyConflictError("idempotency_key_reused_with_different_request")
            return existing, False

        record = IdempotencyRecord(
            idempotency_key=idempotency_key,
            scope=scope,
            request_hash=request_hash,
            correlation_id=correlation_id,
            trace_id=trace_id,
        )
        self._records[idempotency_key] = record
        self._flush()
        return record, True

    def complete(self, idempotency_key: str, result_payload: dict[str, Any]) -> IdempotencyRecord:
        """Mark a key completed with its redacted result payload."""
        record = self._require_record(idempotency_key)
        updated = record.model_copy(
            update={
                "state": IdempotencyStatus.COMPLETED,
                "result_hash": stable_hash(result_payload),
                "result_payload_redacted": result_payload,
                "updated_at": utc_now(),
            }
        )
        self._records[idempotency_key] = updated
        self._flush()
        return updated

    def get(self, idempotency_key: str) -> IdempotencyRecord | None:
        return self._records.get(idempotency_key)

    def _require_record(self, idempotency_key: str) -> IdempotencyRecord:
        record = self._records.get(idempotency_key)
        if record is None:
            raise KeyError(idempotency_key)
        return record

    def _load(self) -> dict[str, IdempotencyRecord]:
        if not self._path.exists():
            return {}
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        return {key: IdempotencyRecord.model_validate(value) for key, value in raw.items()}

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: record.model_dump(mode="json")
            for key, record in sorted(self._records.items(), key=lambda item: item[0])
        }
        encoded = json.dumps(payload, indent=2, sort_keys=True)
        self._path.write_text(f"{encoded}\n", encoding="utf-8")


def stable_hash(payload: dict[str, Any]) -> str:
    """Return a stable SHA-256 digest for a JSON-compatible payload."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
