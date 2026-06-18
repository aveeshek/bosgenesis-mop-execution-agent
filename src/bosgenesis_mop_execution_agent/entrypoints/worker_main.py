"""Worker runtime entrypoint."""

from __future__ import annotations

import os
import time
from pathlib import Path

from bosgenesis_mop_execution_agent.common.ids import new_id
from bosgenesis_mop_execution_agent.persistence import (
    InMemoryRedisLikeClient,
    NamespaceLockService,
    WorkerHeartbeatService,
)
from bosgenesis_mop_execution_agent.persistence.repositories import JsonExecutionRepository
from bosgenesis_mop_execution_agent.runtime import InMemoryJobQueue, WorkerRuntime


def main() -> None:
    """Run the deterministic worker loop."""
    interval_seconds = float(os.getenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "5"))
    repo_path = Path(
        os.getenv("MOP_EXECUTION_REPOSITORY_PATH", "/tmp/mop-execution/repository.json")
    )
    worker_id = os.getenv("WORKER_ID", new_id("worker"))
    redis_client = InMemoryRedisLikeClient()
    repository = JsonExecutionRepository(repo_path)
    queue = InMemoryJobQueue(repository)
    runtime = WorkerRuntime(
        repository=repository,
        queue=queue,
        lock_service=NamespaceLockService(redis_client),
        heartbeat_service=WorkerHeartbeatService(redis_client),
        worker_id=worker_id,
    )
    runtime.recover_restartable_jobs()
    while True:
        runtime.run_once()
        time.sleep(interval_seconds)


if __name__ == "__main__":
    main()
