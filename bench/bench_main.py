"""contextgit-mcp viability benchmark — dimensions D1..D11.

Run with the installed tool's interpreter:
    /Users/regtroka/.local/share/uv/tools/contextgit-mcp/bin/python bench/bench_main.py all
Or one dimension:  ... bench/bench_main.py d7
Results + a summary land in bench/results/<timestamp>/.
"""
from __future__ import annotations

import os
import re
import shutil
import statistics
import sys
import tempfile
import threading
import time
from typing import Any, Dict, List

import cgcommon as C
from cgcommon import toks, new_engine, build_store, gen_turns, MCPClient, save_result, TOPICS


# ===========================================================================
# D1 — Token efficiency
# ===========================================================================
def d1_token_efficiency(run_dir: str) -> Dict[str, Any]:
    sizes = [5000, 20000, 50000, 100000]
    mixes = ["single", "multi", "shift"]
    prompts = {
        "single": "Summarize everything we decided about Atlas database schema and rate limits",
        "multi":  "Summarize everything we decided about Atlas database schema and rate limits",
        "shift":  "Summarize everything we decided about Orion billing invoices and proration",
    }
    rows = []
    for mix in mixes:
        for target in sizes:
            turns = gen_turns(target, mix)
            sdir = os.path.join(run_dir, "stores", f"d1_{mix}_{target}")
            eng = build_store(sdir, turns)
            res = eng.prepare(prompts[mix], conversation_id="default")
            # fair baselines: patch vs last-N-turns sliding window
            def window_tokens(n):
                return sum(toks(u) + toks(a) for u, a in turns[-n:])
            rows.append({
                "mix": mix,
                "target_tokens": target,
                "n_turns": len(turns),
                "n_events": len(turns) * 2,
                "patch_tokens": res["estimated_tokens"],
                "budget": res["budget"],
                "full_history_tokens": res["full_history_tokens"],
                "saved_tokens": res["saved_tokens"],
                "savings_pct_vs_full": res["savings_pct"],
                "selected_items": len(res["selected"]),
                "patch_within_budget": res["estimated_tokens"] <= res["budget"],
                "selected_empty_but_store_nonempty": len(res["selected"]) == 0,
                "fair_window6_tokens": window_tokens(6),
                "fair_window20_tokens": window_tokens(20),
                "patch_vs_window6_ratio": round(res["estimated_tokens"] / max(1, window_tokens(6)), 3),
                "patch_vs_window20_ratio": round(res["estimated_tokens"] / max(1, window_tokens(20)), 3),
            })
            shutil.rmtree(sdir, ignore_errors=True)
    all_in_budget = all(r["patch_within_budget"] for r in rows)
    any_collapsed = any(r["selected_empty_but_store_nonempty"] for r in rows)
    return {
        "dimension": "D1 token_efficiency",
        "token_counter": C.token_count_source(),
        "rows": rows,
        "verdict": {
            "all_patches_within_budget": all_in_budget,
            "any_patch_collapsed_to_empty": any_collapsed,
            "savings_pct_vs_full_range": [min(r["savings_pct_vs_full"] for r in rows),
                                          max(r["savings_pct_vs_full"] for r in rows)],
        },
    }


