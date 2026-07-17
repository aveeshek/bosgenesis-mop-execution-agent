"""Namespace-scoped live snapshot collection for release-delta facts."""

from __future__ import annotations

import os
from typing import Any, Protocol

from bosgenesis_mop_execution_agent.namespace_twin.delta import LiveSnapshot
from bosgenesis_mop_execution_agent.runtime.mcp_rest_adapters import (
    KubernetesInspectorRestDryRunClient,
)


class LiveSnapshotCollector(Protocol):
    def collect(self, namespace: str, *, correlation_id: str) -> LiveSnapshot: ...


class KubernetesLiveSnapshotCollector:
    """Collect covered workload kinds through the namespace-scoped inspector."""

    COLLECTIONS = (
        ("Pod", "list_pods"),
        ("Service", "list_services"),
        ("PersistentVolumeClaim", "list_pvcs"),
        ("Deployment", "list_deployments"),
        ("StatefulSet", "list_statefulsets"),
        ("Ingress", "list_ingresses"),
    )

    def __init__(self, *, base_url: str, api_key: str | None, enabled: bool = True) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.enabled = enabled

    @classmethod
    def from_environment(cls) -> KubernetesLiveSnapshotCollector:
        enabled = os.getenv("NAMESPACE_TWIN_LIVE_COLLECTION_ENABLED", "false").lower() not in {
            "0",
            "false",
            "no",
        }
        return cls(
            base_url=os.getenv(
                "K8S_INSPECTOR_MCP_ENDPOINT",
                "http://bosgenesis-k8s-inspector-mcp:8080",
            ),
            api_key=os.getenv("K8S_INSPECTOR_API_KEY") or os.getenv("BOSGENESIS_API_KEY"),
            enabled=enabled,
        )

    def collect(self, namespace: str, *, correlation_id: str) -> LiveSnapshot:
        if not self.enabled:
            return LiveSnapshot(warning="Live collection is disabled by configuration.")
        client = KubernetesInspectorRestDryRunClient(
            base_url=self.base_url,
            api_key=self.api_key,
            job_id=correlation_id,
            correlation_id=correlation_id,
        )
        resources: list[dict[str, Any]] = []
        complete_kinds: set[str] = set()
        evidence_refs: list[str] = []
        failures: list[str] = []
        for kind, method_name in self.COLLECTIONS:
            result = getattr(client, method_name)(namespace)
            evidence_refs.append(f"bosgenesis-k8s-inspector-mcp:{result.tool_name}")
            if not result.success:
                failures.append(f"{kind}:{result.error.error_code if result.error else 'failed'}")
                continue
            complete_kinds.add(kind)
            resources.extend(_collection_items(result.data or {}, kind, namespace))
        return LiveSnapshot(
            resources=resources,
            available=bool(complete_kinds),
            complete_kinds=complete_kinds,
            evidence_refs=evidence_refs,
            warning=("; ".join(failures) if failures else None),
        )


class NullLiveSnapshotCollector:
    def collect(self, namespace: str, *, correlation_id: str) -> LiveSnapshot:
        del namespace, correlation_id
        return LiveSnapshot(warning="No live snapshot collector is configured.")


def _collection_items(data: dict[str, Any], kind: str, namespace: str) -> list[dict[str, Any]]:
    candidates: Any = data
    for key in ("data", "items", "resources", kind.lower() + "s"):
        if isinstance(candidates, dict) and key in candidates:
            candidates = candidates[key]
    if isinstance(candidates, dict) and isinstance(candidates.get("items"), list):
        candidates = candidates["items"]
    if not isinstance(candidates, list):
        return []
    rows: list[dict[str, Any]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        if item.get("apiVersion") and item.get("kind") and item.get("metadata"):
            rows.append(item)
            continue
        name = item.get("name") or item.get("metadata", {}).get("name")
        if not name:
            continue
        rows.append(
            {
                "apiVersion": item.get("api_version") or "v1",
                "kind": kind,
                "metadata": {"name": name, "namespace": item.get("namespace") or namespace},
                "summary": item,
            }
        )
    return rows
