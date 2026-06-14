"""D12 — Claude integration smoke test (instrumented manual run, no API cost).

Builds a realistic long transcript with ONE decision-relevant fact buried far
back, then captures the EXACT `prepare_context` patch a Claude client would
receive. It emits two ready-to-send message stacks for the same query:
  - WITHOUT the patch (control)
  - WITH the patch injected as a system/context message
plus token accounting so an Anthropic-SDK run can be costed precisely.

The reviewing model then answers both stacks (acting as Claude) and the
report compares them. Running the SDK for real is optional and costed below.
"""
from __future__ import annotations
import json, os, sys
import cgcommon as C
from cgcommon import new_engine, gen_turns, toks

def main():
    run_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(C.RESULTS_DIR, C.ts())
    os.makedirs(run_dir, exist_ok=True)
    sdir = os.path.join(run_dir, "stores", "d12_integration")
    eng = new_engine(sdir, budget=700)

    # 1) Long, realistic, multi-topic history (~12k tokens of chatter).
    chatter = gen_turns(12000, "multi", seed=2027)
    for u, a in chatter:
        eng.commit_turn(u, a, conversation_id="projectwork")

    # 2) A buried, decision-relevant fact stated ~60 turns ago, then more chatter.
    eng.commit_turn(
        "Important decision: for the Atlas service we are migrating off PostgreSQL to "
        "MySQL 8 because PostgreSQL's per-tenant connection ceiling was capping us at "
        "100 rps. Use MySQL instead of PostgreSQL for all new Atlas work. "
        "Remember that the Atlas database is MySQL now.",
        "Understood — Atlas now standardizes on MySQL 8 going forward.",
        conversation_id="projectwork",
    )
    more = gen_turns(4000, "multi", seed=4242)
    for u, a in more:
        eng.commit_turn(u, a, conversation_id="projectwork")

    # 3) The query whose correct answer depends on the buried decision.
    query = "I'm starting a new Atlas feature. Which database should I use, and why?"
    patch = eng.prepare(query, conversation_id="newsession")  # fresh conversation id
    status = eng.status()

    # 4) Build the two message stacks a client would send.
    system_base = ("You are a helpful engineering assistant. Answer concisely using any "
                   "durable project context provided.")
    without_stack = [
        {"role": "system", "content": system_base},
        {"role": "user", "content": query},
    ]
    with_stack = [
        {"role": "system", "content": system_base + "\n\n[Durable project memory]\n" + patch["context"]},
        {"role": "user", "content": query},
    ]
    without_tokens = sum(toks(m["content"]) for m in without_stack)
    with_tokens = sum(toks(m["content"]) for m in with_stack)
    full_history_inject = status["full_history_tokens"]  # if a client dumped the whole journal instead

    out = {
        "dimension": "D12 integration_smoke",
        "store_dir": sdir,
        "query": query,
        "store_status": {k: status[k] for k in ("events", "wiki_pages", "full_history_tokens", "budget_tokens", "token_counter")},
        "prepare_context_patch": patch["context"],
        "patch_tokens": patch["estimated_tokens"],
        "patch_selected": patch["selected"],
        "patch_surfaces_mysql_decision": ("MySQL" in patch["context"]),
        "message_stack_without_patch": without_stack,
        "message_stack_with_patch": with_stack,
        "tokens": {
            "without_patch_input": without_tokens,
            "with_patch_input": with_tokens,
            "patch_overhead": with_tokens - without_tokens,
            "if_full_journal_injected_instead": full_history_inject,
        },
        "sdk_cost_estimate_note": (
            "An Anthropic-SDK confirmation = 2 messages.create calls (with/without). "
            "Input ~%d + ~%d tokens; output a few hundred each. On Opus pricing that is well "
            "under 1 US cent total. Mock run is sufficient; SDK run only needed if Reg wants a "
            "real-model confirmation." % (with_tokens, without_tokens)
        ),
    }
    path = os.path.join(run_dir, "d12.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print("wrote", path)
    print("patch_tokens:", out["patch_tokens"], "| surfaces MySQL decision:", out["patch_surfaces_mysql_decision"])
    print("with_patch_input_tokens:", with_tokens, "| without:", without_tokens,
          "| full-journal-would-be:", full_history_inject)
    print("\n----- PATCH THAT WOULD BE INJECTED -----\n" + patch["context"])

if __name__ == "__main__":
    main()
