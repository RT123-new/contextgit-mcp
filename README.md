# contextgit

**git for your AI's context.** A local-first, deterministic memory engine for Claude Desktop, Claude Code, Codex, and Cursor — served over MCP.

Every conversation turn is **committed** to an append-only journal. Durable facts (decisions, corrections, preferences) **merge** into a versioned memory wiki. Each new prompt gets a **branch**: a compact, token-budgeted context patch compiled from your whole history — with full provenance, per-item salience scores, and a token meter that shows exactly what you saved.

No embeddings API. No cloud. No LLM in the loop. The compiler is mechanical and deterministic: the same history and prompt always produce the same context, and every selection or exclusion has an inspectable reason.

```
$ contextgit branch "What database does Atlas use?"
Context Merge Patch:
- recurring_topics: atlas(20), postgresql(7), dashboard(4)
Selected Context:
- [wiki:Atlas Memory] wiki; score=0.636; Atlas Memory - correction: use MySQL.
- [event:demo_00022] event; score=0.623; Recorded Atlas API limits: 100 rps/tenant ...
Avoid Stale/Superseded:
- event:demo_00003 superseded_by event:demo_00005

-- 299 tokens (budget 300) | full history would be 670 tokens | saved 371 (55%)
```

## Install

```bash
pip install contextgit-mcp          # or: pipx install contextgit-mcp / uv tool install contextgit-mcp
pip install "contextgit-mcp[tokens]"  # + tiktoken for exact token counts (recommended)
```

> Not on PyPI yet? Install from source: `pip install -e ./contextgit`

## Quick start (60 seconds)

```bash
contextgit init        # create a .contextgit/ store here (like git init)
contextgit demo        # optional: seed sample data
contextgit branch "What database does Atlas use?"           # compile a context patch
contextgit branch "What database does Atlas use?" --explain # why each item was selected/excluded
contextgit stats       # token meter: patch tokens vs. tokens saved
```

### Prefer buttons to commands?

```bash
contextgit ui          # opens a private dashboard in your browser
```

A local point-and-click view of everything: what your AI knows, facts waiting
for your approval (approve/reject), a "teach it something" box, search with
one-click "mark outdated", and a live preview of the exact context any
question would get — with the token savings metered. Binds to 127.0.0.1 only;
every request requires a per-session token, so nothing on your network (or any
website you visit) can reach your store.

## Hook it into your AI apps

One command per client — it edits the client's config for you (with a `.bak` backup):

```bash
contextgit install claude-code      # writes .mcp.json in the current project
contextgit install claude-desktop   # edits claude_desktop_config.json
contextgit install codex            # adds [mcp_servers.contextgit] to ~/.codex/config.toml
contextgit install cursor           # edits ~/.cursor/mcp.json
contextgit install print            # just show all config snippets
```

Restart the client. Then ask Claude (or Codex):

> "Use prepare_context to load what you know about this project."
> "Remember that we deploy on Fridays."
> "Show me the merge log — what have you saved about me?"
> "Why didn't you remember X? Explain the selection."

### What the model sees (MCP tools)

| Tool | What it does |
|---|---|
| `prepare_context` | Compile a token-budgeted context patch relevant to the prompt, with token accounting |
| `commit_turn` | Journal a finished turn; durable phrasing auto-merges into the wiki |
| `remember` / `mark_stale` | Explicitly save a fact / retire an outdated one |
| `search_context` | BM25 search over all events + wiki pages |
| `context_log` / `show_context` | Recent events; any record in full by ref |
| `full_context` | Page through the complete raw history (token counts included) |
| `explain_selection` | Per-item salience scores + exclusion reasons for a prompt |
| `merge_log` / `resolve_pending` | Merge history; approve/reject pending merges |
| `context_stats` | Token meter: compilations, tokens served, tokens saved |

## The git mental model

| git | contextgit |
|---|---|
| repository | `.contextgit/` store (per project, or `~/.contextgit/store` global) |
| commit | journaled conversation turn (`commit_turn`, append-only `events.jsonl`) |
| branch | compiled context patch for the current prompt (`contextgit branch`) |
| merge | durable fact saved to the versioned wiki (`merge_log`, `mutations.jsonl`) |
| staging area | pending-merge queue (`contextgit pending list / approve / reject`) |
| log / show | `contextgit log`, `contextgit show event:<id> | wiki:<title> | mut:<id>` |
| blame | provenance: every wiki claim links to the source events that produced it |

Store resolution is git-style too: `--store` flag → `CONTEXTGIT_DIR` env var → nearest `.contextgit/` walking up from the working directory → global `~/.contextgit/store`.

## Why deterministic?

Memory systems that summarize with an LLM are unauditable: you can't know why something was remembered, forgotten, or silently rewritten. contextgit's compiler is a mechanical scoring function (frequency, recency, query relevance via BM25, correction priority, source confidence, open-loop bonus, token cost, staleness penalty). That means:

- **Reproducible** — same store + same prompt = same context, byte for byte.
- **Explainable** — `--explain` shows each item's score components and exclusion reasons.
- **Correction-safe** — "use MySQL instead of PostgreSQL" supersedes the old fact; stale items are excluded *and* listed under "Avoid Stale/Superseded" so the model doesn't relearn them.
- **Auditable** — every memory mutation is in an append-only log with before/after state hashes.

## Token tracking

Every compilation appends a row to `usage.jsonl`: patch tokens, what full history would have cost, tokens saved. Counting uses tiktoken when installed (`o200k_base`), with an honest `fallback_estimate` label otherwise.

```bash
contextgit stats
#                 compilations  patch tokens  saved tokens   savings
# all time                  14          4186          21340    63.1%
```

## Storage format (yours, forever)

Plain JSONL in `.contextgit/` — no database, no lock-in:

```
events.jsonl          append-only conversation journal
wiki_versions.jsonl   every version of every memory page
mutations.jsonl       append-only merge log (save / promote / mark_stale / reject)
audit.jsonl           decision audit with state hashes
pending.json          merge candidates awaiting review
usage.jsonl           token meter ledger
```

`contextgit export` dumps a single JSON snapshot.

## Development

```bash
pip install -e ".[dev,tokens]"
pytest
```

The engine (deterministic compiler, BM25 retrieval, versioned store) is extracted from [branch-context-lab](../branch-context-lab), where it is benchmarked against eager/full-history baselines on contamination, staleness, and recall metrics.

## License

Apache-2.0
