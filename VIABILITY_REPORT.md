# contextgit-mcp v0.1.0 — Viability Report for Claude Long-Context Conversations

**Date:** 2026-06-14
**Subject:** released `contextgit-mcp==0.1.0` from PyPI, installed isolated via `uv tool install "contextgit-mcp[tokens]"` (Python 3.13.11, `tiktoken==0.13.0`).
**Token counter for all numbers:** `tiktoken_o200k_base` (an OpenAI tokenizer — a proxy for Claude, see caveat in `TEST_PLAN.md`).
**Reproduce:** `/Users/regtroka/.local/share/uv/tools/contextgit-mcp/bin/python bench/bench_main.py all` → results in `bench/results/20260614_185021/` (latency curve), `bench/results/20260614_184035/` (integration smoke `d12.json`).
**Hard rules honored:** nothing published to PyPI or pushed to GitHub; Reg's real store (`~/.contextgit/store`) never touched (all tests use throwaway temp stores); no API key used (integration test was a free instrumented run).

---

## Verdict: **VIABLE-WITH-CAVEATS**

For a **single Claude surface** with a **modestly-sized store (up to a few hundred turns)**, contextgit delivers real, measurable value: it surfaces the right durable fact at the top of a compact patch with **zero fabrication**, costs a fraction of the full history, and is robust against malformed input and hard kills. The deterministic, inspectable design is a genuine strength over LLM-summarizer memory.

But it is **not yet safe as an always-on layer for very long-lived stores or across multiple Claude surfaces at once.** Two issues are blockers and one is a significant risk:
1. **A single torn line in the store makes the entire store unreadable** (every tool call then fails). Data-loss/availability blocker on power loss, disk-full, or a torn concurrent write.
2. **`prepare_context` latency explodes super-linearly** — ~0.9s at 1k events, **~31s at 10k events** — so it's only interactive-grade up to ~500 events (~250 turns).
3. **Concurrent writers from two surfaces corrupt id/version integrity** (duplicate event-ids and wiki version numbers), and you use Desktop + Code + Cowork.

All three are *cheap to fix upstream* (guard JSON line reads; cache/incrementalize the compile; add a file lock or per-surface stores). With those fixed it would move to clearly viable. As shipped, use it per-project, keep stores small, and don't write the same store from two surfaces simultaneously.

---

## Top 3 strengths (with numbers)

1. **Retrieval ranking is excellent — and it actually changes Claude's answer.**
   - 25/25 labeled "needle" facts retrieved at **rank 1**: recall@1 = recall@5 = MRR = **1.0** (D2).
   - End-to-end (D12): a database decision buried **~120 turns / 504 events back** was surfaced at **rank 1** in a **657-token** patch (vs **16,242 tokens** to inject the whole journal — 96% cheaper). Asked *"new Atlas feature — which DB and why?"*, the same query produces:
     - **Without the patch (34 input tokens):** generic, no project knowledge — *"I don't have context on your Atlas service… commonly teams use PostgreSQL or MySQL…"* (could even suggest PostgreSQL — the thing the team migrated **off**).
     - **With the patch (697 input tokens):** correct and grounded — *"Use MySQL 8; the team migrated Atlas off PostgreSQL because its per-tenant connection ceiling capped you at 100 rps."*
     - Net: the right answer for **+663 tokens**.

2. **No fabrication.** Of 60 patch claims checked across prompts, **60/60 trace verbatim to stored text — fabrication rate 0.0** (D9). The compiler is mechanical (BM25 + frequency/recency/correction scoring); patch text is truncated stored content, never generated. A memory layer that *invents* is worse than none; this one does not.

3. **Robust I/O and protocol.** (D11/D6/D1)
   - Survives malformed MCP cleanly: non-JSON → JSON-RPC `-32700`, unknown method → `-32601`, unknown tool / missing arg / wrong-typed arg → `isError` text, **and the server stays alive** for the next call.
   - Stores and round-trips **null bytes, emoji, tabs/newlines, non-English (Japanese + French)** exactly; a **54,001-token paste** produced a **234-token** patch (still within the 700 budget) in 159ms.
   - **`kill -9` mid-write**: store reopens cleanly, all fully-written events intact (append-only + `O_APPEND`).
   - Patch stayed within budget in **12/12** token-efficiency runs (5k→100k-token transcripts × single/multi/shift mixes); `commit_turn` ~1ms and `search_context` ≤296ms even at 10k events; storage linear at **~458 bytes/event**.

---

## Top 3 weaknesses (with numbers; blocker vs nice-to-fix)

1. **🔴 BLOCKER — one torn JSON line bricks the whole store (D6).**
   Injecting a single partial line into `events.jsonl` made **every read fail** (`JSONDecodeError`, 0 of 4 events recoverable). `_read_jsonl` parses each line with no per-line guard, and *every* tool call reads the full journal — so one bad line (power loss / disk full / torn concurrent append) takes the entire memory offline. **Fix:** skip+log malformed lines instead of raising. **Mitigation today:** `contextgit export` backups; keep stores small.

2. **🔴 BLOCKER (at scale) — `prepare_context` latency is super-linear (D7).**
   Cold-call p50 / p95 (ms): 100 ev → 77/90; 500 → 407/428; **1,000 → 873/1,324**; 3,000 → 3,582/4,355; **10,000 → 30,802/31,435**. End-to-end over stdio at 1k ≈ 1,230ms. The cost is the compile loop (segment + per-candidate trial-render), **not** disk (engine load is 81ms at 10k). Interactive budget (<500ms p95) holds only to **~500 events (~250 turns)**. **Fix:** memoize segmentation / incremental compile. **Mitigation today:** rotate/archive stores before ~1k events; prefer per-project stores.