# ===========================================================================
# D2/D3 — Retrieval relevance, precision, false positives
# ===========================================================================
def d2_d3_retrieval(run_dir: str) -> Dict[str, Any]:
    sdir = os.path.join(run_dir, "stores", "d2")
    eng = new_engine(sdir, budget=700)

    # Haystack: ~400 unrelated turns from the standard topics.
    haystack = gen_turns(8000, "multi", seed=99)
    for u, a in haystack:
        eng.commit_turn(u, a, conversation_id="default")

    # Needles: durable facts with unique rare entities + unique pages.
    needles = []
    for i in range(20):
        entity = f"Nebulon{i:02d}"
        page = f"Needle{i:02d} Memory"
        fact = f"The {entity} configuration uses setting code Z{i:02d}X and must stay enabled"
        eng.remember(fact, page=page)
        needles.append({
            "id": i, "entity": entity, "expected_ref": f"wiki:{page}",
            # happy-path query shares the rare entity term:
            "query": f"Tell me about the {entity} configuration setting",
            "kind": "wiki",
        })
    # A few raw-event needles (commit_turn) with rare entities, capture refs.
    for i in range(20, 25):
        entity = f"Pulsar{i:02d}"
        u = f"Note that {entity} timeout is exactly 4200 milliseconds for the request handler"
        r = eng.commit_turn(u, "Understood, recorded.", conversation_id="default")
        needles.append({
            "id": i, "entity": entity, "expected_ref": r["committed_event_refs"][0],
            "query": f"What is the {entity} timeout value", "kind": "event",
        })
    # Synonym-mismatch probe (query has NO shared rare term) — expected to be hard.
    eng.remember("The maximum permitted upload payload is two hundred megabytes",
                 page="UploadLimit Memory")
    syn_probe = {"id": "syn", "entity": "(synonym)", "expected_ref": "wiki:UploadLimit Memory",
                 "query": "what is the biggest file I can send", "kind": "synonym_mismatch"}

    def grade(query, expected_ref, k=5):
        res = eng.prepare(query, conversation_id="probe", record_usage=False)
        refs = [s["ref"] for s in res["selected"]]
        rank = refs.index(expected_ref) + 1 if expected_ref in refs else None
        return refs, rank, res

    per = []
    for n in needles:
        refs, rank, res = grade(n["query"], n["expected_ref"])
        per.append({**{k: n[k] for k in ("id", "entity", "expected_ref", "kind", "query")},
                    "rank": rank, "found_at5": rank is not None and rank <= 5,
                    "found_at1": rank == 1, "selected_refs": refs[:5],
                    "n_selected": len(res["selected"])})
    # synonym probe graded separately
    srefs, srank, _ = grade(syn_probe["query"], syn_probe["expected_ref"])
    syn_result = {**syn_probe, "rank": srank, "found_at5": srank is not None and srank <= 5,
                  "selected_refs": srefs[:5]}

    n = len(per)
    recall1 = sum(p["found_at1"] for p in per) / n
    recall5 = sum(p["found_at5"] for p in per) / n
    mrr = sum((1.0 / p["rank"]) if p["rank"] else 0.0 for p in per) / n
    # precision@5: relevant-in-top5 / min(5, n_selected), averaged
    prec5 = statistics.mean(
        (1 if p["found_at5"] else 0) / max(1, min(5, p["n_selected"])) for p in per
    )

    # D3 — no-answer queries (entities guaranteed absent)
    no_answer_qs = [f"What did we decide about Quixotron{j} the hyperloop sprocket" for j in range(15)]
    clean = 0
    na_detail = []
    for q in no_answer_qs:
        res = eng.prepare(q, conversation_id="probe", record_usage=False)
        sel = res["selected"]
        is_clean = len(sel) == 0
        clean += is_clean
        na_detail.append({"query": q, "n_selected": len(sel),
                          "top_refs": [s["ref"] for s in sel[:3]],
                          "top_score": sel[0]["score"] if sel else None})
    clean_miss_rate = clean / len(no_answer_qs)

    shutil.rmtree(sdir, ignore_errors=True)
    return {
        "dimension": "D2/D3 retrieval",
        "n_needles": n,
        "recall_at_1": round(recall1, 3),
        "recall_at_5": round(recall5, 3),
        "MRR": round(mrr, 3),
        "precision_at_5": round(prec5, 3),
        "no_answer_clean_rate": round(clean_miss_rate, 3),
        "synonym_mismatch_probe": syn_result,
        "per_needle": per,
        "no_answer_detail": na_detail,
        "pass_recall5_ge_0.8": recall5 >= 0.8,
        "pass_mrr_ge_0.6": mrr >= 0.6,
        "pass_precision5_ge_0.6": prec5 >= 0.6,
        "pass_clean_rate_ge_0.9": clean_miss_rate >= 0.9,
    }


