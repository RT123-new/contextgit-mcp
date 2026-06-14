"""contextgit - git for your AI's context.

A local-first, deterministic memory engine exposed over MCP (Model Context
Protocol) and a git-style CLI. Every conversation turn is committed to an
append-only journal; durable facts merge into a versioned wiki; each prompt
gets a token-budgeted, salience-ranked context "branch" compiled from history.
"""

__version__ = "0.1.0"

from contextgit.engine import ContextGit, resolve_store_dir  # noqa: F401
