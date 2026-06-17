"""Production data and PVC content-copy guards."""

from __future__ import annotations

import re
from typing import Any

from bosgenesis_mop_execution_agent.models import PolicyBlock, PolicySeverity

DATA_COPY_PATTERN = re.compile(r"\b(kubectl\s+cp|rsync|scp|copy|cp)\b", re.IGNORECASE)
PRODUCTION_PATTERN = re.compile(r"\b(prod|production)\b", re.IGNORECASE)


def production_data_blocks(
    command: str | None,
    manifests: list[dict[str, Any]],
) -> list[PolicyBlock]:
    """Block production data and PVC content-copy attempts."""
    blocks: list[PolicyBlock] = []
    command_text = command or ""
    if DATA_COPY_PATTERN.search(command_text) and PRODUCTION_PATTERN.search(command_text):
        blocks.append(_block("PRODUCTION_DATA_COPY_BLOCKED", "production data copy command"))
    if DATA_COPY_PATTERN.search(command_text) and _mentions_pvc(command_text, manifests):
        blocks.append(_block("PVC_DATA_COPY_BLOCKED", "PVC content copy command"))
    return blocks


def _mentions_pvc(command: str, manifests: list[dict[str, Any]]) -> bool:
    if re.search(r"\b(pvc|persistentvolumeclaim|/data|volume)\b", command, re.IGNORECASE):
        return True
    return any(manifest.get("kind") == "PersistentVolumeClaim" for manifest in manifests)


def _block(code: str, detail: str) -> PolicyBlock:
    return PolicyBlock(
        code=code,
        message=detail,
        severity=PolicySeverity.CRITICAL,
        guardrail="production_data_guard",
    )