# ===========================================================================
# D4 — Staleness / supersession
# ===========================================================================
def d4_staleness(run_dir: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"dimension": "D4 staleness"}

    # sub1: explicit mark_stale
    s1 = os.path.join(run_dir, "stores", "d4_explicit")
    eng = new_engine(s1)
    eng.remember("deploy on Fridays", page="Deploy Memory")
    before = eng.prepare("when do we deploy", record_usage=False)
    eng.mark_stale("Deploy Memory", superseded_by="deploy on Mondays")
    after = eng.prepare("when do we deploy", record_usage=False)
    out["sub1_explicit_mark_stale"] = {
        "before_patch": before["context"],
        "after_patch": after["context"],
        "before_selected": [s["ref"] for s in before["selected"]],
        "after_selected": [s["ref"] for s in after["selected"]],
        "stale_excluded_from_selected": "wiki:Deploy Memory" not in [s["ref"] for s in after["selected"]],
        "appears_in_avoid_section": "Avoid Stale/Superseded" in after["context"],
        "old_value_still_recalled_via_raw_event": "Fridays" in after["context"],
        "note": ("mark_stale retires the WIKI PAGE, but the original raw event ('deploy on Fridays') "
                 "is not staled and can still be selected — old value may persist via the journal."),
    }
    shutil.rmtree(s1, ignore_errors=True)

    # sub2: "instead of" at event level — two variants:
    #   (2a) same wall-clock second (the engine uses 1s timestamp precision)
    #   (2b) >=1s apart (how real turns are spaced)
    def insteadof_case(tag, sleep_between):
        s2 = os.path.join(run_dir, "stores", f"d4_insteadof_{tag}")
        eng = new_engine(s2)
        r_old = eng.commit_turn("Use MySQL for the Atlas database", "ok")
        if sleep_between:
            time.sleep(1.2)
        eng.commit_turn("Actually use PostgreSQL instead of MySQL for the Atlas database", "ok")
        ex = eng.explain("what database does Atlas use", budget=700)
        old_ref = r_old["committed_event_refs"][0]
        old_rows = [r for r in (ex["selected"] + ex["excluded"]) if r["ref"] == old_ref]
        patch = eng.prepare("what database does Atlas use", record_usage=False)
        res = {
            "old_event_ref": old_ref,
            "old_event_superseded_by": old_rows[0]["superseded_by"] if old_rows else "NOT_FOUND",
            "old_event_excluded_from_selected": old_ref not in [s["ref"] for s in patch["selected"]],
            "patch": patch["context"],
        }
        shutil.rmtree(s2, ignore_errors=True)
        return res
    out["sub2a_instead_of_same_second"] = insteadof_case("same", False)
    out["sub2b_instead_of_spaced_1s"] = insteadof_case("spaced", True)
    out["sub2_note"] = ("Event-level 'instead of X' supersession is ordered by 1-second timestamps; "
                        "two corrections in the same second do NOT supersede (2a), but turns >=1s apart do (2b).")

    # sub3: same-page re-save WITHOUT marking stale (the suspected gap)
    s3 = os.path.join(run_dir, "stores", "d4_resave")
    eng = new_engine(s3)
    eng.remember("the cache ttl is 60 seconds", page="Cache Memory")
    eng.remember("the cache ttl is 300 seconds", page="Cache Memory")
    page = eng.show("wiki:Cache Memory")
    patch = eng.prepare("what is the cache ttl", record_usage=False)
    both_present = ("60 seconds" in page["content"]) and ("300 seconds" in page["content"])
    out["sub3_same_page_resave"] = {
        "page_content": page["content"],
        "both_values_coexist_as_bullets": both_present,
        "patch": patch["context"],
        "note": "No auto-supersede on same-page re-save: both values remain active unless mark_stale is used.",
    }
    shutil.rmtree(s3, ignore_errors=True)
    return out


