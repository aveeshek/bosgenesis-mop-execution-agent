import pytest

from bosgenesis_mop_execution_agent.persistence import (
    InMemoryRedisLikeClient,
    NamespaceLockService,
    NamespaceLockUnavailable,
    WorkerHeartbeatService,
)
from bosgenesis_mop_execution_agent.persistence.locks import namespace_lock_key


def test_namespace_lock_prevents_concurrent_owners() -> None:
    client = InMemoryRedisLikeClient()
    service = NamespaceLockService(client, lease_seconds=30)

    first = service.acquire("target-ns", "job-1")

    with pytest.raises(NamespaceLockUnavailable):
        service.acquire("target-ns", "job-2")

    assert client.get(namespace_lock_key("target-ns")) is not None
    assert service.release(first) is True
    second = service.acquire("target-ns", "job-2")
    assert second.owner_id == "job-2"


def test_namespace_lock_renew_and_wrong_owner_release() -> None:
    client = InMemoryRedisLikeClient()
    service = NamespaceLockService(client, lease_seconds=30)
    lock = service.acquire("target-ns", "job-1")
    wrong_lock = lock.__class__(
        target_namespace=lock.target_namespace,
        owner_id="job-2",
        lease_token=lock.lease_token,
        lease_seconds=lock.lease_seconds,
    )

    assert service.renew(lock) is True
    assert service.release(wrong_lock) is False
    assert service.release(lock) is True


def test_worker_heartbeat_records_active_job() -> None:
    client = InMemoryRedisLikeClient()
    heartbeats = WorkerHeartbeatService(client, ttl_seconds=30)

    heartbeats.heartbeat("worker-1", "job-1")

    assert heartbeats.get_active_job("worker-1") == "job-1"
