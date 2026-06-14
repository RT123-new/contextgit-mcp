"""RL-9' definitive harm test — build hard-negative cases from REAL ResearchLoop
corrections, with a retrieval GATE so 'harm' is only attributed when the stale
fact was actually retrieved into the patch. Emits JSON for a blind-grading workflow.

Cases are chosen to be NON-INFERABLE without the store (so a correct answer must
come from memory, not the model's priors) and to actually RETRIEVE (avoids the
'mock' noise-filter blind spot)."""
from __future__ import annotations
import json, os, sys, time, shutil
import cgcommon as C
from cgcommon import new_engine

CASES = [
    {"id": "reconcile",
     "old": "Resuming a queued research run always re-dispatches it to Trigger.dev.",
     "new": "On resume, a queued research run that already has a Trigger.dev handle in its metadata is reconciled, not re-dispatched; only handle-less queued runs are re-scheduled.",
     "q": "What happens when you resume a queued research run that already has a Trigger.dev handle?",
     "current": ["reconcil", "not re-dispatch", "not redispatch", "already has", "existing handle", "handle-less"],
     "stale": ["always re-dispatch", "always redispatch", "re-dispatches it", "redispatches it"],
     "inferable": False},
    {"id": "guardrails",
     "old": "There are no limits on how many research runs can be launched.",
     "new": "Run launch is capped at 3 active runs per project, 5 active per user, 25 runs per project per day, and 50 runs per user per day.",
     "q": "Are there limits on how many research runs can be launched, and what are they?",
     "current": ["3 active", "25", "50", "5 active", "capped", "per project per day"],
     "stale": ["no limit", "no limits", "unlimited", "there are no"],
     "inferable": False},
    {"id": "tables",
     "old": "The Supabase schema has 12 tables, including a profiles table and a run_steps table.",
     "new": "The Supabase schema has exactly 10 tables; there is no profiles table and no run_steps table.",
     "q": "How many tables does the Supabase schema have, and do profiles/run_steps tables exist?",
     "current": ["10", "ten", "no profiles", "no run_steps", "neither"],
     "stale": ["12", "twelve", "profiles table", "run_steps table"],
     "inferable": False},
    {"id": "trigger",  # model-prior-aligned control (v4 is the model's likely guess anyway)
     "old": "Background jobs use the Trigger.dev v2 client.defineJob API.",
     "new": "Background jobs use Trigger.dev v4 via the task and schemaTask API; client.defineJob is deprecated and breaks the app.",
     "q": "Which Trigger.dev API should I use for a new background task?",
     "current": ["v4", "schematask", "schema task", "task("],
     "stale": ["v2", "definejob", "define job"],
     "inferable": True},
]

def build(run_dir):
    cases = []
    for c in CASES:
        # CLEAN: only the corrected fact via remember() (high confidence)
        sc = os.path.join(run_dir, "stores", f"c_{c['id']}")
        e = new_engine(sc, budget=700)
        e.remember(c["new"], page=f"{c['id'].title()} Memory")
        clean_patch = e.prepare(c["q"], conversation_id="v", record_usage=False)["context"]
        # HARDNEG: old (older) then new (newer), plain commit_turn, no markers -> coexist
        sh = os.path.join(run_dir, "stores", f"h_{c['id']}")
        e2 = new_engine(sh, budget=700)
        e2.commit_turn(c["old"], "ok", conversation_id="h"); time.sleep(1.2)
        e2.commit_turn(c["new"], "ok", conversation_id="h")
        hp = e2.prepare(c["q"], conversation_id="v", record_usage=False)["context"]
        low = hp.lower()
        stale_in = any(m in low for m in c["stale"])
        cur_in = any(m in low for m in c["current"])
        cases.append({"id": c["id"], "q": c["q"], "current": c["current"], "stale": c["stale"],
                      "inferable": c["inferable"], "clean_patch": clean_patch, "hardneg_patch": hp,
                      "retrieval_gate_both_present": stale_in and cur_in,
                      "hardneg_has_stale": stale_in, "hardneg_has_current": cur_in})
        shutil.rmtree(sc, ignore_errors=True); shutil.rmtree(sh, ignore_errors=True)
    return cases

if __name__ == "__main__":
    run_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(C.RESULTS_DIR, "rl9def_" + C.ts())
    os.makedirs(run_dir, exist_ok=True)
    cases = build(run_dir)
    json.dump(cases, open(os.path.join(run_dir, "rl9_cases.json"), "w"), indent=2, ensure_ascii=False)
    print("run_dir:", run_dir)
    for c in cases:
        print(f"  {c['id']:10} gate_both_present={c['retrieval_gate_both_present']} (stale={c['hardneg_has_stale']} cur={c['hardneg_has_current']}) inferable={c['inferable']}")