# ===========================================================================
# D5 — Cross-session consistency & scope
# ===========================================================================
def d5_cross_session(run_dir: str) -> Dict[str, Any]:
    sdir = os.path.join(run_dir, "stores", "d5")
    eng = new_engine(sdir)
    # durable fact merged in conversation A
    eng.commit_turn("Remember that the project codename is Falcon9x", "Noted.", conversation_id="A")
    # conversation-A-only chatter with a rare token
    eng.commit_turn("In passing, Marmaduke7 was mentioned during the A standup", "ok", conversation_id="A")
    # query from a NEW conversation B
    durable = eng.prepare("what is the project codename", conversation_id="B", record_usage=False)
    chatter = eng.prepare("tell me about Marmaduke7", conversation_id="B", record_usage=False)
    durable_recalls = any("Falcon9x" in s["summary"] for s in durable["selected"]) or "Falcon9x" in durable["context"]
    chatter_visible = any("Marmaduke7" in s["summary"] for s in chatter["selected"]) or "Marmaduke7" in chatter["context"]
    shutil.rmtree(sdir, ignore_errors=True)
    return {
        "dimension": "D5 cross_session",
        "durable_fact_recalls_in_new_conversation": durable_recalls,
        "durable_patch": durable["context"],
        "raw_event_from_other_conversation_visible": chatter_visible,
        "note": ("conversation_id is a label on events, not a partition: durable wiki facts "
                 "recall everywhere (intended); raw events are globally visible across conversations."),
    }


# ===========================================================================
# D6 — Durability / corruption
# ===========================================================================
def d6_durability(run_dir: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"dimension": "D6 durability"}

    # (a) kill -9 a live server mid-write, then reopen
    sdir = os.path.join(run_dir, "stores", "d6_kill")
    os.makedirs(sdir, exist_ok=True)
    cli = MCPClient(sdir)
    cli.initialize()
    for i in range(5):
        cli.call_tool("commit_turn", {"user_prompt": f"turn {i} hello world", "assistant_answer": "ack"})
    # fire a burst without reading, then hard-kill
    for i in range(5, 25):
        cli.proc.stdin.write(
            '{"jsonrpc":"2.0","id":%d,"method":"tools/call","params":{"name":"commit_turn",'
            '"arguments":{"user_prompt":"burst %d aaaa bbbb cccc","assistant_answer":"ack"}}}\n' % (100 + i, i)
        )
    try:
        cli.proc.stdin.flush()
    except Exception:
        pass
    cli.proc.kill()
    cli.proc.wait(timeout=5)
    # reopen with a fresh engine
    reopen_ok = True
    reopen_err = None
    n_events = None
    try:
        eng = new_engine(sdir)
        evts = eng.runtime.list_events()
        n_events = len(evts)
    except Exception as exc:
        reopen_ok = False
        reopen_err = f"{type(exc).__name__}: {exc}"
    out["kill9_midwrite"] = {
        "store_reopens": reopen_ok,
        "reopen_error": reopen_err,
        "events_recovered": n_events,
        "note": "append-only + O_APPEND: at worst the last in-flight line is lost/torn.",
    }
    shutil.rmtree(sdir, ignore_errors=True)

    # (b) torn-line fault injection — the real worst case
    sdir2 = os.path.join(run_dir, "stores", "d6_torn")
    eng = new_engine(sdir2)
    eng.commit_turn("clean turn one", "ok")
    eng.commit_turn("clean turn two", "ok")
    events_path = os.path.join(sdir2, "events.jsonl")
    with open(events_path, "a", encoding="utf-8") as f:
        f.write('{"event_id": "torn_partial", "timestamp": "2026-')  # crash mid-line
    read_ok = True
    read_err = None
    recovered = None
    try:
        eng2 = new_engine(sdir2)
        recovered = len(eng2.runtime.list_events())
    except Exception as exc:
        read_ok = False
        read_err = f"{type(exc).__name__}: {exc}"
    out["torn_line_injection"] = {
        "store_still_readable": read_ok,
        "read_error": read_err,
        "events_before_corruption": 4,
        "events_readable_after": recovered,
        "severity": ("BLOCKER: one torn line makes the whole store unreadable"
                     if not read_ok else "tolerated"),
    }
    shutil.rmtree(sdir2, ignore_errors=True)
    return out


# ===========================================================================
# D7 — Latency
# ===========================================================================
def _build_n_events(sdir: str, n_events: int) -> "C.ContextGit":
    eng = new_engine(sdir)
    turns = gen_turns(10_000_000, "multi", seed=7)  # plenty; we slice
    needed = n_events // 2 + 1
    for u, a in turns[:needed]:
        eng.commit_turn(u, a, conversation_id="default")
    return eng


