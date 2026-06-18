"""Wait and polling executor for long-running runtime steps."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from bosgenesis_mop_execution_agent.common.time import utc_now


@dataclass(frozen=True)
class WaitResult:
    """Result of one deterministic wait evaluation."""

    complete: bool
    timed_out: bool
    next_poll_after_seconds: int
    elapsed_seconds: int


class WaitExecutor:
    """Evaluate waits without sleeping inside the worker decision path."""

    def __init__(self, *, default_poll_seconds: int = 30, max_timeout_seconds: int = 1800) -> None:
        self._default_poll_seconds = default_poll_seconds
        self._max_timeout_seconds = max_timeout_seconds

    def evaluate(
        self,
        *,
        started_at: datetime | None,
        timeout_seconds: int | None = None,
        poll_seconds: int | None = None,
    ) -> WaitResult:
        now = utc_now()
        effective_started_at = started_at or now
        effective_timeout = min(
            timeout_seconds or self._max_timeout_seconds,
            self._max_timeout_seconds,
        )
        elapsed = max(0, int((now - effective_started_at).total_seconds()))
        timed_out = elapsed >= effective_timeout
        return WaitResult(
            complete=False,
            timed_out=timed_out,
            next_poll_after_seconds=poll_seconds or self._default_poll_seconds,
            elapsed_seconds=elapsed,
        )

    def due_at(self, *, poll_seconds: int | None = None) -> datetime:
        return utc_now() + timedelta(seconds=poll_seconds or self._default_poll_seconds)
