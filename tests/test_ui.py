"""Tests for the contextgit ui dashboard server."""
from __future__ import annotations

import json
import threading
import urllib.request
import urllib.error

import pytest

from contextgit.engine import ContextGit
from contextgit.ui import make_server


@pytest.fixture()
def ui(tmp_path):
    engine = ContextGit(str(tmp_path / "store"))
    server, token = make_server(engine, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield engine, base, token
    server.shutdown()
    server.server_close()


def _request(base, path, token=None, body=None, origin=None):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-ContextGit-Token"] = token
    if origin:
        headers["Origin"] = origin
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, headers=headers)
    with urllib.request.urlopen(req) as resp:
        return resp.status, json.loads(resp.read() or b"{}")


def test_page_served_with_embedded_token(ui):
    _engine, base, token = ui
    with urllib.request.urlopen(base + "/") as resp:
        html = resp.read().decode()
    assert resp.status == 200
    assert token in html  # the page is the only place the token is exposed
    assert "What would my AI see?" in html


def test_api_requires_session_token(ui):
    _engine, base, _token = ui
    with pytest.raises(urllib.error.HTTPError) as err:
        _request(base, "/api/status")
    assert err.value.code == 403


def test_api_rejects_foreign_origin(ui):
    _engine, base, token = ui
    with pytest.raises(urllib.error.HTTPError) as err:
        _request(base, "/api/status", token=token, origin="https://evil.example")
    assert err.value.code == 403


def test_status_and_log_roundtrip(ui):
    engine, base, token = ui
    engine.remember("We deploy on Fridays at 10am")
    status, data = _request(base, "/api/status", token=token)
    assert status == 200 and data["events"] == 1
    status, log = _request(base, "/api/log?n=5", token=token)
    assert status == 200 and len(log) == 1
    assert "Fridays" in log[0]["summary"]


def test_remember_search_and_stale_buttons(ui):
    engine, base, token = ui
    _request(base, "/api/remember", token=token, body={"fact": "Atlas uses MySQL now"})
    status, results = _request(base, "/api/search?q=Atlas%20MySQL", token=token)
    assert status == 200 and results
    wiki_refs = [r["ref"] for r in results if r["ref"].startswith("wiki:")]
    assert wiki_refs
    page = wiki_refs[0].split(":", 1)[1]
    status, data = _request(base, "/api/stale", token=token, body={"page": page})
    assert status == 200 and data["ok"] is True


def test_branch_preview_does_not_touch_usage_ledger(ui):
    engine, base, token = ui
    engine.remember("Atlas uses MySQL now")
    before = engine.usage.summary()["compilations"]
    status, data = _request(base, "/api/branch", token=token, body={"prompt": "What database does Atlas use?"})
    assert status == 200
    assert data["estimated_tokens"] > 0
    assert "context" in data and data["selected"]
    assert engine.usage.summary()["compilations"] == before  # preview is a dry run


def test_pending_approve_flow(ui):
    engine, base, token = ui
    engine.runtime.record_mutation(
        "pending",
        new_claim="Maybe call the mechanism 'reaper of potential'",
        target_page="Naming",
        policy_reason="speculative phrasing",
        confidence=0.4,
        decision_mode="autonomous",
    )
    status, merges = _request(base, "/api/merges", token=token)
    assert status == 200 and len(merges["pending"]) == 1
    status, result = _request(
        base, "/api/pending", token=token,
        body={"content": "Maybe call the mechanism 'reaper of potential'", "action": "approve"},
    )
    assert status == 200 and result["action"] == "approved"
    _status, merges = _request(base, "/api/merges", token=token)
    assert merges["pending"] == []


def test_unknown_pending_item_is_404(ui):
    _engine, base, token = ui
    with pytest.raises(urllib.error.HTTPError) as err:
        _request(base, "/api/pending", token=token, body={"content": "nope", "action": "approve"})
    assert err.value.code == 404


def test_demo_seeds_store(ui):
    _engine, base, token = ui
    status, result = _request(base, "/api/demo", token=token, body={})
    assert status == 200 and result["events_added"] > 0
    _status, data = _request(base, "/api/status", token=token)
    assert data["events"] > 0
