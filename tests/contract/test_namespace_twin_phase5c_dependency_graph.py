from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app
from bosgenesis_mop_execution_agent.artifacts.bundle_validator import load_and_validate_bundle
from bosgenesis_mop_execution_agent.artifacts.models import (
    BundleSource,
    BundleSourceType,
    LoadedManifest,
)
from bosgenesis_mop_execution_agent.namespace_twin.delta import LiveSnapshot
from bosgenesis_mop_execution_agent.namespace_twin.dependency_graph import (
    build_dependency_graph,
    stable_node_id,
)
from bosgenesis_mop_execution_agent.namespace_twin.persistence import NamespaceTwinRepository
from bosgenesis_mop_execution_agent.namespace_twin.service import NamespaceTwinService
from bosgenesis_mop_execution_agent.plans.models import (
    MachinePlanCommand,
    MachinePlanPhase,
    MachinePlanStep,
)

FIXTURE = Path("tests/fixtures/sample_mop_bundle").resolve()
TARGET = "sample-target"


def _manifest(
    api_version: str,
    kind: str,
    name: str,
    content: dict,
    *,
    scope: str = "namespaced",
) -> LoadedManifest:
    metadata = content.setdefault("metadata", {})
    metadata.setdefault("name", name)
    if scope != "cluster":
        metadata.setdefault("namespace", TARGET)
    return LoadedManifest(
        path=f"generated/{kind.lower()}-{name}.yaml",
        document_index=0,
        api_version=api_version,
        kind=kind,
        name=name,
        namespace=None if scope == "cluster" else TARGET,
        scope=scope,
        content={"apiVersion": api_version, "kind": kind, **content},
    )


def _record(manifest: LoadedManifest) -> dict:
    namespace = None if manifest.scope == "cluster" else manifest.namespace or TARGET
    identity = f"{manifest.api_version}:{manifest.kind}:{namespace or '_cluster'}:{manifest.name}"
    return {
        "resource_id": f"resource-{manifest.kind}-{manifest.name}",
        "stable_identity": identity,
        "api_version": manifest.api_version,
        "kind": manifest.kind,
        "name": manifest.name,
        "namespace": namespace,
        "payload_redacted": {
            "path": manifest.path,
            "document_index": manifest.document_index,
            "manifest": manifest.content,
        },
    }


def _graph_bundle():
    source = BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(FIXTURE))
    base = load_and_validate_bundle(source, TARGET)
    config = _manifest("v1", "ConfigMap", "api-config", {"data": {"mode": "test"}})
    claim = _manifest(
        "v1",
        "PersistentVolumeClaim",
        "api-data",
        {"spec": {"accessModes": ["ReadWriteOnce"]}},
    )
    deployment = _manifest(
        "apps/v1",
        "Deployment",
        "api",
        {
            "metadata": {
                "labels": {"app.kubernetes.io/instance": "sample-release"},
            },
            "spec": {
                "selector": {"matchLabels": {"app": "api"}},
                "template": {
                    "metadata": {"labels": {"app": "api"}},
                    "spec": {
                        "serviceAccountName": "api-runner",
                        "volumes": [
                            {"name": "config", "configMap": {"name": "api-config"}},
                            {
                                "name": "data",
                                "persistentVolumeClaim": {"claimName": "api-data"},
                            },
                        ],
                        "containers": [
                            {
                                "name": "api",
                                "image": "example/api:v1",
                                "envFrom": [{"secretRef": {"name": "api-secret"}}],
                            }
                        ],
                    },
                },
            },
        },
    )
    service = _manifest("v1", "Service", "api", {"spec": {"selector": {"app": "api"}}})
    ingress = _manifest(
        "networking.k8s.io/v1",
        "Ingress",
        "api",
        {
            "spec": {
                "rules": [
                    {
                        "host": "api.example.test",
                        "http": {
                            "paths": [
                                {
                                    "path": "/",
                                    "backend": {"service": {"name": "api", "port": {"number": 80}}},
                                }
                            ]
                        },
                    }
                ]
            }
        },
    )
    crd = _manifest(
        "apiextensions.k8s.io/v1",
        "CustomResourceDefinition",
        "widgets.example.com",
        {"spec": {"group": "example.com", "names": {"kind": "Widget"}}},
        scope="cluster",
    )
    custom = _manifest("example.com/v1", "Widget", "sample", {"spec": {"enabled": True}})
    manifests = [config, claim, deployment, service, ingress, crd, custom]
    return base.model_copy(update={"manifests": manifests}), manifests


