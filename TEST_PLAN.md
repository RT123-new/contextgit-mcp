# contextgit-mcp — Viability Test Plan

**Subject:** `contextgit-mcp` v0.1.0 (PyPI), installed isolated via `uv tool install "contextgit-mcp[tokens]"`.
**Question being answered:** Is this memory layer *viable for Claude long-context conversations* — does it reduce token usage and improve relevance, **without** harmful regressions (hallucinated recalls, stale facts surfacing as current, latency spikes, data loss)?
**Author:** automated viability review, 2026-06-14.
**Token counter for all measurements:** `tiktoken_o200k_base` (confirmed live in the installed env). See the honesty caveat below.

---

## 0. What the system actually is (established by reading the source)

These facts drive every test below; they were confirmed by reading the installed package, not assumed.

- **Deterministic, no LLM, no embeddings.** The compiler (`core/compiler.py`) is a mechanical scoring function: `frequency, recency, query_relevance (token-overlap OR BM25), correction_priority, source_confidence, open_loop` minus `token_cost_penalty, stale_noise_penalty`. Same store + same prompt ⇒ same patch.
- **Storage** is append-only JSONL in a store dir (`events.jsonl`, `wiki_versions.jsonl`, `mutations.jsonl`, `audit.jsonl`, `usage.jsonl`, `pending.json`). Store resolution: `--store` → `CONTEXTGIT_DIR` → nearest `.contextgit/` → global `~/.contextgit/store`.
- **`prepare_context`** compiles a token-budgeted patch (default budget 700) and reports `estimated_tokens`, `full_history_tokens`, `saved_tokens`, `savings_pct`. **`saved_tokens = full_history_tokens − estimated_tokens`**, where `full_history_tokens` = sum of *every* event's content tokens + every wiki page. This is the central token claim and its baseline is examined critically below.
- **`commit_turn`** journals BOTH the user prompt and the assistant answer as events, and auto-merges "durable phrasing" (`remember that`, `from now on`, `correction:`, `instead of`, …) into a wiki page.
- **Staleness:** there is **no automatic supersession on same-page re-save.** `_apply_save` *appends* the new claim as a bullet to the existing page. A fact only becomes stale via (a) explicit `mark_stale`, or (b) the compiler's "instead of X" regex, which supersedes older *events* (not wiki pages) at compile time.
- **Read path cost:** every tool call re-reads and re-validates the entire `events.jsonl` from disk; `prepare_context` re-fits BM25 over all events and **re-renders the candidate patch once per selected item** (an O(selected × N) inner loop). This is the main scalability suspect.
- **Robustness suspects:** `_read_jsonl` calls `json.loads` per line with no per-line guard — a single torn line raises and makes the whole store unreadable. Separate processes keep independent in-memory sequence counters (event-id uniqueness, mutation ids, wiki version numbers) — concurrent writers can collide.

### Honesty caveat baked into every token number
The tokenizer is **`o200k_base` (OpenAI family)**, not Claude's tokenizer. Anthropic does not ship a public local tokenizer, so contextgit cannot count Claude tokens exactly; `o200k_base` is a reasonable proxy (typically within ~10–20% of Claude's count for English prose) but every "tokens saved" figure is **a proxy for Claude, exact for OpenAI**. All results are reported in `o200k_base` tokens and labeled as such.

### The baseline question (most important framing)
contextgit defines savings as *patch vs. the entire raw journal re-sent*. Real Claude clients do **not** re-send the full raw journal every turn — they keep the actual recent conversation in the context window. So "saved 60%" answers *"how much smaller is the patch than dumping the whole journal"*, which is the tool's stated claim, **not** "how many tokens you save vs. how Claude normally works." We therefore measure the tool's own metric honestly **and** report a second, fairer baseline (patch vs. a sliding window of recent turns) so Reg sees both.

---

## Dimensions

Each dimension lists **(a) hypothesis**, **(b) setup**, **(c) pass criterion**, **(d) failure modes that matter**.

