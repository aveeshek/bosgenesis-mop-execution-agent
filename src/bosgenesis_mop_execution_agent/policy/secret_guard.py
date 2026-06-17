"""Secret detection policy guards."""

from __future__ import annotations

from typing import Any

from bosgenesis_mop_execution_agent.models import PolicyBlock, PolicySeverity
from bosgenesis_mop_execution_agent.security.redaction import find_sensitive_content


def secret_blocks(payloads: list[Any]) -> list[PolicyBlock]:
    """Block secret-like content across manifests, values, instructions, logs, and outputs."""
    blocks: list[PolicyBlock] = []
    for index, payload in enumerate(payloads):
        if _is_kubernetes_secret_with_values(payload):
            blocks.append(_block("SECRET_VALUES_BLOCKED", f"payload[{index}] Kubernetes Secret"))
        for finding in find_sensitive_content(payload):
            blocks.append(_block("SECRET_VALUES_BLOCKED", f"payload[{index}] {finding.path}"))
    return _dedupe(blocks)


def _is_kubernetes_secret_with_values(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("kind") == "Secret"
        and bool(payload.get("data") or payload.get("stringData"))
    )


def _block(code: str, detail: str) -> PolicyBlock:
    return PolicyBlock(
        code=code,
        message=detail,
        severity=PolicySeverity.CRITICAL,
        guardrail="secret_guard",
    )


def _dedupe(blocks: list[PolicyBlock]) -> list[PolicyBlock]:
    seen: set[tuple[str, str]] = set()
    deduped: list[PolicyBlock] = []
    for block in blocks:
        key = (block.code, block.message)
        if key not in seen:
            seen.add(key)
            deduped.append(block)
    return deduped
