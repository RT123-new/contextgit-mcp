"""High-level contextgit engine.

One facade (`ContextGit`) wraps the durable store, the deterministic context
compiler, and the token-usage ledger. Both the CLI and the MCP server call
into this class, so behavior is identical no matter how users interact with
their context repository.

Store resolution mirrors git: an explicit path wins, then the CONTEXTGIT_DIR
environment variable, then the nearest `.contextgit/` directory walking up
from the working directory, then the global store at `~/.contextgit/store`.
"""

from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Dict, List, Optional

from contextgit.core.compiler import ContextCompiler, ContextCompilerConfig
from contextgit.core.retrieval import BM25Searcher
from contextgit.core.store import MemoryRuntime, utc_now_iso
from contextgit.core.models import RawEvent
from contextgit.core.tokens import estimate_tokens, token_count_source
from contextgit.usage import UsageLedger


STORE_DIRNAME = ".contextgit"
GLOBAL_STORE = os.path.join(os.path.expanduser("~"), ".contextgit", "store")

_DURABLE_MARKERS = (
    "remember that",
    "remember this",
    "from now on",
    "going forward",
    "final decision",
    "final:",
    "decision:",
    "correction:",
    "update:",
    "instead of",
)

_QUESTION_STARTERS = (
    "what ", "when ", "where ", "which ", "who ", "why ", "how ",
    "can ", "could ", "should ", "would ", "is ", "are ", "do ", "does ",
)

_RISKY_DURABLE_PATTERNS = (
    r"https?://",
    r"\bbase[-_ ]?url\b",
    r"\bproxy\b",
    r"\broute\s+through\b",
    r"\bapi[_-]?key\b",
    r"\bsecret\b",
    r"\btoken\b",
    r"\bpassword\b",
    r"\bcredential\b",
    r"\bNEXT_PUBLIC_[A-Z0-9_]*\b",
)


def resolve_store_dir(explicit: Optional[str] = None, cwd: Optional[str] = None) -> str:
    """Resolve which context store to use, git-style."""
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    env = os.environ.get("CONTEXTGIT_DIR")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    current = os.path.abspath(cwd or os.getcwd())
    while True:
        candidate = os.path.join(current, STORE_DIRNAME)
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return GLOBAL_STORE


def _short_hash(*parts: str) -> str:
    return hashlib.sha256("||".join(parts).encode("utf-8")).hexdigest()[:8]


def _slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_") or "conversation"


def _looks_durable(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _DURABLE_MARKERS)


def _is_question_like(text: str) -> bool:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    lowered = clean.lower()
    return clean.endswith("?") or lowered.startswith(_QUESTION_STARTERS)


def _looks_risky_durable(text: str) -> bool:
    return any(re.search(pattern, text or "", flags=re.IGNORECASE) for pattern in _RISKY_DURABLE_PATTERNS)


def _review_required_reason(text: str) -> Optional[str]:
    if _is_question_like(text):
        return "question-shaped text should not auto-merge as durable memory"
    if _looks_risky_durable(text):
        return "security-sensitive durable instruction requires review"
    return None


def _durable_claim(text: str) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    clean = re.sub(
        r"^(remember that|remember this|from now on|going forward|final decision:|decision:|correction:|update:)\s*",
        "", clean, flags=re.IGNORECASE,
    )
    return clean.strip(" \t\r\n,;:-")[:300]


def _target_page_for_prompt(text: str) -> str:
    match = re.search(r"\b(?:project|for)\s+([A-Z][A-Za-z0-9_-]+)\b", text or "")
    if match:
        return f"{match.group(1)} Memory"
    return "Conversation Memory"


