from pydantic import BaseModel, Field
from typing import List, Dict, Any, Optional


class RawEvent(BaseModel):
    event_id: str
    timestamp: str
    speaker: str
    content: str
    project: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    source_type: str = "chat"
    source_ref: Optional[str] = None
    importance: float = 0.5
    supersedes: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    user_id: Optional[str] = None


class WikiPage(BaseModel):
    title: str
    type: str  # user | project | decision | open_loop | contradiction | summary
    status: str  # active | stale | disputed | archived | suppressed
    last_verified: str
    sources: List[str] = Field(default_factory=list)
    confidence: str = "medium"  # high | medium | low
    supersedes: Optional[str] = None
    content: str = ""  # markdown body
    file_path: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    user_id: Optional[str] = None


class PendingMergeItem(BaseModel):
    """A merge candidate that is not yet committed to durable memory."""

    type: str
    content: str
    target_page: Optional[str] = None
    source_refs: List[str] = Field(default_factory=list)
    confidence: str = "medium"
    reason: str
    ttl_turns: int = 1
    created_turn_id: Optional[str] = None
    promotion_signals: List[str] = Field(default_factory=list)
    decision_band: str = "pending"
    promotion_score: float = 0.0
    review_interval_turns: int = 1
    next_review_turn: Optional[int] = None
    last_review_turn_id: Optional[str] = None
    origin_speaker: Optional[str] = None
    created_timestamp: Optional[str] = None
    ttl_seconds: Optional[int] = None
    review_required_reason: Optional[str] = None
    contradiction_signals: List[str] = Field(default_factory=list)


class MemoryMutation(BaseModel):
    mutation_id: str
    timestamp: str
    action: str
    source_event_ids: List[str] = Field(default_factory=list)
    previous_memory_ids: List[str] = Field(default_factory=list)
    previous_page_titles: List[str] = Field(default_factory=list)
    new_claim: Optional[str] = None
    target_page: Optional[str] = None
    policy_reason: str = ""
    provider_class: Optional[str] = None
    provider_model: Optional[str] = None
    human_approved: bool = False
    confidence: float = 0.0
    decision_mode: str = "review-required"  # autonomous | pending | review-required
    metadata: Dict[str, Any] = Field(default_factory=dict)


class MemoryAuditRecord(BaseModel):
    audit_id: str
    timestamp: str
    mutation_id: Optional[str] = None
    action: str
    source_event_ids: List[str] = Field(default_factory=list)
    affected_pages: List[str] = Field(default_factory=list)
    reason_codes: List[str] = Field(default_factory=list)
    policy_reason: str = ""
    before_hash: Optional[str] = None
    after_hash: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)


class WikiPageVersion(BaseModel):
    title: str
    version: int
    page: WikiPage
    mutation_id: str
    timestamp: str
    content_sha256: str
    previous_version: Optional[int] = None


class MemorySnapshot(BaseModel):
    runtime_id: str = "local"
    event_count: int = 0
    mutation_count: int = 0
    wiki_pages: List[WikiPage] = Field(default_factory=list)
    pending_items: List[PendingMergeItem] = Field(default_factory=list)
    stale_links: List[Dict[str, Any]] = Field(default_factory=list)
    audit_records: List[MemoryAuditRecord] = Field(default_factory=list)
    event_ids: List[str] = Field(default_factory=list)
    mutation_ids: List[str] = Field(default_factory=list)
    state_sha256: Optional[str] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
