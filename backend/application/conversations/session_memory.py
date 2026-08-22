"""Persistent session memory stored in Conversation.metadata_."""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime
from typing import Any

from sqlalchemy import select

from backend.application.conversations.local_catalog import (
    get_local_conversation_catalog,
)
from backend.db.connection import get_async_session_factory
from backend.db.models import Conversation

logger = logging.getLogger(__name__)

MEMORY_KEY = "agent_memory"


def default_agent_memory() -> dict[str, Any]:
    return {
        "version": 1,
        "summary": "",
        "key_entities": [],
        "user_preferences": [],
        "confirmed_facts": [],
        "open_questions": [],
        "last_compressed_message_count": 0,
        "last_compressed_message_id": "",
        "updated_at": "",
    }


class ConversationMemoryStore:
    """Read/write lightweight persistent memory without introducing a migration."""

    async def load(self, *, user_id: str | None, thread_id: str | None) -> dict[str, Any] | None:
        if not user_id or not thread_id:
            return None
        try:
            local_catalog = get_local_conversation_catalog()
            if local_catalog is not None:
                conversation = await local_catalog.get(
                    user_id=user_id,
                    thread_id=thread_id,
                    include_deleted=False,
                )
                if conversation is None:
                    return None
                memory = dict((conversation.metadata_ or {}).get(MEMORY_KEY) or {})
                if not memory:
                    return None
                return {**default_agent_memory(), **memory}
            session_factory = get_async_session_factory()
            async with session_factory() as session:
                result = await asyncio.wait_for(
                    session.execute(
                        select(Conversation).where(
                            Conversation.user_id == user_id,
                            Conversation.thread_id == thread_id,
                        )
                    ),
                    timeout=10.0,
                )
                conversation = result.scalar_one_or_none()
                if conversation is None:
                    return None
                memory = dict((conversation.metadata_ or {}).get(MEMORY_KEY) or {})
                if not memory:
                    return None
                return {**default_agent_memory(), **memory}
        except Exception:
            logger.warning(
                "[ConversationMemoryStore] load failed: user_id=%s thread_id=%s",
                user_id,
                thread_id,
                exc_info=True,
            )
            return None

    async def merge_from_summary(
        self,
        *,
        user_id: str | None,
        thread_id: str | None,
        summary: str,
        message_count: int,
        message_id: str | None = None,
    ) -> bool:
        """Merge one compaction summary into ``agent_memory`` atomically.

        The read-merge-write runs inside the catalog row transaction (or the
        MySQL row lock), so concurrent writers cannot base their merge on the
        same stale snapshot and drop each other's structured entries — the
        load-then-save shape did exactly that across requests.
        """

        if not user_id or not thread_id:
            return False

        def _merged(previous_value: Any) -> dict[str, Any]:
            previous: dict[str, Any] | None = None
            if isinstance(previous_value, dict) and previous_value:
                previous = {**default_agent_memory(), **previous_value}
            merged = build_memory_from_summary(
                summary=summary,
                message_count=message_count,
                message_id=message_id,
                previous=previous,
            )
            merged["version"] = 1
            merged["updated_at"] = datetime.now().isoformat()
            return merged

        try:
            local_catalog = get_local_conversation_catalog()
            if local_catalog is not None:
                return await local_catalog.merge_metadata_value_with(
                    user_id=user_id,
                    thread_id=thread_id,
                    key=MEMORY_KEY,
                    merge=_merged,
                )
            session_factory = get_async_session_factory()
            async with session_factory() as session:
                result = await asyncio.wait_for(
                    session.execute(
                        select(Conversation)
                        .where(
                            Conversation.user_id == user_id,
                            Conversation.thread_id == thread_id,
                        )
                        .with_for_update()
                    ),
                    timeout=10.0,
                )
                conversation = result.scalar_one_or_none()
                if conversation is None:
                    return False
                metadata = dict(conversation.metadata_ or {})
                metadata[MEMORY_KEY] = _merged(metadata.get(MEMORY_KEY))
                conversation.metadata_ = metadata
                conversation.updated_at = datetime.now()
                await session.commit()
                return True
        except Exception:
            logger.warning(
                "[ConversationMemoryStore] merge failed: user_id=%s thread_id=%s",
                user_id,
                thread_id,
                exc_info=True,
            )
            return False

    async def save(
        self,
        *,
        user_id: str | None,
        thread_id: str | None,
        memory: dict[str, Any],
    ) -> bool:
        if not user_id or not thread_id:
            return False
        try:
            merged = {**default_agent_memory(), **dict(memory or {})}
            merged["version"] = 1
            merged["updated_at"] = datetime.now().isoformat()
            local_catalog = get_local_conversation_catalog()
            if local_catalog is not None:
                return await local_catalog.merge_metadata_value(
                    user_id=user_id,
                    thread_id=thread_id,
                    key=MEMORY_KEY,
                    value=merged,
                )
            session_factory = get_async_session_factory()
            async with session_factory() as session:
                result = await asyncio.wait_for(
                    session.execute(
                        select(Conversation)
                        .where(
                            Conversation.user_id == user_id,
                            Conversation.thread_id == thread_id,
                        )
                        .with_for_update()
                    ),
                    timeout=10.0,
                )
                conversation = result.scalar_one_or_none()
                if conversation is None:
                    return False
                metadata = dict(conversation.metadata_ or {})
                metadata[MEMORY_KEY] = merged
                conversation.metadata_ = metadata
                conversation.updated_at = datetime.now()
                await session.commit()
                return True
        except Exception:
            logger.warning(
                "[ConversationMemoryStore] save failed: user_id=%s thread_id=%s",
                user_id,
                thread_id,
                exc_info=True,
            )
            return False


