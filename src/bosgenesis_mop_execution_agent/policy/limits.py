"""Timeout and retry limit policy guards."""

from bosgenesis_mop_execution_agent.models import PolicyBlock, PolicySeverity


def limit_blocks(
    *,
    timeout_seconds: int | None,
    retry_attempts: int,
    max_timeout_seconds: int,
    max_retry_attempts: int,
) -> list[PolicyBlock]:
    """Block excessive timeout or retry requests."""
    blocks: list[PolicyBlock] = []
    if timeout_seconds is not None and timeout_seconds > max_timeout_seconds:
        blocks.append(
            PolicyBlock(
                code="TIMEOUT_LIMIT_EXCEEDED",
                message=f"Requested timeout {timeout_seconds}s exceeds {max_timeout_seconds}s.",
                severity=PolicySeverity.BLOCK,
                guardrail="timeout_limits",
            )
        )
    if retry_attempts > max_retry_attempts:
        blocks.append(
            PolicyBlock(
                code="RETRY_LIMIT_EXCEEDED",
                message=f"Requested retries {retry_attempts} exceeds {max_retry_attempts}.",
                severity=PolicySeverity.BLOCK,
                guardrail="retry_limits",
            )
        )
    return blocks