def d7_latency(run_dir: str) -> Dict[str, Any]:
    # Sweep to characterize the scaling curve; few iterations at large N because
    # a single prepare at 10k events is ~30s (compile-bound, not IO-bound).
    iters = {100: 12, 500: 12, 1000: 10, 3000: 4, 10000: 2}
    sizes = [100, 500, 1000, 3000, 10000]
    prompts = [f"summarize what we decided about {TOPICS[k]['entity']}" for k in TOPICS]
    rows = []
    stdio_spot = None
    for sz in sizes:
        sdir = os.path.join(run_dir, "stores", f"d7_{sz}")
        _build_n_events(sdir, sz)
        nprep = iters[sz]
        # fresh engine = cold read from disk, like a server start
        def time_op(fn, n=nprep):
            ds = []
            for i in range(n):
                eng = new_engine(sdir)  # fresh each call -> includes full disk read
                t0 = time.perf_counter()
                fn(eng, i)
                ds.append((time.perf_counter() - t0) * 1000.0)
            return ds
        prep = time_op(lambda e, i: e.prepare(prompts[i % len(prompts)], record_usage=False), nprep)
        srch = time_op(lambda e, i: e.search(prompts[i % len(prompts)]), min(nprep, 8))
        logd = time_op(lambda e, i: e.log(20), min(nprep, 8))
        eng = new_engine(sdir)
        t0 = time.perf_counter(); eng.commit_turn("a latency probe turn", "ok"); commit_ms = (time.perf_counter() - t0) * 1000.0

        def p(vals, q):
            return round(statistics.quantiles(vals, n=100)[q - 1], 1) if len(vals) >= 2 else round(vals[0], 1)
        rows.append({
            "events": sz,
            "prepare_p50_ms": round(statistics.median(prep), 1),
            "prepare_p95_ms": p(prep, 95),
            "prepare_max_ms": round(max(prep), 1),
            "search_p50_ms": round(statistics.median(srch), 1),
            "search_p95_ms": p(srch, 95),
            "log_p50_ms": round(statistics.median(logd), 1),
            "commit_turn_ms": round(commit_ms, 1),
        })
        if sz == 1000:
            cli = MCPClient(sdir); cli.initialize()
            t0 = time.perf_counter()
            cli.call_tool("prepare_context", {"prompt": prompts[0]})
            stdio_spot = round((time.perf_counter() - t0) * 1000.0, 1)
            cli.close()
        shutil.rmtree(sdir, ignore_errors=True)
    p95_1k = next(r["prepare_p95_ms"] for r in rows if r["events"] == 1000)
    return {
        "dimension": "D7 latency",
        "rows": rows,
        "stdio_endtoend_prepare_ms_at_1k": stdio_spot,
        "pass_p95_under_500ms_at_1k": p95_1k < 500,
        "note": "in-process timing isolates compute; each call uses a fresh engine to include the full-journal disk re-read that every real tool call performs.",
    }


# ===========================================================================
# D8 — Storage growth
# ===========================================================================
def _dir_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for fn in files:
            total += os.path.getsize(os.path.join(root, fn))
    return total


def d8_storage(run_dir: str) -> Dict[str, Any]:
    rows = []
    for sz in [100, 1000, 10000]:
        sdir = os.path.join(run_dir, "stores", f"d8_{sz}")
        _build_n_events(sdir, sz)
        size = _dir_size(sdir)
        ev = os.path.getsize(os.path.join(sdir, "events.jsonl"))
        rows.append({"events": sz, "total_bytes": size, "events_jsonl_bytes": ev,
                     "bytes_per_event": round(size / sz, 1)})
        shutil.rmtree(sdir, ignore_errors=True)

    # hot-page quadratic check: many bullets on ONE page
    sdir = os.path.join(run_dir, "stores", "d8_hotpage")
    eng = new_engine(sdir)
    wiki_path = os.path.join(sdir, "wiki_versions.jsonl")
    hot = []
    for i in range(200):
        eng.remember(f"hot page bullet number {i} with some descriptive content here", page="Hot Memory")
        if (i + 1) % 50 == 0:
            hot.append({"bullets": i + 1, "wiki_versions_bytes": os.path.getsize(wiki_path)})
    shutil.rmtree(sdir, ignore_errors=True)
    return {
        "dimension": "D8 storage",
        "rows": rows,
        "hot_page_growth": hot,
        "note": ("wiki_versions.jsonl rewrites the entire (growing) page content on every save, "
                 "so a single hot page grows ~quadratically in bullet count."),
    }


