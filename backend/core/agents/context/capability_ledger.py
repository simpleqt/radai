"""Per-turn capability ledger: handles, grants, dedup and token pool."""

from __future__ import annotations

import os
import secrets
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Literal, TypedDict, cast

from backend.core.agents.context.activation import (
    KEY_ACTIVATION,
    KEY_ACTIVE_TURN_UNRESOLVED,
    KEY_AGE_TURNS,
    KEY_MISSING_TURN_ID,
    KEY_OVERLAP,
    KEY_OVERLAP_BOOST,
    KEY_RECENCY_DECAY,
    KEY_RELEVANCE,
    KEY_RETRIEVAL,
    KEY_TURN_BOOST,
)
from backend.core.agents.context.object_store import ContextObjectStore
from backend.core.agents.context.objects import (
    ContextObject,
    ProjectionReceipt,
)
from backend.core.agents.context.projection import (
    ProjectionCompiler,
    ProjectionResult,
)


def _default_token_pool() -> int:
    try:
        return max(4096, int(os.getenv("CONTEXT_OBJECT_TOKEN_POOL", "96000")))
    except (TypeError, ValueError):
        return 96000


@dataclass(frozen=True)
class Grant:
    oid: str
    allowed_sids: frozenset[str] | None

    def allows(self, sid: str | None) -> bool:
        if self.allowed_sids is None:
            return True
        return str(sid or "") in self.allowed_sids


class LedgerRow(TypedDict, total=False):
    """One citation-registry row rendered into every JIT pack."""

    n: int
    sid: str
    title: str
    locator: str
    score: float
    retrieval_score: float
    activation_score: float
    age_turns: float
    fidelity: Literal["full", "compressed", "headline", "omitted"]


