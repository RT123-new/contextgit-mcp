"""contextgit MCP server.

A dependency-free implementation of the Model Context Protocol (JSON-RPC 2.0
over newline-delimited stdio), exposing the contextgit engine as tools. This
is the transport Claude Desktop, Claude Code, Codex, and Cursor speak, so a
configured `contextgit serve` works in all of them without an SDK.

stdout carries protocol messages only; diagnostics go to stderr.
"""

from __future__ import annotations

import json
import sys
import traceback
from typing import Any, Dict, Optional

from contextgit import __version__
from contextgit.engine import ContextGit

SUPPORTED_PROTOCOL_VERSIONS = ("2025-06-18", "2025-03-26", "2024-11-05")

SERVER_INSTRUCTIONS = (
    "contextgit is the user's durable, local, git-style memory. "
    "Call prepare_context at the start of a conversation (and whenever the topic shifts) "
    "to load a compact, token-budgeted patch of relevant durable context. "
    "Call commit_turn after answering so the turn is journaled and durable facts "
    "(corrections, decisions, 'remember that...' statements) merge into memory. "
    "Use remember for facts the user explicitly asks to save, search_context to look "
    "things up, and explain_selection when the user asks why something was or wasn't recalled."
)

TOOLS = [
    {
        "name": "prepare_context",
        "description": (
            "Compile a compact, token-budgeted context patch from the user's durable memory, "
            "ranked by relevance to the prompt. Call this at the start of a conversation or when "
            "the topic shifts. Returns the rendered context plus token accounting "
            "(patch tokens vs. full-history tokens saved)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The user's current prompt or topic."},
                "conversation_id": {"type": "string", "description": "Stable id for this conversation (default 'default')."},
                "budget": {"type": "integer", "description": "Max tokens for the compiled patch (default: server budget)."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "commit_turn",
        "description": (
            "Commit a completed conversation turn (user prompt + assistant answer) to the append-only "
            "journal. Durable phrasing ('remember that', 'from now on', corrections, decisions) is "
            "automatically merged into the versioned memory wiki. Call after each substantive answer."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_prompt": {"type": "string"},
                "assistant_answer": {"type": "string"},
                "conversation_id": {"type": "string"},
            },
            "required": ["user_prompt", "assistant_answer"],
        },
    },
    {
        "name": "remember",
        "description": (
            "Explicitly save a durable fact to the user's memory wiki. Use when the user asks to "
            "remember something or states a lasting preference, decision, or correction."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "fact": {"type": "string", "description": "The fact to save (kept verbatim, max 300 chars)."},
                "page": {"type": "string", "description": "Target wiki page title (optional; inferred when omitted)."},
            },
            "required": ["fact"],
        },
    },
    {
        "name": "mark_stale",
        "description": (
            "Mark a memory wiki page as stale/superseded so it is excluded from future context. "
            "Use when the user says a saved fact is outdated or wrong."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "page": {"type": "string", "description": "Wiki page title to mark stale."},
                "superseded_by": {"type": "string", "description": "What replaces it (optional)."},
            },
            "required": ["page"],
        },
    },
    {
        "name": "search_context",
        "description": "Full-text (BM25) search over all journaled events and memory wiki pages. Returns refs usable with show_context.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "Max results (default 8)."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "context_log",
        "description": "Recent journaled events, newest first (like `git log` for the user's context).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max events to return (default 20)."},
            },
        },
    },
    {
        "name": "show_context",
        "description": (
            "Show one record in full by ref: 'event:<id>' (a journaled turn), 'wiki:<title>' "
            "(a memory page with its version history), or 'mut:<id>' (a merge mutation)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "event:<id> | wiki:<title> | mut:<id>"},
            },
            "required": ["ref"],
        },
    },
    {
        "name": "full_context",
        "description": (
            "Page through the complete raw history (all events plus all wiki pages) when the compact "
            "patch is not enough. Returns total token counts so cost is visible before loading more."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "offset": {"type": "integer", "description": "Event offset (default 0)."},
                "limit": {"type": "integer", "description": "Max events to return (default 50)."},
            },
        },
    },
    {
        "name": "explain_selection",
        "description": (
            "Explain why each context item was selected or excluded for a prompt: per-item salience "
            "scores (frequency, recency, relevance, correction priority, staleness) and exclusion reasons. "
            "Use when the user asks 'why did/didn't you remember X?'."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "budget": {"type": "integer"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "merge_log",
        "description": (
            "The merge history of durable memory: every save/promote/mark_stale/reject mutation, plus "
            "the pending-merge queue awaiting review (like `git log` for memory merges)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Max mutations to return (default 20)."},
            },
        },
    },
    {
        "name": "resolve_pending",
        "description": "Approve (merge into durable memory) or reject a pending merge item by its exact content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Exact content of the pending item."},
                "action": {"type": "string", "enum": ["approve", "reject"]},
            },
            "required": ["content", "action"],
        },
    },
    {
        "name": "context_stats",
        "description": (
            "Token accounting and store status: compilations run, patch tokens served, tokens saved vs. "
            "sending full history, savings percentage, per-day breakdown, and store counts."
        ),
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class MCPServer:
    def __init__(self, engine: ContextGit):
        self.engine = engine

    # -- tool dispatch -------------------------------------------------

    def call_tool(self, name: str, args: Dict[str, Any]) -> Any:
        if name == "prepare_context":
            return self.engine.prepare(
                args["prompt"],
                conversation_id=args.get("conversation_id") or "default",
                budget=args.get("budget"),
            )
        if name == "commit_turn":
            return self.engine.commit_turn(
                args["user_prompt"],
                args["assistant_answer"],
                conversation_id=args.get("conversation_id") or "default",
            )
        if name == "remember":
            return self.engine.remember(args["fact"], page=args.get("page"))
        if name == "mark_stale":
            return self.engine.mark_stale(args["page"], superseded_by=args.get("superseded_by"))
        if name == "search_context":
            return {"results": self.engine.search(args["query"], limit=int(args.get("limit") or 8))}
        if name == "context_log":
            return {"events": self.engine.log(limit=int(args.get("limit") or 20))}
        if name == "show_context":
            return self.engine.show(args["ref"])
        if name == "full_context":
            return self.engine.full_context(
                offset=int(args.get("offset") or 0),
                limit=int(args.get("limit") or 50),
            )
        if name == "explain_selection":
            return self.engine.explain(args["prompt"], budget=args.get("budget"))
        if name == "merge_log":
            return self.engine.merges(limit=int(args.get("limit") or 20))
        if name == "resolve_pending":
            return self.engine.resolve_pending(args["content"], args["action"])
        if name == "context_stats":
            return {**self.engine.stats(), "status": self.engine.status()}
        raise ValueError(f"unknown tool: {name}")

    # -- JSON-RPC handling ---------------------------------------------

    def handle_message(self, message: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Handle one JSON-RPC message; return a response dict or None (notification)."""
        msg_id = message.get("id")
        method = message.get("method")
        is_notification = "id" not in message

        if method is None:
            return None  # a response from the client (e.g. to a ping); nothing to do

        try:
            if method == "initialize":
                client_version = (message.get("params") or {}).get("protocolVersion")
                version = client_version if client_version in SUPPORTED_PROTOCOL_VERSIONS else SUPPORTED_PROTOCOL_VERSIONS[0]
                result = {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {
                        "name": "contextgit",
                        "title": "contextgit — git for your AI's context",
                        "version": __version__,
                    },
                    "instructions": SERVER_INSTRUCTIONS,
                }
            elif method == "ping":
                result = {}
            elif method == "tools/list":
                result = {"tools": TOOLS}
            elif method == "tools/call":
                params = message.get("params") or {}
                name = params.get("name", "")
                args = params.get("arguments") or {}
                try:
                    payload = self.call_tool(name, args)
                    result = {
                        "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}],
                        "isError": False,
                    }
                except (KeyError, ValueError) as exc:
                    result = {
                        "content": [{"type": "text", "text": f"Error: {exc}"}],
                        "isError": True,
                    }
            elif method == "resources/list":
                result = {"resources": []}
            elif method == "prompts/list":
                result = {"prompts": []}
            elif method.startswith("notifications/"):
                return None
            else:
                if is_notification:
                    return None
                return {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "error": {"code": -32601, "message": f"Method not found: {method}"},
                }
        except Exception as exc:  # internal error -> JSON-RPC error, never a crash
            print(f"contextgit: internal error handling {method}: {exc}", file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            if is_notification:
                return None
            return {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32603, "message": f"Internal error: {exc}"},
            }

        if is_notification:
            return None
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def run_stdio(engine: ContextGit) -> None:
    server = MCPServer(engine)
    print(
        f"contextgit v{__version__} MCP server on stdio (store: {engine.store_dir})",
        file=sys.stderr,
    )
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            response = {
                "jsonrpc": "2.0",
                "id": None,
                "error": {"code": -32700, "message": "Parse error"},
            }
            print(json.dumps(response), flush=True)
            continue
        response = server.handle_message(message)
        if response is not None:
            print(json.dumps(response, ensure_ascii=False), flush=True)