def test_dependency_builder_records_real_and_missing_edges() -> None:
    bundle, manifests = _graph_bundle()
    nodes, edges, findings, summary = build_dependency_graph(
        bundle, [_record(manifest) for manifest in manifests]
    )

    relationships = {edge["edge_type"] for edge in edges}
    assert {
        "configmap_ref",
        "secret_name_ref",
        "pvc_ref",
        "service_account_ref",
        "selector_matches",
        "route_backend",
        "helm_owns_resource",
        "crd_owns_custom_resource",
    } <= relationships
    assert all(edge["confidence"] and edge["evidence_refs"] for edge in edges)
    assert all(
        node["payload_redacted"]["node_id"] == stable_node_id(node["stable_identity"])
        for node in nodes
    )
    missing = {
        (node["kind"], node["name"])
        for node in nodes
        if node["payload_redacted"]["status"] == "missing"
    }
    assert {("Secret", "api-secret"), ("ServiceAccount", "api-runner")} <= missing
    assert not any(
        node["api_version"] == "reference.esda/v1"
        and node["kind"] == "CustomResourceDefinition"
        for node in nodes
    )
    assert summary["missing"] >= 2
    assert any(finding["code"] == "MISSING_DEPENDENCY" for finding in findings)


def test_dependency_builder_ignores_non_mapping_generated_references() -> None:
    bundle, manifests = _graph_bundle()
    deployment = next(item for item in manifests if item.kind == "Deployment")
    container = deployment.content["spec"]["template"]["spec"]["containers"][0]
    container["envFrom"] = [
        {"secretRef": "rendered-env-placeholder"},
        {"configMapRef": {"name": "api-config"}},
    ]
    deployment.content["spec"]["template"]["spec"]["volumes"].append(
        {"name": "templated", "secret": "rendered-volume-placeholder"}
    )
    container["env"] = [
        {
            "name": "TEMPLATED_SECRET",
            "valueFrom": {"secretKeyRef": "rendered-template-placeholder"},
        },
        {
            "name": "VALID_CONFIG",
            "valueFrom": {"configMapKeyRef": {"name": "api-config", "key": "mode"}},
        },
    ]

    nodes, edges, findings, summary = build_dependency_graph(
        bundle, [_record(manifest) for manifest in manifests]
    )

    assert nodes
    assert findings
    assert summary["edges"] == len(edges)
    assert any(edge["edge_type"] == "configmap_ref" for edge in edges)
    assert all(
        node["name"] != "rendered-template-placeholder"
        for node in nodes
    )


def test_namespace_default_service_account_is_present_dependency() -> None:
    bundle, manifests = _graph_bundle()
    deployment = next(item for item in manifests if item.kind == "Deployment")
    deployment.content["spec"]["template"]["spec"]["serviceAccountName"] = "default"

    nodes, edges, findings, _summary = build_dependency_graph(
        bundle, [_record(manifest) for manifest in manifests]
    )

    default_account = next(
        node
        for node in nodes
        if node["kind"] == "ServiceAccount" and node["name"] == "default"
    )
    assert default_account["payload_redacted"]["status"] == "present"
    assert default_account["payload_redacted"]["source"] == "kubernetes_namespace_default"
    assert any(
        edge["edge_type"] == "service_account_ref"
        and edge["source_identity"] == default_account["stable_identity"]
        for edge in edges
    )
    assert not any(
        finding["code"] == "MISSING_DEPENDENCY" and "default" in finding["message"]
        for finding in findings
    )


def test_ignored_manifest_ref_does_not_reappear_as_missing_plan_dependency() -> None:
    source = BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(FIXTURE))
    bundle = load_and_validate_bundle(source, TARGET)
    ignored = "generated/configmap-sample-app.yaml"

    nodes, _edges, findings, summary = build_dependency_graph(
        bundle,
        [],
        ignored_manifest_refs={ignored},
    )

    assert not any(
        node["kind"] == "ManifestArtifact" and node["name"] == ignored
        for node in nodes
    )
    assert not any(ignored in finding["message"] for finding in findings)
    assert summary["missing"] == 0


