from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app
from bosgenesis_mop_execution_agent.artifacts.bundle_validator import load_and_validate_bundle
from bosgenesis_mop_execution_agent.artifacts.models import (
    BundleSource,
    BundleSourceType,
    LoadedManifest,
    LoadedValuesFile,
)
from bosgenesis_mop_execution_agent.namespace_twin.delta import LiveSnapshot
from bosgenesis_mop_execution_agent.namespace_twin.models import (
    POLICY_VERSION,
    RISK_RULE_VERSION,
)
from bosgenesis_mop_execution_agent.namespace_twin.persistence import NamespaceTwinRepository
from bosgenesis_mop_execution_agent.namespace_twin.policy_twin import (
    POLICY_BUNDLE_HASH,
    evaluate_policy_twin,
)
from bosgenesis_mop_execution_agent.namespace_twin.service import NamespaceTwinService

FIXTURE = Path("tests/fixtures/sample_mop_bundle").resolve()
TARGET = "sample-target"


class FakeCollector:
    def __init__(self, snapshot: LiveSnapshot) -> None:
        self.snapshot = snapshot

    def collect(self, namespace: str, *, correlation_id: str) -> LiveSnapshot:
        assert namespace == TARGET
        assert correlation_id.startswith("twin-snapshot-")
        return self.snapshot


def _source() -> BundleSource:
    return BundleSource(type=BundleSourceType.LOCAL_PATH, value=str(FIXTURE))


def _payload(key: str) -> dict:
    return {
        "source": _source().model_dump(mode="json"),
        "target_namespace": TARGET,
        "target_cluster": "contract-cluster",
        "idempotency_key": key,
    }


def test_real_policy_api_preserves_three_axes_and_preliminary_core_decision(tmp_path) -> None:
    bundle = load_and_validate_bundle(_source(), TARGET)
    snapshot = LiveSnapshot(
        resources=[bundle.manifests[0].content],
        available=True,
        complete_kinds={"ConfigMap"},
        evidence_refs=["bosgenesis-k8s-inspector-mcp:configmap.list"],
    )
    service = NamespaceTwinService(
        NamespaceTwinRepository(f"sqlite+pysqlite:///{(tmp_path / 'phase5d.db').as_posix()}"),
        live_collector=FakeCollector(snapshot),
    )
    app = create_app()
    app.state.namespace_twin_service = service

    with TestClient(app) as client:
        created = client.post("/v1/namespace-twins", json=_payload("phase5d-api")).json()["data"]
        response = client.get(f"/v1/namespace-twins/{created['twin_id']}/policy")
        filtered = client.get(
            f"/v1/namespace-twins/{created['twin_id']}/policy",
            params={"effect": "approval_required"},
        )

    assert response.status_code == 200
    policy = response.json()["data"]
    data = policy["data"]
    assert response.headers["x-data-mode"] == "real_core"
    assert policy["availability"]["state"] == "available"
    assert data["verdict"] == "allow_with_approval"
    assert data["policy_version"] == POLICY_VERSION
    assert data["policy_bundle_hash"] == POLICY_BUNDLE_HASH
    assert data["evidence_axis"]["completeness"] == "partial"
    assert data["evidence_axis"]["freshness"] == "fresh"
    assert data["risk_axis"]["rules_version"] == RISK_RULE_VERSION
    assert data["risk_axis"]["score"] == 20
    assert data["risk_axis"]["feature_toggles"]["pvc_risk_enabled"] is False
    assert len(data["risk_axis"]["contributions"]) == 13
    assert data["decision_projection"]["level"] == "amber"
    assert data["decision_projection"]["precedence_rule"] == "approval_required"
    assert data["decision_projection"]["decision_is_final"] is False
    assert data["model_authority"] is False
    assert len(data["rule_contributions"]) >= 30
    approval_contribution = next(
        item
        for item in data["rule_contributions"]
        if item["axis"] == "policy" and item["rule"] == "approval_policy"
    )
    assert approval_contribution["matched"] is True
    assert approval_contribution["effect"] == "approval_required"
    assert filtered.status_code == 200
    assert all(
        item["status"] == "approval_required"
        for item in filtered.json()["data"]["data"]["findings"]
    )
    assert created["decision"] == "pending"
    assert created["decision_is_final"] is False
    assert created["lifecycle_status"] == "awaiting_dry_run"
    assert created["tab_states"]["policy"]["state"] == "available"
    assert "policy" not in created["optional_states"]


