"""Durable-job queue primitives for the execution worker."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from bosgenesis_mop_execution_agent.models import JobState
from bosgenesis_mop_execution_agent.persistence.repositories import JsonExecutionRepository

RUNNABLE_STATES: frozenset[JobState] = frozenset(
    {
        JobState.CREATED,
        JobState.VALIDATING_BUNDLE,
        JobState.DRY_RUN_READY,
        JobState.DRY_RUNNING,
        JobState.AWAITING_LLM_INSTRUCTION,
        JobState.EXECUTING,
        JobState.WAIT_SCHEDULED,
        JobState.VALIDATION_RUNNING,
        JobState.ROLLBACK_REQUESTED,
        JobState.ROLLING_BACK,
    }
)


@dataclass(frozen=True)
class QueuedJob:
    """A single queued job reference."""

    job_id: str


class InMemoryJobQueue:
    """Deterministic FIFO queue with restart rehydration from persisted jobs."""

    def __init__(self, repository: JsonExecutionRepository) -> None:
        self._repository = repository
        self._queue: deque[QueuedJob] = deque()
        self._queued_job_ids: set[str] = set()

    def enqueue(self, job_id: str) -> bool:
        """Queue a job once and return true if this call added it."""
        if job_id in self._queued_job_ids:
            return False
        self._queue.append(QueuedJob(job_id=job_id))
        self._queued_job_ids.add(job_id)
        return True

    def dequeue(self) -> QueuedJob | None:
        """Return the next queued job, or None when empty."""
        if not self._queue:
            return None
        item = self._queue.popleft()
        self._queued_job_ids.discard(item.job_id)
        return item

    def rehydrate(self) -> int:
        """Queue restart-runnable jobs from the repository."""
        count = 0
        for job in sorted(self._repository.list_jobs(), key=lambda item: item.created_at):
            if job.state in RUNNABLE_STATES and self.enqueue(job.job_id):
                count += 1
        return count

    def __len__(self) -> int:
        return len(self._queue)
