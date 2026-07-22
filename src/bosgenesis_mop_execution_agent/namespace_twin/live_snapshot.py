"""Namespace-scoped live snapshot collection for Namespace Twin facts."""

from __future__ import annotations

import asyncio
import json
import os
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any, Protocol

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from bosgenesis_mop_execution_agent.namespace_twin.delta import LiveSnapshot
from bosgenesis_mop_execution_agent.runtime.mcp_rest_adapters import (
    HelmManagerRestDryRunClient,
    KubernetesInspectorRestDryRunClient,
)


class LiveSnapshotCollector(Protocol):
    def collect(self, namespace: str, *, correlation_id: str) -> LiveSnapshot: ...


class KubernetesLiveSnapshotCollector:
    """Collect covered workload kinds through the namespace-scoped inspector."""

    COLLECTIONS = (
        ("Pod", "list_pods", "k8s_list_pods"),
        ("Service", "list_services", "k8s_list_services"),
        ("PersistentVolumeClaim", "list_pvcs", "k8s_list_pvcs"),
        ("Deployment", "list_deployments", "k8s_list_deployments"),
        ("StatefulSet", "list_statefulsets", "k8s_list_statefulsets"),
        ("Ingress", "list_ingresses", "k8s_list_ingresses"),
    )

    def __init__(
        self,
        *,
        base_url: str,
        api_key: str | None,
        enabled: bool = True,
        helm_base_url: str = "http://bosgenesis-helm-manager-mcp:8080",
        helm_api_key: str | None = None,
        helm_ignore_prefixes: tuple[str, ...] = ("bosgenesis-",),
        installed_helm_releases_only: bool = True,
    ) -> None:
        self.base_url = base_url
        self.api_key = api_key
        self.enabled = enabled
        self.helm_base_url = helm_base_url
        self.helm_api_key = helm_api_key
        self.helm_ignore_prefixes = tuple(
            prefix.strip() for prefix in helm_ignore_prefixes if prefix.strip()
        )
        self.installed_helm_releases_only = installed_helm_releases_only

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
            helm_base_url=os.getenv(
                "HELM_MANAGER_MCP_ENDPOINT",
                "http://bosgenesis-helm-manager-mcp:8080",
            ),
            helm_api_key=os.getenv("HELM_MANAGER_API_KEY") or os.getenv("BOSGENESIS_API_KEY"),
            helm_ignore_prefixes=_csv_property(
                "NAMESPACE_TWIN_HELM_IGNORE_PREFIXES",
                default=("bosgenesis-",),
            ),
            installed_helm_releases_only=_bool_property(
                "NAMESPACE_TWIN_HELM_INSTALLED_RELEASES_ONLY",
                default=True,
            ),
        )

    def collect(self, namespace: str, *, correlation_id: str) -> LiveSnapshot:
        if not self.enabled:
            return LiveSnapshot(warning="Live collection is disabled by configuration.")
        if not self.api_key:
            snapshot = self._collect_mcp(namespace, correlation_id=correlation_id)
            return self._with_helm_inventory(
                snapshot, namespace=namespace, correlation_id=correlation_id
            )

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
        for kind, method_name, _mcp_tool in self.COLLECTIONS:
            result = getattr(client, method_name)(namespace)
            evidence_refs.append(f"bosgenesis-k8s-inspector-mcp:{result.tool_name}")
            if not result.success:
                failures.append(f"{kind}:{result.error.error_code if result.error else 'failed'}")
                continue
            complete_kinds.add(kind)
            resources.extend(_collection_items(result.data or {}, kind, namespace))
        if not complete_kinds:
            snapshot = self._collect_mcp(
                namespace,
                correlation_id=correlation_id,
                prior_failures=failures,
            )
        else:
            snapshot = LiveSnapshot(
                resources=resources,
                available=True,
                complete_kinds=complete_kinds,
                evidence_refs=evidence_refs,
                warning=("; ".join(failures) if failures else None),
            )
        return self._with_helm_inventory(
            snapshot, namespace=namespace, correlation_id=correlation_id
        )

    def collect_runtime(self, namespace: str, *, correlation_id: str) -> dict[str, Any]:
        """Collect read-only namespace summary and event facts for runtime rules."""
        if not self.enabled:
            return {
                "available": False,
                "namespace_summary": {},
                "events": [],
                "warning": "Live collection is disabled by configuration.",
                "evidence_refs": [],
            }
        if not self.api_key:
            return self._collect_runtime_mcp(namespace, correlation_id=correlation_id)

        client = KubernetesInspectorRestDryRunClient(
            base_url=self.base_url,
            api_key=self.api_key,
            job_id=correlation_id,
            correlation_id=correlation_id,
        )
        summary_result = client.namespace_summary(namespace)
        events_result = client.list_events(namespace)
        failures = []
        if not summary_result.success:
            failures.append(
                "namespace_summary:"
                + (summary_result.error.error_code if summary_result.error else "failed")
            )
        if not events_result.success:
            failures.append(
                f"events:{events_result.error.error_code if events_result.error else 'failed'}"
            )
        if not summary_result.success and not events_result.success:
            return self._collect_runtime_mcp(
                namespace,
                correlation_id=correlation_id,
                prior_failures=failures,
            )
        return {
            "available": summary_result.success or events_result.success,
            "namespace_summary": _mapping_payload(summary_result.data or {}),
            "events": _list_payload(events_result.data or {}),
            "events_collected": events_result.success,
            "warning": "; ".join(failures) if failures else None,
            "evidence_refs": [
                "bosgenesis-k8s-inspector-mcp:namespace.summary",
                "bosgenesis-k8s-inspector-mcp:event.list",
            ],
        }

    def _collect_mcp(
        self,
        namespace: str,
        *,
        correlation_id: str,
        prior_failures: list[str] | None = None,
    ) -> LiveSnapshot:
        calls = [
            (tool_name, {"namespace": namespace, "actor": "bosgenesis-mop-execution-agent"})
            for _kind_name, _rest_method, tool_name in self.COLLECTIONS
        ]
        payloads, failures = _call_read_only_mcp(self._mcp_url(), calls)
        resources: list[dict[str, Any]] = []
        complete_kinds: set[str] = set()
        evidence_refs: list[str] = []
        for kind, _rest_method, tool_name in self.COLLECTIONS:
            evidence_refs.append(f"bosgenesis-k8s-inspector-mcp:{tool_name}")
            payload = payloads.get(tool_name)
            if payload is None:
                continue
            complete_kinds.add(kind)
            resources.extend(_collection_items(payload, kind, namespace))
        all_failures = [*(prior_failures or []), *failures]
        return LiveSnapshot(
            resources=resources,
            available=bool(complete_kinds),
            complete_kinds=complete_kinds,
            evidence_refs=evidence_refs,
            warning=("; ".join(all_failures) if all_failures else None),
        )

    def _collect_runtime_mcp(
        self,
        namespace: str,
        *,
        correlation_id: str,
        prior_failures: list[str] | None = None,
    ) -> dict[str, Any]:
        del correlation_id
        calls = [
            (
                "k8s_namespace_summary",
                {"namespace": namespace, "actor": "bosgenesis-mop-execution-agent"},
            ),
            (
                "k8s_list_events",
                {"namespace": namespace, "actor": "bosgenesis-mop-execution-agent"},
            ),
        ]
        payloads, failures = _call_read_only_mcp(self._mcp_url(), calls)
        summary = payloads.get("k8s_namespace_summary") or {}
        events_payload = payloads.get("k8s_list_events") or {}
        all_failures = [*(prior_failures or []), *failures]
        return {
            "available": bool(summary or events_payload),
            "namespace_summary": _mapping_payload(summary),
            "events": _list_payload(events_payload),
            "events_collected": "k8s_list_events" in payloads,
            "warning": "; ".join(all_failures) if all_failures else None,
            "evidence_refs": [
                "bosgenesis-k8s-inspector-mcp:k8s_namespace_summary",
                "bosgenesis-k8s-inspector-mcp:k8s_list_events",
            ],
        }

    def _mcp_url(self) -> str:
        base = self.base_url.rstrip("/")
        return base if base.endswith("/mcp") else f"{base}/mcp"

    def _helm_mcp_url(self) -> str:
        base = self.helm_base_url.rstrip("/")
        return base if base.endswith("/mcp") else f"{base}/mcp"

    def _with_helm_inventory(
        self,
        snapshot: LiveSnapshot,
        *,
        namespace: str,
        correlation_id: str,
    ) -> LiveSnapshot:
        rows, inventory_available, failures = self._collect_helm_releases(
            namespace, correlation_id=correlation_id
        )
        installed: set[str] = set()
        ignored: set[str] = set()
        for row in rows:
            release_namespace = str(row.get("namespace") or namespace).strip()
            if release_namespace != namespace:
                continue
            name = str(row.get("name") or row.get("release_name") or "").strip()
            if not name or not _is_installed_release(row):
                continue
            chart = str(row.get("chart") or row.get("chart_name") or "").strip()
            if _starts_with_prefix(name, self.helm_ignore_prefixes) or _starts_with_prefix(
                chart, self.helm_ignore_prefixes
            ):
                ignored.add(name)
                continue
            installed.add(name)

        resources = []
        for resource in snapshot.resources:
            release = _resource_helm_release(resource)
            if release in ignored or _explicit_ignored_release(
                resource, self.helm_ignore_prefixes
            ):
                continue
            resources.append(deepcopy(resource))

        warning_parts = [part for part in (snapshot.warning, *failures) if part]
        evidence_refs = [
            *snapshot.evidence_refs,
            "bosgenesis-helm-manager-mcp:helm_list_releases",
        ]
        return LiveSnapshot(
            resources=resources,
            available=snapshot.available,
            complete_kinds=set(snapshot.complete_kinds),
            evidence_refs=list(dict.fromkeys(evidence_refs)),
            warning="; ".join(warning_parts) if warning_parts else None,
            helm_inventory_available=(
                inventory_available and self.installed_helm_releases_only
            ),
            installed_helm_releases=installed,
            ignored_helm_releases=ignored,
            ignored_helm_prefixes=self.helm_ignore_prefixes,
        )

    def _collect_helm_releases(
        self,
        namespace: str,
        *,
        correlation_id: str,
    ) -> tuple[list[dict[str, Any]], bool, list[str]]:
        failures: list[str] = []
        if self.helm_api_key:
            client = HelmManagerRestDryRunClient(
                base_url=self.helm_base_url,
                api_key=self.helm_api_key,
                job_id=correlation_id,
                correlation_id=correlation_id,
            )
            result = client.list_releases(namespace=namespace, all_statuses=False)
            if result.success:
                return _helm_release_rows(result.data or {}), True, failures
            failures.append(
                "HelmRelease:"
                + (result.error.error_code.value if result.error else "inventory_failed")
            )

        payloads, mcp_failures = _call_read_only_mcp(
            self._helm_mcp_url(),
            [
                (
                    "helm_list_releases",
                    {
                        "namespace": namespace,
                        "all_statuses": False,
                        "actor": "bosgenesis-mop-execution-agent",
                    },
                )
            ],
        )
        failures.extend(mcp_failures)
        payload = payloads.get("helm_list_releases")
        return (_helm_release_rows(payload or {}), payload is not None, failures)


