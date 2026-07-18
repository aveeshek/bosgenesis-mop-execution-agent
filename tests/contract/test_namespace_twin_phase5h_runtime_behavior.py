from __future__ import annotations

import shutil
from copy import deepcopy
from pathlib import Path

from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app
from bosgenesis_mop_execution_agent.namespace_twin import live_snapshot
from bosgenesis_mop_execution_agent.namespace_twin.delta import LiveSnapshot
from bosgenesis_mop_execution_agent.namespace_twin.live_snapshot import (
    KubernetesLiveSnapshotCollector,
)
from bosgenesis_mop_execution_agent.namespace_twin.persistence import NamespaceTwinRepository
from bosgenesis_mop_execution_agent.namespace_twin.service import NamespaceTwinService

FIXTURE = Path("tests/fixtures/sample_mop_bundle").resolve()


def _pod(*, ready: str = "1/1", restarts: int = 0, phase: str = "Running") -> dict:
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": "sample-app-7c9", "namespace": "sample-target"},
        "summary": {
            "name": "sample-app-7c9",
            "namespace": "sample-target",
            "phase": phase,
            "ready": ready,
            "restarts": restarts,
        },
    }


class RuntimeCollector:
    def __init__(self, resources: list[dict], events: list[dict] | None = None) -> None:
        self.resources = resources
        self.events = events if events is not None else []

    def collect(self, namespace: str, *, correlation_id: str) -> LiveSnapshot:
        del namespace, correlation_id
        return LiveSnapshot(
            resources=deepcopy(self.resources),
            available=True,
            complete_kinds={"Pod"},
            evidence_refs=["bosgenesis-k8s-inspector-mcp:pod.list"],
        )

    def collect_runtime(self, namespace: str, *, correlation_id: str) -> dict:
        del correlation_id
        return {
            "available": True,
            "namespace_summary": {"namespace": namespace, "pods": len(self.resources)},
            "events": deepcopy(self.events),
            "events_collected": True,
            "warning": None,
            "evidence_refs": ["bosgenesis-k8s-inspector-mcp:event.list"],
        }


def _service(tmp_path: Path, collector: RuntimeCollector) -> NamespaceTwinService:
    return NamespaceTwinService(
        NamespaceTwinRepository(f"sqlite+pysqlite:///{(tmp_path / 'phase5h.db').as_posix()}"),
        live_collector=collector,
    )


def _payload(tmp_path: Path, key: str) -> dict:
    bundle = tmp_path / "bundle"
    if not bundle.exists():
        shutil.copytree(FIXTURE, bundle)
    return {
        "source": {"type": "local_path", "value": str(bundle)},
        "target_namespace": "sample-target",
        "target_cluster": "contract-cluster",
        "idempotency_key": key,
    }


def test_healthy_current_runtime_is_low_rules_first_and_history_is_unavailable(tmp_path) -> None:
    service = _service(tmp_path, RuntimeCollector([_pod()]))

    created = service.create(_payload(tmp_path, "phase5h-healthy"), actor_id="operator")
    tab = service.runtime_behavior(created["twin_id"])
    data = tab["data"]

    assert tab["availability"]["state"] == "available"
    assert data["method"] == "rules_only"
    assert data["risk"] == "low"
    assert data["current_health"]["status"] == "healthy"
    assert data["historical_context_status"] == "not_available"
    assert data["may_independently_approve"] is False
    assert data["model_authority"] is False
    assert data["evidence_refs"][0]["redacted"] is True


def test_not_ready_restarts_and_warning_events_raise_explainable_runtime_risk(tmp_path) -> None:
    events = [
        {
            "type": "Warning",
            "reason": "BackOff",
            "message": "Back-off restarting failed container",
            "object": "Pod/sample-app-7c9",
        },
        {
            "type": "Warning",
            "reason": "Unhealthy",
            "message": "Readiness probe failed",
            "object": "Pod/sample-app-7c9",
        },
    ]
    service = _service(
        tmp_path,
        RuntimeCollector([_pod(ready="0/1", restarts=7)], events),
    )

    created = service.create(_payload(tmp_path, "phase5h-risk"), actor_id="operator")
    data = service.runtime_behavior(created["twin_id"])["data"]

    assert data["risk"] in {"high", "critical"}
    assert data["current_health"]["status"] == "unhealthy"
    assert data["current_health"]["not_ready_pods"] == 1
    assert data["current_health"]["restarting_pods"] == 1
    assert data["recent_event_counts"]["warnings"] == 2
    assert data["execution_effect"] in {"force_amber", "force_red"}
    assert {item["factor_id"] for item in data["factors"]} >= {
        "runtime_pods_not_ready",
        "runtime_pods_restarting",
        "runtime_crashloop_events",
    }


