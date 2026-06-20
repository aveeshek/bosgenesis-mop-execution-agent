from __future__ import annotations

from bosgenesis_mop_execution_agent.runtime.mcp_rest_adapters import _manifest_for_namespace


def test_ingress_host_is_prefixed_for_target_namespace() -> None:
    manifest = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": "signoz", "namespace": "signoz"},
        "spec": {
            "rules": [{"host": "signoz.bosgenesis.local"}],
            "tls": [{"hosts": ["signoz.bosgenesis.local"]}],
        },
    }

    patched = _manifest_for_namespace(manifest, "agent-testing")

    assert patched["metadata"]["namespace"] == "agent-testing"
    assert patched["spec"]["rules"][0]["host"] == "signoz-agent-testing.bosgenesis.local"
    assert patched["spec"]["tls"][0]["hosts"] == ["signoz-agent-testing.bosgenesis.local"]


def test_ingress_host_prefixing_is_idempotent() -> None:
    manifest = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "Ingress",
        "metadata": {"name": "signoz"},
        "spec": {"rules": [{"host": "signoz-agent-testing.bosgenesis.local"}]},
    }

    patched = _manifest_for_namespace(manifest, "agent-testing")

    assert patched["spec"]["rules"][0]["host"] == "signoz-agent-testing.bosgenesis.local"
