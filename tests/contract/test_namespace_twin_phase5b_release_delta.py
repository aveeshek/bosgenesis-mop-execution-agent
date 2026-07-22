from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app
from bosgenesis_mop_execution_agent.namespace_twin.canonicalization import (
    canonicalize_kubernetes_object,
)
from bosgenesis_mop_execution_agent.namespace_twin.delta import (
    LiveSnapshot,
    calculate_release_delta,
)
from bosgenesis_mop_execution_agent.namespace_twin.persistence import NamespaceTwinRepository
from bosgenesis_mop_execution_agent.namespace_twin.service import NamespaceTwinService

FIXTURE = Path("tests/fixtures/sample_mop_bundle").resolve()


class FakeCollector:
    def __init__(self, snapshot: LiveSnapshot) -> None:
        self.snapshot = snapshot

    def collect(self, namespace: str, *, correlation_id: str) -> LiveSnapshot:
        assert namespace
        assert correlation_id.startswith("twin-snapshot-")
        return self.snapshot


def _manifest(
    *,
    kind: str = "ConfigMap",
    name: str = "sample-app-config",
    data: dict | None = None,
) -> dict:
    body = {
        "apiVersion": "apps/v1" if kind == "Deployment" else "v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": "sample-target"},
    }
    if kind == "Deployment":
        body["spec"] = {
            "replicas": 1,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "containers": [
                        {
                            "name": "app",
                            "image": "example/app:v2",
                            "resources": {"requests": {"cpu": "100m", "memory": "1Gi"}},
                        }
                    ]
                },
            },
        }
    else:
        body["data"] = data or {"mode": "demo", "feature_flag": "false"}
    return body


def _record(manifest: dict, path: str = "generated/configmap-sample-app.yaml") -> dict:
    metadata = manifest["metadata"]
    return {
        "resource_id": "resource-1",
        "stable_identity": (
            f"{manifest['apiVersion']}:{manifest['kind']}:"
            f"{metadata['namespace']}:{metadata['name']}"
        ),
        "api_version": manifest["apiVersion"],
        "kind": manifest["kind"],
        "name": metadata["name"],
        "namespace": metadata["namespace"],
        "payload_redacted": {"path": path, "document_index": 0, "manifest": manifest},
    }


def test_canonicalization_removes_runtime_noise_and_normalizes_intent() -> None:
    first = _manifest(kind="Deployment", name="api")
    first["metadata"].update(
        {"uid": "volatile", "resourceVersion": "99", "creationTimestamp": "now"}
    )
    first["status"] = {"readyReplicas": 1}
    second = _manifest(kind="Deployment", name="api")
    second["spec"]["template"]["spec"]["containers"][0]["resources"]["requests"] = {
        "memory": "1073741824",
        "cpu": "0.1",
    }

    assert canonicalize_kubernetes_object(first) == canonicalize_kubernetes_object(second)


def test_delta_actions_risk_and_omission_never_means_delete() -> None:
    planned = _record(_manifest())
    complete = LiveSnapshot(
        resources=[_manifest(name="unmanaged")],
        available=True,
        complete_kinds={"ConfigMap"},
    )
    created = calculate_release_delta([planned], complete, target_namespace="sample-target")
    assert [row["action"] for row in created] == ["create"]
    assert all(row["name"] != "unmanaged" for row in created)

    no_op_live = _manifest()
    no_op_live["metadata"]["uid"] = "runtime-only"
    no_op_live["status"] = {"observed": True}
    no_op = calculate_release_delta(
        [planned],
        LiveSnapshot(resources=[no_op_live], available=True, complete_kinds={"ConfigMap"}),
        target_namespace="sample-target",
    )
    assert no_op[0]["action"] == "no_op"

    changed = _manifest(data={"mode": "production"})
    updated = calculate_release_delta(
        [planned],
        LiveSnapshot(resources=[changed], available=True, complete_kinds={"ConfigMap"}),
        target_namespace="sample-target",
    )
    assert updated[0]["action"] == "update"
    assert "data" in updated[0]["canonical_diff"]

    deployment = _manifest(kind="Deployment", name="api")
    live_deployment = _manifest(kind="Deployment", name="api")
    live_deployment["spec"]["selector"] = {"matchLabels": {"app": "old"}}
    conflict = calculate_release_delta(
        [_record(deployment, "generated/deployment-api.yaml")],
        LiveSnapshot(resources=[live_deployment], available=True, complete_kinds={"Deployment"}),
        target_namespace="sample-target",
    )
    assert conflict[0]["action"] == "immutable_conflict"
    assert conflict[0]["risk"] == "critical"

    unknown = calculate_release_delta([planned], LiveSnapshot(), target_namespace="sample-target")
    assert unknown[0]["action"] == "unknown"


