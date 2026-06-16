"""Redis-style leases, namespace locks, and worker heartbeats."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from bosgenesis_mop_execution_agent.common.ids import new_id
from bosgenesis_mop_execution_agent.common.time import utc_now


class RedisLikeClient(Protocol):
    """Small Redis command surface used by the lock service."""

    def set(
        self,
        name: str,
        value: str,
        *,
        nx: bool = False,
        xx: bool = False,
        ex: int | None = None,
    ) -> bool: ...

    def get(self, name: str) -> str | None: ...

    def delete(self, name: str) -> int: ...

    def expire(self, name: str, seconds: int) -> bool: ...


class InMemoryRedisLikeClient:
    """Tiny Redis-like client for deterministic unit tests."""

    def __init__(self) -> None:
        self._values: dict[str, tuple[str, datetime | None]] = {}

    def set(
        self,
        name: str,
        value: str,
        *,
        nx: bool = False,
        xx: bool = False,
        ex: int | None = None,
    ) -> bool:
        self._purge_if_expired(name)
        exists = name in self._values
        if nx and exists:
            return False
        if xx and not exists:
            return False
        expires_at = utc_now() + timedelta(seconds=ex) if ex else None
        self._values[name] = (value, expires_at)
        return True

    def get(self, name: str) -> str | None:
        self._purge_if_expired(name)
        stored = self._values.get(name)
        return stored[0] if stored else None

    def delete(self, name: str) -> int:
        existed = name in self._values
        self._values.pop(name, None)
        return 1 if existed else 0

    def expire(self, name: str, seconds: int) -> bool:
        self._purge_if_expired(name)
        if name not in self._values:
            return False
        value, _ = self._values[name]
        self._values[name] = (value, utc_now() + timedelta(seconds=seconds))
        return True

    def _purge_if_expired(self, name: str) -> None:
        stored = self._values.get(name)
        if stored is None:
            return
        _, expires_at = stored
        if expires_at is not None and expires_at <= utc_now():
            self._values.pop(name, None)


@dataclass(frozen=True)
class NamespaceLock:
    """Acquired namespace lock metadata."""

    target_namespace: str
    owner_id: str
    lease_token: str
    lease_seconds: int

    @property
    def key(self) -> str:
        return namespace_lock_key(self.target_namespace)


class NamespaceLockUnavailable(RuntimeError):
    """Raised when a namespace lock is already held."""


class NamespaceLockService:
    """Exclusive target namespace mutation lock backed by Redis semantics."""

    def __init__(self, client: RedisLikeClient, lease_seconds: int = 300) -> None:
        self._client = client
        self._lease_seconds = lease_seconds

    def acquire(self, target_namespace: str, owner_id: str) -> NamespaceLock:
        token = new_id("lease")
        lock = NamespaceLock(
            target_namespace=target_namespace,
            owner_id=owner_id,
            lease_token=token,
            lease_seconds=self._lease_seconds,
        )
        value = lock_value(owner_id, token)
        if not self._client.set(lock.key, value, nx=True, ex=self._lease_seconds):
            raise NamespaceLockUnavailable(f"namespace_lock_unavailable:{target_namespace}")
        return lock

    def renew(self, lock: NamespaceLock) -> bool:
        if self._client.get(lock.key) != lock_value(lock.owner_id, lock.lease_token):
            return False
        return self._client.expire(lock.key, lock.lease_seconds)

    def release(self, lock: NamespaceLock) -> bool:
        if self._client.get(lock.key) != lock_value(lock.owner_id, lock.lease_token):
            return False
        return self._client.delete(lock.key) == 1


class WorkerHeartbeatService:
    """Redis-backed worker heartbeat marker."""

    def __init__(self, client: RedisLikeClient, ttl_seconds: int = 120) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds

    def heartbeat(self, worker_id: str, job_id: str | None = None) -> None:
        payload = job_id or ""
        self._client.set(worker_heartbeat_key(worker_id), payload, ex=self._ttl_seconds)

    def get_active_job(self, worker_id: str) -> str | None:
        value = self._client.get(worker_heartbeat_key(worker_id))
        return value or None


def namespace_lock_key(target_namespace: str) -> str:
    return f"mop-exec:lock:namespace:{target_namespace}"


def worker_heartbeat_key(worker_id: str) -> str:
    return f"mop-exec:worker:{worker_id}:heartbeat"


def lock_value(owner_id: str, lease_token: str) -> str:
    return f"{owner_id}:{lease_token}"
