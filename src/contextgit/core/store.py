from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional

from contextgit.core.models import (
    MemoryAuditRecord,
    MemoryMutation,
    MemorySnapshot,
    PendingMergeItem,
    RawEvent,
    WikiPage,
    WikiPageVersion,
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _stable_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(data: Any) -> str:
    payload = data if isinstance(data, str) else _stable_json(data)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_jsonl(path: str) -> List[Dict[str, Any]]:
    if not os.path.exists(path):
        return []
    rows: List[Dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def _append_jsonl(path: str, row: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, sort_keys=True, ensure_ascii=False) + "\n")


class EventJournal:
    """File-backed append-only raw event journal."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if not os.path.exists(path):
            open(path, "a", encoding="utf-8").close()
        # In-memory id index for the append-time uniqueness check (O(1) per
        # append). Disk format is unchanged; replay reconstructs from disk and
        # never relies on this cache.
        self._event_ids = {row.get("event_id") for row in _read_jsonl(self.path)}

    def append(self, event: RawEvent) -> None:
        if event.event_id in self._event_ids:
            raise ValueError(f"event_id already exists in append-only journal: {event.event_id}")
        _append_jsonl(self.path, event.model_dump())
        self._event_ids.add(event.event_id)

    def has_event(self, event_id: str) -> bool:
        return event_id in self._event_ids

    def event_count(self) -> int:
        return len(self._event_ids)

    def list_events(self) -> List[RawEvent]:
        return [RawEvent.model_validate(row) for row in _read_jsonl(self.path)]

    def snapshot_hash(self, events: Optional[List[RawEvent]] = None) -> str:
        evts = events if events is not None else self.list_events()
        return _sha256([event.model_dump() for event in evts])


class VersionedWikiStore:
    """Append-only wiki version log with current-state projection."""

    def __init__(self, versions_path: str):
        self.versions_path = versions_path
        os.makedirs(os.path.dirname(versions_path), exist_ok=True)
        if not os.path.exists(versions_path):
            open(versions_path, "a", encoding="utf-8").close()
        # Per-title version counter, initialised from disk so append_version
        # is O(1). Numbering matches the historical implementation exactly.
        self._version_count: Dict[str, int] = {}
        for row in _read_jsonl(self.versions_path):
            title = row.get("title")
            self._version_count[title] = self._version_count.get(title, 0) + 1

    def append_version(self, page: WikiPage, mutation_id: str, timestamp: Optional[str] = None) -> WikiPageVersion:
        timestamp = timestamp or utc_now_iso()
        last_version = self._version_count.get(page.title, 0)
        version = last_version + 1
        record = WikiPageVersion(
            title=page.title,
            version=version,
            page=page,
            mutation_id=mutation_id,
            timestamp=timestamp,
            previous_version=last_version if last_version else None,
            content_sha256=_sha256(page.model_dump()),
        )
        _append_jsonl(self.versions_path, record.model_dump())
        self._version_count[page.title] = version
        return record

    def list_versions(self) -> List[WikiPageVersion]:
        return [WikiPageVersion.model_validate(row) for row in _read_jsonl(self.versions_path)]

    def get_page_history(self, title: str) -> List[WikiPageVersion]:
        return [record for record in self.list_versions() if record.title == title]

    def list_pages(self) -> List[WikiPage]:
        latest: Dict[str, WikiPageVersion] = {}
        for record in self.list_versions():
            latest[record.title] = record
        return [latest[title].page for title in sorted(latest)]


class MemoryRuntime:
    """Local-first durable memory runtime.

    Storage layout:
      * `events.jsonl`: append-only raw events.
      * `mutations.jsonl`: append-only memory mutation log.
      * `wiki_versions.jsonl`: append-only wiki page versions.
      * `audit.jsonl`: append-only decision/mutation audit.
      * `pending.json`: current pending-review projection.
    """

    def __init__(self, root_dir: str, runtime_id: str = "local"):
        self.root_dir = root_dir
        self.runtime_id = runtime_id
        os.makedirs(root_dir, exist_ok=True)
        self.event_journal = EventJournal(os.path.join(root_dir, "events.jsonl"))
        self.wiki_store = VersionedWikiStore(os.path.join(root_dir, "wiki_versions.jsonl"))
        self.mutations_path = os.path.join(root_dir, "mutations.jsonl")
        self.audit_path = os.path.join(root_dir, "audit.jsonl")
        self.pending_path = os.path.join(root_dir, "pending.json")
        for path in (self.mutations_path, self.audit_path):
            if not os.path.exists(path):
                open(path, "a", encoding="utf-8").close()
        if not os.path.exists(self.pending_path):
            self._write_pending([])
        # Mutation sequence counter, initialised from disk so mutation-id
        # assignment stays O(1) over a growing log.
        self._mutation_count = len(_read_jsonl(self.mutations_path))

    @classmethod
    def init(cls, root_dir: str, runtime_id: str = "local") -> "MemoryRuntime":
        return cls(root_dir=root_dir, runtime_id=runtime_id)

    def append_raw_event(self, event: RawEvent) -> None:
        self.event_journal.append(event)

    def list_events(self) -> List[RawEvent]:
        return self.event_journal.list_events()

    def list_mutations(self) -> List[MemoryMutation]:
        return [MemoryMutation.model_validate(row) for row in _read_jsonl(self.mutations_path)]

    def list_audit_records(self) -> List[MemoryAuditRecord]:
        return [MemoryAuditRecord.model_validate(row) for row in _read_jsonl(self.audit_path)]

    def list_pending(self) -> List[PendingMergeItem]:
        return self._read_pending()

    def remove_pending(self, content: str) -> bool:
        """Remove a pending item by exact content match. Returns True if removed."""
        items = self._read_pending()
        remaining = [item for item in items if item.content != content]
        if len(remaining) == len(items):
            return False
        self._write_pending(remaining)
        return True

    def _next_mutation_id(self, payload: Dict[str, Any]) -> str:
        seq = self._mutation_count + 1
        return f"mut_{seq:06d}_{_sha256(payload)[:10]}"

    def record_mutation(
        self,
        action: str,
        *,
        source_event_ids: Optional[List[str]] = None,
        previous_memory_ids: Optional[List[str]] = None,
        previous_page_titles: Optional[List[str]] = None,
        new_claim: Optional[str] = None,
        target_page: Optional[str] = None,
        policy_reason: str = "",
        provider_class: Optional[str] = None,
        provider_model: Optional[str] = None,
        human_approved: bool = False,
        confidence: float = 0.0,
        decision_mode: str = "review-required",
        timestamp: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> MemoryMutation:
        timestamp = timestamp or utc_now_iso()
        payload = {
            "action": action,
            "source_event_ids": source_event_ids or [],
            "previous_memory_ids": previous_memory_ids or [],
            "previous_page_titles": previous_page_titles or [],
            "new_claim": new_claim,
            "target_page": target_page,
            "policy_reason": policy_reason,
            "provider_class": provider_class,
            "provider_model": provider_model,
            "human_approved": human_approved,
            "confidence": confidence,
            "decision_mode": decision_mode,
            "timestamp": timestamp,
            "metadata": metadata or {},
        }
        mutation = MemoryMutation(
            mutation_id=self._next_mutation_id(payload),
            **payload,
        )
        self.apply_mutation(mutation, persist=True)
        return mutation

    def apply_mutation(self, mutation: MemoryMutation, persist: bool = True) -> None:
        before_hash = self.snapshot().state_sha256 if persist else None
        if persist:
            _append_jsonl(self.mutations_path, mutation.model_dump())
            self._mutation_count += 1

        if mutation.action in {"save", "promote"}:
            self._apply_save(mutation)
        elif mutation.action == "mark_stale":
            self._apply_mark_stale(mutation)
        elif mutation.action in {"pending", "carry"}:
            self._apply_pending(mutation)
        elif mutation.action in {"discard", "reject", "expire"}:
            self._apply_pending_terminal(mutation)

        if persist:
            after_hash = self.snapshot().state_sha256
            audit = MemoryAuditRecord(
                audit_id=f"audit_{mutation.mutation_id}",
                timestamp=mutation.timestamp,
                mutation_id=mutation.mutation_id,
                action=mutation.action,
                source_event_ids=list(mutation.source_event_ids),
                affected_pages=[p for p in [mutation.target_page, *mutation.previous_page_titles] if p],
                reason_codes=list(mutation.metadata.get("reason_codes", [])),
                policy_reason=mutation.policy_reason,
                before_hash=before_hash,
                after_hash=after_hash,
            )
            _append_jsonl(self.audit_path, audit.model_dump())

    def _current_page_by_title(self) -> Dict[str, WikiPage]:
        return {page.title: page for page in self.wiki_store.list_pages()}

    def _page_type_from_target(self, target_page: Optional[str]) -> str:
        target = (target_page or "").lower()
        if "/users/" in target or target.startswith("user") or "profile" in target:
            return "user"
        if "open_loop" in target or "open loops" in target:
            return "open_loop"
        if "decision" in target:
            return "decision"
        return "project"

    def _apply_save(self, mutation: MemoryMutation) -> None:
        if not mutation.target_page or not mutation.new_claim:
            return
        current = self._current_page_by_title().get(mutation.target_page)
        if current:
            content = current.content
            if mutation.new_claim not in content:
                sep = "\n" if content.strip() else ""
                content = f"{content.rstrip()}{sep}- {mutation.new_claim}"
            page = current.model_copy(update={
                "status": "active",
                "last_verified": mutation.timestamp,
                "sources": sorted(set(current.sources) | set(mutation.source_event_ids)),
                "content": content,
            })
        else:
            page = WikiPage(
                title=mutation.target_page,
                type=self._page_type_from_target(mutation.target_page),
                status="active",
                last_verified=mutation.timestamp,
                sources=list(mutation.source_event_ids),
                confidence="high" if mutation.confidence >= 0.8 else "medium",
                content=f"- {mutation.new_claim}",
            )
        self.wiki_store.append_version(page, mutation.mutation_id, mutation.timestamp)

    def _apply_mark_stale(self, mutation: MemoryMutation) -> None:
        current = self._current_page_by_title()
        targets = mutation.previous_page_titles or ([mutation.target_page] if mutation.target_page else [])
        for title in targets:
            if not title:
                continue
            page = current.get(title)
            if page:
                stale_page = page.model_copy(update={
                    "status": "stale",
                    "last_verified": mutation.timestamp,
                    "supersedes": mutation.metadata.get("superseded_by"),
                })
            else:
                stale_page = WikiPage(
                    title=title,
                    type=self._page_type_from_target(title),
                    status="stale",
                    last_verified=mutation.timestamp,
                    sources=list(mutation.source_event_ids),
                    confidence="low",
                    content=mutation.new_claim or "",
                    supersedes=mutation.metadata.get("superseded_by"),
                )
            self.wiki_store.append_version(stale_page, mutation.mutation_id, mutation.timestamp)

    def _read_pending(self) -> List[PendingMergeItem]:
        if not os.path.exists(self.pending_path):
            return []
        with open(self.pending_path, "r", encoding="utf-8") as f:
            data = json.load(f) if f.readable() else []
        return [PendingMergeItem.model_validate(row) for row in data]

    def _write_pending(self, items: Iterable[PendingMergeItem]) -> None:
        os.makedirs(os.path.dirname(self.pending_path), exist_ok=True)
        with open(self.pending_path, "w", encoding="utf-8") as f:
            json.dump([item.model_dump() for item in items], f, indent=2, sort_keys=True)

    def _apply_pending(self, mutation: MemoryMutation) -> None:
        item_data = mutation.metadata.get("pending_item") or {}
        if not item_data and mutation.new_claim:
            item_data = {
                "type": mutation.metadata.get("type", "decision"),
                "content": mutation.new_claim,
                "target_page": mutation.target_page,
                "source_refs": [f"event:{event_id}" for event_id in mutation.source_event_ids],
                "confidence": "medium",
                "reason": mutation.policy_reason,
                "created_timestamp": mutation.timestamp,
            }
        if not item_data:
            return
        item = PendingMergeItem.model_validate(item_data)
        existing = self._read_pending()
        existing.append(item)
        self._write_pending(existing)

    def _apply_pending_terminal(self, mutation: MemoryMutation) -> None:
        if not mutation.new_claim:
            return
        remaining = [
            item for item in self._read_pending()
            if item.content != mutation.new_claim
        ]
        self._write_pending(remaining)

    def snapshot(self) -> MemorySnapshot:
        events = self.list_events()
        mutations = self.list_mutations()
        pages = self.wiki_store.list_pages()
        pending = self._read_pending()
        stale_links = [
            {
                "title": page.title,
                "supersedes": page.supersedes,
                "sources": page.sources,
            }
            for page in pages
            if page.status == "stale" or page.supersedes
        ]
        payload = {
            "events": [event.event_id for event in events],
            "mutations": [mutation.mutation_id for mutation in mutations],
            "pages": [page.model_dump() for page in sorted(pages, key=lambda p: p.title)],
            "pending": [item.model_dump() for item in pending],
            "stale_links": stale_links,
        }
        return MemorySnapshot(
            runtime_id=self.runtime_id,
            event_count=len(events),
            mutation_count=len(mutations),
            wiki_pages=pages,
            pending_items=pending,
            stale_links=stale_links,
            audit_records=self.list_audit_records(),
            event_ids=[event.event_id for event in events],
            mutation_ids=[mutation.mutation_id for mutation in mutations],
            state_sha256=_sha256(payload),
            metadata={
                "storage_root": self.root_dir,
                "event_journal_sha256": self.event_journal.snapshot_hash(events),
            },
        )