class ContextGit:
    def __init__(
        self,
        store_dir: Optional[str] = None,
        *,
        budget: int = 700,
        compiler_config: Optional[Dict[str, Any]] = None,
    ):
        self.store_dir = resolve_store_dir(store_dir)
        self.runtime = MemoryRuntime.init(self.store_dir)
        config = dict(compiler_config or {})
        config.setdefault("max_patch_tokens", budget)
        self.compiler = ContextCompiler(ContextCompilerConfig.model_validate(config))
        self.usage = UsageLedger(os.path.join(self.store_dir, "usage.jsonl"))

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        events = self.runtime.list_events()
        pages = self.runtime.wiki_store.list_pages()
        pending = self.runtime.list_pending()
        mutations = self.runtime.list_mutations()
        history_tokens = sum(estimate_tokens(e.content) for e in events)
        history_tokens += sum(estimate_tokens(f"{p.title}\n{p.content}") for p in pages)
        usage = self.usage.summary()
        return {
            "store_dir": self.store_dir,
            "events": len(events),
            "wiki_pages": len(pages),
            "wiki_pages_active": sum(1 for p in pages if p.status == "active"),
            "wiki_pages_stale": sum(1 for p in pages if p.status == "stale"),
            "pending_merges": len(pending),
            "mutations": len(mutations),
            "full_history_tokens": history_tokens,
            "budget_tokens": self.compiler.config.max_patch_tokens,
            "token_counter": token_count_source(),
            "usage": {
                "compilations": usage["compilations"],
                "saved_tokens_total": usage["saved_tokens_total"],
                "savings_pct": usage["savings_pct"],
            },
        }

    def log(self, limit: int = 20) -> List[Dict[str, Any]]:
        events = self.runtime.list_events()
        out = []
        for event in events[-limit:][::-1]:
            out.append({
                "ref": f"event:{event.event_id}",
                "timestamp": event.timestamp,
                "speaker": event.speaker,
                "tags": event.tags,
                "tokens": estimate_tokens(event.content),
                "summary": re.sub(r"\s+", " ", event.content.strip())[:160],
            })
        return out

    def full_context(self, offset: int = 0, limit: int = 50) -> Dict[str, Any]:
        events = self.runtime.list_events()
        pages = self.runtime.wiki_store.list_pages()
        window = events[offset:offset + limit]
        return {
            "total_events": len(events),
            "offset": offset,
            "returned": len(window),
            "total_tokens": sum(estimate_tokens(e.content) for e in events),
            "events": [
                {
                    "ref": f"event:{e.event_id}",
                    "timestamp": e.timestamp,
                    "speaker": e.speaker,
                    "tags": e.tags,
                    "content": e.content,
                }
                for e in window
            ],
            "wiki_pages": [
                {
                    "ref": f"wiki:{p.title}",
                    "status": p.status,
                    "type": p.type,
                    "confidence": p.confidence,
                    "content": p.content,
                }
                for p in pages
            ],
        }

    def show(self, ref: str) -> Dict[str, Any]:
        kind, _, ident = ref.partition(":")
        if kind == "event":
            for event in self.runtime.list_events():
                if event.event_id == ident:
                    return {"ref": ref, "kind": "event", **event.model_dump()}
            raise KeyError(f"no event with id {ident!r}")
        if kind == "wiki":
            history = self.runtime.wiki_store.get_page_history(ident)
            if not history:
                raise KeyError(f"no wiki page titled {ident!r}")
            latest = history[-1]
            return {
                "ref": ref,
                "kind": "wiki",
                "versions": len(history),
                "version_history": [
                    {
                        "version": v.version,
                        "timestamp": v.timestamp,
                        "mutation_id": v.mutation_id,
                        "status": v.page.status,
                    }
                    for v in history
                ],
                **latest.page.model_dump(),
            }
        if kind == "mut":
            for mutation in self.runtime.list_mutations():
                if mutation.mutation_id == ident:
                    return {"ref": ref, "kind": "mutation", **mutation.model_dump()}
            raise KeyError(f"no mutation with id {ident!r}")
        raise ValueError(
            f"unrecognized ref {ref!r}: expected event:<id>, wiki:<title>, or mut:<id>"
        )

    def search(self, query: str, limit: int = 8) -> List[Dict[str, Any]]:
        events = self.runtime.list_events()
        pages = self.runtime.wiki_store.list_pages()
        ids: List[str] = [f"event:{e.event_id}" for e in events]
        texts: List[str] = [e.content for e in events]
        for page in pages:
            ids.append(f"wiki:{page.title}")
            texts.append(f"{page.title}\n{page.content}")
        if not ids:
            return []
        searcher = BM25Searcher()
        searcher.fit(ids, texts)
        text_by_id = dict(zip(ids, texts))
        results = []
        for ref, score in searcher.search(query, top_k=limit):
            if score <= 0.0:
                continue
            results.append({
                "ref": ref,
                "score": round(score, 4),
                "summary": re.sub(r"\s+", " ", text_by_id[ref].strip())[:200],
            })
        return results

    def merges(self, limit: int = 20) -> Dict[str, Any]:
        mutations = self.runtime.list_mutations()
        pending = self.runtime.list_pending()
        return {
            "total_mutations": len(mutations),
            "mutations": [
                {
                    "ref": f"mut:{m.mutation_id}",
                    "timestamp": m.timestamp,
                    "action": m.action,
                    "claim": m.new_claim,
                    "target_page": m.target_page,
                    "decision_mode": m.decision_mode,
                    "confidence": m.confidence,
                }
                for m in mutations[-limit:][::-1]
            ],
            "pending": [
                {
                    "content": item.content,
                    "type": item.type,
                    "target_page": item.target_page,
                    "reason": item.reason,
                    "confidence": item.confidence,
                }
                for item in pending
            ],
        }

    def stats(self) -> Dict[str, Any]:
        return {
            "all_time": self.usage.summary(),
            "last_7_days": self.usage.summary(last_n_days=7),
            "token_counter": token_count_source(),
            "store_dir": self.store_dir,
        }

    # ------------------------------------------------------------------
    # Context compilation (the "branch" view)
    # ------------------------------------------------------------------

    def prepare(
        self,
        prompt: str,
        conversation_id: str = "default",
        budget: Optional[int] = None,
        record_usage: bool = True,
    ) -> Dict[str, Any]:
        events = self.runtime.list_events()
        pages = self.runtime.wiki_store.list_pages()
        compilation = self.compiler.compile_context(
            conversation_id=conversation_id,
            user_prompt=prompt,
            events=events,
            wiki_pages=pages,
            budget=budget,
        )
        full_tokens = sum(estimate_tokens(e.content) for e in events)
        full_tokens += sum(estimate_tokens(f"{p.title}\n{p.content}") for p in pages)
        if record_usage:
            self.usage.record_compilation(
                conversation_id=conversation_id,
                patch_tokens=compilation.estimated_tokens,
                full_history_tokens=full_tokens,
                budget=compilation.budget,
                selected_count=len(compilation.selected_context),
                excluded_count=len(compilation.excluded_context),
                token_source=token_count_source(),
            )
        saved = full_tokens - compilation.estimated_tokens
        return {
            "context": compilation.rendered_patch,
            "estimated_tokens": compilation.estimated_tokens,
            "budget": compilation.budget,
            "full_history_tokens": full_tokens,
            "saved_tokens": saved,
            "net_saved_tokens": saved,
            "savings_pct": round(100.0 * saved / full_tokens, 2) if full_tokens else 0.0,
            "selected": [
                {
                    "ref": c.source_id,
                    "score": c.final_score,
                    "tokens": c.estimated_tokens,
                    "summary": c.summary,
                }
                for c in compilation.selected_context
            ],
            "excluded_count": len(compilation.excluded_context),
            "source_count": compilation.source_count,
        }

    def explain(
        self,
        prompt: str,
        conversation_id: str = "default",
        budget: Optional[int] = None,
        max_excluded: int = 25,
    ) -> Dict[str, Any]:
        events = self.runtime.list_events()
        pages = self.runtime.wiki_store.list_pages()
        compilation = self.compiler.compile_context(
            conversation_id=conversation_id,
            user_prompt=prompt,
            events=events,
            wiki_pages=pages,
            budget=budget,
        )
        def _row(c):
            return {
                "ref": c.source_id,
                "score": c.final_score,
                "tokens": c.estimated_tokens,
                "score_components": c.score_components,
                "exclusion_reasons": c.exclusion_reasons,
                "superseded_by": c.superseded_by,
                "status": c.status,
                "summary": c.summary,
            }
        return {
            "budget": compilation.budget,
            "estimated_tokens": compilation.estimated_tokens,
            "selected": [_row(c) for c in compilation.selected_context],
            "excluded": [_row(c) for c in compilation.excluded_context[:max_excluded]],
            "excluded_total": len(compilation.excluded_context),
            "topics": [
                {"topic": t.topic, "frequency": t.frequency}
                for t in compilation.topic_index[:15]
            ],
        }

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def _append_event(
        self,
        speaker: str,
        content: str,
        conversation_id: str,
        tags: Optional[List[str]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        importance: float = 0.5,
    ) -> RawEvent:
        ts = utc_now_iso()
        base = _slug(conversation_id)
        seq = self.runtime.event_journal.event_count() + 1
        event_id = f"{base}_{seq:05d}_{speaker}_{_short_hash(content, ts, str(seq))}"
        event = RawEvent(
            event_id=event_id,
            timestamp=ts,
            speaker=speaker,
            content=content,
            tags=tags or [],
            importance=importance,
            metadata=metadata or {},
        )
        self.runtime.append_raw_event(event)
        return event

    def commit_turn(
        self,
        user_prompt: str,
        assistant_answer: str,
        conversation_id: str = "default",
    ) -> Dict[str, Any]:
        durable_prompt = _looks_durable(user_prompt)
        review_reason = _review_required_reason(user_prompt) if durable_prompt else None
        user_tags = ["turn"]
        user_metadata: Dict[str, Any] = {}
        user_importance = 0.5
        if review_reason:
            user_tags.append("pending_review")
            user_metadata["memory_status"] = "pending_review"
            user_metadata["review_required_reason"] = review_reason
            user_importance = 0.1
        user_event = self._append_event(
            "user",
            user_prompt,
            conversation_id,
            tags=user_tags,
            metadata=user_metadata,
            importance=user_importance,
        )
        assistant_event = self._append_event("assistant", assistant_answer, conversation_id, tags=["turn"])
        mutation_id = None
        durable_claim = None
        pending_id = None
        if durable_prompt:
            durable_claim = _durable_claim(user_prompt)
            if review_reason:
                mutation = self.runtime.record_mutation(
                    "pending",
                    source_event_ids=[user_event.event_id],
                    new_claim=durable_claim,
                    target_page=_target_page_for_prompt(user_prompt),
                    policy_reason=review_reason,
                    confidence=0.55,
                    decision_mode="review-required",
                    metadata={
                        "pending_item": {
                            "type": "decision",
                            "content": durable_claim,
                            "target_page": _target_page_for_prompt(user_prompt),
                            "source_refs": [f"event:{user_event.event_id}"],
                            "confidence": "medium",
                            "reason": review_reason,
                            "created_timestamp": user_event.timestamp,
                            "origin_speaker": "user",
                            "review_required_reason": review_reason,
                        },
                        "reason_codes": ["review_required_durable_marker"],
                    },
                )
                pending_id = mutation.mutation_id
            else:
                mutation = self.runtime.record_mutation(
                    "save",
                    source_event_ids=[user_event.event_id],
                    new_claim=durable_claim,
                    target_page=_target_page_for_prompt(user_prompt),
                    policy_reason="deterministic durable-phrasing detection on commit_turn",
                    confidence=0.85,
                    decision_mode="autonomous",
                )
                mutation_id = mutation.mutation_id
        return {
            "ok": True,
            "conversation_id": conversation_id,
            "committed_event_refs": [
                f"event:{user_event.event_id}",
                f"event:{assistant_event.event_id}",
            ],
            "durable_merge": (
                {"ref": f"mut:{mutation_id}", "claim": durable_claim}
                if mutation_id
                else None
            ),
            "pending_merge": (
                {"ref": f"mut:{pending_id}", "claim": durable_claim}
                if pending_id
                else None
            ),
            "total_events": self.runtime.event_journal.event_count(),
        }

    def remember(
        self,
        fact: str,
        page: Optional[str] = None,
        confidence: float = 0.9,
    ) -> Dict[str, Any]:
        event = self._append_event(
            "user", fact, "remember", tags=["remember", "durable"], importance=0.9,
        )
        mutation = self.runtime.record_mutation(
            "save",
            source_event_ids=[event.event_id],
            new_claim=_durable_claim(fact),
            target_page=page or _target_page_for_prompt(fact),
            policy_reason="explicit remember",
            confidence=confidence,
            decision_mode="human-approved",
            human_approved=True,
        )
        return {
            "ok": True,
            "ref": f"mut:{mutation.mutation_id}",
            "target_page": mutation.target_page,
            "claim": mutation.new_claim,
        }

    def mark_stale(self, page: str, superseded_by: Optional[str] = None) -> Dict[str, Any]:
        mutation = self.runtime.record_mutation(
            "mark_stale",
            previous_page_titles=[page],
            policy_reason="explicit mark_stale",
            confidence=0.9,
            decision_mode="human-approved",
            human_approved=True,
            metadata={"superseded_by": superseded_by} if superseded_by else {},
        )
        return {"ok": True, "ref": f"mut:{mutation.mutation_id}", "page": page}

    def resolve_pending(self, content: str, action: str) -> Dict[str, Any]:
        matches = [item for item in self.runtime.list_pending() if item.content == content]
        if not matches:
            raise KeyError(f"no pending merge item with content {content!r}")
        item = matches[0]
        if action == "approve":
            mutation = self.runtime.record_mutation(
                "promote",
                new_claim=item.content,
                target_page=item.target_page or "Conversation Memory",
                policy_reason="pending item approved",
                confidence=0.9,
                decision_mode="human-approved",
                human_approved=True,
            )
            self.runtime.remove_pending(content)
            return {"ok": True, "action": "approved", "ref": f"mut:{mutation.mutation_id}"}
        if action == "reject":
            mutation = self.runtime.record_mutation(
                "reject",
                new_claim=item.content,
                policy_reason="pending item rejected",
                confidence=0.9,
                decision_mode="human-approved",
                human_approved=True,
            )
            return {"ok": True, "action": "rejected", "ref": f"mut:{mutation.mutation_id}"}
        raise ValueError(f"unknown action {action!r}: expected 'approve' or 'reject'")