3. **🟠 SIGNIFICANT — concurrent writers corrupt id/version integrity (D10).**
   3 simultaneous writers × 20 turns: all 120 event lines were written and the store reopened, but **40/120 had duplicate `event_id`s**, and concurrent `remember` to one page produced **40/60 duplicate wiki version numbers**. Sequence counters are per-process with no lock. No raw-line loss, but ambiguous history and possible **rejection of future legitimate writes** (the append-time uniqueness check). You run Desktop + Code + Cowork → real exposure. **Fix:** file lock or content-addressed ids. **Mitigation today:** one writer/store; per-surface or per-project stores.

### Other findings worth knowing (nice-to-fix / caveats)
- **No relevance gate / always fills the budget (D3).** No-answer queries still returned **10 items at a constant 0.544 score** (~700 tokens of topical filler). Precision@5 ≈ 0.2. The right item is at the top, but every call injects noise and spends ~budget even when nothing is relevant. Lowering `--budget` reduces the waste.
- **Staleness needs active curation (D4).** Same-page re-save keeps **both** old and new values as bullets; `mark_stale` retires the wiki page **but the original journal event still surfaces the old value**; event-level "use X instead of Y" supersession works only when the two turns are **≥1 second apart** (fails for same-second corrections). Old facts won't auto-expire — the user must mark them stale, and even then the raw turn lingers.
- **Assistant answers are memory-eligible (D9).** `commit_turn` journals the assistant reply too, so a **wrong** assistant statement can resurface later as "context" (propagation, not fabrication). Confirmed: a planted false claim reappeared.
- **Hot-page storage is quadratic (D8).** One page hammered with `remember` grew `wiki_versions.jsonl` to **2.0 MB at 200 bullets** (rewrites whole page each save). Fine for normal use (few bullets/page).
- **Synonym blindness (D2).** A query using only synonyms of a stored fact (no shared rare term) ranked the right page **6th** (just outside top-5) — expected for BM25 with no embeddings.
- **Token-savings baseline is partly self-inflating.** "Saved 86–99% vs full history" is largely `1 − 700/full`, which trivially grows with history. The honest, fair number: the patch is **~0.5× the cost of even a 20-turn recent window** while drawing on the entire history.
- **Tokenizer caveat.** Counts use `o200k_base` (OpenAI), not Claude's tokenizer — a reasonable proxy, off by perhaps ~10–20% for Claude.

---

## Recommended config defaults (based on what worked best)

| Setting | Recommendation | Why |
|---|---|---|
| **Token budget** | `--budget 500` (default 700 is fine but generous) | Patch reliably lands ~650–700; given the low precision/always-fills behavior, 500 trims filler while still capturing the rank-1 fact. |
| **Store scope** | **Per-project** `.contextgit/` (project-scope install / `CONTEXTGIT_DIR` per project), **not** one giant global store | Keeps each store under the ~500–1,000-event latency knee and avoids cross-surface concurrent writes. |
| **Store size policy** | Archive/rotate when a store exceeds ~**1,000 events (~500 turns)** | Beyond this `prepare_context` exceeds ~1s and climbs fast. |
| **Concurrency** | **One Claude surface per store at a time** until locking is added | Prevents the duplicate-id / version-collision corruption. |
| **tokens extra** | Keep `[tokens]` installed (done) | Real `o200k_base` counts instead of char-estimate. |
| **Backups** | Periodic `contextgit export` | Cheap insurance against the torn-line fragility. |
| **Curation** | Use `mark_stale` actively; prefer `remember` with an explicit `page`; review `merge_log` | Old facts don't auto-expire; explicit pages keep needles distinguishable. |

**`server.json` bug to note (not a runtime issue):** the shipped `runtimeHint` is `uvx` with package args `serve`, but the working invocation is `uvx --from contextgit-mcp contextgit serve` (package ≠ command name). Your Claude Desktop entry uses the absolute installed-binary path, so this doesn't affect you.

---

## Wiring status (Step 2)
- **Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json`): the `contextgit` entry was **repointed from the old dev install** (`…/branchingcontextclean-main/contextgit/.venv/bin/python3.13 -m contextgit serve`) **to the released binary** `/Users/regtroka/.local/bin/contextgit serve`. All other preferences preserved. Backups: installer's `.bak` next to the config + `bench/results/claude_desktop_config.pre-wire.20260614_183122.json`. **Restart Claude Desktop** to load it (stale dev servers exit on restart).
- **Claude Code project `.mcp.json`:** intentionally **not** written (hard rule: don't add files to this repo beyond the report/plan/bench). To enable per-project, run in the project root: `claude mcp add --scope user contextgit -- /Users/regtroka/.local/bin/contextgit serve` (user scope) or drop `{"mcpServers":{"contextgit":{"command":"/Users/regtroka/.local/bin/contextgit","args":["serve"]}}}` into that project's `.mcp.json`.

---

## Open questions (only Reg can decide)
1. **Primary surface?** If it's Claude Desktop, the current global-store wiring is fine for light use; if you bounce between Desktop/Code/Cowork, switch to **per-project stores** to dodge the concurrency issue.
2. **Real-model confirmation?** The integration test was a free instrumented (mock) run that clearly shows the patch flips a wrong/generic answer into the correct one. A real Anthropic-SDK A/B would cost **well under 1¢** (two short calls). Want me to run it for a hard confirmation?
3. **Store-size discipline.** Are you comfortable archiving/rotating context periodically (or scoping per project) to stay under the latency knee? If you want one big lifelong store, the compile path needs optimizing first.
4. **Report upstream?** The torn-line and concurrency issues are small, well-localized fixes. Want these filed as issues on your `RT123-new/contextgit-mcp` fork (not the upstream `contextgit/contextgit`)?