def test_pvc_risk_is_disabled_by_default_and_can_be_enabled() -> None:
    bundle = load_and_validate_bundle(_source(), TARGET)
    pvc_delta = {
        "resource_id": f"PersistentVolumeClaim:{TARGET}:sample-data",
        "kind": "PersistentVolumeClaim",
        "namespace": TARGET,
        "name": "sample-data",
        "action": "create",
        "risk": "high",
        "canonical_diff": "{}",
        "reason": "PVC is planned but absent from the target namespace.",
        "evidence_refs": ["generated/pvc.yaml"],
    }
    arguments = {
        "bundle": bundle,
        "planned_resources": [],
        "deltas": [pvc_delta],
        "snapshot": LiveSnapshot(
            available=True,
            complete_kinds={"PersistentVolumeClaim"},
        ),
        "provenance": {"artifact_index_present": True},
        "graph_summary": {"missing": 0},
        "explicit_deletes": [],
        "input_hash": "b" * 64,
        "target_namespace": TARGET,
        "evaluated_at": datetime(2026, 7, 20, tzinfo=UTC),
    }

    disabled = evaluate_policy_twin(**arguments)
    enabled = evaluate_policy_twin(**arguments, pvc_risk_enabled=True)
    disabled_rule = next(
        item
        for item in disabled["risk_axis"]["contributions"]
        if item["rule"] == "pvc_create_or_explicit_delete"
    )
    enabled_rule = next(
        item
        for item in enabled["risk_axis"]["contributions"]
        if item["rule"] == "pvc_create_or_explicit_delete"
    )

    assert disabled_rule["matched"] is False
    assert disabled_rule["contribution"] == 0
    assert "disabled by configuration" in disabled_rule["reason"]
    assert disabled["risk_axis"]["feature_toggles"]["pvc_risk_enabled"] is False
    assert enabled_rule["matched"] is True
    assert enabled_rule["contribution"] == 30
    assert enabled["risk_axis"]["feature_toggles"]["pvc_risk_enabled"] is True


def test_inferred_helm_values_only_score_an_installed_release_delta() -> None:
    bundle = load_and_validate_bundle(_source(), TARGET)
    phase = bundle.machine_plan.phases[0]
    inferred_step = phase.steps[0].model_copy(
        update={
            "type": "helm_upgrade",
            "inference": {"source": "chart"},
        }
    )
    inferred_plan = bundle.machine_plan.model_copy(
        update={"phases": [phase.model_copy(update={"steps": [inferred_step]})]}
    )
    inferred_bundle = bundle.model_copy(update={"machine_plan": inferred_plan})
    base = {
        "bundle": inferred_bundle,
        "planned_resources": [],
        "snapshot": LiveSnapshot(available=True, complete_kinds={"ConfigMap"}),
        "provenance": {"artifact_index_present": True},
        "graph_summary": {"missing": 0},
        "explicit_deletes": [],
        "input_hash": "d" * 64,
        "target_namespace": TARGET,
        "evaluated_at": datetime(2026, 7, 22, tzinfo=UTC),
    }
    raw_resource = {
        "kind": "ConfigMap",
        "name": "raw-config",
        "namespace": TARGET,
        "action": "create",
        "canonical_diff": "{}",
        "evidence_refs": ["generated/raw-config.yaml"],
        "helm_release": None,
    }
    installed_release = {**raw_resource, "helm_release": "installed-release"}

    raw_assessment = evaluate_policy_twin(**base, deltas=[raw_resource])
    installed_assessment = evaluate_policy_twin(**base, deltas=[installed_release])
    raw_rule = next(
        item
        for item in raw_assessment["risk_axis"]["contributions"]
        if item["rule"] == "inferred_chart_or_value"
    )
    installed_rule = next(
        item
        for item in installed_assessment["risk_axis"]["contributions"]
        if item["rule"] == "inferred_chart_or_value"
    )

    assert raw_rule["matched"] is False
    assert raw_rule["contribution"] == 0
    assert installed_rule["matched"] is True
    assert installed_rule["contribution"] == 20