class CapabilityLedger:
    """Request-scoped mapping from opaque handles to context-object grants."""

    def __init__(
        self,
        *,
        turn_id: str = "",
        token_pool: int | None = None,
        token_counter: Any | None = None,
        compiler: ProjectionCompiler | None = None,
        object_store: ContextObjectStore | None = None,
        user_id: str | None = None,
        thread_id: str | None = None,
        max_tokens_per_read: int = 2000,
        activation_enabled: bool = False,
        activation_omit_threshold: float = 0.08,
        sticky_window_calls: int = 1,
    ) -> None:
        self._turn_id = str(turn_id or "")
        self._token_pool = max(
            4096, int(token_pool if token_pool is not None else _default_token_pool())
        )
        self._used_tokens = 0
        self._jit_body_tokens = 0
        self._navigator_tokens = 0
        self._handles: dict[str, Grant] = {}
        self._oid_to_handle: dict[str, str] = {}
        self._sid_to_oid: dict[str, str] = {}
        self._projected_sids: dict[str, str] = {}
        # 上一次 JIT(charge="replace")渲染出正文的 sid 集合。JIT pack 每次
        # 模型调用整体替换,不驻留历史:这些 sid 的可见正文随替换消失,必须
        # 允许下一次 JIT 重新渲染;否则第 2+ 次模型调用/溢出重试的 pack 只剩
        # 指向已消失 pack 的悬空指针,台账 fidelity 还停留在 full 谎称
        # "正文已在上文"。工具消息渲染的 sid 不在此列(其正文驻留历史)。
        self._jit_projected_sids: set[str] = set()
        self._receipts: list[ProjectionReceipt] = []
        self._citation_numbers: dict[str, int] = {}
        self._row_metadata: dict[str, dict[str, Any]] = {}
        self._citation_numbers_published = False
        self._published_sids: frozenset[str] = frozenset()
        self._sticky_window_calls = max(1, int(sticky_window_calls or 1))
        self._published_sid_windows: list[frozenset[str]] = []
        self._navigator_read_sids: dict[str, str] = {}
        self._ledger_tokens = 0
        self._compiler = compiler or ProjectionCompiler(token_counter=token_counter)
        self._object_store = object_store
        self._user_id = str(user_id or "").strip() or None
        self._thread_id = str(thread_id or "").strip() or None
        self._max_tokens_per_read = max(128, int(max_tokens_per_read))
        self._activation_enabled = bool(activation_enabled)
        self._activation_omit_threshold = max(0.0, float(activation_omit_threshold))
        self._unbound = False
        self._navigator_calls = 0
        self._dedup_hits = 0
        self._denied = 0
        # 并行工具捕获(to_thread)与事件循环上的 JIT/navigator 共用一份台账,
        # 引用编号分配与 token 计费的读-改-写必须互斥(可重入:project 内部
        # 会再进 mint/_register_objects)。
        self._mutation_lock = threading.RLock()

    @property
    def turn_id(self) -> str:
        return self._turn_id

    def adopt_turn_id(self, turn_id: str) -> None:
        """Backfill the active turn id after deferred creation.

        流入口的 capability_scope 在拿到 messages 之前预创建台账,此时
        _active_turn_id([]) 只能返回空串。若不回填,本请求捕获的所有 COS
        对象 origin.turn_id 恒空,跨请求激活时间衰减永远走 MISSING_TURN_AGE
        常数回退。只允许从空到非空的一次性回填,不改写已存在的轮次。
        """

        if not self._turn_id and str(turn_id or "").strip():
            self._turn_id = str(turn_id).strip()

    @property
    def token_pool(self) -> int:
        return self._token_pool

    @property
    def remaining_tokens(self) -> int:
        return max(0, self._token_pool - self._used_tokens)

    @property
    def jit_body_tokens(self) -> int:
        return self._jit_body_tokens

    @property
    def navigator_tokens(self) -> int:
        return self._navigator_tokens

    @property
    def published_sids(self) -> frozenset[str]:
        """Sids already shown to the model in a rendered ledger (sticky set)."""

        return self._published_sids

    @property
    def navigator_read_sids(self) -> dict[str, str]:
        """Sids already rendered by ``context_read`` in this request."""

        return dict(self._navigator_read_sids)

    def mark_navigator_sids_read(self, sids: Iterable[str]) -> None:
        """Record sids whose body was returned by the navigator this request."""

        for sid in sids:
            value = str(sid or "").strip()
            if value:
                self._navigator_read_sids[value] = "read"

    def note_history_compaction(self) -> None:
        """Invalidate "body lives in history" bookkeeping after compaction.

        Tool-message projections and navigator reads leave their body text in
        conversation history, so their sids are deduplicated into pointers on
        later renders. History compaction replaces those messages with a
        summary (and truncates retained tool bodies), breaking that premise:
        without this reset the ledger keeps claiming 正文已在上文 while every
        later render emits pointers to removed text, leaving the evidence
        unreachable. JIT-rendered sids are excluded — the JIT pack is rebuilt
        per model call and runs its own stale sweep on replace.
        """

        with self._mutation_lock:
            jit_sids = set(self._jit_projected_sids)
            for sid in list(self._projected_sids):
                if sid in jit_sids:
                    continue
                self._projected_sids.pop(sid, None)
                meta = self._row_metadata.get(sid)
                if meta is not None and str(meta.get("fidelity") or "omitted") in {
                    "full",
                    "compressed",
                    "headline",
                }:
                    meta["fidelity"] = "omitted"
            self._navigator_read_sids.clear()

    def count_tokens(self, text: str) -> int:
        """Count tokens with the shared projection counter."""

        counter = getattr(self._compiler, "token_counter", None)
        if counter is None or not callable(getattr(counter, "count", None)):
            return max(1, len(str(text or "")) // 2)
        return max(1, int(counter.count(text)))

    def clear_jit_charge(self) -> None:
        """Remove the current JIT body charge (e.g. when no objects remain)."""

        with self._mutation_lock:
            self._used_tokens = max(0, self._used_tokens - self._jit_body_tokens)
            self._jit_body_tokens = 0

    @property
    def receipts(self) -> list[ProjectionReceipt]:
        return list(self._receipts)

    @property
    def compiler(self) -> ProjectionCompiler:
        return self._compiler

    @property
    def object_store(self) -> ContextObjectStore | None:
        return self._object_store

    @property
    def user_id(self) -> str | None:
        return self._user_id

    @property
    def thread_id(self) -> str | None:
        return self._thread_id

    @property
    def max_tokens_per_read(self) -> int:
        return self._max_tokens_per_read

    def mint(self, oid: str, allowed_sids: list[str] | None = None) -> str:
        with self._mutation_lock:
            return self._mint_locked(oid, allowed_sids)

    def _mint_locked(self, oid: str, allowed_sids: list[str] | None = None) -> str:
        oid = str(oid or "").strip()
        if not oid:
            raise ValueError("cannot mint a handle for an empty oid")
        allowed = (
            [str(sid) for sid in allowed_sids if str(sid).strip()]
            if allowed_sids is not None
            else []
        )
        existing = self._oid_to_handle.get(oid)
        if existing is not None:
            for sid in allowed:
                self._sid_to_oid[sid] = oid
            return existing
        handle = f"obj:{secrets.token_hex(4)}"
        self._handles[handle] = Grant(
            oid=oid,
            allowed_sids=(frozenset(allowed) if allowed else None),
        )
        self._oid_to_handle[oid] = handle
        for sid in allowed:
            self._sid_to_oid[sid] = oid
        return handle

    def resolve(self, handle: str) -> Grant | None:
        grant = self._handles.get(str(handle or "").strip())
        if grant is None:
            self._denied += 1
        return grant

    def resolve_sid(self, sid: str) -> Grant | None:
        """Resolve a stable segment id through this turn's authorization table."""

        oid = self._sid_to_oid.get(str(sid or "").strip())
        if oid is None:
            self._denied += 1
            return None
        handle = self._oid_to_handle.get(oid)
        if handle is None:
            self._denied += 1
            return None
        grant = self._handles.get(handle)
        if grant is None:
            self._denied += 1
        return grant

    def handle_for(self, oid: str) -> str | None:
        return self._oid_to_handle.get(str(oid or "").strip())

    def citation_number(self, sid: str) -> int | None:
        return self._citation_numbers.get(str(sid or "").strip())

    def ledger_rows(self) -> list[LedgerRow]:
        """Return the request-stable citation registry sorted by [n].

        Runs under the mutation lock: parallel tool captures register new
        sids from worker threads, and an unlocked iteration raced them with
        ``RuntimeError: dictionary changed size during iteration`` (navigator
        error paths build ledger excerpts on those same worker threads).
        """

        with self._mutation_lock:
            return self._ledger_rows_locked()

    def _ledger_rows_locked(self) -> list[LedgerRow]:
        rows: list[LedgerRow] = []
        for sid, number in self._citation_numbers.items():
            meta = self._row_metadata.get(sid) or {}
            raw_fidelity = str(meta.get("fidelity") or "omitted")
            row = LedgerRow(
                n=int(number),
                sid=str(sid),
                title=str(meta.get("title") or sid),
                locator=str(meta.get("locator") or ""),
                score=float(meta.get("score") or 0.0),
                fidelity=cast(
                    Literal["full", "compressed", "headline", "omitted"],
                    (
                        raw_fidelity
                        if raw_fidelity in {"full", "compressed", "headline", "omitted"}
                        else "omitted"
                    ),
                ),
            )
            if self._activation_enabled and meta.get(KEY_ACTIVATION) is not None:
                row["retrieval_score"] = float(
                    meta.get(KEY_RETRIEVAL) or meta.get("score") or 0.0
                )
                row["activation_score"] = float(meta.get(KEY_ACTIVATION) or 0.0)
                row["age_turns"] = float(meta.get(KEY_AGE_TURNS) or 0.0)
            rows.append(row)
        return sorted(rows, key=lambda row: int(row.get("n") or 0))

    def record_ledger_tokens(self, tokens: int) -> None:
        with self._mutation_lock:
            amount = max(0, int(tokens))
            delta = amount - self._ledger_tokens
            self._ledger_tokens = amount
            self._used_tokens = max(0, self._used_tokens + delta)

    def mark_citation_numbers_published(self) -> None:
        """Freeze citation numbers once the ledger has been shown to the model.

        Numbering freeze is separate from the sticky set: sticky sids are
        recorded by :meth:`record_published_sids` with the actual JIT-selected
        candidates, so an ever-growing citation registry cannot inflate it.
        """

        self._citation_numbers_published = True

    def record_published_sids(self, sids: Iterable[str]) -> None:
        """Add JIT-rendered sids to the request sticky set.

        Only sids actually selected into a rendered JIT pack are recorded
        (bounded by the per-call top-K), and only the most recent
        ``sticky_window_calls`` renders are kept.  The window keeps
        recently-published evidence re-renderable without letting a full-request
        union starve new evidence; sids outside the window remain readable
        because the request authorization table only grows.
        """

        added = frozenset(str(sid) for sid in sids if str(sid or "").strip())
        if added:
            with self._mutation_lock:
                self._published_sid_windows.append(added)
                self._published_sid_windows = self._published_sid_windows[
                    -self._sticky_window_calls:
                ]
                self._published_sids = frozenset().union(
                    *self._published_sid_windows
                )

    def register_objects(self, objects: list[ContextObject]) -> None:
        """Register object sids/grants and citation numbers before rendering."""

        with self._mutation_lock:
            self._register_objects(objects)

    def reassign_citation_numbers(self) -> None:
        """Reassign citation numbers by score order only before first publication.

        Tool-message projections register rows incrementally; the JIT pack is
        the first surface where numbers are visible, so it may renumber by
        global score before the first render. Once numbers have been published,
        new rows are appended after the existing numbers and existing numbers
        never move.
        """

        with self._mutation_lock:
            if self._citation_numbers_published:
                return
            ordered = sorted(
                (
                    (sid, float(meta.get("score") or 0.0))
                    for sid, meta in self._row_metadata.items()
                ),
                key=lambda item: (-item[1], item[0]),
            )
            self._citation_numbers = {
                sid: number for number, (sid, _score) in enumerate(ordered, start=1)
            }

    def spend(self, tokens: int) -> bool:
        with self._mutation_lock:
            amount = max(0, int(tokens))
            if amount > self.remaining_tokens:
                return False
            self._used_tokens += amount
            return True

    def reserve(self, tokens: int) -> int:
        with self._mutation_lock:
            amount = max(0, int(tokens))
            granted = min(amount, self.remaining_tokens)
            self._used_tokens += granted
            self._navigator_tokens += granted
            return granted

    def project(
        self,
        objects: list[ContextObject],
        *,
        budget_tokens: int,
        query: str = "",
        requested_sids: list[str] | None = None,
        reason: str = "budget",
        charge: Literal["accumulate", "replace"] = "accumulate",
        budget_weights: list[float] | None = None,
    ) -> ProjectionResult | None:
        """Mint handles, project under the shared pool and record a receipt.

        全程持台账互斥锁:工具结果捕获在线程里执行(awrap_tool_call →
        to_thread),LangGraph 对单条 AI 消息的多个 tool_calls 并行,两个
        捕获并发进入时,引用编号的 len+1 读-改-写会把同一个 [n] 发给两个
        sid(编号冻结后永不纠正),token 计数也会丢更新。
        """

        with self._mutation_lock:
            return self._project_impl(
                objects,
                budget_tokens=budget_tokens,
                query=query,
                requested_sids=requested_sids,
                reason=reason,
                charge=charge,
                budget_weights=budget_weights,
            )

    def _project_impl(
        self,
        objects: list[ContextObject],
        *,
        budget_tokens: int,
        query: str = "",
        requested_sids: list[str] | None = None,
        reason: str = "budget",
        charge: Literal["accumulate", "replace"] = "accumulate",
        budget_weights: list[float] | None = None,
    ) -> ProjectionResult | None:
        if not objects:
            return None
        self._register_objects(objects)
        handles: dict[str, str] = {}
        for obj in objects:
            oid = str(obj.get("oid") or "").strip()
            if not oid:
                continue
            segment_sids = [
                str(segment.get("sid") or "")
                for segment in obj.get("segments") or []
                if str(segment.get("sid") or "").strip()
            ]
            handles[oid] = self.mint(oid, segment_sids)
        # A projection can span several objects; the compiler currently renders
        # one handle per call, so project object-by-object and merge output.
        budget = max(0, int(budget_tokens))
        weights = list(budget_weights or [])
        if weights and len(weights) != len(objects):
            weights = []
        total_weight = sum(max(0.0, float(weight)) for weight in weights)
        if not weights or total_weight <= 0:
            per_object_budget = max(1, budget // max(1, len(objects)))
        else:
            per_object_budget = None
        rendered_blocks: list[str] = []
        entries: list[Any] = []
        spent = 0
        omitted: list[str] = []
        # JIT 重投影(charge="replace")把上一轮 JIT 的 sid 从 seen 集中撤销:
        # 旧 pack 已被整体替换,这些正文不再有任何可见渲染面,再按"已渲染"
        # 指针化就是悬空指针。工具消息渲染的 sid 不撤销(正文驻留历史)。
        replaced_jit_sids = (
            set(self._jit_projected_sids) if charge == "replace" else set()
        )
        rendered_body_fidelities = {"full", "compressed", "headline"}
        current_jit_rendered: set[str] = set()
        for index, obj in enumerate(objects):
            oid = str(obj.get("oid") or "").strip()
            if not oid:
                continue
            handle = handles[oid]
            object_budget = (
                max(1, int(budget * max(0.0, float(weights[index])) / total_weight))
                if per_object_budget is None
                else per_object_budget
            )
            result = self._compiler.project(
                [obj],
                handle=handle,
                budget_tokens=object_budget,
                query=query,
                turn_id=self._turn_id,
                projected_sids=set(self._projected_sids) - replaced_jit_sids,
                requested_sids=requested_sids,
                reason=reason,
            )
            rendered_blocks.append(result.rendered)
            entries.extend(result.entries)
            omitted.extend(result.receipt.get("omitted_sids") or [])
            for entry in result.entries:
                sid = str(entry.get("sid") or "")
                if sid:
                    fidelity = str(entry.get("fidelity") or "omitted")
                    self._projected_sids[sid] = fidelity
                    if fidelity in rendered_body_fidelities:
                        # 同一调用内后续对象不再重复渲染该 sid。
                        replaced_jit_sids.discard(sid)
                        if charge == "replace":
                            current_jit_rendered.add(sid)
                        else:
                            # 工具消息渲染出正文后,该 sid 的可见性不再依赖
                            # JIT pack,后续 replace 不应再撤销它。
                            self._jit_projected_sids.discard(sid)
                    meta = self._row_metadata.get(sid)
                    if meta is not None:
                        previous = str(meta.get("fidelity") or "omitted")
                        if fidelity != "omitted" or previous not in {
                            "full",
                            "compressed",
                            "headline",
                        }:
                            meta["fidelity"] = fidelity
                    if entry.get("fidelity") == "omitted":
                        self._dedup_hits += 1
            for sid in result.receipt.get("omitted_sids") or []:
                meta = self._row_metadata.get(str(sid))
                if meta is not None and str(meta.get("fidelity") or "omitted") not in {
                    "full",
                    "compressed",
                    "headline",
                }:
                    meta["fidelity"] = "omitted"
            spent += result.tokens_spent
            if charge != "replace":
                self._used_tokens += result.tokens_spent
            self._receipts.append(result.receipt)
        if charge == "replace":
            # 上一轮 JIT 渲染过、本轮没有再渲染出正文的 sid:它们已无可见
            # 渲染面。回落登记与台账 fidelity,让台账诚实地给出
            # context_read 指引,并允许之后的渲染面重新注入正文。
            for stale_sid in set(self._jit_projected_sids) - current_jit_rendered:
                self._projected_sids.pop(stale_sid, None)
                meta = self._row_metadata.get(stale_sid)
                if meta is not None and str(
                    meta.get("fidelity") or "omitted"
                ) in rendered_body_fidelities:
                    meta["fidelity"] = "omitted"
            self._jit_projected_sids = current_jit_rendered
            self._used_tokens = max(
                0,
                self._used_tokens - self._jit_body_tokens + spent,
            )
            self._jit_body_tokens = spent
        receipt = ProjectionReceipt(
            oid=",".join(handles),
            handle=",".join(handles.values()),
            turn_id=self._turn_id,
            entries=entries,
            tokens_spent=spent,
            omitted_sids=list(dict.fromkeys(omitted)),
            reason=reason,
        )
        return ProjectionResult(
            rendered="\n".join(rendered_blocks),
            receipt=receipt,
            entries=entries,
            tokens_spent=spent,
        )

    def record_navigator_call(self, *, dedup_hit: bool = False) -> None:
        self._navigator_calls += 1
        if dedup_hit:
            self._dedup_hits += 1

    def accounting(self) -> dict[str, Any]:
        full = sum(
            1
            for receipt in self._receipts
            for entry in receipt.get("entries") or []
            if entry.get("fidelity") == "full"
        )
        compressed = sum(
            1
            for receipt in self._receipts
            for entry in receipt.get("entries") or []
            if entry.get("fidelity") == "compressed"
        )
        headline = sum(
            1
            for receipt in self._receipts
            for entry in receipt.get("entries") or []
            if entry.get("fidelity") == "headline"
        )
        omitted = sum(
            len(receipt.get("omitted_sids") or []) for receipt in self._receipts
        )
        total = full + compressed + headline + omitted
        body_counts: dict[str, int] = {}
        for receipt in self._receipts:
            for entry in receipt.get("entries") or []:
                if entry.get("fidelity") == "omitted":
                    continue
                sid = str(entry.get("sid") or "")
                body_counts[sid] = body_counts.get(sid, 0) + 1
        duplicate_bodies = sum(max(0, count - 1) for count in body_counts.values())
        total_bodies = sum(body_counts.values())
        duplicate_segment_rate = (
            round(duplicate_bodies / total_bodies, 4) if total_bodies else 0.0
        )
        activation_rows = list(self._row_metadata.values())
        activation_count = len(activation_rows)
        mean_activation = (
            round(
                sum(float(row.get(KEY_ACTIVATION) or 0.0) for row in activation_rows)
                / activation_count,
                4,
            )
            if activation_count
            else 0.0
        )
        mean_retrieval = (
            round(
                sum(
                    float(row.get(KEY_RETRIEVAL) or row.get("score") or 0.0)
                    for row in activation_rows
                )
                / activation_count,
                4,
            )
            if activation_count
            else 0.0
        )
        threshold = self._activation_omit_threshold

        def _relevance_of(row: dict[str, Any]) -> float | None:
            if KEY_RELEVANCE not in row:
                return None
            return float(row.get(KEY_RELEVANCE) or 0.0)

        below_threshold = sum(
            1
            for row in activation_rows
            if (rel := _relevance_of(row)) is not None and rel < threshold
        )
        activation = {
            "enabled": self._activation_enabled,
            "segments": activation_count if self._activation_enabled else 0,
            "mean_activation": mean_activation if self._activation_enabled else 0.0,
            "mean_retrieval": mean_retrieval if self._activation_enabled else 0.0,
            "same_turn_segments": (
                sum(
                    1
                    for row in activation_rows
                    if float(row.get(KEY_AGE_TURNS) or 0.0) <= 0
                )
                if self._activation_enabled
                else 0
            ),
            "decayed_segments": (
                sum(
                    1
                    for row in activation_rows
                    if float(row.get(KEY_AGE_TURNS) or 0.0) > 0
                )
                if self._activation_enabled
                else 0
            ),
            "below_threshold": below_threshold if self._activation_enabled else 0,
            "omitted_by_threshold": (
                sum(
                    1
                    for row in activation_rows
                    if (rel := _relevance_of(row)) is not None
                    and rel < threshold
                    and str(row.get("fidelity") or "omitted") == "omitted"
                )
                if self._activation_enabled
                else 0
            ),
            "turn_boosted": (
                sum(
                    1
                    for row in activation_rows
                    if float(row.get(KEY_TURN_BOOST) or 1.0) > 1.0
                )
                if self._activation_enabled
                else 0
            ),
            "missing_turn_id": (
                sum(1 for row in activation_rows if row.get(KEY_MISSING_TURN_ID))
                if self._activation_enabled
                else 0
            ),
            "active_turn_unresolved": (
                sum(1 for row in activation_rows if row.get(KEY_ACTIVE_TURN_UNRESOLVED))
                if self._activation_enabled
                else 0
            ),
        }
        return {
            "turn_id": self._turn_id,
            "token_pool": self._token_pool,
            "tokens_spent": self._used_tokens,
            "remaining_tokens": self.remaining_tokens,
            "jit_body_tokens": self._jit_body_tokens,
            "navigator_tokens": self._navigator_tokens,
            "ledger_tokens": self._ledger_tokens,
            "ledger_rows": len(self._citation_numbers),
            "handles": len(self._handles),
            "segments": total,
            "full": full,
            "compressed": compressed,
            "headline": headline,
            "omitted": omitted,
            "navigator": {
                "calls": self._navigator_calls,
                "dedup_hits": self._dedup_hits,
                "denied": self._denied,
            },
            "evidence_reachability": (
                round((total - omitted) / total, 4) if total else 1.0
            ),
            "duplicate_segment_rate": duplicate_segment_rate,
            "activation": activation,
        }

    _ACTIVATION_REFRESH_KEYS = (
        KEY_RETRIEVAL,
        KEY_ACTIVATION,
        KEY_OVERLAP,
        KEY_OVERLAP_BOOST,
        KEY_RELEVANCE,
        KEY_AGE_TURNS,
        KEY_RECENCY_DECAY,
        KEY_TURN_BOOST,
    )

    def _refresh_activation_metadata(self, meta: dict[str, Any], segment: dict) -> None:
        """Update the displayed activation breakdown on re-registration.

        Activation is recomputed per JIT projection against the current query
        and turn distance; without this refresh the ledger's ``act=``/``ret=``
        columns and the pre-publication numbering stayed frozen at the values
        of the first registration (usually the capture-time projection),
        drifting away from the scores that actually drive selection ordering.
        Fidelity and citation numbers are intentionally left untouched.
        """

        if segment.get(KEY_ACTIVATION) is None:
            return
        meta["score"] = float(segment.get("score") or 0.0)
        for key in self._ACTIVATION_REFRESH_KEYS:
            value = segment.get(key)
            if value is not None:
                meta[key] = float(value)
        meta[KEY_MISSING_TURN_ID] = bool(segment.get(KEY_MISSING_TURN_ID))
        meta[KEY_ACTIVE_TURN_UNRESOLVED] = bool(
            segment.get(KEY_ACTIVE_TURN_UNRESOLVED)
        )

    def _register_objects(self, objects: list[ContextObject]) -> None:
        """Build the sid->oid authorization table and citation registry."""

        new_rows: list[tuple[str, float]] = []
        for obj in objects or []:
            if not isinstance(obj, dict):
                continue
            oid = str(obj.get("oid") or "").strip()
            if not oid:
                continue
            segment_sids: list[str] = []
            for segment in obj.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                sid = str(segment.get("sid") or "").strip()
                if not sid:
                    continue
                segment_sids.append(sid)
                existing = self._row_metadata.get(sid)
                if existing is not None:
                    self._refresh_activation_metadata(existing, segment)
                    continue
                activation_value = segment.get(KEY_ACTIVATION)
                self._row_metadata[sid] = {
                    "sid": sid,
                    "title": str(segment.get("title") or sid),
                    "locator": str(segment.get("locator") or ""),
                    "score": float(segment.get("score") or 0.0),
                    KEY_RETRIEVAL: float(
                        segment.get(KEY_RETRIEVAL) or segment.get("score") or 0.0
                    ),
                    KEY_ACTIVATION: (
                        float(activation_value)
                        if activation_value is not None
                        else float(segment.get("score") or 0.0)
                    ),
                    KEY_OVERLAP: float(segment.get(KEY_OVERLAP) or 0.0),
                    KEY_OVERLAP_BOOST: float(segment.get(KEY_OVERLAP_BOOST) or 0.0),
                    KEY_RELEVANCE: float(segment.get(KEY_RELEVANCE) or 0.0),
                    KEY_AGE_TURNS: float(segment.get(KEY_AGE_TURNS) or 0.0),
                    KEY_RECENCY_DECAY: float(segment.get(KEY_RECENCY_DECAY) or 0.0),
                    KEY_TURN_BOOST: float(segment.get(KEY_TURN_BOOST) or 0.0),
                    KEY_MISSING_TURN_ID: bool(segment.get(KEY_MISSING_TURN_ID)),
                    KEY_ACTIVE_TURN_UNRESOLVED: bool(
                        segment.get(KEY_ACTIVE_TURN_UNRESOLVED)
                    ),
                    "fidelity": "omitted",
                }
                new_rows.append((sid, float(segment.get("score") or 0.0)))
            self.mint(oid, segment_sids)
        if new_rows:
            start = len(self._citation_numbers) + 1
            for number, (sid, _score) in enumerate(
                sorted(new_rows, key=lambda item: (-item[1], item[0])),
                start=start,
            ):
                self._citation_numbers[sid] = number


__all__ = ["CapabilityLedger", "Grant", "LedgerRow"]
