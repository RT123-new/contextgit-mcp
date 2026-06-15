"""contextgit CLI — git-style commands over your AI's context.

    contextgit init            create a context store here (.contextgit/)
    contextgit ui              open the point-and-click dashboard in your browser
    contextgit serve           run the MCP server on stdio
    contextgit install X       wire the server into claude-desktop/claude-code/codex/cursor
    contextgit status          store overview + token counters
    contextgit log             recent events (newest first)
    contextgit show REF        full record: event:<id> | wiki:<title> | mut:<id>
    contextgit search QUERY    BM25 search over events + wiki
    contextgit branch PROMPT   compile the context branch for a prompt
    contextgit merges          merge history + pending queue
    contextgit pending ...     list / approve / reject pending merges
    contextgit remember FACT   save a durable fact
    contextgit stale PAGE      mark a wiki page stale
    contextgit stats           token usage ledger
    contextgit demo            seed demo data to try things out
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from contextgit import __version__
from contextgit.engine import ContextGit, GLOBAL_STORE, STORE_DIRNAME, resolve_store_dir
from contextgit.install import INSTALLERS, snippets


def _print_json(data: Any) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def _engine(args: argparse.Namespace) -> ContextGit:
    return ContextGit(getattr(args, "store", None), budget=getattr(args, "budget", None) or 700)


def cmd_init(args: argparse.Namespace) -> int:
    if args.store:
        target = os.path.abspath(os.path.expanduser(args.store))
    elif getattr(args, "global_store", False):
        target = GLOBAL_STORE
    else:
        target = os.path.join(os.getcwd(), STORE_DIRNAME)
    ContextGit(target)
    print(f"Initialized empty context store in {target}")
    print("Next steps:")
    print("  contextgit demo                  # optional: seed sample data")
    print("  contextgit install claude-code   # or claude-desktop / codex / cursor")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from contextgit.server import run_stdio

    store = args.store or os.environ.get("CONTEXTGIT_DIR") or None
    engine = ContextGit(
        store,
        budget=args.budget,
        compiler_config={"include_full_history": args.full_history},
    )
    run_stdio(engine)
    return 0


def cmd_ui(args: argparse.Namespace) -> int:
    from contextgit.ui import run_ui

    return run_ui(
        _engine(args),
        port=args.port,
        open_browser=not args.no_open,
    )


def cmd_status(args: argparse.Namespace) -> int:
    status = _engine(args).status()
    if args.json:
        _print_json(status)
        return 0
    print(f"store:    {status['store_dir']}")
    print(f"events:   {status['events']}  ({status['full_history_tokens']} tokens of raw history)")
    print(
        f"wiki:     {status['wiki_pages']} pages "
        f"({status['wiki_pages_active']} active, {status['wiki_pages_stale']} stale)"
    )
    print(f"merges:   {status['mutations']} mutations, {status['pending_merges']} pending review")
    print(f"budget:   {status['budget_tokens']} tokens/patch  (counter: {status['token_counter']})")
    usage = status["usage"]
    print(
        f"usage:    {usage['compilations']} compilations, "
        f"{usage['saved_tokens_total']} tokens saved ({usage['savings_pct']}%)"
    )
    return 0


def cmd_log(args: argparse.Namespace) -> int:
    rows = _engine(args).log(limit=args.number)
    if args.json:
        _print_json(rows)
        return 0
    if not rows:
        print("(no events yet — try `contextgit demo` or `contextgit remember \"...\"`)")
        return 0
    for row in rows:
        print(f"{row['ref']}")
        print(f"  {row['timestamp']}  {row['speaker']:<9}  {row['tokens']:>4} tok  {row['summary']}")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    try:
        _print_json(_engine(args).show(args.ref))
        return 0
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def cmd_search(args: argparse.Namespace) -> int:
    results = _engine(args).search(args.query, limit=args.number)
    if args.json:
        _print_json(results)
        return 0
    if not results:
        print("(no matches)")
        return 0
    for row in results:
        print(f"{row['score']:>7.3f}  {row['ref']}")
        print(f"         {row['summary']}")
    return 0


def cmd_branch(args: argparse.Namespace) -> int:
    engine = _engine(args)
    if args.explain:
        explanation = engine.explain(args.prompt, budget=args.budget)
        if args.json:
            _print_json(explanation)
            return 0
        print(f"budget: {explanation['budget']} tokens -> patch: {explanation['estimated_tokens']} tokens\n")
        print("SELECTED:")
        for row in explanation["selected"]:
            comps = row["score_components"]
            print(f"  {row['score']:+.3f}  {row['ref']}  ({row['tokens']} tok)")
            print(
                f"          rel={comps.get('query_relevance', 0):.2f} "
                f"rec={comps.get('recency', 0):.2f} freq={comps.get('frequency', 0):.2f} "
                f"corr={comps.get('correction_priority', 0):.0f} stale={comps.get('stale_noise_penalty', 0):.1f}"
            )
        print(f"\nEXCLUDED ({explanation['excluded_total']}):")
        for row in explanation["excluded"]:
            reasons = ",".join(row["exclusion_reasons"]) or "ranked_below_cut"
            print(f"  {row['score']:+.3f}  {row['ref']}  [{reasons}]")
        return 0
    result = engine.prepare(args.prompt, budget=args.budget, record_usage=not args.dry_run)
    if args.json:
        _print_json(result)
        return 0
    print(result["context"])
    print(
        f"\n-- {result['estimated_tokens']} tokens (budget {result['budget']}) | "
        f"full history would be {result['full_history_tokens']} tokens | "
        f"saved {result['saved_tokens']} ({result['savings_pct']}%)"
    )
    return 0


def cmd_merges(args: argparse.Namespace) -> int:
    data = _engine(args).merges(limit=args.number)
    if args.json:
        _print_json(data)
        return 0
    if not data["mutations"]:
        print("(no merges yet)")
    for row in data["mutations"]:
        claim = (row["claim"] or "")[:90]
        target = f" -> {row['target_page']}" if row["target_page"] else ""
        print(f"{row['ref']}")
        print(f"  {row['timestamp']}  {row['action']:<11} {claim}{target}")
    if data["pending"]:
        print(f"\nPENDING REVIEW ({len(data['pending'])}):")
        for item in data["pending"]:
            print(f"  [{item['type']}] {item['content'][:90]}")
            print(f"      reason: {item['reason']}")
    return 0


def cmd_pending(args: argparse.Namespace) -> int:
    engine = _engine(args)
    if args.action == "list":
        items = engine.merges(limit=0)["pending"]
        if args.json:
            _print_json(items)
        elif not items:
            print("(pending queue is empty)")
        else:
            for item in items:
                print(f"[{item['type']}] {item['content']}")
                print(f"    reason: {item['reason']}")
        return 0
    if not args.content:
        print("error: approve/reject require the pending item's content", file=sys.stderr)
        return 1
    try:
        result = engine.resolve_pending(args.content, args.action)
    except (KeyError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"{result['action']}: {args.content[:90]}  ({result['ref']})")
    return 0


def cmd_remember(args: argparse.Namespace) -> int:
    result = _engine(args).remember(args.fact, page=args.page)
    if result.get("pending_review"):
        print(f"pending review for '{result['target_page']}' ({result['ref']})")
        print(f"  {result['claim']}")
        return 0
    print(f"saved to '{result['target_page']}' ({result['ref']})")
    print(f"  {result['claim']}")
    return 0


def cmd_stale(args: argparse.Namespace) -> int:
    result = _engine(args).mark_stale(args.page, superseded_by=args.superseded_by)
    print(f"marked stale: {args.page}  ({result['ref']})")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    stats = _engine(args).stats()
    if args.json:
        _print_json(stats)
        return 0
    all_time = stats["all_time"]
    week = stats["last_7_days"]
    print(f"store: {stats['store_dir']}  (token counter: {stats['token_counter']})\n")
    print(f"{'':<14}{'compilations':>14}{'patch tokens':>14}{'saved tokens':>14}{'savings':>10}")
    print(
        f"{'all time':<14}{all_time['compilations']:>14}{all_time['patch_tokens_total']:>14}"
        f"{all_time['saved_tokens_total']:>14}{all_time['savings_pct']:>9}%"
    )
    print(
        f"{'last 7 days':<14}{week['compilations']:>14}{week['patch_tokens_total']:>14}"
        f"{week['saved_tokens_total']:>14}{week['savings_pct']:>9}%"
    )
    if all_time["by_day"]:
        print("\nby day:")
        for day, row in all_time["by_day"].items():
            print(f"  {day}  {row['compilations']:>4} compilations  {row['saved_tokens']:>8} tokens saved")
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    from contextgit.demo import seed_demo

    engine = _engine(args)
    result = seed_demo(engine)
    print(f"Seeded {result['turns']} demo turns into {engine.store_dir}")
    print(f"  events added: {result['events_added']}, wiki pages now: {result['wiki_pages']}")
    print("Try:")
    print('  contextgit branch "What database does Atlas use?"')
    print('  contextgit branch "What database does Atlas use?" --explain')
    print("  contextgit merges")
    print("  contextgit stats")
    return 0


def cmd_install(args: argparse.Namespace) -> int:
    if args.client == "print":
        print(snippets(store=args.store, budget=args.budget))
        return 0
    installer = INSTALLERS[args.client]
    kwargs = {"store": args.store, "budget": args.budget}
    if args.client == "codex":
        kwargs["force"] = args.force
    print(installer(**kwargs))
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    engine = _engine(args)
    snapshot = engine.runtime.snapshot().model_dump()
    payload = json.dumps(snapshot, indent=2, ensure_ascii=False)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as f:
            f.write(payload)
        print(f"exported snapshot to {args.out}")
    else:
        print(payload)
    return 0


def _add_common(parser: argparse.ArgumentParser, json_flag: bool = True) -> None:
    parser.add_argument("--store", help="context store path (default: nearest .contextgit/, else global)")
    if json_flag:
        parser.add_argument("--json", action="store_true", help="machine-readable output")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="contextgit",
        description="git for your AI's context — local-first memory over MCP",
    )
    parser.add_argument("--version", action="version", version=f"contextgit {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="create a context store")
    p.add_argument("--global", dest="global_store", action="store_true", help=f"use the global store ({GLOBAL_STORE})")
    p.add_argument("--store", help="explicit store path")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("serve", aliases=["mcp"], help="run the MCP server on stdio")
    p.add_argument("--store", help="context store path")
    p.add_argument("--budget", type=int, default=700, help="token budget per context patch (default 700)")
    p.add_argument("--full-history", action="store_true", help="append full history to every patch (debug)")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("ui", help="open the point-and-click dashboard in your browser")
    p.add_argument("--store", help="context store path (default: nearest .contextgit/, else global)")
    p.add_argument("--port", type=int, default=0, help="port to listen on (default: random free port)")
    p.add_argument("--no-open", action="store_true", help="don't open the browser automatically")
    p.add_argument("--budget", type=int, default=700, help="token budget for context previews (default 700)")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("status", help="store overview + token counters")
    _add_common(p)
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("log", help="recent events, newest first")
    _add_common(p)
    p.add_argument("-n", "--number", type=int, default=20)
    p.set_defaults(func=cmd_log)

    p = sub.add_parser("show", help="show one record by ref")
    _add_common(p, json_flag=False)
    p.add_argument("ref", help="event:<id> | wiki:<title> | mut:<id>")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("search", help="BM25 search over events + wiki")
    _add_common(p)
    p.add_argument("query")
    p.add_argument("-n", "--number", type=int, default=8)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("branch", help="compile the context branch for a prompt")
    _add_common(p)
    p.add_argument("prompt")
    p.add_argument("--budget", type=int, help="token budget (default 700)")
    p.add_argument("--explain", action="store_true", help="show selected/excluded with score components")
    p.add_argument("--dry-run", action="store_true", help="don't record this compilation in the usage ledger")
    p.set_defaults(func=cmd_branch)

    p = sub.add_parser("merges", help="merge history + pending queue")
    _add_common(p)
    p.add_argument("-n", "--number", type=int, default=20)
    p.set_defaults(func=cmd_merges)

    p = sub.add_parser("pending", help="list/approve/reject pending merges")
    _add_common(p)
    p.add_argument("action", choices=["list", "approve", "reject"], nargs="?", default="list")
    p.add_argument("content", nargs="?", help="exact content of the pending item (for approve/reject)")
    p.set_defaults(func=cmd_pending)

    p = sub.add_parser("remember", help="save a durable fact")
    _add_common(p, json_flag=False)
    p.add_argument("fact")
    p.add_argument("--page", help="target wiki page title")
    p.set_defaults(func=cmd_remember)

    p = sub.add_parser("stale", help="mark a wiki page stale")
    _add_common(p, json_flag=False)
    p.add_argument("page")
    p.add_argument("--superseded-by", help="what replaces it")
    p.set_defaults(func=cmd_stale)

    p = sub.add_parser("stats", help="token usage ledger")
    _add_common(p)
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("demo", help="seed demo data")
    _add_common(p, json_flag=False)
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("install", help="wire the MCP server into a client")
    p.add_argument("client", choices=[*sorted(INSTALLERS), "print"])
    p.add_argument("--store", help="pin the server to a specific store path")
    p.add_argument("--budget", type=int, help="token budget per patch")
    p.add_argument("--force", action="store_true", help="replace an existing contextgit block when supported")
    p.set_defaults(func=cmd_install)

    p = sub.add_parser("export", help="dump a full JSON snapshot of the store")
    _add_common(p, json_flag=False)
    p.add_argument("--out", help="output file (default: stdout)")
    p.set_defaults(func=cmd_export)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BrokenPipeError:
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
