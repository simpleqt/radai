"""Deterministic per-JIT activation scoring for context objects.

``retrieval_score`` is frozen at write time in the Object Store. At every JIT
boundary the projection pipeline recomputes an ``activation_score`` from the
current user query and the user-turn distance. No wall-clock values enter the
formula; ``created_at`` may appear only in accounting/debug output.

The projection consumes activation through private ``_activation_*`` keys on
per-request segment copies. Those copies are never written back to the Object
Store, so content addressing and stored ``Segment.score`` stay unchanged.

Design notes (plan revision R1-R10):

- Compaction removes old turn ids from ``messages``; objects whose origin turn
  is no longer present fall back to ``missing_turn_age`` (bounded fallback, not
  a true turn distance).
- An unresolved ``active_turn_id`` also falls back instead of returning 0 for
  every object, so recency decay can never silently switch off.
- Objects captured by the current request that lack a turn id are treated as
  current-turn (``fresh_oids``), so a new object is not mistaken for old
  legacy evidence.
- ``ACTIVATION_OMIT_THRESHOLD`` compares against ``relevance``
  (``retrieval x overlap_boost``), which does not include decay; old but
  highly relevant evidence is only re-ordered, never removed solely by age.
- ``activation`` is intentionally not clamped: same-turn boost may exceed 1.0.
  It is an ordering/display score, not a probability.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage

from backend.core.agents.context.ledger import (
    ContextLedger,
    _simple_terms,
    _token_overlap,
)
from backend.core.agents.context.objects import ContextObject
from backend.core.agents.context.policy import ContextPolicy

# Private segment keys attached only to projection copies. They never reach the
# Object Store payload or the content digest.
KEY_RETRIEVAL = "_activation_retrieval"
KEY_OVERLAP = "_activation_overlap"
KEY_OVERLAP_BOOST = "_activation_overlap_boost"
KEY_RELEVANCE = "_activation_relevance"
KEY_AGE_TURNS = "_activation_age_turns"
KEY_RECENCY_DECAY = "_activation_recency_decay"
KEY_TURN_BOOST = "_activation_turn_boost"
KEY_ACTIVATION = "_activation"
KEY_MISSING_TURN_ID = "_activation_missing_turn_id"
KEY_ACTIVE_TURN_UNRESOLVED = "_activation_active_turn_unresolved"
KEY_OMIT_THRESHOLD = "_activation_omit_threshold"

_ACTIVATION_KEYS: tuple[str, ...] = (
    KEY_RETRIEVAL,
    KEY_OVERLAP,
    KEY_OVERLAP_BOOST,
    KEY_RELEVANCE,
    KEY_AGE_TURNS,
    KEY_RECENCY_DECAY,
    KEY_TURN_BOOST,
    KEY_ACTIVATION,
    KEY_MISSING_TURN_ID,
    KEY_ACTIVE_TURN_UNRESOLVED,
    KEY_OMIT_THRESHOLD,
)


@dataclass(frozen=True)
class ActivationBreakdown:
    retrieval: float
    overlap: float
    overlap_boost: float
    relevance: float
    age_turns: float
    recency_decay: float
    turn_boost: float
    activation: float
    missing_turn_id: bool = False
    active_turn_unresolved: bool = False


def list_regular_user_turn_ids(messages: list) -> list[str]:
    """Return ordered unique ids of real user turns (compaction excluded)."""

    ids: list[str] = []
    seen: set[str] = set()
    for message in messages or []:
        if not isinstance(message, HumanMessage):
            continue
        if ContextLedger._is_regular_user_message(message):
            message_id = str(getattr(message, "id", None) or "").strip()
            if message_id and message_id not in seen:
                seen.add(message_id)
                ids.append(message_id)
    return ids


def count_age_turns(
    *,
    origin_turn_id: str,
    active_turn_id: str,
    messages: list,
    turn_ids: list[str] | None = None,
    turn_positions: dict[str, int] | None = None,
    missing_turn_age: float = 3.0,
    fresh_origin: bool = False,
) -> tuple[float, bool, bool]:
    """Return ``(age_turns, origin_missing, active_unresolved)``.

    Compaction removes old turn ids from ``messages``: a missing origin turn
    falls back to ``missing_turn_age`` (R1). An unresolved active turn also
    falls back and is flagged instead of silently disabling decay (R2). A
    missing origin turn on an object captured by this request is treated as
    current-turn (R3).
    """

    if turn_ids is None:
        turn_ids = list_regular_user_turn_ids(messages or [])
    positions = (
        turn_positions
        if turn_positions is not None
        else {turn_id: index for index, turn_id in enumerate(turn_ids)}
    )
    origin = str(origin_turn_id or "").strip()
    active = str(active_turn_id or "").strip()
    if not origin or origin not in positions:
        if fresh_origin:
            return 0.0, False, False
        return float(missing_turn_age), True, False
    if not active or active not in positions:
        return float(missing_turn_age), False, True
    age = max(0, positions[active] - positions[origin])
    return float(age), False, False


def compute_activation(
    *,
    retrieval_score: float,
    segment_text: str,
    segment_title: str,
    origin_query: str,
    current_query: str,
    origin_turn_id: str,
    active_turn_id: str,
    messages: list,
    turn_ids: list[str] | None = None,
    turn_positions: dict[str, int] | None = None,
    term_cache: dict[str, list[str]] | None = None,
    term_cache_key: str = "",
    fresh_origin: bool = False,
    overlap_floor: float = 0.15,
    half_life_turns: float = 2.0,
    turn_boost_value: float = 1.25,
    missing_turn_age: float = 3.0,
) -> ActivationBreakdown:
    """Compute one deterministic activation breakdown for a segment."""

    retrieval = max(0.0, min(1.0, float(retrieval_score or 0.0)))
    cache = term_cache if term_cache is not None else {}

    def cached_terms(key: str, text: str) -> list[str]:
        if key not in cache:
            cache[key] = _simple_terms(text)
        return cache[key]

    current_terms = cached_terms(f"query:{current_query}", str(current_query or ""))
    title_text = f"{str(segment_title or '')} {str(segment_text or '')}"
    text_key = f"text:{term_cache_key or title_text}"
    text_terms = cached_terms(text_key, title_text)
    origin_terms = cached_terms(f"query:{origin_query}", str(origin_query or ""))
    overlap = max(
        _token_overlap(
            str(current_query or ""),
            title_text,
            query_terms=current_terms,
            text_terms=text_terms,
        ),
        _token_overlap(
            str(current_query or ""),
            str(origin_query or ""),
            query_terms=current_terms,
            text_terms=origin_terms,
        ),
    )
    overlap_boost = float(overlap_floor) + (1.0 - float(overlap_floor)) * overlap
    relevance = retrieval * overlap_boost
    age_turns, missing_turn_id, active_unresolved = count_age_turns(
        origin_turn_id=origin_turn_id,
        active_turn_id=active_turn_id,
        messages=messages or [],
        turn_ids=turn_ids,
        turn_positions=turn_positions,
        missing_turn_age=missing_turn_age,
        fresh_origin=fresh_origin,
    )
    if age_turns <= 0:
        recency_decay = 1.0
    else:
        recency_decay = 0.5 ** (age_turns / max(0.1, float(half_life_turns)))
    # R3: fresh-oid boost only when origin.turn_id is missing. An oid that was
    # captured this request but already carries a real turn_id must use the
    # normal same-turn equality check, otherwise old turn ids would get boost.
    origin = str(origin_turn_id or "").strip()
    active = str(active_turn_id or "").strip()
    boosted = (bool(origin) and bool(active) and origin == active) or (
        fresh_origin and bool(active) and not origin
    )
    turn_boost = float(turn_boost_value) if boosted else 1.0
    activation = relevance * recency_decay * turn_boost
    return ActivationBreakdown(
        retrieval=retrieval,
        overlap=overlap,
        overlap_boost=overlap_boost,
        relevance=relevance,
        age_turns=age_turns,
        recency_decay=recency_decay,
        turn_boost=turn_boost,
        activation=activation,
        missing_turn_id=missing_turn_id,
        active_turn_unresolved=active_unresolved,
    )


def _activation_defaults(policy: ContextPolicy | None) -> dict[str, float]:
    if policy is None:
        return {
            "overlap_floor": 0.15,
            "half_life_turns": 2.0,
            "turn_boost": 1.25,
            "missing_turn_age": 3.0,
        }
    return {
        "overlap_floor": policy.activation_overlap_floor,
        "half_life_turns": policy.activation_half_life_turns,
        "turn_boost": policy.activation_turn_boost,
        "missing_turn_age": policy.activation_missing_turn_age,
    }


def apply_activation_to_objects(
    objects: list[ContextObject],
    *,
    current_query: str,
    messages: list,
    active_turn_id: str = "",
    fresh_oids: set[str] | None = None,
    policy: ContextPolicy | None = None,
    term_cache: dict[str, list[str]] | None = None,
) -> list[ContextObject]:
    """Return projection copies with ``segment.score`` replaced by activation.

    The Object Store payloads are never touched: only per-request copies are
    mutated, and the activation breakdown rides on private ``_activation_*``
    segment keys that are ignored by content addressing. When activation
    scoring is disabled, objects are returned unchanged (rollback baseline).

    ``term_cache`` may be a request-scoped dict: sids embed a content digest,
    so cached CJK n-gram term sets stay valid across repeated JIT projections
    of the same request instead of being re-tokenized per model call.
    """

    if policy is not None and not policy.activation_scoring:
        return list(objects or [])
    if not objects:
        return []
    params = _activation_defaults(policy)
    fresh = set(fresh_oids or set())
    turn_ids = list_regular_user_turn_ids(messages or [])
    turn_positions = {
        turn_id: index for index, turn_id in enumerate(turn_ids)
    }
    if term_cache is None:
        term_cache = {}
    activated: list[ContextObject] = []
    for obj in objects or []:
        if not isinstance(obj, dict):
            continue
        copy: dict[str, Any] = {**obj}
        segments: list[Any] = []
        for segment in obj.get("segments") or []:
            if not isinstance(segment, dict):
                segments.append(segment)
                continue
            seg: dict[str, Any] = dict(segment)
            breakdown = compute_activation(
                retrieval_score=float(segment.get("score") or 0.0),
                segment_text=str(segment.get("text") or ""),
                segment_title=str(segment.get("title") or ""),
                origin_query=str((obj.get("index") or {}).get("query") or ""),
                current_query=str(current_query or ""),
                origin_turn_id=str((obj.get("origin") or {}).get("turn_id") or ""),
                active_turn_id=str(active_turn_id or ""),
                messages=messages or [],
                turn_ids=turn_ids,
                turn_positions=turn_positions,
                term_cache=term_cache,
                term_cache_key=str(segment.get("sid") or ""),
                fresh_origin=bool(str(obj.get("oid") or "") in fresh),
                overlap_floor=params["overlap_floor"],
                half_life_turns=params["half_life_turns"],
                turn_boost_value=params["turn_boost"],
                missing_turn_age=params["missing_turn_age"],
            )
            seg["score"] = breakdown.activation
            seg[KEY_RETRIEVAL] = breakdown.retrieval
            seg[KEY_OVERLAP] = breakdown.overlap
            seg[KEY_OVERLAP_BOOST] = breakdown.overlap_boost
            seg[KEY_RELEVANCE] = breakdown.relevance
            seg[KEY_AGE_TURNS] = breakdown.age_turns
            seg[KEY_RECENCY_DECAY] = breakdown.recency_decay
            seg[KEY_TURN_BOOST] = breakdown.turn_boost
            seg[KEY_ACTIVATION] = breakdown.activation
            seg[KEY_MISSING_TURN_ID] = breakdown.missing_turn_id
            seg[KEY_ACTIVE_TURN_UNRESOLVED] = breakdown.active_turn_unresolved
            seg[KEY_OMIT_THRESHOLD] = (
                policy.activation_omit_threshold if policy is not None else 0.08
            )
            segments.append(seg)
        copy["segments"] = segments
        activated.append(copy)
    return activated


__all__ = [
    "ActivationBreakdown",
    "KEY_ACTIVE_TURN_UNRESOLVED",
    "KEY_ACTIVATION",
    "KEY_AGE_TURNS",
    "KEY_MISSING_TURN_ID",
    "KEY_OMIT_THRESHOLD",
    "KEY_RECENCY_DECAY",
    "KEY_RELEVANCE",
    "KEY_RETRIEVAL",
    "KEY_TURN_BOOST",
    "apply_activation_to_objects",
    "compute_activation",
    "count_age_turns",
    "list_regular_user_turn_ids",
]
