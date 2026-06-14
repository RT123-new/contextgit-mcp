# Codex Findings for contextgit-mcp

Date: 2026-06-14  
Author: Codex-side additive viability pass  
Target real project: `/Users/regtroka/Downloads/branchingcontextclean-main`  
Released package under test: `contextgit-mcp==0.1.0` via `/Users/regtroka/.local/share/uv/tools/contextgit-mcp/bin/python`  
Token counter: `tiktoken_o200k_base`

Results: `bench/codex/results/20260614_234438/`  
Harness: `bench/codex/codex_viability.py`

## Verdict: viable-with-caveats for curated, small stores; not viable as an always-on Codex memory layer for a meaty project yet

The positive result is real: the installed MCP server exposes all 12 expected tools over stdio, works through the released package path, retrieves a deliberately remembered fact, is deterministic for byte-identical patches, and the isolated-store guardrails held. No real `~/.contextgit/store`, target-project `.contextgit`, or `~/.codex/config.toml` bytes changed during the harness run.

The negative result is also real, and sharper than the prior ResearchLoop pass: on `branchingcontextclean-main`, a 320-chunk real-project store caused every one of 20 natural qrels to collapse to the empty `force_tiny` patch at the default 700-token budget. Raising budget to 3000 removed the empty-patch failure, but retrieval quality remained weak: recall@5 was only 0.05 and mean nDCG@10 was 0.011. On a smaller 80-chunk subset, default-budget recall@5 improved to 0.25, still not good enough for an always-on assistant memory layer.

## Methodology

Each automated test recorded hypothesis/setup/pass/failure criteria in code and graded mechanically. The harness built a fresh corpus from the target repo files while excluding `.git`, `.venv`, `.contextgit`, `dist`, caches, and existing results. It indexed 320 chunks from 79 files, then wrote all test stores under `bench/codex/results/20260614_234438/stores/` and removed them at the end.

Isolation checks passed:

| Guardrail | Before | After |
|---|---:|---:|
| global `~/.contextgit/store/events.jsonl` bytes | 6525 | 6525 |
| global store total bytes | 26025 | 26025 |
| target `.contextgit/events.jsonl` bytes | 9694 | 9694 |
| target `.contextgit` total bytes | 14758 | 14758 |
| `~/.codex/config.toml` hash | unchanged | unchanged |
| test stores removed | n/a | yes |

## Findings

### 1. Codex install cannot safely repoint an existing contextgit block

**Hypothesis:** `contextgit install codex --store <isolated>` should let Codex dogfood with an isolated store.  
**Setup:** Read current `~/.codex/config.toml`; run the released install command with an isolated result-local store.  
**Result:** Current config already has:

```toml
[mcp_servers.contextgit]
command = "/Users/regtroka/Downloads/branchingcontextclean-main/contextgit/.venv/bin/python3.13"
args = ["-m", "contextgit", "serve"]
```

The released installer returned: `'contextgit' is already configured ... nothing changed.` It did not add `--store`, and did not repoint to the released uv tool environment. This prevented a true restart-and-dogfood run inside the current Codex session.

**Solution:** add `contextgit install codex --force` or `--update-existing`, plus a config audit warning when the block points at a project venv or lacks `--store`. For safe testing, installer should write a `.bak`, replace only the contextgit block, and print an exact restore command.

### 2. Stdio MCP protocol works

**Hypothesis:** the installed server works over real MCP stdio, not only in-process.  
**Setup:** Spawned `python -m contextgit serve --store <isolated> --budget 700`; initialized JSON-RPC; listed tools; called `remember`, `prepare_context`, and `context_stats`.  
**Result:** Pass. Tool count = 12; `prepare_context` returned the remembered `ContextOnlyDeterministicProvider` fact; `context_stats` reported the isolated store path.

**Solution:** keep this as a CI smoke test so tool schemas and stdio behavior do not drift.

### 3. Default-budget real-project qrels collapsed to empty patches

**Hypothesis:** natural questions over a larger real project should retrieve answer chunks with useful recall/nDCG.  
**Setup:** 20 labeled qrels over `branchingcontextclean-main`, 320 chunks, 640 events, default budget 700.  
**Pass criterion:** non-empty patches and recall@5 meaningfully above the prior ResearchLoop 0.58 baseline.  
**Result:** Fail. 20/20 qrels returned the 20-token empty placeholder:

```text
Context Merge Patch:
- No selected context fits the configured budget.
Provenance: (none)
```

Metrics at budget 700:

| Metric | Value |
|---|---:|
| force_tiny rate | 1.00 |
| recall@1 | 0.00 |
| recall@5 | 0.00 |
| recall@10 | 0.00 |
| MRR | 0.00 |
| mean nDCG@10 | 0.00 |

**Solution:** fix final-render overflow so it degrades by dropping lowest-value selected items or avoid-stale/provenance lines until the patch fits. Count avoid-stale/provenance cost during greedy selection. Never return empty when relevant candidates were selected.

### 4. Bigger budget avoids collapse but not poor ranking

At budget 3000 on the same 320 chunks, force_tiny disappeared, but recall stayed very low:

| Metric | Value |
|---|---:|
| force_tiny rate | 0.00 |
| recall@5 | 0.05 |
| recall@10 | 0.05 |
| MRR | 0.021 |
| mean nDCG@10 | 0.011 |

On an 80-chunk subset at budget 700:

| Metric | Value |
|---|---:|
| force_tiny rate | 0.00 |
| recall@5 | 0.25 |
| recall@10 | 0.35 |
| MRR | 0.121 |
| mean nDCG@10 | 0.102 |

This suggests two separate issues: the empty-patch bug at realistic project size, and weak lexical ranking even when the patch renders.

