# contextgit-mcp viability benchmark harness

Re-runnable harness behind `TEST_PLAN.md` / `VIABILITY_REPORT.md`. It drives the
**installed** released package (not the source tree) so it measures what users get.

## Prereqs
```bash
uv tool install "contextgit-mcp[tokens]"      # isolated install incl. tiktoken
```
The harness imports the engine in-process, so run it with that tool's interpreter:
```
VENV_PY=/Users/regtroka/.local/share/uv/tools/contextgit-mcp/bin/python
```
(If your uv tool dir differs, run `uv tool dir` and adjust `VENV_PY`/`CONTEXTGIT_BIN` in `cgcommon.py`.)

## Run
```bash
cd bench
$VENV_PY bench_main.py all        # all dimensions D1..D11 -> results/<timestamp>/
$VENV_PY bench_main.py d7         # one dimension
$VENV_PY integration_smoke.py     # D12 end-to-end patch capture (no API cost)
```

## Files
- `cgcommon.py` — deterministic transcript/fixture generation, token counting (`o200k_base`), a minimal stdio MCP client (`MCPClient`) that spawns the real `contextgit serve`.
- `bench_main.py` — dimensions:
  - `d1` token efficiency · `d2`(+D3) retrieval recall/precision/MRR · `d4` staleness/supersession · `d5` cross-session · `d6` durability (kill-9 + torn-line) · `d7` latency curve · `d8` storage growth · `d9` hallucination/provenance · `d10` concurrent writers · `d11` edge cases (giant event, non-English, special chars, malformed MCP).
- `integration_smoke.py` — D12: builds a long store, captures the exact `prepare_context` patch, emits with/without message stacks + token accounting.
- `results/<timestamp>/` — one JSON per dimension + `_summary.json`.

## Isolation & determinism
- Every test uses a throwaway store; Reg's real `~/.contextgit/store` is never touched.
- Fixtures are seeded (fixed vocab/seed) so re-runs reproduce. Store timestamps use wall-clock (1s precision) which only marginally affects recency weighting.
- All token figures are `tiktoken_o200k_base` (OpenAI proxy for Claude — see TEST_PLAN caveat).
- Heavy notes: a single `prepare_context` at 10k events takes ~30s; `d7` keeps few iterations at large N.
