"""Idempotency policy guard."""

from __future__ import annotations

from typing import Any

from bosgenesis_mop_execution_agent.models import PolicyBlock, PolicySeverity
from bosgenesis_mop_execution_agent.persistence.idempotency import IdempotencyRecord, stable_hash


def idempotency_blocks(
    *,
    mutating: bool,
    idempotency_record: IdempotencyRecord | None,
    request_payload: dict[str, Any] | None,
) -> list[PolicyBlock]:
    """Block mutating decisions without matching idempotency state."""
    if not mutating:
        return []
    if idempotency_record is None or request_payload is None:
        return [_block("IDEMPOTENCY_REQUIRED", "Mutating action requires an idempotency record.")]
    if idempotency_record.request_hash != stable_hash(request_payload):
        return [_block("IDEMPOTENCY_REQUEST_MISMATCH", "Idempotency key request hash mismatch.")]
    return []


def _block(code: str, message: str) -> PolicyBlock:
    return PolicyBlock(
        code=code,
        message=message,
        severity=PolicySeverity.BLOCK,
        guardrail="idempotency",
    )
