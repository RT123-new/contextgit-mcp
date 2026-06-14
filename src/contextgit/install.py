"""One-command installers for MCP clients.

`contextgit install <client>` wires the server into Claude Desktop, Claude
Code, Codex, or Cursor by editing the client's own config file (with a backup
written next to it). `contextgit install print` shows every snippet without
touching anything.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import sys
from typing import Any, Dict, List, Optional, Tuple

try:
    import tomllib  # py311+
except ImportError:  # pragma: no cover - py310
    tomllib = None


def server_command(store: Optional[str] = None, budget: Optional[int] = None) -> Tuple[str, List[str]]:
    """Resolve the most robust way to launch the server on this machine.

    Prefers the absolute path of the installed `contextgit` script (GUI apps
    like Claude Desktop do not inherit a shell PATH), falling back to
    `python -m contextgit`.
    """
    args = ["serve"]
    if store:
        args += ["--store", os.path.abspath(os.path.expanduser(store))]
    if budget:
        args += ["--budget", str(budget)]
    exe = shutil.which("contextgit")
    if exe:
        return exe, args
    return sys.executable, ["-m", "contextgit", *args]


def _server_entry(store: Optional[str] = None, budget: Optional[int] = None) -> Dict[str, Any]:
    command, args = server_command(store, budget)
    return {"command": command, "args": args}


def _backup(path: str) -> Optional[str]:
    if os.path.exists(path):
        backup_path = path + ".bak"
        shutil.copy2(path, backup_path)
        return backup_path
    return None


def _merge_json_config(path: str, store: Optional[str], budget: Optional[int]) -> str:
    config: Dict[str, Any] = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
        if raw:
            config = json.loads(raw)
    config.setdefault("mcpServers", {})["contextgit"] = _server_entry(store, budget)
    _backup(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
        f.write("\n")
    return path


def claude_desktop_config_path() -> str:
    system = platform.system()
    home = os.path.expanduser("~")
    if system == "Darwin":
        return os.path.join(home, "Library", "Application Support", "Claude", "claude_desktop_config.json")
    if system == "Windows":
        return os.path.join(os.environ.get("APPDATA", home), "Claude", "claude_desktop_config.json")
    return os.path.join(home, ".config", "Claude", "claude_desktop_config.json")


def install_claude_desktop(store: Optional[str] = None, budget: Optional[int] = None) -> str:
    path = _merge_json_config(claude_desktop_config_path(), store, budget)
    return f"Added 'contextgit' to {path}\nRestart Claude Desktop to load it."


def install_claude_code(store: Optional[str] = None, budget: Optional[int] = None, project_dir: Optional[str] = None) -> str:
    """Project-scope install: writes .mcp.json in the project directory."""
    path = os.path.join(os.path.abspath(project_dir or os.getcwd()), ".mcp.json")
    _merge_json_config(path, store, budget)
    command, args = server_command(store, budget)
    user_cmd = f"claude mcp add --scope user contextgit -- {command} {' '.join(args)}"
    return (
        f"Added 'contextgit' to {path} (project scope).\n"
        f"For every project instead, run:\n  {user_cmd}"
    )


def install_cursor(store: Optional[str] = None, budget: Optional[int] = None) -> str:
    path = _merge_json_config(
        os.path.join(os.path.expanduser("~"), ".cursor", "mcp.json"), store, budget
    )
    return f"Added 'contextgit' to {path}\nRestart Cursor to load it."


def install_codex(store: Optional[str] = None, budget: Optional[int] = None) -> str:
    """Adds an [mcp_servers.contextgit] block to ~/.codex/config.toml."""
    path = os.path.join(os.path.expanduser("~"), ".codex", "config.toml")
    existing = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            existing = f.read()
        if tomllib is not None:
            try:
                parsed = tomllib.loads(existing)
                if "contextgit" in (parsed.get("mcp_servers") or {}):
                    return f"'contextgit' is already configured in {path}; nothing changed."
            except Exception:
                pass  # unparseable config: fall back to the substring check below
        if "[mcp_servers.contextgit]" in existing:
            return f"'contextgit' is already configured in {path}; nothing changed."
    command, args = server_command(store, budget)
    args_toml = ", ".join(json.dumps(a) for a in args)
    block = (
        f"\n[mcp_servers.contextgit]\n"
        f"command = {json.dumps(command)}\n"
        f"args = [{args_toml}]\n"
    )
    _backup(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        if existing and not existing.endswith("\n"):
            f.write("\n")
        f.write(block)
    return f"Added 'contextgit' to {path}\nRestart Codex to load it."


def snippets(store: Optional[str] = None, budget: Optional[int] = None) -> str:
    command, args = server_command(store, budget)
    json_entry = json.dumps({"mcpServers": {"contextgit": _server_entry(store, budget)}}, indent=2)
    args_toml = ", ".join(json.dumps(a) for a in args)
    return "\n".join([
        "# Claude Desktop  (claude_desktop_config.json)",
        f"#   {claude_desktop_config_path()}",
        json_entry,
        "",
        "# Claude Code  (one-liner, user scope)",
        f"claude mcp add --scope user contextgit -- {command} {' '.join(args)}",
        "",
        "# Claude Code  (project scope: save as .mcp.json in the project root)",
        json_entry,
        "",
        "# Codex CLI  (~/.codex/config.toml)",
        "[mcp_servers.contextgit]",
        f"command = {json.dumps(command)}",
        f"args = [{args_toml}]",
        "",
        "# Cursor  (~/.cursor/mcp.json)",
        json_entry,
    ])


INSTALLERS = {
    "claude-desktop": install_claude_desktop,
    "claude-code": install_claude_code,
    "codex": install_codex,
    "cursor": install_cursor,
}