def test_runtime_refresh_can_restrict_but_never_rewrite_or_approve_final_decision(tmp_path) -> None:
    collector = RuntimeCollector([_pod()])
    service = _service(tmp_path, collector)
    created = service.create(_payload(tmp_path, "phase5h-restrict"), actor_id="operator")
    current = service.get(created["twin_id"])
    service.repository.transition(
        created["twin_id"],
        "decision_calculating",
        message="Authoritative dry-run evidence attached.",
    )
    service.repository.persist_terminal_decision(
        created["twin_id"],
        decision="green",
        report_hash="a" * 64,
        facts=current["foundation_facts"],
    )
    collector.resources = [_pod(ready="0/1", restarts=9)]
    collector.events = [
        {
            "type": "Warning",
            "reason": "BackOff",
            "message": "Back-off restarting failed container",
            "object": "Pod/sample-app-7c9",
        }
    ]

    refreshed = service.refresh_runtime_behavior(created["twin_id"], actor_id="operator")
    twin = service.get(created["twin_id"])
    actions = {item["code"]: item for item in twin["actions"]}

    assert refreshed["data"]["risk"] in {"high", "critical"}
    assert twin["decision"] == "green"
    assert twin["decision_is_final"] is True
    assert twin["autonomy_eligibility"] == "approval_required"
    assert actions["start_execution"]["enabled"] is False
    assert actions["start_execution"]["reason_code"] == "runtime_review_required"
    assert actions["refresh_runtime_behavior"]["enabled"] is True
    assert refreshed["data"]["may_independently_approve"] is False


def test_runtime_behavior_read_and_refresh_are_exposed_by_rest(tmp_path) -> None:
    collector = RuntimeCollector([_pod()])
    service = _service(tmp_path, collector)
    app = create_app()
    app.state.namespace_twin_service = service
    with TestClient(app) as client:
        created = client.post(
            "/v1/namespace-twins",
            json=_payload(tmp_path, "phase5h-rest"),
        ).json()["data"]
        initial = client.get(f"/v1/namespace-twins/{created['twin_id']}/runtime-behavior")
        refreshed = client.post(
            f"/v1/namespace-twins/{created['twin_id']}/runtime-behavior/refresh"
        )

    assert initial.status_code == 200
    assert initial.json()["data"]["data"]["rules_version"] == (
        "namespace-twin-runtime-behavior-1.0.0"
    )
    assert refreshed.status_code == 200
    assert refreshed.json()["data"]["data"]["model_authority"] is False
    assert refreshed.json()["data"]["data"]["historical_context_status"] == ("not_available")


def test_live_collector_falls_back_to_read_only_mcp_without_rest_key(monkeypatch) -> None:
    def fake_call(url: str, calls: list[tuple[str, dict]]) -> tuple[dict, list[str]]:
        assert url == "http://k8s-inspector.bosgenesis.local/mcp"
        payloads = {}
        for tool_name, arguments in calls:
            assert arguments["namespace"] == "agent-testing"
            if tool_name == "k8s_list_pods":
                payloads[tool_name] = {
                    "result": [
                        {
                            "name": "problem-pod",
                            "namespace": "agent-testing",
                            "phase": "Pending",
                            "ready": "0/1",
                            "restarts": 3,
                        }
                    ]
                }
            elif tool_name == "k8s_namespace_summary":
                payloads[tool_name] = {
                    "namespace": "agent-testing",
                    "counts": {"pods": 1},
                    "pods_by_phase": {"Pending": 1},
                }
            elif tool_name == "k8s_list_events":
                payloads[tool_name] = {
                    "result": [
                        {
                            "type": "Warning",
                            "reason": "BackOff",
                            "message": "Back-off restarting failed container",
                            "object": "Pod/problem-pod",
                        }
                    ]
                }
            else:
                payloads[tool_name] = {"result": []}
        return payloads, []

    monkeypatch.setattr(live_snapshot, "_call_read_only_mcp", fake_call)
    collector = KubernetesLiveSnapshotCollector(
        base_url="http://k8s-inspector.bosgenesis.local",
        api_key=None,
    )

    snapshot = collector.collect("agent-testing", correlation_id="phase5h-mcp")
    runtime = collector.collect_runtime("agent-testing", correlation_id="phase5h-mcp")

    assert snapshot.available is True
    assert "Pod" in snapshot.complete_kinds
    assert snapshot.resources[0]["summary"]["name"] == "problem-pod"
    assert runtime["available"] is True
    assert runtime["namespace_summary"]["counts"]["pods"] == 1
    assert runtime["events"][0]["reason"] == "BackOff"