def test_explicit_delete_requires_machine_plan_evidence() -> None:
    manifest = _manifest()
    record = _record(manifest)
    rows = calculate_release_delta(
        [record],
        LiveSnapshot(resources=[manifest], available=True, complete_kinds={"ConfigMap"}),
        target_namespace="sample-target",
        explicit_deletes=[{"manifest_refs": ["generated/configmap-sample-app.yaml"]}],
    )
    assert len(rows) == 1
    assert rows[0]["action"] == "explicit_delete"


def test_helm_upgrade_identity_requires_installed_target_release_and_honors_ignore_prefix() -> None:
    planned_manifest = _manifest(data={"mode": "new"})
    planned_manifest["metadata"]["labels"] = {"app.kubernetes.io/instance": "candidate-release"}
    current_manifest = _manifest(data={"mode": "old"})
    current_manifest["metadata"]["labels"] = {"app.kubernetes.io/instance": "candidate-release"}
    record = _record(planned_manifest)

    unverified = calculate_release_delta(
        [record],
        LiveSnapshot(
            resources=[current_manifest],
            available=True,
            complete_kinds={"ConfigMap"},
            helm_inventory_available=True,
            installed_helm_releases={"another-release"},
        ),
        target_namespace="sample-target",
    )
    installed = calculate_release_delta(
        [record],
        LiveSnapshot(
            resources=[current_manifest],
            available=True,
            complete_kinds={"ConfigMap"},
            helm_inventory_available=True,
            installed_helm_releases={"candidate-release"},
        ),
        target_namespace="sample-target",
    )

    assert unverified == []
    assert installed[0]["helm_release"] == "candidate-release"

    absent_create = calculate_release_delta(
        [record],
        LiveSnapshot(
            available=True,
            complete_kinds={"ConfigMap"},
            helm_inventory_available=True,
            installed_helm_releases=set(),
        ),
        target_namespace="sample-target",
    )
    assert absent_create == []

    intentional_install = calculate_release_delta(
        [record],
        LiveSnapshot(
            available=True,
            complete_kinds={"ConfigMap"},
            helm_inventory_available=True,
            installed_helm_releases=set(),
        ),
        target_namespace="sample-target",
        planned_helm_installs={"candidate-release"},
    )
    assert intentional_install[0]["action"] == "create"
    assert intentional_install[0]["helm_release"] == "candidate-release"

    ignored_manifest = _manifest(name="internal")
    ignored_manifest["metadata"]["labels"] = {"app.kubernetes.io/instance": "bosgenesis-internal"}
    ignored = calculate_release_delta(
        [_record(ignored_manifest)],
        LiveSnapshot(
            available=True,
            complete_kinds={"ConfigMap"},
            ignored_helm_prefixes=("bosgenesis-",),
        ),
        target_namespace="sample-target",
    )
    assert ignored == []


def test_real_release_delta_api_is_filterable_and_contract_shaped(tmp_path) -> None:
    current = _manifest(data={"mode": "old"})
    service = NamespaceTwinService(
        NamespaceTwinRepository(f"sqlite+pysqlite:///{(tmp_path / 'phase5b.db').as_posix()}"),
        live_collector=FakeCollector(
            LiveSnapshot(
                resources=[current],
                available=True,
                complete_kinds={"ConfigMap"},
                evidence_refs=["bosgenesis-k8s-inspector-mcp:configmap.list"],
            )
        ),
    )
    app = create_app()
    app.state.namespace_twin_service = service
    with TestClient(app) as client:
        created = client.post(
            "/v1/namespace-twins",
            json={
                "source": {"type": "local_path", "value": str(FIXTURE)},
                "target_namespace": "sample-target",
                "target_cluster": "contract-cluster",
                "idempotency_key": "phase5b-api",
            },
        ).json()["data"]
        response = client.get(
            f"/v1/namespace-twins/{created['twin_id']}/release-delta",
            params={"action": "update", "risk": "low", "limit": 25},
        )
        empty_filter = client.get(
            f"/v1/namespace-twins/{created['twin_id']}/release-delta",
            params={"risk": "critical", "limit": 25},
        )

    assert response.status_code == 200
    empty_payload = empty_filter.json()["data"]
    assert empty_payload["availability"]["state"] == "available"
    assert (
        empty_payload["availability"]["message"]
        == "No Release Delta rows match the selected filters."
    )
    assert empty_payload["data"]["changes"] == []
    assert empty_payload["data"]["summary"]["total"] == 1

    payload = response.json()["data"]
    assert payload["availability"]["state"] == "available"
    assert payload["data"]["summary"]["update"] == 1
    assert payload["data"]["page"] == {
        "limit": 25,
        "has_more": False,
        "next_cursor": None,
        "result_count": 1,
    }
    change = payload["data"]["changes"][0]
    assert change["action"] == "update"
    assert change["redacted"] is True
    assert {ref["source_type"] for ref in change["evidence_refs"]} == {
        "bundle",
        "kubernetes",
    }