### D1 — Token efficiency (the headline claim)
- **(a) Hypothesis:** For a growing conversation, the compiled patch is materially smaller than the full raw history while staying within budget, and the savings grow with history length.
- **(b) Setup:** Deterministic synthetic transcripts at target sizes **5k / 20k / 50k / 100k o200k tokens**, in three topic mixes: *single-topic*, *multi-topic (5 interleaved threads)*, *abrupt topic shift (topic A then a hard switch to topic B)*. Feed each turn via `commit_turn`; at checkpoints call `prepare_context` with a realistic prompt for the current topic. Record `estimated_tokens`, `full_history_tokens`, `saved_tokens`, `savings_pct`, and `budget`. Also compute the **fair baseline**: patch tokens vs. last-N-turns sliding window (N chosen to match a typical client).
- **(c) Pass criterion:** Patch ≤ budget in 100% of calls; `savings_pct` vs full-history > 0 and non-decreasing with size for single-topic; patch remains coherent (non-empty selected context) when relevant memory exists. Report both baselines.
- **(d) Failure modes that matter:** patch exceeds budget; "savings" is an artifact of a strawman baseline (flag, don't fail); patch collapses to the empty `force_tiny` placeholder at large sizes (selected context silently lost); savings driven mostly by journaled *assistant* answers (self-inflating).

### D2 — Retrieval relevance (recall@k)
- **(a) Hypothesis:** When a prompt concerns a topic that has a prior memory, `prepare_context` surfaces the correct prior item.
- **(b) Setup:** Build a labeled fixture: **K needle facts** each tied to a query, embedded in a **haystack** of M unrelated turns (M ≫ K). For each query, the expected ref(s) are known. Run `prepare_context` (and `search_context` as a retrieval-only comparison). Compute **recall@5, recall@1, MRR** over the `selected` refs (and over search results). Grade mechanically against `expected_refs`; no eyeballing.
- **(c) Pass criterion:** recall@5 ≥ 0.8 and MRR ≥ 0.6 across the needle set; needles that are durable wiki facts should rank above raw chatter.
- **(d) Failure modes that matter:** needles buried below budget cutoff; BM25 term-mismatch (query uses synonyms the fact doesn't contain) → silent miss; wiki page with many appended bullets dilutes relevance.

### D3 — Retrieval precision / false positives
- **(a) Hypothesis:** For a prompt unrelated to anything in memory, the patch does **not** inject load-bearing recalls.
- **(b) Setup:** Queries guaranteed to have no relevant memory (topics absent from the store). Inspect `selected` context. Compute **precision@5** on the needle set and a **"clean miss" rate** on the no-answer set (fraction of no-answer queries whose patch contains zero selected items or only generic boilerplate).
- **(c) Pass criterion:** precision@5 ≥ 0.6 on needle queries; ≥ 90% of no-answer queries return no selected context (boilerplate header only). The compiler's `min_score` floor should keep junk out.
- **(d) Failure modes that matter:** unrelated facts surfaced as relevant (precision rot); recency/frequency weight dragging in irrelevant-but-recent or irrelevant-but-frequent items even at near-zero query relevance.

### D4 — Staleness handling & supersession
- **(a) Hypothesis:** After a fact is updated/contradicted, `prepare_context` surfaces the **latest** value and does not present the old one as current; `mark_stale` / `supersedes` actually work.
- **(b) Setup:** Three sub-tests:
  1. **Explicit:** `remember("deploy on Fridays")`, later `mark_stale("…", superseded_by="deploy on Mondays")`, then query "when do we deploy?". Check the stale fact is excluded and appears under "Avoid Stale/Superseded".
  2. **"instead of" phrasing:** `commit_turn` an event "use MySQL", then "use PostgreSQL instead of MySQL", then query "what database?". Check newer supersedes older event.
  3. **Same-page re-save (the suspected gap):** `remember("deploy on Fridays")` then `remember("deploy on Mondays")` with no stale marking. Inspect the wiki page and the patch — does it show BOTH as active bullets?
- **(c) Pass criterion:** sub-tests 1 & 2 surface only the latest and list the superseded ref. Sub-test 3 is **diagnostic**: document exactly what happens (expectation: both bullets coexist, i.e. the user must explicitly mark stale — a real caveat, not necessarily a fail).
- **(d) Failure modes that matter:** stale fact surfaced as current with no warning (the worst outcome for a memory layer); `mark_stale` no-ops; contradictory bullets presented with equal weight and no signal of which is current.

### D5 — Cross-session consistency & scope leakage
- **(a) Hypothesis:** Durable memory recalls across a new `conversation_id`; conversation-scoped behavior does not leak inappropriately.
- **(b) Setup:** Save durable facts under `conversation_id="A"`. Start fresh with `conversation_id="B"` and query. Confirm durable facts recall in B. Then check whether per-conversation raw events from A also surface in B (they share one store — is that intended sharing or leakage?).
- **(c) Pass criterion:** durable wiki facts recall in any conversation (intended). Document whether raw events are global or scoped — `conversation_id` is a label on events, not a partition, so cross-conversation visibility is expected; verify there is no *budget/ranking* corruption from mixing.
- **(d) Failure modes that matter:** durable fact fails to cross sessions (defeats the purpose); private conversation A content unexpectedly dominates conversation B's patch.

### D6 — Durability / crash & corruption
- **(a) Hypothesis:** Killing the server mid-write leaves a consistent, readable store; no corruption.
- **(b) Setup:** (1) Write many turns, `kill -9` the serving process during a burst of writes, restart, and verify the store reads back and event count is sane (append-only ⇒ at worst a lost/truncated last line). (2) **Fault injection:** deliberately append a torn/partial JSON line to `events.jsonl` (simulating a crash mid-line) and call any read tool. Observe whether the whole store becomes unreadable.
- **(c) Pass criterion:** after kill/restart the store opens and all fully-written events are intact. Document the torn-line behavior precisely.
- **(d) Failure modes that matter:** one partial line bricks the entire store (total data loss on read) — **blocker** if real; silent loss of acknowledged writes; corrupted JSON that crashes the server on every subsequent start.

### D7 — Latency (interactive budget)
- **(a) Hypothesis:** `prepare_context` p50/p95 stay within an interactive budget (<500ms ideal) at realistic store sizes.
- **(b) Setup:** Build stores of **100, 1,000, 10,000 events**. Run `prepare_context` ≥ 30 times per size with varied prompts; record p50/p95 wall-clock (in-process against the installed engine to isolate compute from MCP/stdio overhead, plus a spot check end-to-end over stdio). Also time `search_context`, `context_log`, `commit_turn`.
- **(c) Pass criterion:** p95 < 500ms at 1k events; report the curve and the size at which p95 crosses 500ms and 2s.
- **(d) Failure modes that matter:** superlinear blow-up (the O(selected×N) render loop + full re-read each call); p95 > 2s at 10k events makes it unusable for chat; commit_turn slows as journal grows.

### D8 — Storage growth
- **(a) Hypothesis:** Disk footprint grows ~linearly and modestly with event count.
- **(b) Setup:** Measure store dir size in bytes at 100 / 1k / 10k events and after heavy wiki updates (many bullets on one hot page → wiki_versions rewrites the whole page each version, a suspected quadratic). Report bytes/event.
- **(c) Pass criterion:** events.jsonl ~linear; flag any super-linear component (esp. wiki_versions for hot pages).
- **(d) Failure modes that matter:** unbounded growth; a single frequently-updated page bloating wiki_versions.jsonl quadratically.

### D9 — Hallucination risk (critical)
- **(a) Hypothesis:** The patch only ever contains text actually present in the store; it never fabricates content the user/assistant never produced.
- **(b) Setup:** Seed a store with known verbatim events. For many prompts, extract every quoted/summarized claim in the patch and verify each substring traces to a stored event/page (the summaries are mechanical truncations, so this should hold by construction — we verify empirically). **Separately, test the more subtle vector:** journal a turn where the *assistant answer* contains a wrong "fact", then query — does that assistant-originated claim resurface as durable context presented as truth?
- **(c) Pass criterion:** 100% of patch claims trace to stored text (no fabrication). Document clearly that *assistant answers are memory-eligible*, so a wrong answer can be recalled later — this is propagation, not fabrication, but it matters.
- **(d) Failure modes that matter:** any patch text with no source in the store (true fabrication — **blocker**); assistant misstatements silently promoted to durable memory and re-surfaced as fact.

### D10 — Concurrent writers
- **(a) Hypothesis:** Two Claude sessions writing the same store concurrently do not corrupt it or collide on ids.
- **(b) Setup:** Spawn 2+ processes each committing many turns to the **same** store simultaneously. Afterward verify: total events == sum of writes, no duplicate `event_id`, no duplicate mutation id / wiki version number, store still reads.
- **(c) Pass criterion:** no lost or duplicated events; ids unique; store readable. (POSIX O_APPEND makes single small writes atomic, but independent sequence counters are the risk.)
- **(d) Failure modes that matter:** interleaved/torn lines; duplicate ids causing the append-time uniqueness check to reject legitimate writes after restart; wiki version-number collisions corrupting history ordering.

### D11 — Edge cases
- **(a) Hypothesis:** The server degrades gracefully on adversarial/unusual input.
- **(b) Setup, each captured:** empty store first call (done — 31-tok boilerplate, 0 saved); **very long single event** (~50k-token paste) then prepare (does token_cost_penalty exclude it? does the patch stay in budget?); **non-English text** (CJK, accented) — token counting and BM25 tokenization behavior; **special characters / control chars / emoji / JSON-breaking strings**; **malformed MCP input** (missing required arg, wrong type, unknown tool, non-JSON line) — server must return a JSON-RPC error, not crash.
- **(c) Pass criterion:** no crash on any input; malformed calls return structured errors; budget respected even with a giant event; non-English content is stored and retrievable.
- **(d) Failure modes that matter:** server crash/hang on malformed input (would take down the MCP connection for the whole Claude session); giant event blowing the budget or the latency; non-Latin text silently unsearchable (BM25 `tokenize` strips on `\w` which *does* keep Unicode word chars in Python — verify).

### D12 — Claude integration smoke test (end-to-end usefulness)
- **(a) Hypothesis:** Injecting the `prepare_context` patch changes Claude's answer in a useful, correct direction vs. not injecting it.
- **(b) Setup:** A realistic long transcript stored in contextgit. Pick a query whose correct answer depends on a stored fact. Capture the exact patch `prepare_context` returns. Produce two answers to the same query — **WITH** the patch in context and **WITHOUT** — and compare for correctness/relevance. Programmatic Anthropic-SDK runs cost money: by default this is done as an **instrumented manual run** (the reviewing model acts as Claude, no API spend); the SDK option + cost estimate is offered to Reg for a paid confirmation if wanted.
- **(c) Pass criterion:** WITH-patch answer is correct/grounded in the stored fact; WITHOUT-patch answer is wrong/generic — demonstrating the patch carries decision-relevant information at a small token cost.
- **(d) Failure modes that matter:** patch present but answer unchanged (no value); patch injects the *stale* value and degrades the answer (anti-value); patch too noisy to help.

---

## Reproducibility & isolation rules for the harness
- **Never touch Reg's real store** (`~/.contextgit/store`) or the live Claude config during benchmarks. Every test uses a throwaway store via `CONTEXTGIT_DIR` / `--store` under a temp dir or `bench/results/<ts>/`.
- Drive the **installed** package (`/Users/regtroka/.local/share/uv/tools/contextgit-mcp/bin/python`), not the source tree — we test what users get.
- Fixtures generated **deterministically** (fixed seed, fixed vocabulary) so re-runs reproduce. Wall-clock timestamps inside the store differ run-to-run (engine uses `utc_now`), which affects only recency weighting marginally; retrieval/token metrics are stable.
- Every run writes timestamped JSON + a human summary to `bench/results/`.
- All token figures labeled `o200k_base`. Latency reported as p50/p95 with N.