def test_explicit_rendered_helm_and_values_do_not_score_as_inferred() -> None:
    bundle = load_and_validate_bundle(_source(), TARGET)
    phase = bundle.machine_plan.phases[0]
    values_path = "deployment-artifacts/helm/values.yaml"
    inferred_step = phase.steps[0].model_copy(
        update={
            "type": "helm_upgrade",
            "inference": {"source": "chart"},
            "values_refs": [values_path],
        }
    )
    plan = bundle.machine_plan.model_copy(
        update={"phases": [phase.model_copy(update={"steps": [inferred_step]})]}
    )
    explicit_bundle = bundle.model_copy(
        update={
            "machine_plan": plan,
            "values_files": [LoadedValuesFile(path=values_path, content={})],
        }
    )
    assessment = evaluate_policy_twin(
        bundle=explicit_bundle,
        planned_resources=[
            {
                "payload_redacted": {
                    "source": "helm_rendered_manifest",
                    "manifest": {
                        "metadata": {
                            "labels": {"app.kubernetes.io/instance": "installed-release"}
                        }
                    },
                }
            }
        ],
        deltas=[
            {
                "kind": "ConfigMap",
                "name": "rendered-config",
                "namespace": TARGET,
                "action": "create",
                "canonical_diff": "{}",
                "evidence_refs": ["rendered.yaml"],
                "helm_release": "installed-release",
            }
        ],
        snapshot=LiveSnapshot(available=True, complete_kinds={"ConfigMap"}),
        provenance={"artifact_index_present": True},
        graph_summary={"missing": 0},
        explicit_deletes=[],
        input_hash="e" * 64,
        target_namespace=TARGET,
        evaluated_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    rule = next(
        item
        for item in assessment["risk_axis"]["contributions"]
        if item["rule"] == "inferred_chart_or_value"
    )

    assert rule["matched"] is False
    assert rule["contribution"] == 0


def test_observed_helm_provenance_does_not_score_as_inferred() -> None:
    bundle = load_and_validate_bundle(_source(), TARGET)
    phase = bundle.machine_plan.phases[0]
    observed_step = phase.steps[0].model_copy(
        update={
            "type": "helm_upgrade",
            "inference": {
                "label": "observed",
                "confidence": "medium",
                "rationale": "Chart and values were collected from explicit release evidence.",
            },
            "values_refs": ["values/values-release.yaml"],
        }
    )
    plan = bundle.machine_plan.model_copy(
        update={"phases": [phase.model_copy(update={"steps": [observed_step]})]}
    )
    observed_bundle = bundle.model_copy(update={"machine_plan": plan})

    assessment = evaluate_policy_twin(
        bundle=observed_bundle,
        planned_resources=[],
        deltas=[
            {
                "kind": "ConfigMap",
                "name": "release-config",
                "namespace": TARGET,
                "action": "create",
                "canonical_diff": "{}",
                "evidence_refs": ["values/values-release.yaml"],
                "helm_release": "release",
            }
        ],
        snapshot=LiveSnapshot(available=True, complete_kinds={"ConfigMap"}),
        provenance={"artifact_index_present": True},
        graph_summary={"missing": 0},
        explicit_deletes=[],
        input_hash="f" * 64,
        target_namespace=TARGET,
        evaluated_at=datetime(2026, 7, 22, tzinfo=UTC),
    )
    rule = next(
        item
        for item in assessment["risk_axis"]["contributions"]
        if item["rule"] == "inferred_chart_or_value"
    )

    assert rule["matched"] is False
    assert rule["contribution"] == 0

def test_machine_plan_rollback_commands_prevent_false_missing_rollback() -> None:
    bundle = load_and_validate_bundle(_source(), TARGET)
    phase = bundle.machine_plan.phases[0]
    step = phase.steps[0].model_copy(
        update={"rollback_commands": ["kubectl delete configmap sample -n sample-target"]}
    )
    plan = bundle.machine_plan.model_copy(
        update={"phases": [phase.model_copy(update={"steps": [step]})]}
    )
    bundle_with_rollback = bundle.model_copy(update={"machine_plan": plan})

    assessment = evaluate_policy_twin(
        bundle=bundle_with_rollback,
        planned_resources=[],
        deltas=[
            {
                "resource_id": f"ConfigMap:{TARGET}:sample-config",
                "kind": "ConfigMap",
                "namespace": TARGET,
                "name": "sample-config",
                "action": "create",
                "risk": "medium",
                "canonical_diff": "{}",
                "reason": "ConfigMap is planned but absent.",
                "evidence_refs": ["generated/configmap.yaml"],
            }
        ],
        snapshot=LiveSnapshot(available=True, complete_kinds={"ConfigMap"}),
        provenance={"artifact_index_present": True},
        graph_summary={"missing": 0},
        explicit_deletes=[],
        input_hash="c" * 64,
        target_namespace=TARGET,
        evaluated_at=datetime(2026, 7, 20, tzinfo=UTC),
    )
    rollback_rule = next(
        item
        for item in assessment["risk_axis"]["contributions"]
        if item["rule"] == "missing_rollback_step"
    )

    assert rollback_rule["matched"] is False
    assert rollback_rule["contribution"] == 0


def test_existing_policy_engine_hard_block_has_red_precedence() -> None:
    bundle = load_and_validate_bundle(_source(), TARGET)
    cluster_role = LoadedManifest(
        path="generated/clusterrole.yaml",
        document_index=0,
        api_version="rbac.authorization.k8s.io/v1",
        kind="ClusterRole",
        name="unsafe",
        namespace=None,
        scope="cluster",
        content={
            "apiVersion": "rbac.authorization.k8s.io/v1",
            "kind": "ClusterRole",
            "metadata": {"name": "unsafe"},
            "rules": [],
        },
    )
    blocked = bundle.model_copy(update={"manifests": [*bundle.manifests, cluster_role]})

    assessment = evaluate_policy_twin(
        bundle=blocked,
        planned_resources=[],
        deltas=[],
        snapshot=LiveSnapshot(available=True, complete_kinds={"ConfigMap"}),
        provenance={"artifact_index_present": True},
        graph_summary={"missing": 0},
        explicit_deletes=[],
        input_hash="a" * 64,
        target_namespace=TARGET,
        evaluated_at=datetime(2026, 7, 16, tzinfo=UTC),
    )

    assert assessment["policy_axis"]["verdict"] == "deny"
    assert "CLUSTER_SCOPED_RESOURCE_BLOCKED" in assessment["policy_axis"]["hard_blocks"]
    assert assessment["decision_projection"]["level"] == "red"
    assert assessment["decision_projection"]["precedence_rule"] == ("policy_deny_or_hard_block")
    assert assessment["decision_projection"]["model_authority"] is False
    cluster_scope = next(
        item
        for item in assessment["rule_contributions"]
        if item["axis"] == "policy" and item["rule"] == "cluster_scope"
    )
    assert cluster_scope["matched"] is True
    assert cluster_scope["effect"] == "deny"
    assert "ClusterRole" in cluster_scope["reason"]


def test_policy_assessment_survives_restart_without_recalculation(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'restart.db').as_posix()}"
    first = NamespaceTwinService(NamespaceTwinRepository(database_url))
    created = first.create(_payload("phase5d-restart"), actor_id="operator")
    before = first.policy(created["twin_id"])["data"]

    restarted = NamespaceTwinService(NamespaceTwinRepository(database_url))
    after = restarted.policy(created["twin_id"])["data"]

    assert after == before
    assert after["decision_projection"]["axes_hash"]
    assert after["model_authority"] is False
