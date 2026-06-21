"""Worker runtime entrypoint."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

from bosgenesis_mop_execution_agent.common.ids import new_id
from bosgenesis_mop_execution_agent.persistence import (
    InMemoryRedisLikeClient,
)
from bosgenesis_mop_execution_agent.persistence.repositories import JsonExecutionRepository
from bosgenesis_mop_execution_agent.runtime import InMemoryJobQueue
from bosgenesis_mop_execution_agent.runtime.factory import create_worker_runtime


def main() -> None:
    """Run the deterministic worker loop."""
    interval_seconds = float(os.getenv("WORKER_HEARTBEAT_INTERVAL_SECONDS", "5"))
    repo_path = _repository_path()
    worker_id = os.getenv("WORKER_ID", new_id("worker"))
    redis_client = InMemoryRedisLikeClient()
    repository = JsonExecutionRepository(repo_path)
    queue = InMemoryJobQueue(repository)
    runtime = create_worker_runtime(
        repository=repository,
        queue=queue,
        redis_client=redis_client,
        worker_id=worker_id,
    )
    runtime.recover_restartable_jobs()
    while True:
        runtime.run_once()
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
