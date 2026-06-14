import os

import pytest

from contextgit.engine import ContextGit, resolve_store_dir


@pytest.fixture()
def engine(tmp_path):
    return ContextGit(str(tmp_path / "store"))


def test_resolve_store_dir_explicit_wins(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTEXTGIT_DIR", str(tmp_path / "env-store"))
    assert resolve_store_dir(str(tmp_path / "explicit")) == str(tmp_path / "explicit")
    assert resolve_store_dir() == str(tmp_path / "env-store")


def test_resolve_store_dir_walks_up(tmp_path, monkeypatch):
    monkeypatch.delenv("CONTEXTGIT_DIR", raising=False)
    store = tmp_path / ".contextgit"
    store.mkdir()
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert resolve_store_dir(cwd=str(nested)) == str(store)


def test_commit_turn_journals_events(engine):
    result = engine.commit_turn("What's the weather?", "I don't have weather data.")
    assert result["ok"] is True
    assert len(result["committed_event_refs"]) == 2
    assert result["durable_merge"] is None
    assert engine.status()["events"] == 2


def test_commit_turn_durable_phrasing_merges(engine):
    result = engine.commit_turn(
        "Remember that my favorite editor is Neovim.",
        "Saved: your favorite editor is Neovim.",
    )
    assert result["durable_merge"] is not None
    pages = engine.runtime.wiki_store.list_pages()
    assert any("Neovim" in p.content for p in pages)


def test_remember_and_show(engine):
    saved = engine.remember("The API key lives in 1Password under 'acme-prod'.", page="Acme Project")
    assert saved["target_page"] == "Acme Project"
    shown = engine.show("wiki:Acme Project")
    assert "1Password" in shown["content"]
    assert shown["versions"] == 1


def test_prepare_respects_budget_and_records_usage(engine):
    for i in range(8):
        engine.commit_turn(f"Note number {i} about project Zephyr.", f"Noted item {i}.")
    result = engine.prepare("Tell me about project Zephyr", budget=200)
    assert result["estimated_tokens"] <= 200
    assert result["full_history_tokens"] > 0
    stats = engine.stats()
    assert stats["all_time"]["compilations"] == 1


def test_prepare_dry_run_skips_ledger(engine):
    engine.commit_turn("hello", "hi")
    engine.prepare("hello", record_usage=False)
    assert engine.stats()["all_time"]["compilations"] == 0


def test_mark_stale_excludes_from_branch(engine):
    engine.remember("Deploy target is Heroku.", page="Deploy Notes")
    engine.mark_stale("Deploy Notes", superseded_by="Fly.io")
    explanation = engine.explain("Where do we deploy?")
    stale_refs = [
        row["ref"] for row in explanation["excluded"]
        if "stale_or_superseded" in row["exclusion_reasons"]
    ]
    assert "wiki:Deploy Notes" in stale_refs


def test_correction_supersedes_old_value(engine):
    engine.commit_turn("For Atlas use PostgreSQL for storage.", "Noted: PostgreSQL.")
    engine.commit_turn(
        "Correction: use MySQL instead of PostgreSQL for Atlas.",
        "Understood, MySQL supersedes PostgreSQL.",
    )
    result = engine.prepare("What database does Atlas use?")
    selected_text = " ".join(row["summary"] for row in result["selected"])
    assert "MySQL" in selected_text


def test_search_finds_events_and_wiki(engine):
    engine.commit_turn("The retro is every second Friday.", "Noted.")
    engine.remember("Standups happen at 9:30 CET.", page="Team Rituals")
    refs = [row["ref"] for row in engine.search("standups time")]
    assert any(ref.startswith("wiki:") for ref in refs)


def test_resolve_pending_roundtrip(engine):
    engine.runtime.record_mutation(
        "pending",
        new_claim="We might switch to pnpm.",
        target_page="Tooling",
        policy_reason="speculative",
    )
    assert len(engine.runtime.list_pending()) == 1
    result = engine.resolve_pending("We might switch to pnpm.", "approve")
    assert result["action"] == "approved"
    assert engine.runtime.list_pending() == []
    pages = engine.runtime.wiki_store.list_pages()
    assert any("pnpm" in p.content for p in pages)


def test_show_unknown_ref_raises(engine):
    with pytest.raises(KeyError):
        engine.show("event:nope")
    with pytest.raises(ValueError):
        engine.show("garbage")


def test_usage_ledger_persists_on_disk(engine):
    engine.commit_turn("hello", "hi")
    engine.prepare("hello")
    assert os.path.exists(os.path.join(engine.store_dir, "usage.jsonl"))
