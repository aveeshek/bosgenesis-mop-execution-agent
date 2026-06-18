from __future__ import annotations

from fastapi.testclient import TestClient

from bosgenesis_mop_execution_agent.api.app import create_app


def test_mcp_get_returns_server_info() -> None:
    client = TestClient(create_app())

    response = client.get("/mcp")

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["server_name"] == "bosgenesis-mop-execution-agent"


def test_mcp_initialize_and_tools_list() -> None:
    client = TestClient(create_app())

    initialize = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest", "version": "0.1.0"},
            },
        },
    )
    tools = client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    )

    assert initialize.status_code == 200
    assert initialize.json()["result"]["serverInfo"]["name"] == "bosgenesis-mop-execution-agent"
    tool_names = {tool["name"] for tool in tools.json()["result"]["tools"]}
    assert "mop_execution_health" in tool_names
    assert "mop_execution_create_job" in tool_names


def test_mcp_health_tool_call_returns_standard_envelope() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "mop_execution_health", "arguments": {}},
        },
    )

    result = response.json()["result"]
    envelope = result["structuredContent"]
    assert response.status_code == 200
    assert result["isError"] is False
    assert envelope["ok"] is True
    assert envelope["data"]["service"] == "bosgenesis-mop-execution-agent"
    assert envelope["redaction_applied"] is True


def test_mcp_policy_tool_call_reports_blocks() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {
                "name": "mop_execution_evaluate_policy",
                "arguments": {
                    "job_id": "job-1",
                    "target_namespace": "target-ns",
                    "mutating": True,
                    "dry_run_satisfied": False,
                    "audit_written": False,
                },
            },
        },
    )

    envelope = response.json()["result"]["structuredContent"]
    assert response.status_code == 200
    assert envelope["ok"] is True
    assert envelope["data"]["allowed"] is False
    assert {block["code"] for block in envelope["data"]["blocks"]} >= {
        "DRY_RUN_REQUIRED",
        "APPROVAL_REQUIRED",
        "IDEMPOTENCY_REQUIRED",
        "AUDIT_REQUIRED_BEFORE_MUTATION",
    }


def test_mcp_create_job_tool_returns_standard_envelope() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 5,
            "method": "tools/call",
            "params": {
                "name": "mop_execution_create_job",
                "arguments": {
                    "bundle_id": "bundle-1",
                    "target_namespace": "target-ns",
                    "job_name": "smoke",
                },
            },
        },
    )

    result = response.json()["result"]
    envelope = result["structuredContent"]
    assert response.status_code == 200
    assert result["isError"] is False
    assert envelope["ok"] is True
    assert envelope["state"] == "created"
    assert envelope["data"]["job"]["target_namespace"] == "target-ns"


def test_mcp_malformed_json_returns_parse_error() -> None:
    client = TestClient(create_app())

    response = client.post(
        "/mcp",
        content="{bad json",
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32700