# ===========================================================================
# D9 — Hallucination risk
# ===========================================================================
def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", s.strip()).lower()


def d9_hallucination(run_dir: str) -> Dict[str, Any]:
    sdir = os.path.join(run_dir, "stores", "d9")
    eng = new_engine(sdir)
    turns = gen_turns(6000, "multi", seed=51)
    stored_texts = []
    for u, a in turns:
        eng.commit_turn(u, a)
        stored_texts.append(_norm(u)); stored_texts.append(_norm(a))
    # provenance check over several prompts
    corpus = " || ".join(stored_texts)
    checked = 0
    traceable = 0
    untraceable_examples = []
    for k in TOPICS:
        res = eng.prepare(f"summarize {TOPICS[k]['entity']}", record_usage=False)
        for s in res["selected"]:
            checked += 1
            summ = _norm(s["summary"]).rstrip(".")
            if summ.endswith("..."):
                summ = summ[:-3].strip()
            # the leading chunk of the summary should be a substring of stored text
            probe = summ[:60]
            if probe and probe in corpus:
                traceable += 1
            else:
                untraceable_examples.append({"ref": s["ref"], "summary_probe": probe})

    # assistant-misstatement propagation
    sdir2 = os.path.join(run_dir, "stores", "d9b")
    eng2 = new_engine(sdir2)
    eng2.commit_turn("What is the Wibblefish max upload size?",
                     "The Wibblefish max upload size is 5 gigabytes.")  # planted wrong assistant claim
    res2 = eng2.prepare("Wibblefish max upload size", record_usage=False)
    assistant_claim_surfaces = any("5 gigabytes" in s["summary"] or "Wibblefish" in s["summary"]
                                   for s in res2["selected"])
    shutil.rmtree(sdir, ignore_errors=True)
    shutil.rmtree(sdir2, ignore_errors=True)
    return {
        "dimension": "D9 hallucination",
        "patch_claims_checked": checked,
        "patch_claims_traceable_to_store": traceable,
        "fabrication_rate": round(1 - (traceable / checked), 4) if checked else None,
        "untraceable_examples": untraceable_examples[:5],
        "assistant_misstatement_resurfaces_as_context": assistant_claim_surfaces,
        "note": ("Summaries are mechanical truncations of stored text -> no fabrication by construction. "
                 "BUT commit_turn journals the assistant answer too, so a wrong assistant claim is "
                 "memory-eligible and can resurface as 'context' (propagation, not fabrication)."),
    }


