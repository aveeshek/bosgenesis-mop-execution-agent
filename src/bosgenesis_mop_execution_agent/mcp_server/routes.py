"""Streamable HTTP JSON-RPC routes for MCP clients."""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bosgenesis_mop_execution_agent.mcp_server.tools import (
    PROTOCOL_VERSION,
    SERVER_NAME,
    call_tool,
    capabilities,
    list_tools,
)

router = APIRouter(tags=["MCP"])


@router.get("/mcp")
async def get_mcp_info() -> dict[str, Any]:
    """Return basic MCP server information for smoke tests."""
    return {
        "ok": True,
        "server_name": SERVER_NAME,
        "endpoint": "/mcp",
        "transport": "streamable-http-json-rpc",
        "capabilities": {"tools": True},
        "redaction_applied": True,
    }


@router.post("/mcp")
async def post_mcp(request: Request) -> JSONResponse:
    """Handle JSON-RPC MCP requests."""
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse(_jsonrpc_error(None, -32700, "Parse error"), status_code=400)
    if isinstance(payload, list):
        return JSONResponse([_handle_jsonrpc(item) for item in payload])
    if not isinstance(payload, dict):
        return JSONResponse(_jsonrpc_error(None, -32600, "Invalid Request"), status_code=400)
    response = _handle_jsonrpc(payload)
    if response is None:
        return JSONResponse({}, status_code=202)
    return JSONResponse(response)


def _handle_jsonrpc(payload: dict[str, Any]) -> dict[str, Any] | None:
    request_id = payload.get("id")
    method = payload.get("method")
    params = payload.get("params")

    if method == "notifications/initialized":
        return None
    if method == "initialize":
        return _jsonrpc_result(
            request_id,
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {
                    "tools": {"listChanged": False},
                    "resources": {"subscribe": False, "listChanged": False},
                },
                "serverInfo": {"name": SERVER_NAME, "version": capabilities()["version"]},
            },
        )
    if method == "tools/list":
        return _jsonrpc_result(request_id, {"tools": list_tools()})
    if method == "tools/call":
        if not isinstance(params, dict):
            return _jsonrpc_error(request_id, -32602, "Invalid params")
        name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return _jsonrpc_error(request_id, -32602, "Invalid params")
        return _jsonrpc_result(request_id, call_tool(name, arguments))
    if method == "resources/list":
        return _jsonrpc_result(
            request_id,
            {
                "resources": [
                    {
                        "uri": "mop-execution://capabilities",
                        "name": "MoP Execution capabilities",
                        "mimeType": "application/json",
                    }
                ]
            },
        )
    if method == "resources/read":
        capabilities_text = call_tool("mop_execution_get_capabilities", {})["content"][0]["text"]
        return _jsonrpc_result(
            request_id,
            {
                "contents": [
                    {
                        "uri": "mop-execution://capabilities",
                        "mimeType": "application/json",
                        "text": capabilities_text,
                    }
                ]
            },
        )
    return _jsonrpc_error(request_id, -32601, f"Method not found: {method}")


def _jsonrpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }
