from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app


def test_healthz_returns_openapi_health_shape() -> None:
    client = TestClient(create_app())

    response = client.get("/healthz")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ok"
    assert payload["service"] == "bosgenesis-mop-execution-agent"
    assert payload["version"] == "0.1.0"
    assert "timestamp" in payload
