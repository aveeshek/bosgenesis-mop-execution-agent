"""Policy models."""

from __future__ import annotations

from enum import StrEnum

from bosgenesis_mop_execution_agent.models.base import StrictBaseModel


class PolicySeverity(StrEnum):
    """Policy finding severity."""

    WARNING = "warning"
    BLOCK = "block"
    CRITICAL = "critical"


class PolicyBlock(StrictBaseModel):
    """Deterministic guardrail finding."""

    code: str
    message: str
    severity: PolicySeverity = PolicySeverity.BLOCK
    guardrail: str | None = None
