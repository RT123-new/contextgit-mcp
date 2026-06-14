import json
import os
import subprocess
import sys

import pytest

from contextgit.engine import ContextGit
from contextgit.server import MCPServer, TOOLS


@pytest.fixture()
def server(tmp_path):
    return MCPServer(ContextGit(str(tmp_path / "store")))


def _request(server, method, params=None, msg_id=1):
    message = {"jsonrpc": "2.0", "id": msg_id, "method": method}
    if params is not None:
        message["params"] = params
    return server.handle_message(message)


def test_initialize_handshake(server):
    response = _request(server, "initialize", {
        "protocolVersion": "2025-03-26",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    })
    assert response["result"]["protocolVersion"] == "2025-03-26"
    assert response["result"]["serverInfo"]["name"] == "contextgit"
    assert "tools" in response["result"]["capabilities"]


def test_initialize_unknown_version_falls_back(server):
    response = _request(server, "initialize", {"protocolVersion": "1999-01-01"})
    assert response["result"]["protocolVersion"] == "2025-06-18"


def test_initialized_notification_gets_no_response(server):
    assert server.handle_message({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_matches_registry(server):
    response = _request(server, "tools/list")
    names = [tool["name"] for tool in response["result"]["tools"]]
    assert names == [tool["name"] for tool in TOOLS]
    for tool in response["result"]["tools"]:
        assert tool["inputSchema"]["type"] == "object"


def test_tool_call_roundtrip(server):
    _request(server, "tools/call", {
        "name": "commit_turn",
        "arguments": {"user_prompt": "Remember that I deploy on Fridays.", "assistant_answer": "Saved."},
    })
    response = _request(server, "tools/call", {
        "name": "prepare_context",
        "arguments": {"prompt": "When do I deploy?"},
    })
    assert response["result"]["isError"] is False
    payload = json.loads(response["result"]["content"][0]["text"])
    assert "Context Merge Patch" in payload["context"]
    assert payload["estimated_tokens"] <= payload["budget"]


def test_tool_call_bad_args_is_tool_error_not_protocol_error(server):
    response = _request(server, "tools/call", {"name": "show_context", "arguments": {"ref": "event:nope"}})
    assert response["result"]["isError"] is True
    assert "error" not in response


def test_unknown_tool_is_tool_error(server):
    response = _request(server, "tools/call", {"name": "nope", "arguments": {}})
    assert response["result"]["isError"] is True


def test_unknown_method_is_method_not_found(server):
    response = _request(server, "definitely/not/a/method")
    assert response["error"]["code"] == -32601


def test_ping(server):
    assert _request(server, "ping")["result"] == {}


def test_stdio_end_to_end(tmp_path):
    """Full subprocess handshake: the same path Claude Desktop/Codex exercise."""
    store = str(tmp_path / "store")
    messages = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize",
         "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "context_stats", "arguments": {}}},
    ]
    stdin = "".join(json.dumps(m) + "\n" for m in messages)
    proc = subprocess.run(
        [sys.executable, "-m", "contextgit", "serve", "--store", store],
        input=stdin, capture_output=True, text=True, timeout=60,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)},
    )
    lines = [json.loads(line) for line in proc.stdout.strip().splitlines()]
    by_id = {msg["id"]: msg for msg in lines}
    assert by_id[1]["result"]["serverInfo"]["name"] == "contextgit"
    assert len(by_id[2]["result"]["tools"]) == len(TOOLS)
    assert by_id[3]["result"]["isError"] is False
