"""Regression tests for two default-config bugs found during viability testing:

1. A budget overflow (driven by the final-render Avoid-Stale/provenance lines)
   used to discard ALL context (force_tiny), leaving an empty patch on the
   default budget once a store accumulated stale facts. It must now degrade
   gracefully and keep the in-budget context.
2. The token-savings ledger clamped per-call savings to >= 0, so a patch that
   cost more than the full history was hidden. It must now report the true net,
   which can be negative.
"""
import tempfile, shutil
import pytest
from contextgit.engine import ContextGit


@pytest.fixture
def store():
    d = tempfile.mkdtemp()
    yield d
    shutil.rmtree(d, ignore_errors=True)


def test_budget_overflow_degrades_gracefully_not_empty(store):
    eng = ContextGit(store_dir=store, budget=700)
    # in-budget relevant facts ...
    for i in range(9):
        eng.commit_turn(
            f"Schema note {i}: the database schema design includes tables and columns "
            f"and indexes and constraints relevant to schema item number {i}", "ok")
    # ... plus stale facts that inflate the final Avoid-Stale block
    for i in range(8):
        eng.remember(f"Old schema fact {i}: deprecated tables layout variant {i}",
                     page=f"OldSchema{i} Memory")
        eng.mark_stale(f"OldSchema{i} Memory", superseded_by="new schema")
    res = eng.prepare("describe the schema tables design", budget=700, record_usage=False)
    assert "No selected context fits" not in res["context"], "patch wiped to empty despite in-budget facts"
    assert len(res["selected"]) > 0
    assert res["estimated_tokens"] <= 700


def test_savings_ledger_reports_net_loss(store):
    eng = ContextGit(store_dir=store, budget=700)
    eng.commit_turn("short note one", "ok")
    eng.commit_turn("short note two", "ok")
    last = None
    for i in range(5):
        last = eng.prepare(f"unrelated query {i} not in store", record_usage=True)
    # a tiny store -> patch boilerplate costs MORE than full history -> negative net
    assert last["saved_tokens"] < 0
    s = eng.stats()["all_time"]
    assert s["saved_tokens_total"] < 0
    assert s["net_tokens_saved"] < 0
    assert s["overhead_compilations"] >= 1
    assert s["savings_pct"] < 0


def test_savings_still_positive_on_a_real_win(store):
    eng = ContextGit(store_dir=store, budget=700)
    # a large history so the compact patch genuinely saves tokens
    for i in range(60):
        eng.commit_turn(
            f"Turn {i}: a fairly long conversational message about topic alpha and beta "
            f"with assorted details number {i} that bulk up the raw history considerably", "ok")
    res = eng.prepare("topic alpha beta", record_usage=True)
    assert res["saved_tokens"] > 0
    assert eng.stats()["all_time"]["savings_pct"] > 0
