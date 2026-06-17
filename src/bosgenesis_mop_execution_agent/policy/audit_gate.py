"""Audit-before-mutation policy guard."""

from bosgenesis_mop_execution_agent.models import PolicyBlock, PolicySeverity


def audit_blocks(*, mutating: bool, audit_written: bool) -> list[PolicyBlock]:
    """Block mutation unless its audit intent has already been persisted."""
    if not mutating or audit_written:
        return []
    return [
        PolicyBlock(
            code="AUDIT_REQUIRED_BEFORE_MUTATION",
            message="Mutation requires append-only audit write before execution.",
            severity=PolicySeverity.BLOCK,
            guardrail="audit_before_mutation",
        )
    ]
