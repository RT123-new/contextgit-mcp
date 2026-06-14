from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from pydantic import BaseModel, Field

from contextgit.core.models import RawEvent, WikiPage
from contextgit.core.retrieval import BM25Searcher, tokenize
from contextgit.core.tokens import estimate_tokens


_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "do", "does", "for",
    "from", "has", "have", "i", "in", "is", "it", "me", "of", "on", "or",
    "our", "please", "should", "that", "the", "this", "to", "use", "using",
    "we", "what", "when", "where", "which", "who", "with", "you",
}

_CORRECTION_MARKERS = (
    "correction",
    "final correction",
    "instead of",
    "supersedes",
    "updated",
    "update:",
    "final:",
    "from now on",
)

_OPEN_LOOP_MARKERS = (
    "todo",
    "follow up",
    "follow-up",
    "open loop",
    "blocked",
    "waiting on",
    "next step",
    "action item",
)

_NOISE_MARKERS = (
    "scratch",
    "placeholder",
    "temporary",
    "test value",
    "mock",
    "sample",
    "distractor",
    "bulk archive",
    "unrelated",
)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


# ---------------------------------------------------------------------------
# Natural-law scoring helpers (opt-in `scoring_profile="natural_law"`).
#
# Each helper implements one empirically grounded statistical-linguistic or
# cognitive law. They are deterministic, bounded to ``[0, 1]``, and only used
# when the natural-law profile is selected; the baseline profile is unchanged.
# ---------------------------------------------------------------------------


def _surprisal_recurrence(event_topics: Sequence[str], topic_df: Dict[str, int], n_events: int) -> Tuple[float, float]:
    """Saturating TF times Zipf/Shannon surprisal IDF for the event's best topic.

    Implements Weber-Fechner / Zipf's law of abbreviation (log term-frequency
    saturation, ``log1p(df)/log1p(N)``) combined with Shannon self-information /
    Zipf's law (``idf = log((N+1)/(df+1))``). The product peaks at the
    informative middle of the frequency-rank curve.

    Returns ``(recurrence_score, idf_norm)`` both in ``[0, 1]``.
    """
    if n_events <= 0 or not event_topics:
        return 0.0, 0.0
    log_n1 = math.log(n_events + 1)
    idf_denom = math.log((n_events + 1) / 2.0)  # maximum idf, attained at df == 1
    best_score = 0.0
    best_idf_norm = 0.0
    for topic in event_topics:
        df = topic_df.get(topic, 0)
        if df <= 0:
            continue
        tf_sat = math.log1p(df) / log_n1 if log_n1 > 0 else 0.0
        idf = math.log((n_events + 1) / (df + 1))
        idf_norm = (idf / idf_denom) if idf_denom > 0 else 0.0
        score = tf_sat * idf_norm
        if score > best_score:
            best_score = score
            best_idf_norm = idf_norm
    return _clamp01(best_score), _clamp01(best_idf_norm)


def _activation(positions: Sequence[int], newest_index: int, half_life: float) -> float:
    """ACT-R-style base-level activation combining recency and reinforcement."""
    if not positions:
        return 0.0
    hl = max(half_life, 1.0)
    raw = 0.0
    for pos in positions:
        age = max(0, newest_index - pos)
        raw += 2.0 ** (-age / hl)
    if raw <= 0.0:
        return 0.0
    return _clamp01(raw / (raw + 1.0))


def _temporal_dispersion(positions: Sequence[int], n_events: int) -> float:
    """Spacing-effect signal: how widely a topic's mentions span the timeline."""
    if n_events <= 1 or len(positions) < 2:
        return 0.0
    spread = (max(positions) - min(positions)) / (n_events - 1)
    return _clamp01(spread)


def _signature_topic(event_topics: Sequence[str], topic_df: Dict[str, int]) -> Optional[str]:
    """Pick the event's most-recurring topic (ties broken by name for determinism)."""
    best: Optional[str] = None
    best_df = 0
    for topic in event_topics:
        df = topic_df.get(topic, 0)
        if df > best_df or (df == best_df and best is not None and topic < best):
            best, best_df = topic, df
    return best


def _parse_ts(value: Optional[str]) -> datetime:
    if not value:
        return datetime.min
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return datetime.min


def _content_tokens(text: str) -> List[str]:
    return [tok for tok in tokenize(text) if tok not in _STOPWORDS and len(tok) >= 2]


