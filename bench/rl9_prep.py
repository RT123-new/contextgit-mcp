"""RL-9 prep: build hard-negative stores from REAL ResearchLoop corrections and
capture the exact prepare_context patches to feed to a real model (blind subagents).

For each case we build two stores:
  - CLEAN: only the corrected (current) fact, via remember() (high-confidence).
  - HARDNEG: the OLD (pre-correction) fact committed first (older, no marker) +
    the NEW fact committed later (no marker) -> per RL-3 both coexist unflagged.
We verify via explain_selection that BOTH the stale and current values are in the
selected patch, then emit the patch text so a blind Claude call answers the question.
"""
from __future__ import annotations
import json, os, sys, time
import cgcommon as C
from cgcommon import new_engine

CASES = [
    {"id": "tables",
     "old": "The live Supabase schema has 12 tables, including a profiles table and a run_steps table.",
     "new": "The live Supabase schema has exactly 10 tables; there is no profiles table and no run_steps table.",
     "q": "How many tables does the live Supabase schema have, and do profiles/run_steps tables exist?",
     "current_markers": ["10", "ten"], "stale_markers": ["12", "twelve"]},
    {"id": "trigger",
     "old": "Background jobs use the Trigger.dev v2 client.defineJob API.",
     "new": "Background jobs use Trigger.dev v4 via the task and schemaTask API; client.defineJob is deprecated and breaks the app.",
     "q": "Which Trigger.dev API should I use for a new background task?",
     "current_markers": ["v4", "schematask", "task("], "stale_markers": ["definejob", "v2"]},
    {"id": "mockfallback",
     "old": "In production the app silently falls back to the deterministic mock research provider when no real provider is configured.",
     "new": "In production the app refuses to fall back to the mock provider and throws a config error unless ALLOW_MOCK_PROVIDER=true is explicitly set.",
     "q": "If no real research provider is configured, does production fall back to the mock provider?",
     "current_markers": ["refus", "throw", "error", "allow_mock_provider"], "stale_markers": ["silently fall", "falls back"]},
]

def build_patches(run_dir):
    out = []
    for c in CASES:
        # CLEAN
        sc = os.path.join(run_dir, "stores", f"rl9_clean_{c['id']}")
        e = new_engine(sc, budget=700)
        e.remember(c["new"], page=f"{c['id'].title()} Memory")
        clean_patch = e.prepare(c["q"], conversation_id="v", record_usage=False)["context"]
        # HARDNEG: old (older) then new (newer), plain commit_turn, no markers
        sh = os.path.join(run_dir, "stores", f"rl9_hard_{c['id']}")
        e2 = new_engine(sh, budget=700)
        e2.commit_turn(c["old"], "ok", conversation_id="h")
        time.sleep(1.2)
        e2.commit_turn(c["new"], "ok", conversation_id="h")
        hard = e2.prepare(c["q"], conversation_id="v", record_usage=False)
        ex = e2.explain(c["q"], budget=700)
        ctx = hard["context"]
        stale_in = any(m in ctx.lower() for m in c["stale_markers"])
        cur_in = any(m in ctx.lower() for m in c["current_markers"])
        out.append({"id": c["id"], "q": c["q"],
                    "current_markers": c["current_markers"], "stale_markers": c["stale_markers"],
                    "clean_patch": clean_patch, "hardneg_patch": ctx,
                    "hardneg_has_stale_value": stale_in, "hardneg_has_current_value": cur_in,
                    "hardneg_both_present": stale_in and cur_in})
        import shutil; shutil.rmtree(sc, ignore_errors=True); shutil.rmtree(sh, ignore_errors=True)
    return out

if __name__ == "__main__":
    run_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(C.RESULTS_DIR, "rl9_" + C.ts())
    os.makedirs(run_dir, exist_ok=True)
    data = build_patches(run_dir)
    json.dump(data, open(os.path.join(run_dir, "rl9_patches.json"), "w"), indent=2, ensure_ascii=False)
    print("run_dir:", run_dir)
    for d in data:
        print(f"\n### CASE {d['id']}: both_present={d['hardneg_both_present']} (stale={d['hardneg_has_stale_value']}, current={d['hardneg_has_current_value']})")
        print("Q:", d["q"])
        print("--- HARDNEG PATCH ---")
        print(d["hardneg_patch"])