**Solution:** add field-aware retrieval over paths/headings/code identifiers, semantic or trigram expansion, and a query-relevance gate. Avoid auto-merged wiki pages from indexed source text dominating the result set.

### 5. Multi-hop synthesis failed under the same default collapse

**Hypothesis:** queries needing 2+ facts should retrieve all required facts.  
**Setup:** four multi-hop probes over real-provider setup, branch/merge architecture, reporting rules, and Codex installation.  
**Result:** mean selected completeness = 0.0; mean patch-visible completeness = 0.0. Every probe selected zero refs because default compile collapsed.

**Solution:** after fixing force_tiny, preserve multi-value facts as structured, non-truncated bullets and add a completeness signal when a query asks for "how", "all", "setup", or "configure".

### 6. Long-session drift loses early facts by 220 turns

**Hypothesis:** early raw and durable facts should remain retrievable after a 200+ turn session.  
**Setup:** seeded three early raw facts and two durable wiki facts, then appended 220 real-project noise turns.  
**Result:** found@5 started at 1.0, stayed 1.0 after 50 turns, dropped to 0.6 after 100, and reached 0.0 after 220. By 220 turns, none of the early anchors were visible in the patch.

**Solution:** add rehearsal/recurrence boosts for durable facts, archive indexes that do not depend on recency, and a hard preference for explicitly remembered wiki facts over recent source-ingestion chatter.

### 7. `commit_turn` auto-merge has high recall but false-merges question-shaped text

**Hypothesis:** marker-based durable detection should capture durable statements without promoting ordinary questions.  
**Setup:** 12 prompts, six durable and six non-durable.  
**Result:** precision = 0.857, recall = 1.0. The false positive was:

```text
What is better, branch_merge instead of full_context for this case?
```

It auto-merged only because it contained `instead of`.

**Solution:** require imperative/save intent or put marker-detected claims into pending review. Do not auto-promote question-shaped text. Also normalize durable claims after marker stripping; the test reproduced the leading-comma claim from `From now on, ...`.

### 8. Determinism claim held

**Hypothesis:** same store + same prompt should produce byte-identical patches.  
**Setup:** six repeated `prepare_context(..., record_usage=False)` calls over the same store and prompt.  
**Result:** pass. One unique SHA-256 hash across six runs.

**Solution:** keep this regression test; if usage/timestamps ever enter patch rendering, this will catch it.

### 9. Independent empty-patch repro succeeded

**Hypothesis:** the prior empty-patch bug is reproducible with a new harness.  
**Setup:** varied relevant wiki pages plus stale pages at default budget 700.  
**Result:** reproduced with 11 relevant pages and 8 stale pages: selected_count = 0 and `force_tiny = true`.

**Solution:** same as finding 3: final overflow must degrade gracefully, and avoid-stale cost must be budgeted before final render.

### 10. Independent savings-meter repro succeeded

**Hypothesis:** tiny stores can cost more tokens than they save, and the ledger should show that.  
**Setup:** 10 prepare calls on a tiny two-event store.  
**Result:** patch_total = 1170, full_total = 20, true net = -1150 tokens, but recorded_saved_total = 0 because losses are clamped away.

**Solution:** record signed `net_saved_tokens = full_history_tokens - patch_tokens`; report gross wins and losses separately; allow headline savings percentage to go negative.

### 11. GPT cross-model harm test was not run programmatically

**Hypothesis:** GPT-family models may be more/less robust than Claude against stale facts.  
**Setup:** checked feasibility from the harness.  
**Result:** not run. This Codex session uses a GPT reasoning model, but the script does not have a repeatable model-call API surface without a separately approved API key/run. I did not read or use `.env.local` secrets.

**Solution:** add an optional `--run-openai` lane that requires an explicit user-approved `OPENAI_API_KEY`, retrieval-gates stale patches with `explain_selection`, pads prompts, and runs K>=5 per clean/hard-negative/closed-book arm.

## Agreement with the prior agent

Confirmed:

- empty default-budget patch is real and can be severe;
- token-savings meter is one-sided;
- correction/stale rendering can create harmful budget behavior;
- auto-merge is over-eager;
- determinism holds when the patch renders.

Sharpened:

- `branchingcontextclean-main` is large enough to make default-budget empty patches happen on every natural qrel in the full 320-chunk corpus;
- even after raising budget to 3000, retrieval over dense project prose/code remains weak;
- Codex installer UX has a specific no-op problem when a contextgit block already exists.

Not completed:

- true restarted-Codex dogfood with an isolated store;
- paid/programmatic GPT stale-harm A/B.

## Recommended defaults before daily Codex use

| Area | Recommendation |
|---|---|
| Codex config | Repoint to the released tool path and pin `--store` per project. |
| Store scope | Small, curated per-project stores; avoid bulk-indexing whole repos through `commit_turn`. |
| Budget | Do not rely on 700 until graceful degradation is fixed; raising budget avoids collapse but does not fix ranking. |
| Auto-merge | Disable autonomous marker merges or route them to pending review. |
| Curation | Store short atomic facts with `remember`; actively mark stale; avoid compound pages until truncation is fixed. |
| Metrics | Treat token savings as untrusted until signed net savings are reported. |

## Issue drafts to add or update

1. Codex installer cannot update existing contextgit block; needs `--force`/audit.
2. Empty patch at default budget on `branchingcontextclean-main` qrels: 20/20 force_tiny.
3. Dense real-project retrieval remains poor at larger budget: recall@5 0.05 over 320 chunks.
4. Long-session drift loses early raw and durable facts by 220 turns.
5. Auto-merge false-promotes non-durable questions containing `instead of`.
6. Token ledger clamps negative savings to zero.
