"""The single projection renderer for context objects.

Every surface that turns segments into model-visible text — tool messages,
JIT packs and the context navigator — goes through this compiler. The
fidelity ladder is deterministic and model-call free:

    full -> compressed -> headline -> omitted
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from backend.core.agents.context.activation import (
    KEY_OMIT_THRESHOLD,
    KEY_RELEVANCE,
)
from backend.core.agents.context.objects import (
    ContextObject,
    ProjectionEntry,
    ProjectionReceipt,
    Segment,
    estimate_tokens,
)
from backend.core.agents.context.summary import generate_summary


class _HeuristicCounter:
    """Conservative mixed-language counter matching ledger defaults."""

    def count(self, text: object) -> int:
        return estimate_tokens(text)


def _first_sentence(text: str, *, max_chars: int = 120) -> str:
    body = str(text or "").strip()
    if not body:
        return ""
    match = re.split(r"(?<=[。！？.!?])\s+", body, maxsplit=1)
    head = match[0] if match else body
    if len(head) <= max_chars:
        return head
    return head[:max_chars].rstrip() + "…"


def _escape_body(value: str) -> str:
    return (
        str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


def _escape_attr(value: str) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
    )


@dataclass(frozen=True)
class ProjectionResult:
    rendered: str
    receipt: ProjectionReceipt
    entries: list[ProjectionEntry]
    tokens_spent: int


class ProjectionCompiler:
    """Render segment sets under one token budget with one fidelity policy."""

    MAX_HEADLINE_SEGMENTS = 200

    def __init__(
        self,
        token_counter: Any | None = None,
        *,
        headline_tokens: int = 25,
    ) -> None:
        self._counter = token_counter or _HeuristicCounter()
        self._headline_tokens = max(4, int(headline_tokens))

    @property
    def token_counter(self) -> Any:
        return self._counter

    def project(
        self,
        objects: Iterable[ContextObject],
        *,
        handle: str,
        budget_tokens: int,
        query: str = "",
        turn_id: str = "",
        projected_sids: frozenset[str] | set[str] | None = None,
        requested_sids: list[str] | None = None,
        reason: str = "budget",
    ) -> ProjectionResult:
        """Project segments and return rendered text plus a receipt."""

        budget = max(0, int(budget_tokens))
        seen = set(projected_sids or set())
        objects = list(objects or [])
        candidates: list[tuple[ContextObject, Segment]] = []
        for obj in objects:
            if not isinstance(obj, dict):
                continue
            for segment in obj.get("segments") or []:
                if not isinstance(segment, dict):
                    continue
                sid = str(segment.get("sid") or "").strip()
                if not sid:
                    continue
                candidates.append((obj, segment))

        if requested_sids:
            wanted = {str(sid) for sid in requested_sids if str(sid).strip()}
            candidates = [
                (obj, segment)
                for obj, segment in candidates
                if str(segment.get("sid") or "") in wanted
            ]
            reason = "requested_subset"

        entries_by_sid: dict[str, ProjectionEntry] = {}
        blocks_by_sid: dict[str, tuple[str, int]] = {}
        blocks_order: list[str] = []
        used = 0
        omitted: list[str] = []
        pointers: list[tuple[str, str]] = []

        # Headline pass: deterministic score ordering, hard budget.
        ordered = sorted(
            candidates,
            key=lambda item: (
                -float(item[1].get("score") or 0.0),
                str(item[1].get("sid") or ""),
            ),
        )
        # Seen sids only ever render as pointer lines, so they must not
        # consume headline slots that unseen evidence could use.
        unseen_ordered = [
            (obj, segment)
            for obj, segment in ordered
            if str(segment.get("sid") or "") not in seen
        ]
        headline_rendered = {
            str(segment.get("sid") or ""): self._render_headline(segment)
            for _obj, segment in unseen_ordered
        }
        headline_tokens = {
            sid: self._counter.count(rendered)
            for sid, rendered in headline_rendered.items()
        }
        omitted_summary = self._render_omitted_summary(len(ordered))
        reserved_omitted_cost = (
            self._counter.count(omitted_summary) if omitted_summary else 0
        )
        headline_budget = max(0, budget - reserved_omitted_cost)
        if sum(headline_tokens.values()) <= headline_budget:
            selected_headlines = list(unseen_ordered)
        else:
            selected_headlines = []
            remaining_headline_budget = headline_budget
            for obj, segment in unseen_ordered:
                sid = str(segment.get("sid") or "")
                cost = headline_tokens.get(sid, self._headline_tokens)
                if cost <= remaining_headline_budget:
                    selected_headlines.append((obj, segment))
                    remaining_headline_budget -= cost
        headline_sids = {
            str(segment.get("sid") or "") for _obj, segment in selected_headlines
        }
        for _obj, segment in ordered:
            sid = str(segment.get("sid") or "")
            if sid in seen:
                pointers.append((sid, self._render_pointer(handle, segment)))
                omitted.append(sid)
                entries_by_sid[sid] = ProjectionEntry(
                    sid=sid,
                    fidelity="omitted",
                    tokens=0,
                )
                continue
            if sid not in headline_sids:
                omitted.append(sid)
                continue
            rendered = headline_rendered[sid]
            cost = headline_tokens[sid]
            blocks_by_sid[sid] = (rendered, cost)
            blocks_order.append(sid)
            used += cost
            entries_by_sid[sid] = ProjectionEntry(
                sid=sid,
                fidelity="headline",
                tokens=cost,
            )

        # Full/compressed upgrade pass; upgrading replaces the headline block
        # so one sid body is rendered at exactly one fidelity.
        remaining = budget - used
        for obj, segment in selected_headlines:
            sid = str(segment.get("sid") or "")
            if sid in seen:
                continue
            # Missing keys ⇒ no threshold gate (rollback / activation-off path).
            # Do not use ``x or default``: relevance 0.0 is a valid low score.
            raw_relevance = segment.get(KEY_RELEVANCE)
            raw_omit = segment.get(KEY_OMIT_THRESHOLD)
            if raw_relevance is not None and raw_omit is not None:
                if float(raw_relevance) < float(raw_omit):
                    # 低相关（relevance，不乘衰减）：只允许 headline/omitted，
                    # 禁止 full/compressed 升级；不额外扣预算（R7）。
                    continue
            full = self._render_full(handle, obj, segment)
            cost = self._counter.count(full)
            if cost <= remaining:
                replaced_cost = blocks_by_sid.get(sid, (None, 0))[1]
                blocks_by_sid[sid] = (full, cost)
                used = used - replaced_cost + cost
                remaining = budget - used
                entries_by_sid[sid] = ProjectionEntry(
                    sid=sid,
                    fidelity="full",
                    tokens=cost,
                )
                continue

            compressed, cost = self._render_compressed_fit(
                handle,
                obj,
                segment,
                remaining,
            )
            if compressed is not None:
                replaced_cost = blocks_by_sid.get(sid, (None, 0))[1]
                blocks_by_sid[sid] = (compressed, cost)
                used = used - replaced_cost + cost
                remaining = budget - used
                entries_by_sid[sid] = ProjectionEntry(
                    sid=sid,
                    fidelity="compressed",
                    tokens=cost,
                )

        blocks = [blocks_by_sid[sid][0] for sid in blocks_order]
        # Pointer lines are real rendered output: charge them against the
        # budget instead of emitting them for free (dozens of seen sids used
        # to leak hundreds of unaccounted tokens per JIT render). A pointer
        # that no longer fits is dropped; its sid already sits in `omitted`.
        for pointer_sid, pointer in pointers:
            pointer_cost = self._counter.count(pointer)
            if pointer_cost > budget - used:
                continue
            blocks.append(pointer)
            used += pointer_cost
            entries_by_sid[pointer_sid] = ProjectionEntry(
                sid=pointer_sid,
                fidelity="omitted",
                tokens=pointer_cost,
            )
        remaining = budget - used

        omitted_summary = self._render_omitted_summary(len(omitted))
        if omitted_summary:
            omitted_cost = self._counter.count(omitted_summary)
            if omitted_cost <= remaining:
                blocks.append(omitted_summary)
                used += omitted_cost
                remaining -= omitted_cost

        if not requested_sids:
            summary = self._render_summary(objects)
            if summary:
                summary_cost = self._counter.count(summary)
                if summary_cost <= remaining:
                    blocks.insert(0, summary)
                    used += summary_cost
                    remaining -= summary_cost

        rendered = "\n".join(blocks)
        entries = list(entries_by_sid.values())
        first_oid = next(
            (str(obj.get("oid") or "") for obj in objects if obj.get("oid")),
            "",
        )
        receipt = ProjectionReceipt(
            oid=first_oid,
            handle=handle,
            turn_id=turn_id,
            entries=entries,
            tokens_spent=used,
            overhead_tokens=used - sum(entry.get("tokens") or 0 for entry in entries),
            omitted_sids=list(dict.fromkeys(omitted)),
            reason=reason,
        )
        return ProjectionResult(
            rendered=rendered,
            receipt=receipt,
            entries=entries,
            tokens_spent=used,
        )

    def render_navigator(
        self,
        segments: list[Segment],
        *,
        handle: str,
        budget_tokens: int,
        turn_id: str = "",
    ) -> ProjectionResult:
        """Render requested segments for the navigator tool."""

        fake_objects = [
            {
                "oid": "",
                "schema": "navigator/v1",
                "origin": {},
                "index": {},
                "segments": segments,
            }
        ]
        return self.project(
            fake_objects,
            handle=handle,
            budget_tokens=budget_tokens,
            turn_id=turn_id,
            requested_sids=[str(segment.get("sid") or "") for segment in segments],
            reason="navigator",
        )

    def render_ledger(
        self,
        rows: list[dict[str, Any]],
        *,
        budget_tokens: int,
        dynamic_budget: int | None = None,
    ) -> tuple[str, int]:
        """Render the request-stable citation registry under a hard budget.

        The ledger is the single addressable surface for evidence: every sid,
        citation number and document mapping lives here. It is capped at 15%
        of the dynamic budget (20-80 rows) and never over-spends ``budget_tokens``.
        """

        rows = [
            dict(row)
            for row in rows or []
            if isinstance(row, dict) and str(row.get("sid") or "").strip()
        ]
        if not rows:
            return "", 0
        budget = max(0, int(budget_tokens))
        dynamic = max(
            0, int(dynamic_budget if dynamic_budget is not None else budget_tokens)
        )
        rows.sort(key=lambda row: int(row.get("n") or 0))

        per_row = max(
            1,
            self._counter.count(self._render_ledger_line(rows[0])),
        )
        cap = 80
        if dynamic > 0:
            cap = int(dynamic * 0.15 / per_row)
            cap = min(80, max(20, cap))
        max_rows = min(cap, len(rows))
        header = self._render_ledger_header(len(rows))

        # 编号冻结后 n 序 ≠ 分数序:截断保留集必须按分数选(否则紧预算下保留
        # 的是低分旧行、丢弃的恰是最新高分证据),渲染仍按 n 序保证编号稳定。
        by_score = sorted(
            rows,
            key=lambda row: (
                -float(row.get("score") or 0.0),
                int(row.get("n") or 0),
            ),
        )

        def build(count: int) -> tuple[str, int]:
            kept = sorted(by_score[:count], key=lambda row: int(row.get("n") or 0))
            parts = [header]
            parts.extend(self._render_ledger_line(row) for row in kept)
            if count < len(rows):
                omitted = by_score[count:]
                threshold = "score 较低"
                if omitted:
                    # 诚实上界:所有被省略行的分数都 ≤ 该值(旧实现按编号序
                    # 截断却宣称 score 阈值,伪造省略依据)。
                    threshold = (
                        "score ≤ "
                        f"{float(omitted[0].get('score') or 0.0):.2f}"
                    )
                note = self._render_ledger_truncation(
                    len(rows) - count,
                    threshold,
                    omitted_sids=[
                        str(row.get("sid") or "") for row in omitted
                    ],
                )
                parts.append(note)
            rendered = "\n".join(parts)
            return rendered, self._counter.count(rendered)

        low, high = 0, max_rows
        best_count = 0
        best_rendered = ""
        best_cost = 0
        while low <= high:
            midpoint = (low + high) // 2
            rendered, cost = build(midpoint)
            if cost <= budget:
                best_count = midpoint
                best_rendered = rendered
                best_cost = cost
                low = midpoint + 1
            else:
                high = midpoint - 1
        if best_count:
            return best_rendered, best_cost
        fallback = self._render_ledger_summary(len(rows))
        fallback_cost = self._counter.count(fallback)
        if fallback_cost <= budget:
            return fallback, fallback_cost
        return "", 0

    def _render_summary(self, objects: Iterable[ContextObject]) -> str:
        labels = []
        total_segments = 0
        for obj in objects or []:
            if not isinstance(obj, dict):
                continue
            index = dict(obj.get("index") or {})
            title = str(index.get("summary") or obj.get("schema") or "").strip()
            if title:
                labels.append(title)
            total_segments += len(obj.get("segments") or [])
        if not labels and not total_segments:
            return ""
        joined = "；".join(labels)
        return (
            f"【摘要】{joined}，共 {total_segments} 段证据。"
            "取正文：context_read(sids=[...])"
        )

    def _render_headline(self, segment: Segment) -> str:
        sid = str(segment.get("sid") or "")
        title = str(segment.get("title") or sid or "")
        locator = str(segment.get("locator") or "").strip()
        score = float(segment.get("score") or 0.0)
        tokens = int(segment.get("tokens") or estimate_tokens(segment.get("text")))
        first = _first_sentence(str(segment.get("text") or ""))
        locator_part = f"  {_escape_attr(locator)}" if locator else ""
        first_part = f"  {_escape_body(first)}" if first else ""
        return (
            f"[{_escape_attr(sid)}] {_escape_attr(title)}{locator_part}"
            f"  score={score:.2f}  {tokens}tok{first_part}"
        )

    def _render_full(
        self,
        handle: str,
        obj: ContextObject,
        segment: Segment,
    ) -> str:
        sid = str(segment.get("sid") or "")
        title = str(segment.get("title") or sid or "")
        locator = str(segment.get("locator") or "").strip()
        text = str(segment.get("text") or "")
        locator_attr = f' locator="{_escape_attr(locator)}"' if locator else ""
        return (
            f'<context_object handle="{_escape_attr(handle)}" sid="{_escape_attr(sid)}"'
            f' schema="{_escape_attr(str(obj.get("schema") or ""))}"{locator_attr}>'
            f"\n<title>{_escape_body(title)}</title>\n{_escape_body(text)}\n"
            "</context_object>"
        )

    def _render_compressed(
        self,
        handle: str,
        obj: ContextObject,
        segment: Segment,
        char_limit: int | None = None,
    ) -> str:
        text = str(segment.get("text") or "")
        tokens = int(segment.get("tokens") or estimate_tokens(text))
        resolved_char_limit = (
            max(40, int(char_limit))
            if char_limit is not None
            else max(240, min(len(text), tokens * 3))
        )
        resolved_char_limit = min(len(text), resolved_char_limit)
        body = generate_summary(text, max_chars=resolved_char_limit)
        sid = str(segment.get("sid") or "")
        title = str(segment.get("title") or sid or "")
        return (
            f'<context_object handle="{_escape_attr(handle)}" sid="{_escape_attr(sid)}"'
            f' schema="{_escape_attr(str(obj.get("schema") or ""))}" fidelity="compressed">'
            f"\n<title>{_escape_body(title)}</title>\n{_escape_body(body)}\n"
            "</context_object>"
        )

    def _render_compressed_fit(
        self,
        handle: str,
        obj: ContextObject,
        segment: Segment,
        remaining_tokens: int,
    ) -> tuple[str | None, int]:
        """Render a compressed block that actually fits the remaining budget.

        The character ceiling is derived from the remaining token budget, not
        from the segment's own token estimate; otherwise CJK text would never
        shrink (estimate_tokens ~= 1.5 chars/token makes ``tokens * 3`` larger
        than the body).
        """

        text = str(segment.get("text") or "")
        if not text:
            return None, 0
        char_limit = min(
            len(text),
            max(40, int(max(0, remaining_tokens - 16))),
        )
        for _ in range(6):
            block = self._render_compressed(
                handle,
                obj,
                segment,
                char_limit=char_limit,
            )
            cost = self._counter.count(block)
            if cost <= remaining_tokens:
                return block, cost
            char_limit = max(40, int(char_limit * 0.6))
        return None, 0

    def _render_omitted_summary(self, count: int) -> str:
        if count <= 0:
            # 全量注入（无省略）时不再渲染「另有 0 段省略」的误导性清单。
            return ""
        return (
            f"【省略清单】另有 {count} 段省略，"
            "可用 context_read(sids=[...]) 按本轮【证据台账】中的 sid 取回。"
        )

    def _render_ledger_header(self, total: int) -> str:
        return (
            "【证据台账】本轮共 "
            f"{total} 条证据。正文引用 [n] 必须取自本表；"
            "末尾「参考文档」逐条复制被引用的行。"
            "需要取回正文时使用 context_read(sids=[...])，"
            "禁止重新发起知识库检索。"
        )

    def _render_ledger_line(self, row: dict[str, Any]) -> str:
        sid = str(row.get("sid") or "")
        title = str(row.get("title") or sid)
        locator = str(row.get("locator") or "").strip()
        if locator and locator not in title:
            title = f"{title}，{locator}"
        score = float(row.get("score") or 0.0)
        if row.get("activation_score") is not None:
            retrieval = float(row.get("retrieval_score") or score)
            activation = float(row.get("activation_score") or score)
            score_label = f"act={activation:.2f} (ret={retrieval:.2f})"
        else:
            score_label = f"score={score:.2f}"
        fidelity = str(row.get("fidelity") or "omitted")
        if fidelity == "full":
            label = "正文已在上文"
        elif fidelity == "compressed":
            label = f'摘要，取正文：context_read(sids=["{_escape_attr(sid)}"])'
        elif fidelity == "headline":
            label = f'标题，取正文：context_read(sids=["{_escape_attr(sid)}"])'
        else:
            label = f'未注入，取正文：context_read(sids=["{_escape_attr(sid)}"])'
        return (
            f"[{int(row.get('n') or 0)}] {_escape_body(sid)} | "
            f"{_escape_body(title)} | {score_label} | {label}"
        )

    MAX_TRUNCATION_SIDS = 24

    def _render_ledger_truncation(
        self,
        count: int,
        threshold: str = "score 较低",
        *,
        omitted_sids: list[str] | None = None,
    ) -> str:
        if count <= 0:
            return ""
        sids = [sid for sid in (omitted_sids or []) if str(sid).strip()]
        if not sids:
            # 没有 sid 可给时不再指示"按 sid 取回"——模型无从取回,
            # 旧文案是一个无法执行的指令。
            return (
                f"另有 {count} 条证据未列入台账（{threshold}），"
                "本轮无法按 sid 取回；如确有必要可缩小问题范围后重新检索。"
            )
        shown = sids[: self.MAX_TRUNCATION_SIDS]
        listed = "、".join(_escape_body(sid) for sid in shown)
        more = (
            f" 等 {len(sids)} 条" if len(sids) > len(shown) else ""
        )
        return (
            f"另有 {count} 条证据未列入台账（{threshold}），"
            f"其 sid：{listed}{more}；"
            "如需取回请用 context_read(sids=[...]) 按上述 sid 取回，禁止重新检索。"
        )

    def _render_ledger_summary(self, total: int) -> str:
        return (
            "【证据台账】本轮共 "
            f"{total} 条证据因预算受限未逐条列出；"
            "正文引用编号与文献映射见本轮工具消息与可引用文献，"
            "取回正文用 context_read(sids=[...])，禁止重新检索。"
        )

    def _render_pointer(self, handle: str, segment: Segment) -> str:
        sid = str(segment.get("sid") or "")
        return (
            f"[重复片段见上文 {_escape_attr(handle)}:{_escape_attr(sid)}"
            f" {_escape_attr(str(segment.get('title') or ''))}]"
        )


__all__ = ["ProjectionCompiler", "ProjectionResult"]
