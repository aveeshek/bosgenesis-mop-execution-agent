"""Reconciler runtime entrypoint placeholder."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from bosgenesis_mop_execution_agent.common.ids import new_id
from bosgenesis_mop_execution_agent.persistence import InMemoryRedisLikeClient
from bosgenesis_mop_execution_agent.persistence.repositories import JsonExecutionRepository
from bosgenesis_mop_execution_agent.runtime import InMemoryJobQueue
from bosgenesis_mop_execution_agent.runtime.factory import create_worker_runtime


def main() -> None:
    """Run periodic persisted-state reconciliation without performing new mutations."""
    interval_seconds = int(os.getenv("RECONCILER_INTERVAL_SECONDS", "60"))
    repository = JsonExecutionRepository(_repository_path())
    runtime = create_worker_runtime(
        repository=repository,
        queue=InMemoryJobQueue(repository),
        redis_client=InMemoryRedisLikeClient(),
        worker_id=os.getenv("RECONCILER_ID", new_id("reconciler")),
    )
    while True:
        runtime.reconcile_after_restart()
        time.sleep(interval_seconds)


def _repository_path() -> Path:
    configured = os.getenv("MOP_EXECUTION_REPOSITORY_PATH")
    if configured:
        return Path(configured)
    artifact_root = os.getenv("ARTIFACT_ROOT_PATH")
    if artifact_root:
        return Path(artifact_root) / "repository.json"
    return Path(tempfile.gettempdir()) / "mop-execution" / "repository.json"


if __name__ == "__main__":
    main()