def _normalize_topic(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _extract_capitalized_entities(text: str) -> List[str]:
    entities: List[str] = []
    for match in re.finditer(r"\b[A-Z][A-Za-z0-9]*(?:[-_ ][A-Z]?[A-Za-z0-9]+)*\b", text or ""):
        raw = match.group(0).strip(" .,:;!?")
        if raw and raw.lower() not in _STOPWORDS:
            entities.append(raw)
    return entities


def _stable_unique(values: Iterable[str]) -> List[str]:
    seen = set()
    out: List[str] = []
    for value in values:
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(value)
    return out


def _is_correction_text(text: str, tags: Sequence[str], supersedes: Optional[str]) -> bool:
    lowered = (text or "").lower()
    tag_set = {tag.lower() for tag in tags}
    return bool(
        supersedes
        or {"correction", "update", "decision"} & tag_set
        or any(marker in lowered for marker in _CORRECTION_MARKERS)
    )


def _is_open_loop_text(text: str, tags: Sequence[str], page_type: str = "") -> bool:
    lowered = (text or "").lower()
    tag_set = {tag.lower() for tag in tags}
    return bool(
        page_type == "open_loop"
        or {"open_loop", "todo", "action_item", "blocked"} & tag_set
        or any(marker in lowered for marker in _OPEN_LOOP_MARKERS)
    )


def _is_noise_text(text: str, tags: Sequence[str], importance: float = 0.5) -> bool:
    lowered = (text or "").lower()
    tag_set = {tag.lower() for tag in tags}
    return bool(
        "distractor" in tag_set
        or "noise" in tag_set
        or any(marker in lowered for marker in _NOISE_MARKERS)
        or importance < 0.15
    )


def _source_confidence_for_event(event: RawEvent) -> float:
    raw = event.metadata.get("source_confidence") if event.metadata else None
    if isinstance(raw, (int, float)):
        return _clamp01(float(raw))
    if isinstance(raw, str):
        raw_norm = raw.lower()
        if raw_norm == "high":
            return 1.0
        if raw_norm == "medium":
            return 0.65
        if raw_norm == "low":
            return 0.3
    speaker_score = 0.85 if event.speaker == "user" else 0.65
    return _clamp01((float(event.importance) + speaker_score) / 2.0)


def _source_confidence_for_page(page: WikiPage) -> float:
    return {"high": 1.0, "medium": 0.65, "low": 0.3}.get((page.confidence or "").lower(), 0.55)


def _mechanical_summary(text: str, max_chars: int = 180) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    if len(clean) <= max_chars:
        return clean
    return clean[: max_chars - 3].rstrip() + "..."


def _active_claim_summary(text: str, max_chars: int = 180) -> str:
    clean = re.sub(r"\s+", " ", (text or "").strip())
    clean = re.sub(r"\s+instead of\s+[^.;,]+", "", clean, flags=re.IGNORECASE)
    return _mechanical_summary(clean, max_chars=max_chars)


class ContextCompilerConfig(BaseModel):
    max_patch_tokens: int = 700
    include_full_history: bool = False
    min_score: float = 0.05
    max_selected_items: int = 12
    recency_half_life_events: float = 20.0
    # Selection scoring profile. "baseline" is the original, preregistered
    # behavior. "natural_law" swaps three ad-hoc score shapes for empirically
    # grounded statistical-linguistic / cognitive laws (see module helpers).
    scoring_profile: str = "baseline"
    weights: Dict[str, float] = Field(
        default_factory=lambda: {
            "frequency": 0.18,
            "recency": 0.14,
            "query_relevance": 0.30,
            "correction_priority": 0.20,
            "source_confidence": 0.10,
            "open_loop": 0.08,
            "token_cost_penalty": 0.15,
            "stale_noise_penalty": 0.60,
        }
    )
    natural_law_weights: Dict[str, float] = Field(
        default_factory=lambda: {
            "frequency": 0.20,  # surprisal-weighted saturating recurrence (TF-IDF)
            "recency": 0.16,  # ACT-R base-level activation (recency x reinforcement)
            "dispersion": 0.06,  # spacing effect (temporal spread)
            "query_relevance": 0.30,
            "correction_priority": 0.20,
            "source_confidence": 0.10,
            "open_loop": 0.08,
            "token_cost_penalty": 0.15,
            "stale_noise_penalty": 0.60,
        }
    )


class ContextEvent(BaseModel):
    source_id: str
    source_type: str
    timestamp: str = ""
    speaker: str = ""
    text: str
    project: Optional[str] = None
    tags: List[str] = Field(default_factory=list)
    status: str = "active"
    supersedes: Optional[str] = None
    confidence: float = 0.5
    importance: float = 0.5
    topics: List[str] = Field(default_factory=list)
    entities: List[str] = Field(default_factory=list)
    token_count: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class TopicRecord(BaseModel):
    topic: str
    frequency: int
    source_ids: List[str] = Field(default_factory=list)


class ContextCandidate(BaseModel):
    source_id: str
    source_type: str
    summary: str
    provenance: List[str] = Field(default_factory=list)
    topics: List[str] = Field(default_factory=list)
    entities: List[str] = Field(default_factory=list)
    final_score: float = 0.0
    score_components: Dict[str, float] = Field(default_factory=dict)
    estimated_tokens: int = 0
    selected: bool = False
    exclusion_reasons: List[str] = Field(default_factory=list)
    supersedes: Optional[str] = None
    superseded_by: Optional[str] = None
    status: str = "active"
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ContextCompilation(BaseModel):
    conversation_id: str
    user_prompt: str
    budget: int
    rendered_patch: str
    estimated_tokens: int
    selected_source_ids: List[str] = Field(default_factory=list)
    selected_context: List[ContextCandidate] = Field(default_factory=list)
    excluded_context: List[ContextCandidate] = Field(default_factory=list)
    topic_index: List[TopicRecord] = Field(default_factory=list)
    includes_full_history: bool = False
    source_count: int = 0
    metadata: Dict[str, Any] = Field(default_factory=dict)


class ContextCompiler:
    """Deterministic compiler for compact branch-and-merge prompt patches."""

    def __init__(self, config: Optional[ContextCompilerConfig | Dict[str, Any]] = None):
        if isinstance(config, ContextCompilerConfig):
            self.config = config
        else:
            self.config = ContextCompilerConfig.model_validate(config or {})

    def segment_history(
        self,
        events: Iterable[RawEvent | Dict[str, Any]],
        wiki_pages: Iterable[WikiPage | Dict[str, Any]] = (),
    ) -> List[ContextEvent]:
        segmented: List[ContextEvent] = []
        for raw in events:
            event = raw if isinstance(raw, RawEvent) else RawEvent.model_validate(raw)
            topics, entities = self._detect_topics_and_entities(
                event.content,
                project=event.project,
                tags=event.tags,
            )
            segmented.append(
                ContextEvent(
                    source_id=f"event:{event.event_id}",
                    source_type="event",
                    timestamp=event.timestamp,
                    speaker=event.speaker,
                    text=event.content,
                    project=event.project,
                    tags=list(event.tags),
                    status="active",
                    supersedes=f"event:{event.supersedes}" if event.supersedes else None,
                    confidence=_source_confidence_for_event(event),
                    importance=event.importance,
                    topics=topics,
                    entities=entities,
                    token_count=estimate_tokens(event.content),
                    metadata={
                        "event_id": event.event_id,
                        "source_type": event.source_type,
                        "source_ref": event.source_ref,
                        **(event.metadata or {}),
                    },
                )
            )
        for raw in wiki_pages:
            page = raw if isinstance(raw, WikiPage) else WikiPage.model_validate(raw)
            text = f"{page.title}\n{page.content}".strip()
            topics, entities = self._detect_topics_and_entities(
                text,
                project=page.metadata.get("project") if page.metadata else None,
                tags=[page.type],
            )
            segmented.append(
                ContextEvent(
                    source_id=f"wiki:{page.title}",
                    source_type="wiki",
                    timestamp=page.last_verified,
                    speaker="wiki",
                    text=text,
                    project=page.metadata.get("project") if page.metadata else None,
                    tags=[page.type],
                    status=page.status,
                    supersedes=f"wiki:{page.supersedes}" if page.supersedes else None,
                    confidence=_source_confidence_for_page(page),
                    importance=0.7 if page.confidence == "high" else 0.5,
                    topics=topics,
                    entities=entities,
                    token_count=estimate_tokens(text),
                    metadata={
                        "title": page.title,
                        "page_type": page.type,
                        "sources": list(page.sources),
                        **(page.metadata or {}),
                    },
                )
            )
        segmented.sort(key=lambda item: (_parse_ts(item.timestamp), item.source_id))
        return segmented

    def detect_topics(self, events: Iterable[ContextEvent]) -> List[TopicRecord]:
        sources_by_topic: Dict[str, List[str]] = {}
        for event in events:
            for topic in event.topics:
                sources_by_topic.setdefault(topic, []).append(event.source_id)
        records = [
            TopicRecord(topic=topic, frequency=len(source_ids), source_ids=_stable_unique(source_ids))
            for topic, source_ids in sources_by_topic.items()
        ]
        records.sort(key=lambda row: (-row.frequency, row.topic))
        return records

    def compile_context(
        self,
        conversation_id: str,
        user_prompt: str,
        events: Iterable[RawEvent | Dict[str, Any]],
        wiki_pages: Iterable[WikiPage | Dict[str, Any]] = (),
        budget: Optional[int] = None,
    ) -> ContextCompilation:
        effective_budget = int(budget or self.config.max_patch_tokens)
        segmented = self.segment_history(events, wiki_pages)
        topic_index = self.detect_topics(segmented)
        topic_frequency = {row.topic: row.frequency for row in topic_index}
        topic_positions: Dict[str, List[int]] = {}
        for idx, event in enumerate(segmented):
            for topic in event.topics:
                topic_positions.setdefault(topic, []).append(idx)
        superseded_by = self._build_supersession_index(segmented)
        bm25_scores = self._bm25_scores(user_prompt, segmented)
        candidates = [
            self._score_event(
                event,
                idx,
                len(segmented),
                user_prompt,
                topic_frequency,
                topic_positions,
                superseded_by,
                bm25_scores,
                effective_budget,
            )
            for idx, event in enumerate(segmented)
        ]

        ranked = sorted(
            candidates,
            key=lambda row: (row.final_score, row.score_components.get("recency", 0.0), row.source_id),
            reverse=True,
        )
        selected: List[ContextCandidate] = []
        excluded: List[ContextCandidate] = []
        for candidate in ranked:
            if candidate.exclusion_reasons:
                excluded.append(candidate)
                continue
            if candidate.final_score < self.config.min_score:
                excluded.append(candidate.model_copy(update={"exclusion_reasons": ["below_min_score"]}))
                continue
            if len(selected) >= self.config.max_selected_items:
                excluded.append(candidate.model_copy(update={"exclusion_reasons": ["over_item_limit"]}))
                continue
            trial = [item.model_copy(update={"selected": True}) for item in selected + [candidate]]
            patch = self._render_patch(
                conversation_id=conversation_id,
                selected=trial,
                excluded=excluded,
                topic_index=topic_index,
                include_full_history=self.config.include_full_history,
                all_events=segmented,
            )
            if estimate_tokens(patch) <= effective_budget:
                selected.append(candidate.model_copy(update={"selected": True}))
            else:
                excluded.append(candidate.model_copy(update={"exclusion_reasons": ["over_token_budget"]}))

        patch = self._render_patch(
            conversation_id=conversation_id,
            selected=selected,
            excluded=excluded,
            topic_index=topic_index,
            include_full_history=self.config.include_full_history,
            all_events=segmented,
        )
        if estimate_tokens(patch) > effective_budget:
            selected = []
            patch = self._render_patch(
                conversation_id=conversation_id,
                selected=selected,
                excluded=excluded,
                topic_index=topic_index,
                include_full_history=False,
                all_events=[],
                force_tiny=True,
            )

        return ContextCompilation(
            conversation_id=conversation_id,
            user_prompt=user_prompt,
            budget=effective_budget,
            rendered_patch=patch,
            estimated_tokens=estimate_tokens(patch),
            selected_source_ids=[source for item in selected for source in item.provenance],
            selected_context=selected,
            excluded_context=excluded,
            topic_index=topic_index,
            includes_full_history=self.config.include_full_history,
            source_count=len(segmented),
            metadata={
                "compiler": self.__class__.__name__,
                "scoring": "mechanical_frequency_recency_relevance_correction_confidence_open_loop_cost_stale_noise",
                "scoring_profile": self.config.scoring_profile,
                "llm_summarization_required": False,
                "raw_full_history_allowed": self.config.include_full_history,
            },
        )

    def _detect_topics_and_entities(
        self,
        text: str,
        *,
        project: Optional[str] = None,
        tags: Sequence[str] = (),
    ) -> Tuple[List[str], List[str]]:
        raw_entities: List[str] = []
        if project:
            raw_entities.append(project)
        raw_entities.extend(_extract_capitalized_entities(text))
        raw_entities.extend(tag for tag in tags if tag)
        identifier_entities = re.findall(r"\b[A-Za-z][A-Za-z0-9_./:-]{2,}\b", text or "")
        raw_entities.extend(tok for tok in identifier_entities if tok.lower() not in _STOPWORDS)
        entities = _stable_unique(raw_entities)
        topics = [_normalize_topic(entity) for entity in entities]
        topics.extend(tok for tok in _content_tokens(text) if len(tok) >= 4)
        return _stable_unique(topics), entities

    def _build_supersession_index(self, events: Sequence[ContextEvent]) -> Dict[str, str]:
        superseded_by: Dict[str, str] = {}
        for event in events:
            if event.supersedes:
                superseded_by[event.supersedes] = event.source_id

        # Deterministic fallback for "instead of X" corrections where the
        # source did not populate `supersedes`.
        for newer in events:
            if not _is_correction_text(newer.text, newer.tags, newer.supersedes):
                continue
            stale_values = [
                value.rstrip(".,;:!?")
                for value in re.findall(r"instead of\s+([^\s,;!]+)", newer.text, flags=re.IGNORECASE)
            ]
            if not stale_values:
                continue
            newer_ts = _parse_ts(newer.timestamp)
            for older in events:
                if older.source_id == newer.source_id or _parse_ts(older.timestamp) >= newer_ts:
                    continue
                if newer.project and older.project and _normalize_topic(newer.project) != _normalize_topic(older.project):
                    continue
                old_text = older.text.lower()
                if any(value and value.lower() in old_text for value in stale_values):
                    superseded_by.setdefault(older.source_id, newer.source_id)
        return superseded_by

    def _bm25_scores(self, query: str, events: Sequence[ContextEvent]) -> Dict[str, float]:
        if not events:
            return {}
        searcher = BM25Searcher()
        searcher.fit([event.source_id for event in events], [event.text for event in events])
        raw = dict(searcher.search(query, top_k=len(events)))
        max_score = max(raw.values(), default=0.0)
        return {
            source_id: (score / max_score if max_score > 0.0 else 0.0)
            for source_id, score in raw.items()
        }

    def _score_event(
        self,
        event: ContextEvent,
        index: int,
        event_count: int,
        query: str,
        topic_frequency: Dict[str, int],
        topic_positions: Dict[str, List[int]],
        superseded_by: Dict[str, str],
        bm25_scores: Dict[str, float],
        budget: int,
    ) -> ContextCandidate:
        natural_law = self.config.scoring_profile == "natural_law"
        half_life = max(self.config.recency_half_life_events, 1.0)
        newest_index = event_count - 1
        age = max(0, newest_index - index)

        query_tokens = set(_content_tokens(query))
        event_tokens = set(_content_tokens(event.text))
        overlap = len(query_tokens & event_tokens) / len(query_tokens) if query_tokens else 0.0
        query_relevance = max(overlap, bm25_scores.get(event.source_id, 0.0))
        correction = 1.0 if _is_correction_text(event.text, event.tags, event.supersedes) else 0.0
        open_loop = 1.0 if _is_open_loop_text(event.text, event.tags, event.metadata.get("page_type", "")) else 0.0
        token_cost_penalty = _clamp01(event.token_count / max(float(budget), 1.0))
        stale = event.status in {"stale", "archived", "suppressed"} or event.source_id in superseded_by
        noise = _is_noise_text(event.text, event.tags, event.importance)
        stale_noise_penalty = 1.0 if stale else (0.6 if noise else 0.0)

        dispersion = 0.0
        if natural_law:
            frequency, _idf_norm = _surprisal_recurrence(event.topics, topic_frequency, event_count)
            signature = _signature_topic(event.topics, topic_frequency)
            positions = topic_positions.get(signature, [index]) if signature else [index]
            recency = _activation(positions, newest_index, half_life)
            dispersion = _temporal_dispersion(positions, event_count)
            weights = self.config.natural_law_weights
        else:
            max_frequency = max(topic_frequency.values(), default=1)
            recurring_hits = [topic_frequency.get(topic, 0) for topic in event.topics]
            frequency = (max(recurring_hits) / max_frequency) if recurring_hits else 0.0
            recency = 2.0 ** (-age / half_life)
            weights = self.config.weights

        components = {
            "frequency": round(_clamp01(frequency), 6),
            "recency": round(_clamp01(recency), 6),
            "query_relevance": round(_clamp01(query_relevance), 6),
            "correction_priority": correction,
            "source_confidence": round(_clamp01(event.confidence), 6),
            "open_loop": open_loop,
            "token_cost_penalty": round(token_cost_penalty, 6),
            "stale_noise_penalty": stale_noise_penalty,
        }
        if natural_law:
            components["dispersion"] = round(_clamp01(dispersion), 6)
        final = (
            weights["frequency"] * components["frequency"]
            + weights["recency"] * components["recency"]
            + weights["query_relevance"] * components["query_relevance"]
            + weights["correction_priority"] * components["correction_priority"]
            + weights["source_confidence"] * components["source_confidence"]
            + weights["open_loop"] * components["open_loop"]
            + weights.get("dispersion", 0.0) * components.get("dispersion", 0.0)
            - weights["token_cost_penalty"] * components["token_cost_penalty"]
            - weights["stale_noise_penalty"] * components["stale_noise_penalty"]
        )
        reasons: List[str] = []
        if stale:
            reasons.append("stale_or_superseded")
        if event.status == "suppressed":
            reasons.append("suppressed")
        if noise and not correction:
            reasons.append("noise")
        return ContextCandidate(
            source_id=event.source_id,
            source_type=event.source_type,
            summary=_active_claim_summary(event.text) if correction else _mechanical_summary(event.text),
            provenance=[event.source_id],
            topics=event.topics[:8],
            entities=event.entities[:8],
            final_score=round(final, 6),
            score_components=components,
            estimated_tokens=event.token_count,
            selected=False,
            exclusion_reasons=reasons,
            supersedes=event.supersedes,
            superseded_by=superseded_by.get(event.source_id),
            status=event.status,
            metadata=event.metadata,
        )

    def _render_patch(
        self,
        *,
        conversation_id: str,
        selected: Sequence[ContextCandidate],
        excluded: Sequence[ContextCandidate],
        topic_index: Sequence[TopicRecord],
        include_full_history: bool,
        all_events: Sequence[ContextEvent],
        force_tiny: bool = False,
    ) -> str:
        if force_tiny:
            return "Context Merge Patch:\n- No selected context fits the configured budget.\nProvenance: (none)"

        lines = [
            "Context Merge Patch:",
            f"- conversation_id: {conversation_id}",
            "- generation: deterministic mechanical compiler",
        ]
        recurring = [row for row in topic_index if row.frequency > 1][:5]
        if recurring:
            lines.append("- recurring_topics: " + ", ".join(f"{row.topic}({row.frequency})" for row in recurring))

        if selected:
            lines.append("Selected Context:")
            for candidate in selected:
                prefix = "open_loop" if candidate.score_components.get("open_loop") else candidate.source_type
                lines.append(
                    f"- [{candidate.provenance[0]}] {prefix}; score={candidate.final_score:.3f}; "
                    f"{candidate.summary}"
                )
        else:
            lines.append("Selected Context:")
            lines.append("- (none)")

        stale = [candidate for candidate in excluded if "stale_or_superseded" in candidate.exclusion_reasons]
        if stale:
            lines.append("Avoid Stale/Superseded:")
            for candidate in stale[:6]:
                if candidate.superseded_by:
                    lines.append(f"- {candidate.source_id} superseded_by {candidate.superseded_by}")
                elif candidate.status != "active":
                    lines.append(f"- {candidate.source_id} status={candidate.status}")
                else:
                    lines.append(f"- {candidate.source_id} superseded")

        provenance = [source for candidate in selected for source in candidate.provenance]
        lines.append("Provenance: " + (", ".join(provenance) if provenance else "(none)"))

        if include_full_history:
            lines.append("Full History (explicitly enabled):")
            for event in all_events:
                lines.append(f"- [{event.source_id}] {_mechanical_summary(event.text, max_chars=220)}")
        return "\n".join(lines)
