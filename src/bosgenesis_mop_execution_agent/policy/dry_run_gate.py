"""Dry-run policy guard."""

from bosgenesis_mop_execution_agent.models import PolicyBlock, PolicySeverity


def dry_run_blocks(*, mutating: bool, dry_run_satisfied: bool) -> list[PolicyBlock]:
    """Block mutating actions that have not completed dry-run/preflight."""
    if not mutating or dry_run_satisfied:
        return []
    return [
        PolicyBlock(
            code="DRY_RUN_REQUIRED",
            message="Mutating action requires successful dry-run/preflight.",
            severity=PolicySeverity.BLOCK,
            guardrail="dry_run_before_mutation",
        )
    ]