class NullLiveSnapshotCollector:
    def collect(self, namespace: str, *, correlation_id: str) -> LiveSnapshot:
        del namespace, correlation_id
        return LiveSnapshot(warning="No live snapshot collector is configured.")

    def collect_runtime(self, namespace: str, *, correlation_id: str) -> dict[str, Any]:
        del namespace, correlation_id
        return {
            "available": False,
            "namespace_summary": {},
            "events": [],
            "warning": "No live snapshot collector is configured.",
            "evidence_refs": [],
        }


def _call_read_only_mcp(
    url: str,
    calls: list[tuple[str, dict[str, Any]]],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Run read-only MCP calls in a worker thread so async routes remain safe."""

    async def invoke() -> tuple[dict[str, dict[str, Any]], list[str]]:
        payloads: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        try:
            async with streamable_http_client(url) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    for tool_name, arguments in calls:
                        try:
                            result = await session.call_tool(tool_name, arguments)
                        except Exception as exc:  # MCP transport errors are normalized below.
                            failures.append(f"{tool_name}:mcp_unavailable:{type(exc).__name__}")
                            continue
                        if result.isError:
                            failures.append(f"{tool_name}:mcp_tool_error")
                            continue
                        payloads[tool_name] = _mcp_payload(result)
        except Exception as exc:  # Connection/session setup failure.
            failures.append(f"mcp_session:mcp_unavailable:{type(exc).__name__}")
        return payloads, failures

    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="namespace-twin-mcp") as executor:
        return executor.submit(lambda: asyncio.run(invoke())).result(timeout=120)


def _mcp_payload(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    for content in getattr(result, "content", []) or []:
        text = getattr(content, "text", None)
        if not text:
            continue
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
        if isinstance(parsed, list):
            return {"result": parsed}
    return {}


def _collection_items(data: dict[str, Any], kind: str, namespace: str) -> list[dict[str, Any]]:
    candidates: Any = data
    for key in ("response", "data", "result", "items", "resources", kind.lower() + "s"):
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


def _list_payload(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("response", "data", "result", "items", "events"):
        candidate = data.get(key)
        if isinstance(candidate, list):
            return [item for item in candidate if isinstance(item, dict)]
        if isinstance(candidate, dict):
            nested = _list_payload(candidate)
            if nested:
                return nested
    return []


def _mapping_payload(data: dict[str, Any]) -> dict[str, Any]:
    for key in ("data", "response", "summary"):
        candidate = data.get(key)
        if isinstance(candidate, dict):
            return candidate
    return data if isinstance(data, dict) else {}


def _helm_release_rows(data: dict[str, Any]) -> list[dict[str, Any]]:
    def find(value: Any) -> list[dict[str, Any]] | None:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if not isinstance(value, dict):
            return None
        for key in ("output", "releases", "items", "result", "data", "response"):
            if key not in value:
                continue
            rows = find(value[key])
            if rows is not None:
                return rows
        return None

    return find(data) or []


def _is_installed_release(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "").strip().casefold().replace("_", "-")
    return status not in {"uninstalled", "uninstalling", "superseded"}


def _resource_helm_release(resource: dict[str, Any]) -> str | None:
    metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
    annotations = (
        metadata.get("annotations") if isinstance(metadata.get("annotations"), dict) else {}
    )
    labels = metadata.get("labels") if isinstance(metadata.get("labels"), dict) else {}
    value = (
        annotations.get("meta.helm.sh/release-name")
        or labels.get("app.kubernetes.io/instance")
        or labels.get("release")
    )
    return str(value).strip() if value else None


def _explicit_ignored_release(resource: dict[str, Any], prefixes: tuple[str, ...]) -> bool:
    metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
    annotations = (
        metadata.get("annotations") if isinstance(metadata.get("annotations"), dict) else {}
    )
    release = annotations.get("meta.helm.sh/release-name")
    return bool(release and _starts_with_prefix(str(release), prefixes))


def _starts_with_prefix(value: str, prefixes: tuple[str, ...]) -> bool:
    folded = value.casefold()
    return bool(folded and any(folded.startswith(prefix.casefold()) for prefix in prefixes))


def _csv_property(name: str, *, default: tuple[str, ...]) -> tuple[str, ...]:
    value = os.getenv(name)
    if value is None:
        return default
    return tuple(item.strip() for item in value.split(",") if item.strip())


def _bool_property(name: str, *, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().casefold() not in {"0", "false", "no", "off"}