# ===========================================================================
# D10 — Concurrent writers
# ===========================================================================
def d10_concurrency(run_dir: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"dimension": "D10 concurrency"}

    # (a) concurrent commit_turn (events only) from P servers on one store
    sdir = os.path.join(run_dir, "stores", "d10_events")
    os.makedirs(sdir, exist_ok=True)
    P, K = 3, 20
    def worker(pid):
        cli = MCPClient(sdir); cli.initialize()
        for i in range(K):
            cli.call_tool("commit_turn",
                          {"user_prompt": f"proc{pid} turn{i} token{pid}{i}", "assistant_answer": "ack"})
        cli.close()
    threads = [threading.Thread(target=worker, args=(pid,)) for pid in range(P)]
    [t.start() for t in threads]; [t.join() for t in threads]
    eng = new_engine(sdir)
    events = eng.runtime.list_events()
    ids = [e.event_id for e in events]
    expected = P * K * 2
    out["concurrent_commits"] = {
        "writers": P, "turns_each": K, "expected_events": expected,
        "actual_events": len(events),
        "no_lost_events": len(events) == expected,
        "duplicate_event_ids": len(ids) - len(set(ids)),
        "store_readable": True,
    }
    shutil.rmtree(sdir, ignore_errors=True)

    # (b) concurrent `remember` to the SAME page (mutations + wiki versions)
    sdir2 = os.path.join(run_dir, "stores", "d10_remember")
    os.makedirs(sdir2, exist_ok=True)
    def worker2(pid):
        cli = MCPClient(sdir2); cli.initialize()
        for i in range(K):
            cli.call_tool("remember", {"fact": f"proc{pid} fact{i} value{pid}{i}", "page": "Shared Memory"})
        cli.close()
    threads = [threading.Thread(target=worker2, args=(pid,)) for pid in range(P)]
    [t.start() for t in threads]; [t.join() for t in threads]
    reopen_ok = True; err = None; mut_ids = []; ver_collision = None
    try:
        eng = new_engine(sdir2)
        muts = eng.runtime.list_mutations()
        mut_ids = [m.mutation_id for m in muts]
        versions = eng.runtime.wiki_store.list_versions()
        vnums = [(v.title, v.version) for v in versions]
        ver_collision = len(vnums) - len(set(vnums))
    except Exception as exc:
        reopen_ok = False; err = f"{type(exc).__name__}: {exc}"
    out["concurrent_remember_same_page"] = {
        "writers": P, "facts_each": K, "expected_mutations": P * K,
        "actual_mutations": len(mut_ids),
        "duplicate_mutation_ids": (len(mut_ids) - len(set(mut_ids))) if mut_ids else None,
        "wiki_version_number_collisions": ver_collision,
        "store_reopens": reopen_ok, "error": err,
    }
    shutil.rmtree(sdir2, ignore_errors=True)
    return out


