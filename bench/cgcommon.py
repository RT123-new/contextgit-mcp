"""Shared harness utilities for the contextgit-mcp viability benchmark.

Run every bench script with the *installed* package's interpreter so we test
what users actually get, not the source tree:

    VENV_PY = /Users/regtroka/.local/share/uv/tools/contextgit-mcp/bin/python

Nothing here touches Reg's real store (~/.contextgit/store): every store is a
throwaway temp dir passed explicitly.
"""
from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

# --- where the installed tool lives -----------------------------------------
VENV_PY = "/Users/regtroka/.local/share/uv/tools/contextgit-mcp/bin/python"
CONTEXTGIT_BIN = "/Users/regtroka/.local/bin/contextgit"
RESULTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")

# Import the installed engine (only works when run under VENV_PY).
try:
    from contextgit.engine import ContextGit
    from contextgit.core.tokens import estimate_tokens, token_count_source
except Exception as exc:  # pragma: no cover
    print(
        "ERROR: import contextgit failed. Run this with the tool venv python:\n"
        f"  {VENV_PY} {sys.argv[0] if sys.argv else ''}\n"
        f"  (underlying: {exc})",
        file=sys.stderr,
    )
    raise


def new_engine(store_dir: str, budget: int = 700, **cfg) -> "ContextGit":
    os.makedirs(store_dir, exist_ok=True)
    return ContextGit(store_dir=store_dir, budget=budget, compiler_config=cfg or None)


def toks(text: str) -> int:
    return estimate_tokens(text)


# ---------------------------------------------------------------------------
# Deterministic transcript / fixture generation
# ---------------------------------------------------------------------------

# Distinct topic threads. Each has a rare "entity" token (high BM25 idf) plus
# filler vocab. Rare entities make grading unambiguous.
TOPICS = {
    "atlas":     {"entity": "Atlas",     "vocab": "database schema migration tenant rate limit endpoint deploy pipeline"},
    "orion":     {"entity": "Orion",     "vocab": "billing invoice subscription proration webhook retry currency tax"},
    "vega":      {"entity": "Vega",      "vocab": "frontend component render bundle cache route latency hydration"},
    "lyra":      {"entity": "Lyra",      "vocab": "auth token session oauth scope refresh cookie csrf"},
    "draco":     {"entity": "Draco",     "vocab": "infra cluster node kubernetes scaling pod ingress secret"},
    "cygnus":    {"entity": "Cygnus",    "vocab": "analytics event funnel cohort dashboard query warehouse partition"},
}
TOPIC_KEYS = list(TOPICS.keys())

FILLER = ("the team discussed how the system should handle this case and what "
          "the right approach would be given the constraints we have in place")


def _sentence(rng: random.Random, topic: str, n_words: int = 26) -> str:
    t = TOPICS[topic]
    words = t["vocab"].split()
    out = [t["entity"]]
    fill = FILLER.split()
    for _ in range(n_words):
        if rng.random() < 0.45:
            out.append(rng.choice(words))
        else:
            out.append(rng.choice(fill))
    return " ".join(out)


def gen_turns(target_tokens: int, mix: str, seed: int = 1234) -> List[Tuple[str, str]]:
    """Generate (user_prompt, assistant_answer) turns until ~target_tokens of
    event content (user+assistant) is reached. `mix` in {single, multi, shift}.
    Deterministic for a given (target_tokens, mix, seed). Non-durable phrasing
    so commit_turn stays O(1) per turn."""
    rng = random.Random(seed)
    turns: List[Tuple[str, str]] = []
    total = 0
    i = 0
    while total < target_tokens:
        if mix == "single":
            topic = TOPIC_KEYS[0]
        elif mix == "multi":
            topic = TOPIC_KEYS[i % 5]  # 5 interleaved threads
        elif mix == "shift":
            topic = TOPIC_KEYS[0] if total < target_tokens / 2 else TOPIC_KEYS[1]
        else:
            raise ValueError(mix)
        u = f"Question {i} about {_sentence(rng, topic, 18)}"
        a = f"Answer {i}: {_sentence(rng, topic, 34)}"
        turns.append((u, a))
        total += toks(u) + toks(a)
        i += 1
    return turns


def build_store(store_dir: str, turns: List[Tuple[str, str]], budget: int = 700,
                conversation_id: str = "default") -> "ContextGit":
    eng = new_engine(store_dir, budget=budget)
    for u, a in turns:
        eng.commit_turn(u, a, conversation_id=conversation_id)
    return eng


# ---------------------------------------------------------------------------
# Minimal stdio MCP client (spawns the real `contextgit serve`)
# ---------------------------------------------------------------------------

class MCPClient:
    """Speaks JSON-RPC line protocol to `contextgit serve` over stdio."""

    def __init__(self, store_dir: str, budget: Optional[int] = None, extra_env: Optional[Dict[str, str]] = None):
        env = dict(os.environ)
        env["CONTEXTGIT_DIR"] = store_dir
        if extra_env:
            env.update(extra_env)
        args = [CONTEXTGIT_BIN, "serve"]
        if budget:
            args += ["--budget", str(budget)]
        self.proc = subprocess.Popen(
            args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, bufsize=1, env=env,
        )
        self._id = 0

    def _send(self, obj: Dict[str, Any]) -> None:
        self.proc.stdin.write(json.dumps(obj) + "\n")
        self.proc.stdin.flush()

    def _recv(self) -> Dict[str, Any]:
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError("server closed stdout")
        return json.loads(line)

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        self._id += 1
        mid = self._id
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        # skip any notifications/out-of-order; match id
        while True:
            msg = self._recv()
            if msg.get("id") == mid:
                return msg

    def send_raw(self, raw_line: str) -> Optional[Dict[str, Any]]:
        """Send a raw (possibly malformed) line; return next response line or None."""
        self.proc.stdin.write(raw_line + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        return json.loads(line) if line.strip() else None

    def notify(self, method: str, params: Optional[Dict[str, Any]] = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def initialize(self) -> Dict[str, Any]:
        r = self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                         "clientInfo": {"name": "bench", "version": "0"}})
        self.notify("notifications/initialized")
        return r

    def call_tool(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        r = self.request("tools/call", {"name": name, "arguments": arguments})
        result = r.get("result") or {}
        if "content" in result and result["content"]:
            txt = result["content"][0].get("text", "")
            try:
                return {"isError": result.get("isError", False), "payload": json.loads(txt), "raw": txt}
            except json.JSONDecodeError:
                return {"isError": result.get("isError", False), "payload": None, "raw": txt}
        return {"isError": result.get("isError", False), "payload": None, "raw": r}

    def close(self):
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Results IO
# ---------------------------------------------------------------------------

def ts() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def save_result(name: str, data: Dict[str, Any], run_dir: str) -> str:
    os.makedirs(run_dir, exist_ok=True)
    path = os.path.join(run_dir, f"{name}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    return path


def pct(n: float) -> str:
    return f"{n:.1f}%"