def test_twin_planning_includes_intended_helm_service_and_excludes_platform_configmaps(
    tmp_path,
) -> None:
    source = BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(FIXTURE))
    base = load_and_validate_bundle(source, TARGET)
    rendered = tmp_path / "rendered.yaml"
    rendered.write_text(
        """
apiVersion: v1
kind: Service
metadata:
  name: sample-release
  labels:
    app.kubernetes.io/instance: sample-release
spec:
  selector:
    app: sample
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata:
  name: sample-release
  labels:
    app.kubernetes.io/instance: sample-release
spec:
  rules:
    - host: sample.example.test
      http:
        paths:
          - path: /
            pathType: Prefix
            backend:
              service:
                name: sample-release
                port:
                  number: 80
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: kube-root-ca.crt
  labels:
    app.kubernetes.io/instance: sample-release
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: istio-ca-root-cert
  labels:
    app.kubernetes.io/instance: sample-release
---
apiVersion: v1
kind: ConfigMap
metadata:
  name: application-config
  labels:
    app.kubernetes.io/instance: sample-release
""".strip(),
        encoding="utf-8",
    )
    install_step = MachinePlanStep(
        step_id="install-sample",
        title="Install sample release",
        type="helm_upgrade",
        metadata={"release_name": "sample-release"},
        commands=[
            MachinePlanCommand(
                kind="dry_run",
                command="helm upgrade --install sample-release repo/sample --dry-run",
            )
        ],
    )
    install_phase = MachinePlanPhase(
        phase_id="install_sample",
        title="Install sample",
        objective="Install a new Helm release.",
        steps=[install_step],
    )
    plan = base.machine_plan.model_copy(
        update={"phases": [*base.machine_plan.phases, install_phase]}
    )
    bundle = base.model_copy(
        update={
            "root_path": tmp_path,
            "artifact_index_root_path": tmp_path,
            "artifact_index_json": {"rendered_manifests": ["rendered.yaml"]},
            "machine_plan": plan,
        }
    )
    service = NamespaceTwinService(
        NamespaceTwinRepository(
            f"sqlite+pysqlite:///{(tmp_path / 'planning.db').as_posix()}"
        )
    )
    planned_installs = service._planned_helm_install_releases(bundle)
    resources = service._resources(
        bundle,
        snapshot=LiveSnapshot(
            helm_inventory_available=True,
            installed_helm_releases=set(),
        ),
        planned_helm_installs=planned_installs,
    )

    identities = {(item["kind"], item["name"]) for item in resources}
    assert planned_installs == {"sample-release"}
    assert ("Service", "sample-release") in identities
    assert ("Ingress", "sample-release") in identities
    assert ("ConfigMap", "application-config") in identities
    assert ("ConfigMap", "kube-root-ca.crt") not in identities
    assert ("ConfigMap", "istio-ca-root-cert") not in identities

    nodes, edges, findings, _summary = build_dependency_graph(bundle, resources)
    assert any(
        edge["edge_type"] == "route_backend"
        and "Service:sample-target:sample-release" in edge["target_identity"]
        for edge in edges
    )
    assert not any(
        node["kind"] == "Service"
        and node["name"] == "sample-release"
        and node["payload_redacted"]["status"] == "missing"
        for node in nodes
    )
    assert not any(
        finding["code"] == "MISSING_DEPENDENCY"
        and "sample-release" in finding["message"]
        for finding in findings
    )

def test_real_dependency_graph_api_supports_filters_and_selected_context(tmp_path) -> None:
    service = NamespaceTwinService(
        NamespaceTwinRepository(f"sqlite+pysqlite:///{(tmp_path / 'phase5c.db').as_posix()}")
    )
    app = create_app()
    app.state.namespace_twin_service = service
    with TestClient(app) as client:
        created = client.post(
            "/v1/namespace-twins",
            json={
                "source": {"type": "local_path", "value": str(FIXTURE)},
                "target_namespace": TARGET,
                "target_cluster": "contract-cluster",
                "idempotency_key": "phase5c-api",
            },
        ).json()["data"]
        twin_id = created["twin_id"]
        response = client.get(
            f"/v1/namespace-twins/{twin_id}/dependency-graph",
            params={"relationship": "plan_applies", "limit": 25},
        )

        payload = response.json()["data"]
        selected_id = payload["data"]["table_rows"][0]["source"]
        selected = client.get(
            f"/v1/namespace-twins/{twin_id}/dependency-graph",
            params={"resource": selected_id, "limit": 25},
        ).json()["data"]
        searched = client.get(
            f"/v1/namespace-twins/{twin_id}/dependency-graph",
            params={
                "kind": "PlanPhase",
                "relationship": "plan_applies",
                "confidence": "deterministic",
                "search": "apply",
                "limit": 25,
            },
        ).json()["data"]

    assert response.status_code == 200
    assert payload["availability"]["state"] == "available"
    assert payload["data"]["summary"]["nodes"] >= 2
    assert payload["data"]["table_rows"]
    assert {edge["relationship"] for edge in payload["data"]["table_rows"]} == {"plan_applies"}
    assert payload["data"]["node_page"]["limit"] == 25
    assert selected["data"]["selected_context"]["found"] is True
    assert selected["data"]["selected_context"]["node"]["node_id"] == selected_id
    impact_path = selected["data"]["selected_context"]["impact_paths"][0]
    assert impact_path["nodes"]
    assert len(impact_path["relationships"]) == len(impact_path["nodes"]) - 1
    assert impact_path["confidence"] in {"deterministic", "high", "medium", "uncertain"}
    assert impact_path["evidence_refs"]
    assert all(isinstance(ref["evidence_id"], str) for ref in impact_path["evidence_refs"])
    assert searched["data"]["node_page"]["result_count"] == 1
    assert searched["data"]["edge_page"]["result_count"] == 1
    assert searched["data"]["table_rows"][0]["relationship"] == "plan_applies"
    assert all(ref["redacted"] for ref in payload["data"]["table_rows"][0]["evidence_refs"])
