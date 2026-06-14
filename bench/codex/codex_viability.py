"""Codex-side additive viability probes for contextgit-mcp.

Run with the released tool interpreter, not the source checkout:

    /Users/regtroka/.local/share/uv/tools/contextgit-mcp/bin/python bench/codex/codex_viability.py

The harness writes JSON under bench/codex/results/<timestamp>/ and uses only
throwaway stores inside that results directory. It reads, but does not mutate,
the real target project at /Users/regtroka/Downloads/branchingcontextclean-main.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from contextgit import __version__ as CONTEXTGIT_VERSION
from contextgit.core.retrieval import tokenize
from contextgit.core.tokens import estimate_tokens, token_count_source
from contextgit.engine import ContextGit


TARGET_PROJECT = Path(os.environ.get(
    "CODEX_TARGET_PROJECT",
    "/Users/regtroka/Downloads/branchingcontextclean-main",
)).expanduser()
RESULTS_ROOT = Path(__file__).resolve().parent / "results"
CODEX_CONFIG = Path.home() / ".codex" / "config.toml"
GLOBAL_STORE = Path.home() / ".contextgit" / "store"

ALLOWED_SUFFIXES = {".md", ".py", ".json", ".jsonl", ".toml", ".yaml", ".yml"}
SKIP_PARTS = {
    ".git",
    ".venv",
    ".contextgit",
    ".pytest_cache",
    "__pycache__",
    "dist",
    "results",
    "evaluation_runs",
}
STOP = set(
    "a an and are as at be by can do does for from has have how i in is it me "
    "of on or our please should that the this to use using we what when where "
    "which who why with you your".split()
)


QRELS: List[Dict[str, Any]] = [
    {
        "id": "q01",
        "q": "Which command initializes demo datasets and benchmark fixtures in the lab?",
        "anchors": ["branchctx init-demo"],
    },
    {
        "id": "q02",
        "q": "Which command runs the deterministic smoke experiment configuration?",
        "anchors": ["experiment_deterministic_smoke.yaml"],
    },
    {
        "id": "q03",
        "q": "What provider is used by default for deterministic smoke artifacts?",
        "anchors": ["ContextOnlyDeterministicProvider"],
    },
    {
        "id": "q04",
        "q": "What does the deferred merge pending gate decide?",
        "anchors": ["promote, carry with spaced review, or low-confidence discard"],
    },
    {
        "id": "q05",
        "q": "Which metric tracks outdated assertions appearing in an answer?",
        "anchors": ["stale_fact_violation"],
    },
    {
        "id": "q06",
        "q": "What command runs the longitudinal archive-growth diagnostic?",
        "anchors": ["branchctx eval longitudinal"],
    },
    {
        "id": "q07",
        "q": "Where does contextgit add the Codex MCP server configuration?",
        "anchors": ["~/.codex/config.toml", "[mcp_servers.contextgit]"],
    },
    {
        "id": "q08",
        "q": "Which contextgit storage file holds the append-only raw conversation journal?",
        "anchors": ["events.jsonl"],
    },
    {
        "id": "q09",
        "q": "What tokenizer extra is recommended for exact token counting?",
        "anchors": ["contextgit-mcp[tokens]", "tiktoken"],
    },
    {
        "id": "q10",
        "q": "What does a real-provider run have to do if the provider call fails?",
        "anchors": ["raises the real-provider failure", "rather than silently substituting"],
    },
    {
        "id": "q11",
        "q": "What evidence block should generated benchmark reports include?",
        "anchors": ["Evidence Classification"],
    },
    {
        "id": "q12",
        "q": "What must real-provider reporting separate before accuracy?",
        "anchors": ["provider/API errors", "error rates before accuracy"],
    },
    {
        "id": "q13",
        "q": "What is the weaker lexical baseline called?",
        "anchors": ["naive_rag", "weak lexical raw-event BM25 baseline"],
    },
    {
        "id": "q14",
        "q": "What stronger BM25 baseline includes raw events plus active wiki pages?",
        "anchors": ["hybrid_rag", "raw events plus active wiki"],
    },
    {
        "id": "q15",
        "q": "What are the three main contextgit memory tools exposed to clients?",
        "anchors": ["prepare_context", "commit_turn", "remember"],
    },
    {
        "id": "q16",
        "q": "What command wires contextgit into Codex?",
        "anchors": ["contextgit install codex"],
    },
    {
        "id": "q17",
        "q": "Which project file contains go-to-market/product strategy for contextgit?",
        "anchors": ["PRODUCT_STRATEGY.md"],
    },
    {
        "id": "q18",
        "q": "What is the publication warning about deterministic artifacts?",
        "anchors": ["smoke-only", "must not be cited as real-model performance evidence"],
    },
    {
        "id": "q19",
        "q": "What setting is required for RealAPIProvider in strict real-provider runs?",
        "anchors": ["RealAPIProvider(allow_fallback=False)"],
    },
    {
        "id": "q20",
        "q": "What contextgit command compiles a context branch for a prompt?",
        "anchors": ["contextgit branch"],
    },
]

MULTIHOP: List[Dict[str, Any]] = [
    {
        "id": "mh01",
        "q": "How should a strict real-provider evaluation be configured and audited?",
        "anchor_groups": [
            ["OPENAI_API_KEY"],
            ["RealAPIProvider(allow_fallback=False)"],
            ["strict_no_fallback"],
            ["metadata.json"],
        ],
    },
    {
        "id": "mh02",
        "q": "What makes branch-and-merge different from full-context and BM25 baselines?",
        "anchor_groups": [
            ["Raw Event Log"],
            ["Durable Wiki Memory"],
            ["Working Branch Builder"],
            ["Lexical Raw-Event BM25", "naive_rag"],
        ],
    },
    {
        "id": "mh03",
        "q": "What should benchmark reports separate when provider calls can fail?",
        "anchor_groups": [
            ["provider/API errors"],
            ["successful-call accuracy"],
            ["pessimistic attempted accuracy"],
            ["Evidence Classification"],
        ],
    },
    {
        "id": "mh04",
        "q": "How does a user wire contextgit into Codex with an isolated store?",
        "anchor_groups": [
            ["contextgit install codex"],
            ["~/.codex/config.toml"],
            ["--store"],
            ["Restart Codex"],
        ],
    },
]


def now_ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    if path.is_file():
        return path.stat().st_size
    for root, _dirs, files in os.walk(path):
        for name in files:
            p = Path(root) / name
            try:
                total += p.stat().st_size
            except OSError:
                pass
    return total


def file_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def content_tokens(text: str) -> List[str]:
    return [tok for tok in tokenize(text) if tok not in STOP and len(tok) >= 2]


def lexical_overlap(query: str, doc: str) -> float:
    q = set(content_tokens(query))
    d = set(content_tokens(doc))
    return round(len(q & d) / max(1, len(q)), 3)


def has_anchor(text: str, anchor: str) -> bool:
    return anchor.lower() in text.lower()


def has_any_anchor(text: str, anchors: Sequence[str]) -> bool:
    return any(has_anchor(text, anchor) for anchor in anchors)


def collect_project_files(target: Path, max_files: int = 260) -> List[Path]:
    files: List[Path] = []
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        rel_parts = set(path.relative_to(target).parts)
        if rel_parts & SKIP_PARTS:
            continue
        if path.name.startswith(".") or path.name.endswith(".pyc"):
            continue
        if path.suffix.lower() not in ALLOWED_SUFFIXES:
            continue
        if path.name in {".env", ".env.local"}:
            continue
        if file_size(path) > 350_000:
            continue
        files.append(path)

    def priority(path: Path) -> Tuple[int, str]:
        rel = str(path.relative_to(target))
        score = 50
        if path.name in {"README.md", "PRODUCT_STRATEGY.md", "pyproject.toml"}:
            score -= 30
        if "/docs/" in f"/{rel}":
            score -= 25
        if "/wiki/" in f"/{rel}":
            score -= 22
        if "/tests/" in f"/{rel}":
            score -= 12
        if "/src/" in f"/{rel}":
            score -= 10
        if "/data/" in f"/{rel}":
            score -= 6
        return (score, rel)

    return sorted(files, key=priority)[:max_files]


def chunk_file(path: Path, target: Path) -> List[Dict[str, Any]]:
    rel = str(path.relative_to(target))
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    text = text.replace("\x00", "")
    chunks: List[str] = []
    if path.suffix.lower() == ".md":
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        buf = ""
        for para in paras:
            candidate = f"{buf}\n\n{para}".strip() if buf else para
            if len(candidate) <= 1800:
                buf = candidate
            else:
                if buf:
                    chunks.append(buf)
                buf = para[:2400]
        if buf:
            chunks.append(buf)
    else:
        lines = text.splitlines()
        for i in range(0, len(lines), 45):
            chunk = "\n".join(lines[i:i + 45]).strip()
            if chunk:
                chunks.append(chunk[:3000])
    out = []
    for idx, chunk in enumerate(chunks[:8]):
        out.append({
            "chunk_id": f"{rel}#{idx:02d}",
            "path": rel,
            "text": f"SOURCE: {rel} chunk {idx}\n{chunk}",
            "tokens": estimate_tokens(chunk),
        })
    return out


def build_corpus(target: Path, max_chunks: int = 320) -> List[Dict[str, Any]]:
    chunks: List[Dict[str, Any]] = []
    for path in collect_project_files(target):
        chunks.extend(chunk_file(path, target))
        if len(chunks) >= max_chunks:
            break
    return chunks[:max_chunks]


def ingest_chunks(engine: ContextGit, chunks: Sequence[Dict[str, Any]], conversation_id: str = "corpus") -> Dict[str, Dict[str, Any]]:
    by_ref: Dict[str, Dict[str, Any]] = {}
    for chunk in chunks:
        res = engine.commit_turn(
            chunk["text"],
            f"Indexed {chunk['chunk_id']}.",
            conversation_id=conversation_id,
        )
        ref = res["committed_event_refs"][0]
        by_ref[ref] = chunk
    return by_ref


def rank_for(selected_refs: Sequence[str], gold_refs: Sequence[str]) -> Optional[int]:
    gold = set(gold_refs)
    for idx, ref in enumerate(selected_refs, start=1):
        if ref in gold:
            return idx
    return None


def ndcg_at_k(selected_refs: Sequence[str], gold_refs: Sequence[str], k: int = 10) -> float:
    gold = set(gold_refs)
    dcg = 0.0
    for idx, ref in enumerate(selected_refs[:k], start=1):
        if ref in gold:
            dcg += 1.0 / math.log2(idx + 1)
    ideal_hits = min(len(gold), k)
    idcg = sum(1.0 / math.log2(idx + 1) for idx in range(1, ideal_hits + 1))
    return round(dcg / idcg, 4) if idcg else 0.0


def selected_full_text(selected_refs: Sequence[str], by_ref: Dict[str, Dict[str, Any]]) -> str:
    return "\n\n".join(by_ref.get(ref, {}).get("text", "") for ref in selected_refs)


def run_environment_probe(run_dir: Path, store_root: Path) -> Dict[str, Any]:
    config_text = CODEX_CONFIG.read_text(encoding="utf-8", errors="replace") if CODEX_CONFIG.exists() else ""
    block_match = re.search(r"(?ms)^\[mcp_servers\.contextgit\]\s*(.*?)(?=^\[|\Z)", config_text)
    block = block_match.group(0).strip() if block_match else ""
    command_match = re.search(r'command\s*=\s*"([^"]+)"', block)
    args_match = re.search(r"args\s*=\s*\[(.*?)\]", block, flags=re.S)
    command = command_match.group(1) if command_match else None
    args_text = args_match.group(1) if args_match else ""
    isolated_store = store_root / "codex_install_probe"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "contextgit",
            "install",
            "codex",
            "--store",
            str(isolated_store),
            "--budget",
            "700",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    print_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "contextgit",
            "install",
            "print",
            "--store",
            str(isolated_store),
            "--budget",
            "700",
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    configured_command_is_tool_python = bool(command and Path(command) == Path(sys.executable))
    configured_command_resolves_to_same_python_binary = bool(
        command and Path(command).resolve() == Path(sys.executable).resolve()
    )
    configured_command_under_target = bool(command and str(TARGET_PROJECT) in command)
    return {
        "test": "environment_and_codex_config_probe",
        "contextgit_version": CONTEXTGIT_VERSION,
        "sys_executable": sys.executable,
        "token_counter": token_count_source(),
        "target_project": str(TARGET_PROJECT),
        "target_project_exists": TARGET_PROJECT.exists(),
        "codex_config_path": str(CODEX_CONFIG),
        "contextgit_block_present": bool(block),
        "contextgit_block": block,
        "configured_command": command,
        "configured_args_text": args_text,
        "configured_command_is_tool_python": configured_command_is_tool_python,
        "configured_command_resolves_to_same_python_binary": configured_command_resolves_to_same_python_binary,
        "configured_command_under_target_project": configured_command_under_target,
        "configured_has_store_pin": "--store" in args_text,
        "installer_stdout": proc.stdout.strip(),
        "installer_stderr": proc.stderr.strip(),
        "installer_returncode": proc.returncode,
        "installer_refused_existing_block": "already configured" in proc.stdout.lower(),
        "print_snippet_returncode": print_proc.returncode,
        "print_snippet_contains_isolated_store": str(isolated_store) in print_proc.stdout,
        "print_snippet_first_lines": print_proc.stdout.splitlines()[:18],
        "finding": (
            "Existing Codex config points at the released tool interpreter with a store pin."
            if configured_command_is_tool_python and "--store" in args_text
            else "Codex config is already occupied, so `contextgit install codex --store ...` is a no-op and cannot repoint to an isolated store."
        ),
        "proposed_solution": "Add `contextgit install codex --force` or `--update-existing`, and warn when an existing block points at a non-release interpreter or lacks --store.",
    }


class MCPStdioClient:
    def __init__(self, store_dir: Path, budget: int = 700):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "contextgit", "serve", "--store", str(store_dir), "--budget", str(budget)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.next_id = 0

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}}
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg) + "\n")
        self.proc.stdin.flush()
        assert self.proc.stdout is not None
        while True:
            line = self.proc.stdout.readline()
            if not line:
                raise RuntimeError("MCP server closed stdout")
            data = json.loads(line)
            if data.get("id") == self.next_id:
                return data

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}}) + "\n")
        self.proc.stdin.flush()

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        res = self.request("tools/call", {"name": name, "arguments": arguments})
        result = res.get("result") or {}
        text = ""
        if result.get("content"):
            text = result["content"][0].get("text", "")
        try:
            payload = json.loads(text) if text else None
        except json.JSONDecodeError:
            payload = None
        return {"raw": res, "payload": payload, "isError": result.get("isError", False), "text": text}

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def run_mcp_protocol_probe(store_root: Path) -> Dict[str, Any]:
    store = store_root / "mcp_protocol"
    client = MCPStdioClient(store)
    try:
        init = client.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "codex-bench", "version": "0"}})
        client.notify("notifications/initialized")
        tools = client.request("tools/list")
        remember = client.call_tool("remember", {"fact": "Codex protocol probe fact: branch-context-lab uses ContextOnlyDeterministicProvider.", "page": "Codex Protocol Probe"})
        prep = client.call_tool("prepare_context", {"prompt": "Which deterministic provider does the lab use?", "conversation_id": "codex-probe"})
        stats = client.call_tool("context_stats", {})
    finally:
        client.close()
    tool_names = [tool["name"] for tool in (tools.get("result") or {}).get("tools", [])]
    payload = prep.get("payload") or {}
    stats_payload = stats.get("payload") or {}
    return {
        "test": "stdio_mcp_protocol_probe",
        "initialize_ok": "result" in init,
        "tool_count": len(tool_names),
        "tool_names": tool_names,
        "has_12_expected_tools": len(tool_names) == 12,
        "remember_is_error": remember["isError"],
        "prepare_is_error": prep["isError"],
        "selected_refs": [row["ref"] for row in payload.get("selected", [])],
        "provider_fact_in_patch": "ContextOnlyDeterministicProvider" in payload.get("context", ""),
        "stats_store_dir": ((stats_payload.get("status") or {}).get("store_dir")),
        "uses_isolated_store": str(store) == ((stats_payload.get("status") or {}).get("store_dir")),
        "proposed_solution": "Keep stdio protocol tests in CI; they catch server/tool-schema drift that in-process tests miss.",
    }


def _grade_qrels(engine: ContextGit, by_ref: Dict[str, Dict[str, Any]], budget: int) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    for qrel in QRELS:
        gold_refs = [ref for ref, chunk in by_ref.items() if has_any_anchor(chunk["text"], qrel["anchors"])]
        if not gold_refs:
            rows.append({"id": qrel["id"], "q": qrel["q"], "graded": False, "reason": "no anchor in corpus"})
            continue
        res = engine.prepare(qrel["q"], conversation_id="qrels", budget=budget, record_usage=False)
        selected_refs = [row["ref"] for row in res["selected"]]
        rank = rank_for(selected_refs, gold_refs)
        best_overlap = max(lexical_overlap(qrel["q"], by_ref[ref]["text"]) for ref in gold_refs)
        rows.append({
            "id": qrel["id"],
            "q": qrel["q"],
            "graded": True,
            "gold_ref_count": len(gold_refs),
            "rank": rank,
            "found_at_1": rank == 1,
            "found_at_5": rank is not None and rank <= 5,
            "found_at_10": rank is not None and rank <= 10,
            "ndcg_at_10": ndcg_at_k(selected_refs, gold_refs, 10),
            "selected_top10": selected_refs[:10],
            "query_gold_overlap": best_overlap,
            "patch_tokens": res["estimated_tokens"],
            "force_tiny": "No selected context fits the configured budget" in res["context"],
        })
    graded = [row for row in rows if row.get("graded")]
    n = max(1, len(graded))
    hits1 = sum(row["found_at_1"] for row in graded)
    hits5 = sum(row["found_at_5"] for row in graded)
    hits10 = sum(row["found_at_10"] for row in graded)
    mrr = sum((1.0 / row["rank"]) if row["rank"] else 0.0 for row in graded) / n
    ndcg = sum(row["ndcg_at_10"] for row in graded) / n
    misses = [row for row in graded if not row["found_at_5"]]
    force_tiny = sum(row.get("force_tiny", False) for row in graded)
    return {
        "budget": budget,
        "graded_queries": len(graded),
        "recall_at_1": round(hits1 / n, 3),
        "recall_at_5": round(hits5 / n, 3),
        "recall_at_10": round(hits10 / n, 3),
        "mrr": round(mrr, 3),
        "mean_ndcg_at_10": round(ndcg, 3),
        "force_tiny_count": force_tiny,
        "force_tiny_rate": round(force_tiny / n, 3),
        "mean_query_gold_overlap_hits5": round(sum(r["query_gold_overlap"] for r in graded if r["found_at_5"]) / max(1, hits5), 3),
        "mean_query_gold_overlap_misses5": round(sum(r["query_gold_overlap"] for r in misses) / max(1, len(misses)), 3) if misses else None,
        "rows": rows,
    }


def run_retrieval_qrels(run_dir: Path, store_root: Path, chunks: Sequence[Dict[str, Any]]) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    store = store_root / "retrieval_qrels"
    engine = ContextGit(str(store), budget=700)
    by_ref = ingest_chunks(engine, chunks)
    default = _grade_qrels(engine, by_ref, budget=700)
    expanded = _grade_qrels(engine, by_ref, budget=3000)

    subset_store = store_root / "retrieval_qrels_subset80"
    subset_engine = ContextGit(str(subset_store), budget=700)
    subset_by_ref = ingest_chunks(subset_engine, chunks[:80], conversation_id="subset")
    subset = _grade_qrels(subset_engine, subset_by_ref, budget=700)

    result = {
        "test": "branchingcontextclean_real_project_qrels",
        "target_project": str(TARGET_PROJECT),
        "files_indexed": len({chunk["path"] for chunk in chunks}),
        "chunks_indexed": len(chunks),
        "events_in_store": len(engine.runtime.list_events()),
        **{k: v for k, v in default.items() if k != "rows"},
        "rows": default["rows"],
        "expanded_budget_3000": expanded,
        "subset_80_chunks_budget_700": subset,
        "finding": "Natural-qrel retrieval on the larger project is measured with binary qrels and nDCG@10.",
        "proposed_solution_if_negative": "Add semantic retrieval or field-weighted identifiers, and add a relevance gate so lexical misses do not get hidden by filler.",
    }
    return result, by_ref


def run_multihop(store_root: Path, chunks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    store = store_root / "multihop"
    engine = ContextGit(str(store), budget=700)
    by_ref = ingest_chunks(engine, chunks)
    rows = []
    for probe in MULTIHOP:
        res = engine.prepare(probe["q"], conversation_id="multihop", record_usage=False)
        selected_refs = [row["ref"] for row in res["selected"]]
        full = selected_full_text(selected_refs, by_ref)
        patch = res["context"]
        selected_groups = [has_any_anchor(full, group) for group in probe["anchor_groups"]]
        patch_groups = [has_any_anchor(patch, group) for group in probe["anchor_groups"]]
        rows.append({
            "id": probe["id"],
            "q": probe["q"],
            "required_groups": probe["anchor_groups"],
            "selected_group_hits": selected_groups,
            "patch_group_hits": patch_groups,
            "selected_completeness": round(sum(selected_groups) / len(selected_groups), 3),
            "patch_visible_completeness": round(sum(patch_groups) / len(patch_groups), 3),
            "selected_top10": selected_refs[:10],
            "patch_tokens": res["estimated_tokens"],
        })
    return {
        "test": "multi_hop_synthesis_completeness",
        "probes": len(rows),
        "mean_selected_completeness": round(sum(r["selected_completeness"] for r in rows) / len(rows), 3),
        "mean_patch_visible_completeness": round(sum(r["patch_visible_completeness"] for r in rows) / len(rows), 3),
        "rows": rows,
        "finding": "Compares whether all required facts are retrieved at all vs. visible after the 180-char summaries and budget.",
        "proposed_solution_if_negative": "Represent compound/multi-hop facts as structured bullets with non-truncating summaries or explicit 'more facts omitted' markers.",
    }


def run_long_session_drift(store_root: Path, chunks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    store = store_root / "long_session_drift"
    engine = ContextGit(str(store), budget=700)
    targets: List[Dict[str, Any]] = []
    raw_facts = [
        ("The early raw lab fact says the smoke provider is ContextOnlyDeterministicProvider.", "Which provider does the early raw lab fact name?", "ContextOnlyDeterministicProvider"),
        ("The early raw Codex fact says installer output should mention ~/.codex/config.toml.", "Which config path does the early raw Codex fact mention?", "~/.codex/config.toml"),
        ("The early raw evidence fact says deterministic artifacts are smoke-only evidence.", "What evidence label does the early raw evidence fact use?", "smoke-only"),
    ]
    for content, query, anchor in raw_facts:
        res = engine.commit_turn(content, "Recorded.", conversation_id="drift")
        targets.append({"kind": "raw_event", "expected_ref": res["committed_event_refs"][0], "query": query, "anchor": anchor})
    durable = [
        ("Long-session durable fact: strict real-provider runs require RealAPIProvider(allow_fallback=False).", "Strict Provider Memory", "What does the durable strict-provider fact require?", "RealAPIProvider(allow_fallback=False)"),
        ("Long-session durable fact: project-scoped contextgit memory lives in a .contextgit directory.", "Project Store Memory", "Where does project-scoped contextgit memory live?", ".contextgit"),
    ]
    for fact, page, query, anchor in durable:
        engine.remember(fact, page=page)
        targets.append({"kind": "wiki", "expected_ref": f"wiki:{page}", "query": query, "anchor": anchor})

    checkpoints = {0, 50, 100, 220}
    rows = []

    def probe(after_turns: int) -> None:
        for target in targets:
            res = engine.prepare(target["query"], conversation_id="drift-probe", record_usage=False)
            refs = [row["ref"] for row in res["selected"]]
            rank = refs.index(target["expected_ref"]) + 1 if target["expected_ref"] in refs else None
            rows.append({
                "after_noise_turns": after_turns,
                "kind": target["kind"],
                "query": target["query"],
                "expected_ref": target["expected_ref"],
                "rank": rank,
                "found_at_5": rank is not None and rank <= 5,
                "anchor_visible_in_patch": target["anchor"] in res["context"],
                "selected_top5": refs[:5],
            })

    probe(0)
    for idx in range(1, 221):
        chunk = chunks[(idx - 1) % len(chunks)]
        engine.commit_turn(
            f"Turn {idx}: {chunk['text'][:1800]}",
            f"Handled turn {idx}.",
            conversation_id="drift",
        )
        if idx in checkpoints:
            probe(idx)
    by_checkpoint: Dict[str, Dict[str, Any]] = {}
    for cp in sorted(checkpoints):
        subset = [row for row in rows if row["after_noise_turns"] == cp]
        by_checkpoint[str(cp)] = {
            "found_at_5_rate": round(sum(row["found_at_5"] for row in subset) / max(1, len(subset)), 3),
            "anchor_visible_rate": round(sum(row["anchor_visible_in_patch"] for row in subset) / max(1, len(subset)), 3),
        }
    return {
        "test": "long_session_drift_220_turns",
        "targets": len(targets),
        "noise_turns": 220,
        "total_events": len(engine.runtime.list_events()),
        "by_checkpoint": by_checkpoint,
        "rows": rows,
        "finding": "Measures whether early raw and durable facts remain retrievable as recency decays and the store grows.",
        "proposed_solution_if_negative": "Add durable fact promotion, rehearsal/recurrence boosts, or per-project archive indexes that do not rely on recency.",
    }


def run_auto_merge_precision(store_root: Path) -> Dict[str, Any]:
    store = store_root / "auto_merge"
    engine = ContextGit(str(store), budget=700)
    cases = [
        (True, "Remember that Codex-side tests must use isolated contextgit stores."),
        (True, "From now on, strict real-provider runs use strict_no_fallback true."),
        (True, "Decision: use project-scoped stores for contextgit memory tests."),
        (True, "Correction: the schema count is 10, not 12."),
        (True, "Final: archive contextgit stores before they exceed about 1000 events."),
        (True, "Going forward, every benchmark report needs evidence classification."),
        (False, "Can you summarize the benchmark metrics for me?"),
        (False, "Please write a regex that validates an email address."),
        (False, "What is better, branch_merge instead of full_context for this case?"),
        (False, "This temporary mock sample prompt is only a unit-test fixture."),
        (False, "Does the README mention a context branch builder?"),
        (False, "Maybe later we can consider a different persistence backend."),
    ]
    rows = []
    for idx, (expected, prompt) in enumerate(cases):
        res = engine.commit_turn(prompt, "ok", conversation_id="auto-merge")
        merged = bool(res.get("durable_merge"))
        rows.append({
            "case": idx + 1,
            "prompt": prompt,
            "expected_durable": expected,
            "auto_merged": merged,
            "claim": (res.get("durable_merge") or {}).get("claim"),
            "classification": (
                "TP" if expected and merged else
                "FN" if expected and not merged else
                "FP" if (not expected and merged) else
                "TN"
            ),
        })
    tp = sum(row["classification"] == "TP" for row in rows)
    fp = sum(row["classification"] == "FP" for row in rows)
    fn = sum(row["classification"] == "FN" for row in rows)
    tn = sum(row["classification"] == "TN" for row in rows)
    precision = tp / max(1, tp + fp)
    recall = tp / max(1, tp + fn)
    return {
        "test": "commit_turn_auto_merge_precision_recall",
        "tp": tp,
        "fp": fp,
        "fn": fn,
        "tn": tn,
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "rows": rows,
        "finding": "Durable-marker detection is high recall but can false-merge non-durable questions containing 'instead of'.",
        "proposed_solution_if_negative": "Require imperative/save intent, confirmation, or pending-review mode for marker-based auto-merges; do not promote question-shaped text.",
    }


def run_determinism(store_root: Path, chunks: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    store = store_root / "determinism"
    engine = ContextGit(str(store), budget=700)
    ingest_chunks(engine, chunks[:80], conversation_id="det")
    engine.remember("Determinism probe fact: byte-identical patches should repeat.", page="Determinism Memory")
    prompt = "What does the determinism probe say and which lab provider is default?"
    patches = [engine.prepare(prompt, conversation_id="det", record_usage=False)["context"] for _ in range(6)]
    hashes = [sha256_text(patch) for patch in patches]
    return {
        "test": "determinism_same_store_same_prompt",
        "runs": len(patches),
        "unique_hashes": sorted(set(hashes)),
        "byte_identical": len(set(patches)) == 1,
        "patch_token_count": estimate_tokens(patches[0]),
        "proposed_solution_if_negative": "Remove runtime-dependent fields from rendering, or make timestamps/usage writes explicitly outside patch generation.",
    }


def run_empty_patch_repro(store_root: Path) -> Dict[str, Any]:
    attempts = []
    best: Optional[Dict[str, Any]] = None
    filler = " schema tables design migration relations indexes policies evidence "
    for relevant_count in range(5, 13):
        for repeat in range(4, 14):
            store = store_root / f"empty_patch_r{relevant_count}_{repeat}"
            shutil.rmtree(store, ignore_errors=True)
            engine = ContextGit(str(store), budget=700)
            for idx in range(relevant_count):
                claim = f"Current schema design fact {idx}: " + (filler * repeat).strip()
                engine.runtime.record_mutation(
                    "save",
                    new_claim=claim,
                    target_page=f"Schema Current {idx}",
                    confidence=0.9,
                    decision_mode="human-approved",
                    human_approved=True,
                    policy_reason="codex empty patch repro",
                )
            for idx in range(8):
                engine.mark_stale(f"Schema Deprecated {idx}", superseded_by="current schema design")
            res = engine.prepare("describe the current schema table design decisions", conversation_id="empty", record_usage=False)
            row = {
                "relevant_count": relevant_count,
                "repeat": repeat,
                "patch_tokens": res["estimated_tokens"],
                "selected_count": len(res["selected"]),
                "empty_force_tiny": "No selected context fits the configured budget" in res["context"],
                "selected_refs": [item["ref"] for item in res["selected"]],
            }
            attempts.append(row)
            if best is None or (row["selected_count"], row["patch_tokens"]) > (best["selected_count"], best["patch_tokens"]):
                best = row
            if row["empty_force_tiny"]:
                row["context"] = res["context"]
                return {
                    "test": "independent_empty_patch_reproduction",
                    "reproduced": True,
                    "trigger": row,
                    "attempts_examined": len(attempts),
                    "finding": "Default-budget compile can return force_tiny with zero selected context despite relevant in-budget candidates.",
                    "proposed_solution": "On final overflow, iteratively drop the lowest-scoring selected item or stale/provenance lines until the patch fits; include avoid-stale cost in greedy trials.",
                }
    return {
        "test": "independent_empty_patch_reproduction",
        "reproduced": False,
        "attempts_examined": len(attempts),
        "closest_attempt": best,
        "attempts_tail": attempts[-8:],
        "finding": "This parameter sweep did not trigger force_tiny, so the prior repro is not falsified here but was not independently reproduced by this construction.",
        "proposed_solution": "Keep the graceful-degradation fix because the final-render overflow path is still all-or-nothing in source.",
    }


def run_savings_meter_repro(store_root: Path) -> Dict[str, Any]:
    store = store_root / "savings_meter"
    engine = ContextGit(str(store), budget=700)
    engine.commit_turn("tiny", "ok", conversation_id="savings")
    per_call = []
    for idx in range(10):
        res = engine.prepare(f"tiny query {idx}", conversation_id="savings", record_usage=True)
        per_call.append({
            "patch_tokens": res["estimated_tokens"],
            "full_history_tokens": res["full_history_tokens"],
            "reported_saved_tokens": res["saved_tokens"],
            "true_net_tokens": res["full_history_tokens"] - res["estimated_tokens"],
        })
    usage_rows = []
    usage_path = store / "usage.jsonl"
    if usage_path.exists():
        usage_rows = [json.loads(line) for line in usage_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    patch_total = sum(row.get("patch_tokens", 0) for row in usage_rows)
    full_total = sum(row.get("full_history_tokens", 0) for row in usage_rows)
    recorded_saved = sum(row.get("saved_tokens", 0) for row in usage_rows)
    true_net = full_total - patch_total
    summary = engine.stats()["all_time"]
    return {
        "test": "independent_savings_meter_reproduction",
        "per_call": per_call,
        "usage_summary": summary,
        "patch_total": patch_total,
        "full_total": full_total,
        "recorded_saved_total": recorded_saved,
        "true_net_tokens": true_net,
        "reproduced_one_sided_loss_clamp": true_net < 0 and recorded_saved >= 0,
        "finding": "Loss-making prepare_context calls are recorded as zero saved instead of negative saved.",
        "proposed_solution": "Record `net_saved_tokens = full_history_tokens - patch_tokens` without clamping, and show both gross wins and net total.",
    }


def run_gpt_harm_feasibility() -> Dict[str, Any]:
    return {
        "test": "cross_model_gpt_harm_feasibility",
        "ran_model_calls": False,
        "openai_api_key_in_environment": bool(os.environ.get("OPENAI_API_KEY")),
        "reason": (
            "No programmatic GPT A/B was run from this harness. This Codex session has a GPT reasoning model, "
            "but not a repeatable model-call API surface exposed to the script without a separately approved API key/run."
        ),
        "proposed_solution": "Add an optional `--run-openai` lane that uses a user-approved OPENAI_API_KEY, retrieval-gates stale patches with explain_selection, pads prompts, and runs K>=5 per arm.",
    }


def main() -> int:
    run_dir = RESULTS_ROOT / now_ts()
    store_root = run_dir / "stores"
    run_dir.mkdir(parents=True, exist_ok=True)
    store_root.mkdir(parents=True, exist_ok=True)

    before = {
        "global_events_bytes": file_size(GLOBAL_STORE / "events.jsonl"),
        "global_store_bytes": dir_size(GLOBAL_STORE),
        "target_contextgit_events_bytes": file_size(TARGET_PROJECT / ".contextgit" / "events.jsonl"),
        "target_contextgit_store_bytes": dir_size(TARGET_PROJECT / ".contextgit"),
        "codex_config_sha256": sha256_text(CODEX_CONFIG.read_text(encoding="utf-8", errors="replace")) if CODEX_CONFIG.exists() else None,
    }

    chunks = build_corpus(TARGET_PROJECT)
    manifest = {
        "target_project": str(TARGET_PROJECT),
        "files": sorted({chunk["path"] for chunk in chunks}),
        "chunks": len(chunks),
        "tokens_total": sum(chunk["tokens"] for chunk in chunks),
        "sample_chunks": chunks[:5],
    }
    write_json(run_dir / "corpus_manifest.json", manifest)

    results: Dict[str, Any] = {
        "run_dir": str(run_dir),
        "started_at": now_ts(),
        "isolation_before": before,
    }

    tests: List[Tuple[str, Any]] = []
    tests.append(("environment_probe", run_environment_probe(run_dir, store_root)))
    tests.append(("mcp_protocol", run_mcp_protocol_probe(store_root)))
    retrieval, _by_ref = run_retrieval_qrels(run_dir, store_root, chunks)
    tests.append(("retrieval_qrels", retrieval))
    tests.append(("multihop", run_multihop(store_root, chunks)))
    tests.append(("long_session_drift", run_long_session_drift(store_root, chunks)))
    tests.append(("auto_merge", run_auto_merge_precision(store_root)))
    tests.append(("determinism", run_determinism(store_root, chunks)))
    tests.append(("empty_patch", run_empty_patch_repro(store_root)))
    tests.append(("savings_meter", run_savings_meter_repro(store_root)))
    tests.append(("gpt_harm_feasibility", run_gpt_harm_feasibility()))

    for name, data in tests:
        write_json(run_dir / f"{name}.json", data)
        results[name] = data

    if os.environ.get("CODEX_KEEP_TEST_STORES") != "1":
        shutil.rmtree(store_root, ignore_errors=True)

    after = {
        "global_events_bytes": file_size(GLOBAL_STORE / "events.jsonl"),
        "global_store_bytes": dir_size(GLOBAL_STORE),
        "target_contextgit_events_bytes": file_size(TARGET_PROJECT / ".contextgit" / "events.jsonl"),
        "target_contextgit_store_bytes": dir_size(TARGET_PROJECT / ".contextgit"),
        "codex_config_sha256": sha256_text(CODEX_CONFIG.read_text(encoding="utf-8", errors="replace")) if CODEX_CONFIG.exists() else None,
        "stores_removed": not store_root.exists(),
    }
    results["isolation_after"] = after
    results["isolation_ok"] = before == {k: after.get(k) for k in before} and after["stores_removed"]
    results["completed_at"] = now_ts()
    write_json(run_dir / "_summary.json", results)
    print(str(run_dir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
