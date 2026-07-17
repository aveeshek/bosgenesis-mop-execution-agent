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
        created = client.post("/v1/namespace-twins", json=_payload("phase5d-api")).json()[
            "data"
        ]
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
    assert assessment["decision_projection"]["precedence_rule"] == (
        "policy_deny_or_hard_block"
    )
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