# ===========================================================================
# D11 — Edge cases
# ===========================================================================
def d11_edge_cases(run_dir: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {"dimension": "D11 edge_cases"}

    # empty store first call
    s0 = os.path.join(run_dir, "stores", "d11_empty")
    eng = new_engine(s0)
    empty = eng.prepare("anything", record_usage=False)
    out["empty_store_first_call"] = {"patch_tokens": empty["estimated_tokens"],
                                     "saved": empty["saved_tokens"], "selected": len(empty["selected"]),
                                     "no_crash": True}
    shutil.rmtree(s0, ignore_errors=True)

    # very long single event (~50k tokens)
    s1 = os.path.join(run_dir, "stores", "d11_giant")
    eng = new_engine(s1)
    giant = ("Gigantor " + "lorem ipsum dolor sit amet consectetur ") * 6000  # ~ tens of thousands of tokens
    giant_tok = toks(giant)
    eng.commit_turn(giant, "ack")
    eng.commit_turn("Also, the Tinyfact code is QQ-7", "ack")
    t0 = time.perf_counter()
    res = eng.prepare("Tinyfact code", record_usage=False)
    giant_ms = (time.perf_counter() - t0) * 1000.0
    out["giant_event"] = {
        "giant_event_tokens": giant_tok,
        "patch_tokens": res["estimated_tokens"],
        "patch_within_budget": res["estimated_tokens"] <= res["budget"],
        "giant_excluded_from_patch": all("Gigantor" not in s["summary"] for s in res["selected"]),
        "prepare_ms": round(giant_ms, 1),
    }
    shutil.rmtree(s1, ignore_errors=True)

    # non-English
    s2 = os.path.join(run_dir, "stores", "d11_intl")
    eng = new_engine(s2)
    eng.remember("会議は毎週金曜日に行われます Sakuramoto プロジェクト", page="Sakuramoto Memory")
    eng.remember("La réunion a lieu chaque vendredi pour le projet Café", page="Cafe Memory")
    r_jp = eng.search("Sakuramoto")
    r_fr = eng.prepare("projet Café réunion", record_usage=False)
    out["non_english"] = {
        "japanese_searchable": any("Sakuramoto" in x["summary"] for x in r_jp),
        "japanese_token_count": toks("会議は毎週金曜日に行われます"),
        "french_recalls": "Café" in r_fr["context"] or any("Café" in s["summary"] for s in r_fr["selected"]),
        "no_crash": True,
    }
    shutil.rmtree(s2, ignore_errors=True)

    # special characters / emoji / json-breaking
    s3 = os.path.join(run_dir, "stores", "d11_special")
    eng = new_engine(s3)
    nasty = 'Weird: "quotes" \\backslash\t tab\n newline 🚀💾 emoji \x00 nullchar ctrl and {"json":true}'
    r = eng.commit_turn(nasty, "ack")
    shown = eng.show(r["committed_event_refs"][0])
    out["special_characters"] = {
        "roundtrip_exact": shown["content"] == nasty,
        "no_crash": True,
    }
    shutil.rmtree(s3, ignore_errors=True)

    # malformed MCP over stdio — server must stay alive
    s4 = os.path.join(run_dir, "stores", "d11_mcp")
    os.makedirs(s4, exist_ok=True)
    cli = MCPClient(s4); cli.initialize()
    mcp = {}
    # non-JSON line
    resp = cli.send_raw("this is not json at all")
    mcp["non_json_line"] = {"error_code": (resp or {}).get("error", {}).get("code")}
    # unknown method
    resp = cli.request("does/notExist", {})
    mcp["unknown_method"] = {"error_code": resp.get("error", {}).get("code")}
    # unknown tool
    r = cli.call_tool("no_such_tool", {})
    mcp["unknown_tool"] = {"isError": r["isError"], "text": (r["raw"] or "")[:60] if isinstance(r["raw"], str) else None}
    # missing required arg
    r = cli.call_tool("prepare_context", {})
    mcp["missing_required_arg"] = {"isError": r["isError"], "text": (r["raw"] or "")[:60] if isinstance(r["raw"], str) else None}
    # wrong type for budget
    r = cli.call_tool("prepare_context", {"prompt": "hi", "budget": "not-an-int"})
    mcp["wrong_type_budget"] = {"isError": r["isError"], "text": (r["raw"] or "")[:60] if isinstance(r["raw"], str) else None}
    # server still alive?
    r = cli.call_tool("context_stats", {})
    mcp["server_alive_after_errors"] = (not r["isError"]) and isinstance(r["payload"], dict)
    cli.close()
    out["malformed_mcp"] = mcp
    shutil.rmtree(s4, ignore_errors=True)
    return out


# ===========================================================================
DIMS = {
    "d1": d1_token_efficiency,
    "d2": d2_d3_retrieval,
    "d4": d4_staleness,
    "d5": d5_cross_session,
    "d6": d6_durability,
    "d7": d7_latency,
    "d8": d8_storage,
    "d9": d9_hallucination,
    "d10": d10_concurrency,
    "d11": d11_edge_cases,
}


def main():
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    run_dir = os.path.join(C.RESULTS_DIR, C.ts())
    os.makedirs(run_dir, exist_ok=True)
    targets = list(DIMS.keys()) if which == "all" else [which]
    summary = {"run_dir": run_dir, "token_counter": C.token_count_source(),
               "contextgit_version": __import__("contextgit").__version__, "dims": {}}
    for d in targets:
        print(f"\n=== running {d} ===", flush=True)
        t0 = time.perf_counter()
        try:
            res = DIMS[d](run_dir)
            dt = round(time.perf_counter() - t0, 1)
            res["_elapsed_s"] = dt
            save_result(d, res, run_dir)
            summary["dims"][d] = {"ok": True, "elapsed_s": dt, "file": f"{d}.json"}
            print(f"--- {d} done in {dt}s", flush=True)
        except Exception as exc:
            import traceback
            tb = traceback.format_exc()
            save_result(d, {"error": f"{type(exc).__name__}: {exc}", "traceback": tb}, run_dir)
            summary["dims"][d] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            print(f"!!! {d} FAILED: {exc}\n{tb}", flush=True)
    save_result("_summary", summary, run_dir)
    print(f"\nALL RESULTS -> {run_dir}", flush=True)


if __name__ == "__main__":
    main()