def validate_memory_summary(*, source_text: str, summary: str) -> bool:
    """Reject invented facts and require critical fact markers to survive compression."""

    source = str(source_text or "")
    target = str(summary or "")
    if not target.strip():
        return False
    source_numbers = set(re.findall(r"(?<!\w)\d+(?:\.\d+)?%?(?!\w)", source))
    target_numbers = set(re.findall(r"(?<!\w)\d+(?:\.\d+)?%?(?!\w)", target))
    if not target_numbers.issubset(source_numbers):
        return False
    important_text = _extract_critical_memory_text(source)
    important_numbers = set(re.findall(r"(?<!\w)\d+(?:\.\d+)?%?(?!\w)", important_text))
    if not important_numbers.issubset(target_numbers):
        return False

    source_refs = _extract_refs(source)
    target_refs = _extract_refs(target)
    if not target_refs.issubset(source_refs):
        return False
    important_refs = _extract_refs(important_text)
    if not important_refs.issubset(target_refs):
        return False
    if not _summary_sentences_supported(source, target):
        return False
    return True


_MEMORY_SUMMARY_MAX_CHARS = 8000


def build_memory_from_summary(
    *,
    summary: str,
    message_count: int,
    message_id: str | None = None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    from backend.core.agents.context.summary import generate_summary

    memory = {**default_agent_memory(), **dict(previous or {})}
    # Compaction summaries grow with the conversation; an unbounded value
    # bloats Conversation.metadata_ on every save and slows each load.
    memory["summary"] = generate_summary(
        str(summary or "").strip(),
        max_chars=_MEMORY_SUMMARY_MAX_CHARS,
    )
    extracted = _extract_structured_memory(summary)
    for key, values in extracted.items():
        memory[key] = _merge_unique_memory_values(memory.get(key), values)
    memory["last_compressed_message_count"] = max(0, int(message_count or 0))
    memory["last_compressed_message_id"] = str(message_id or "")
    memory["updated_at"] = datetime.now().isoformat()
    return memory


def _extract_structured_memory(summary: str) -> dict[str, list[str]]:
    result = {
        "key_entities": [],
        "user_preferences": [],
        "confirmed_facts": [],
        "open_questions": [],
    }
    for raw_line in str(summary or "").splitlines():
        line = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", raw_line).strip()
        if not line:
            continue
        if re.search(r"用户偏好|偏好|希望|要求", line):
            result["user_preferences"].append(line)
        if re.search(r"已确认|确认事实|关键事实|结论[:：]|实测|标准(?:限值|依据)?[:：]|阈值", line):
            result["confirmed_facts"].append(line)
        if re.search(r"未解决|待确认|待核验|证据缺口|仍需|未知|不足", line):
            result["open_questions"].append(line)
        entity_match = re.match(r"(?:关键实体|实体|项目|文档|标准名称)[:：]\s*(.+)", line)
        if entity_match:
            result["key_entities"].extend(
                item.strip()
                for item in re.split(r"[,，、;；]", entity_match.group(1))
                if item.strip()
            )
    return result


def _merge_unique_memory_values(existing: Any, new_values: list[str]) -> list[Any]:
    merged = [*(existing if isinstance(existing, list) else []), *new_values]
    seen: set[str] = set()
    result: list[Any] = []
    for item in merged:
        marker = str(item.get("fact") or item.get("text") or item) if isinstance(item, dict) else str(item)
        if not marker or marker in seen:
            continue
        seen.add(marker)
        result.append(item)
    return result[-24:]


def format_memory_for_prompt(memory: dict[str, Any] | None) -> str:
    if not memory:
        return ""
    lines: list[str] = []
    summary = str(memory.get("summary") or "").strip()
    if summary:
        lines.extend(["[持久会话摘要]", summary])
    for key, label in (
        ("key_entities", "关键实体"),
        ("user_preferences", "用户偏好"),
        ("confirmed_facts", "已确认事实"),
        ("open_questions", "未解决问题"),
    ):
        values = memory.get(key)
        if isinstance(values, list) and values:
            rendered = []
            for item in values[:12]:
                if isinstance(item, dict):
                    rendered.append(str(item.get("fact") or item.get("text") or item))
                else:
                    rendered.append(str(item))
            lines.append(f"[{label}]\n" + "\n".join(f"- {item}" for item in rendered if item))
    return "\n\n".join(lines).strip()


def _extract_refs(text: str) -> set[str]:
    refs = set(re.findall(r"https?://\S+", text or ""))
    refs.update(re.findall(r"\[[A-Za-z0-9_\-./:]{1,80}\]", text or ""))
    return {ref.rstrip(").,;:，。；：") for ref in refs}


def _extract_critical_memory_text(text: str) -> str:
    markers = (
        "确认",
        "已确认",
        "关键事实",
        "重要数值",
        "引用约束",
        "结论",
        "阈值",
        "标准",
        "必须",
        "不得",
        "用户偏好",
    )
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    critical = [line for line in lines if any(marker in line for marker in markers)]
    return "\n".join(critical[:80])


def _summary_sentences_supported(source: str, summary: str) -> bool:
    source_sentences = _memory_sentences(source)
    if not source_sentences:
        return False
    source_term_sets = [_memory_terms(sentence) for sentence in source_sentences]
    for sentence in _memory_sentences(summary):
        terms = _memory_terms(sentence)
        if len(terms) < 2:
            continue
        best = max(
            (len(terms & source_terms) / max(1, len(terms)) for source_terms in source_term_sets),
            default=0.0,
        )
        if best < 0.45:
            return False
    return True


def _memory_sentences(text: str) -> list[str]:
    return [
        item.strip()
        for item in re.split(r"[。！？!?，,；;]\s*|\n+", str(text or ""))
        if item.strip()
    ]


def _memory_terms(text: str) -> set[str]:
    lowered = str(text or "").lower()
    terms = set(re.findall(r"[a-z0-9][a-z0-9_.:/%+-]{1,}", lowered))
    for cjk_run in re.findall(r"[\u4e00-\u9fff]+", lowered):
        if len(cjk_run) == 1:
            terms.add(cjk_run)
        else:
            terms.update(cjk_run[index : index + 2] for index in range(len(cjk_run) - 1))
    return terms
