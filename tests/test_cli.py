import json

import pytest

from contextgit.cli import main


@pytest.fixture()
def store(tmp_path):
    return str(tmp_path / "store")


def run(capsys, *argv):
    code = main(list(argv))
    return code, capsys.readouterr().out


def test_init_and_status(capsys, store):
    code, out = run(capsys, "init", "--store", store)
    assert code == 0 and "Initialized" in out
    code, out = run(capsys, "status", "--store", store)
    assert code == 0 and "events:   0" in out


def test_demo_branch_and_stats_flow(capsys, store):
    run(capsys, "demo", "--store", store)
    code, out = run(capsys, "branch", "What database does Atlas use?", "--store", store)
    assert code == 0
    assert "Context Merge Patch" in out
    assert "saved" in out

    code, out = run(capsys, "branch", "What database does Atlas use?", "--store", store, "--explain")
    assert code == 0 and "SELECTED" in out and "EXCLUDED" in out

    code, out = run(capsys, "stats", "--store", store)
    assert code == 0 and "all time" in out

    code, out = run(capsys, "merges", "--store", store)
    assert code == 0 and "mut:" in out

    code, out = run(capsys, "log", "--store", store, "--json")
    rows = json.loads(out)
    assert code == 0 and len(rows) > 0


def test_remember_search_show(capsys, store):
    run(capsys, "remember", "Use ruff for linting.", "--page", "Tooling", "--store", store)
    code, out = run(capsys, "search", "linting", "--store", store)
    assert code == 0 and "wiki:Tooling" in out
    code, out = run(capsys, "show", "wiki:Tooling", "--store", store)
    assert code == 0 and "ruff" in out


def test_show_missing_ref_fails(capsys, store):
    run(capsys, "init", "--store", store)
    assert main(["show", "event:nope", "--store", store]) == 1


def test_install_print(capsys, store):
    code, out = run(capsys, "install", "print", "--store", store)
    assert code == 0
    assert "Claude Desktop" in out
    assert "mcp_servers.contextgit" in out
    assert "claude mcp add" in out
