"""Generate contextual follow-up question suggestions after assistant replies."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from backend.application.llm_content import normalize_llm_content as _normalize_content
from backend.config.settings import settings
from backend.application.suggestions.follow_up_prompts import (
    build_follow_up_context_prompt,
    build_follow_up_prompt,
)

logger = logging.getLogger(__name__)

_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*?\]")
_SUGGESTIONS_OBJ_RE = re.compile(r"\{\s*\"suggestions\"\s*:\s*\[[\s\S]*?\]\s*\}")
_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>[\s\S]*?</think>", re.IGNORECASE)
_THINK_CLOSE_RE = re.compile(r"</think\s*>", re.IGNORECASE)
_THINK_TAG_RE = re.compile(r"</?think\b[^>]*>", re.IGNORECASE)
_FENCED_BLOCK_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)\s*```", re.IGNORECASE)


@dataclass
class FollowUpContext:
    user_question: str
    assistant_answer: str = ""
    tool_summary: str = ""
    context_summary: str = ""
    agent_mode: str = "normal"
    selected_skills: list[str] | None = None
    model_name: str | None = None
    locale: str = "zh-CN"
    use_llm: bool = True


@dataclass
class FollowUpStreamState:
    task: asyncio.Task[tuple[list[str], dict[str, Any]]]
    fallback: Callable[[], tuple[list[str], dict[str, Any]]] | None = None
    first_visible_at: float | None = None
    visible_chars: int = 0
    emitted: bool = False

    def note_visible(self, text: str) -> None:
        if not text:
            return
        if self.first_visible_at is None:
            self.first_visible_at = time.perf_counter()
        self.visible_chars += len(text)

    def cancel(self) -> None:
        if not self.task.done():
            self.task.cancel()

    async def take_ready(self, *, force: bool = False) -> tuple[list[str], dict[str, Any]] | None:
        if self.emitted or (self.first_visible_at is None and not force):
            return None

        elapsed_ms = (
            int((time.perf_counter() - self.first_visible_at) * 1000)
            if self.first_visible_at is not None
            else 0
        )
        min_delay_ms = max(0, int(settings.follow_up_suggestion_min_display_delay_ms or 800))
        min_visible_chars = max(0, int(settings.follow_up_suggestion_min_visible_chars or 80))

        display_ready = elapsed_ms >= min_delay_ms or self.visible_chars >= min_visible_chars
        if not self.task.done():
            if not force:
                return None
            self.task.cancel()
            self.emitted = True
            return self.fallback() if self.fallback is not None else None
        if not force and not display_ready:
            return None

        self.emitted = True
        try:
            suggestions, metadata = await self.task
        except asyncio.CancelledError:
            return None
        except Exception as exc:
            logger.debug("[FollowUp] async stream task failed: %s", exc)
            return None
        if not suggestions:
            return None
        metadata = dict(metadata or {})
        metadata.setdefault("delivery", "early_context")
        return suggestions, metadata


@dataclass
class FollowUpSuggestionService:
    """Lightweight post-processing service for follow-up suggestions."""

    _tool_titles: list[str] = field(default_factory=list)

    def reset_tool_summary(self) -> None:
        self._tool_titles.clear()

    def note_process_event(self, event_payload: dict[str, Any]) -> None:
        kind = str(event_payload.get("kind") or "")
        if kind not in {"tool_end", "tool_result"}:
            return
        tool = str(event_payload.get("tool") or "")
        title = str(event_payload.get("title") or "").strip()
        detail = str(event_payload.get("detail") or "").strip()
        summary = str(event_payload.get("summary") or "").strip()
        metadata = event_payload.get("metadata") if isinstance(event_payload.get("metadata"), dict) else {}

        parts: list[str] = []
        if title:
            parts.append(title)
        if summary:
            parts.append(summary)
        if detail and detail not in parts:
            parts.append(detail[:200])

        for key in ("papers", "results", "titles", "documents"):
            raw = metadata.get(key)
            if isinstance(raw, list):
                for item in raw[:8]:
                    text = str(item or "").strip()
                    if text:
                        parts.append(text[:120])

        if tool and parts:
            self._tool_titles.append(f"[{tool}] " + " | ".join(parts[:3]))
        elif parts:
            self._tool_titles.extend(parts[:3])

        if len(self._tool_titles) > 12:
            self._tool_titles = self._tool_titles[-12:]

    def build_tool_summary(self) -> str:
        if not self._tool_titles:
            return ""
        return "\n".join(f"- {line}" for line in self._tool_titles[:8])

    async def generate(self, ctx: FollowUpContext) -> tuple[list[str], dict[str, Any]]:
        started = time.perf_counter()
        count = max(2, min(4, int(settings.follow_up_suggestion_count or 3)))
        user_q = _truncate(ctx.user_question, 500)
        answer = _truncate(ctx.assistant_answer, 2000)
        tool_summary = _truncate(ctx.tool_summary or self.build_tool_summary(), 500)
        context_summary = _truncate(ctx.context_summary, 1000)

        if not answer.strip() and not context_summary.strip() and not tool_summary.strip():
            return [], {"source": "skipped", "reason": "empty_context"}

        metadata: dict[str, Any] = {
            "source": "fallback",
            "latency_ms": 0,
        }

        suggestions: list[str] = []
        if ctx.use_llm and settings.follow_up_suggestions_enabled:
            try:
                suggestions, llm_meta = await self._generate_with_llm(
                    user_q=user_q,
                    answer=answer,
                    tool_summary=tool_summary,
                    context_summary=context_summary,
                    count=count,
                    selected_skills=ctx.selected_skills,
                    model_name=ctx.model_name,
                )
                metadata.update(llm_meta)
            except Exception as exc:
                logger.warning(
                    "[FollowUp] LLM generation failed (model=%s): %s",
                    _resolve_model_name(ctx.model_name) or "(default)",
                    exc,
                )
                metadata["source"] = "fallback"
                metadata["reason"] = "llm_error"

        if not suggestions:
            suggestions = self._fallback_templates(
                user_q=user_q,
                answer=answer,
                tool_summary=tool_summary,
                context_summary=context_summary,
                selected_skills=ctx.selected_skills,
                count=count,
            )
            if metadata.get("source") != "llm":
                metadata["source"] = "fallback"

        suggestions = _post_filter(suggestions, answer, count)
        metadata["latency_ms"] = int((time.perf_counter() - started) * 1000)
        metadata["model"] = metadata.get("model") or _resolve_model_name(ctx.model_name) or "default"
        return suggestions, metadata

    def start_stream_task(self, ctx: FollowUpContext) -> FollowUpStreamState:
        return FollowUpStreamState(
            task=asyncio.create_task(self.generate(ctx)),
            fallback=lambda: self.generate_fallback(ctx, reason="stream_finished"),
        )

    def generate_fallback(
        self,
        ctx: FollowUpContext,
        *,
        reason: str,
    ) -> tuple[list[str], dict[str, Any]]:
        count = max(2, min(4, int(settings.follow_up_suggestion_count or 3)))
        user_q = _truncate(ctx.user_question, 500)
        answer = _truncate(ctx.assistant_answer, 2000)
        tool_summary = _truncate(ctx.tool_summary or self.build_tool_summary(), 500)
        context_summary = _truncate(ctx.context_summary, 1000)
        suggestions = self._fallback_templates(
            user_q=user_q,
            answer=answer,
            tool_summary=tool_summary,
            context_summary=context_summary,
            selected_skills=ctx.selected_skills,
            count=count,
        )
        return _post_filter(suggestions, answer, count), {
            "source": "fallback",
            "reason": reason,
            "latency_ms": 0,
            "model": _resolve_model_name(ctx.model_name) or "default",
        }

    async def _generate_with_llm(
        self,
        *,
        user_q: str,
        answer: str,
        tool_summary: str,
        context_summary: str,
        count: int,
        selected_skills: list[str] | None,
        model_name: str | None = None,
    ) -> tuple[list[str], dict[str, Any]]:
        from langchain_core.messages import HumanMessage

        from backend.core.models import create_chat_model_async

        resolved_model = _resolve_model_name(model_name)
        logger.info("[FollowUp] 使用模型生成建议: %s", resolved_model or "(default)")
        # 勿向 create_chat_model 传入 request_timeout/temperature/max_tokens，
        # 会与 DB/YAML 配置冲突导致创建失败并回退到无 Key 的兜底模型。
        model = await create_chat_model_async(
            name=resolved_model,
            thinking_enabled=False,
        )
        max_tokens = max(768, int(settings.follow_up_suggestion_max_tokens or 4096))
        model = model.bind(temperature=0.3, max_tokens=max_tokens)
        if answer.strip():
            prompt = build_follow_up_prompt(
                count=count,
                user_question=user_q,
                assistant_answer=answer,
                tool_summary=tool_summary,
                selected_skills=selected_skills,
            )
            source = "llm"
        else:
            prompt = build_follow_up_context_prompt(
                count=count,
                user_question=user_q,
                context_summary=context_summary,
                tool_summary=tool_summary,
                selected_skills=selected_skills,
            )
            source = "llm_context"
        timeout_seconds = max(
            1.0,
            float(settings.follow_up_suggestion_timeout_ms or 30000) / 1000.0,
        )
        response = await asyncio.wait_for(
            model.ainvoke([HumanMessage(content=prompt)]),
            timeout=timeout_seconds,
        )
        content = _normalize_content(getattr(response, "content", ""))
        suggestions = _parse_suggestions(content)
        if not suggestions:
            raise ValueError(f"empty suggestions from LLM, raw={content[:200]!r}")
        return suggestions, {"source": source, "model": resolved_model or "default"}

    def _fallback_templates(
        self,
        *,
        user_q: str,
        answer: str,
        tool_summary: str,
        context_summary: str,
        selected_skills: list[str] | None,
        count: int,
    ) -> list[str]:
        skills = {str(s).strip().lower() for s in (selected_skills or []) if str(s).strip()}
        combined = f"{tool_summary}\n{answer}\n{context_summary}".lower()
        academic = (
            "academic-search" in skills
            or "academic_search" in skills
            or any(k in combined for k in ("论文", "paper", "arxiv", "doi"))
        )
        kb = any(k in combined for k in ("知识库", "标准", "规范", "条文"))

        if academic:
            pool = [
                "这些论文的作者有哪些？",
                "实验方法和主要结果如何？",
                "有没有更新的相关研究？",
                "这些工作的局限性是什么？",
            ]
        elif kb:
            pool = [
                "能否展开说明其中的关键要点？",
                "有无相关的标准或规范可参考？",
                "与常见方案相比有何差异？",
                "实际应用时需要注意什么？",
            ]
        else:
            pool = [
                "请用更简明的语言总结要点",
                "有哪些需要注意的限制条件？",
                "能否举一个具体的应用场景？",
                "和相关概念相比有什么异同？",
            ]

        if user_q and len(user_q) <= 30:
            pool.insert(0, f"关于「{user_q[:20]}」还有哪些细节值得了解？")

        return pool[:count]


def suggestions_to_metadata_items(texts: list[str]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for text in texts:
        normalized = str(text or "").strip()
        if not normalized:
            continue
        digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
        items.append({"id": f"sug_{digest}", "text": normalized})
    return items


async def persist_follow_up_to_checkpoint(
    *,
    checkpointer: Any,
    graph: Any = None,
    internal_thread_id: str,
    suggestions: list[dict[str, str]],
) -> None:
    """Patch the latest AI message metadata in LangGraph checkpoint (best-effort)."""
    if not suggestions or checkpointer is None:
        return
    try:
        config = {"configurable": {"thread_id": internal_thread_id}}
        checkpoint = await checkpointer.aget(config)
        if not checkpoint:
            return
        channel_values = checkpoint.get("channel_values", {}) or {}
        messages = list(channel_values.get("messages", []) or [])
        if not messages:
            return

        last_idx = None
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            role = str(getattr(msg, "type", "") or "").lower()
            if role in {"ai", "assistant"}:
                last_idx = idx
                break
        if last_idx is None:
            return

        msg = messages[last_idx]
        additional = dict(getattr(msg, "additional_kwargs", {}) or {})
        additional["follow_up_suggestions"] = suggestions
        if hasattr(msg, "model_copy"):
            messages[last_idx] = msg.model_copy(update={"additional_kwargs": additional})
        else:
            msg.additional_kwargs = additional

        from langgraph.types import Overwrite

        if graph is None:
            logger.debug("[FollowUp] checkpoint persist skipped: graph unavailable")
            return
        state = await graph.aget_state(config)
        as_node = state.next[0] if state and state.next else None
        if not as_node and state and getattr(state, "created_by", None):
            as_node = state.created_by
        if not as_node:
            node_names = list(getattr(graph, "nodes", {}).keys())
            as_node = next((n for n in node_names if not n.startswith("__")), None)

        update_kwargs: dict[str, Any] = {
            "values": {"messages": Overwrite(value=messages)},
        }
        if as_node:
            update_kwargs["as_node"] = as_node
        await graph.aupdate_state(config, **update_kwargs)
    except Exception as exc:
        logger.debug("[FollowUp] checkpoint persist skipped: %s", exc)


async def persist_corrected_answer_to_checkpoint(
    *,
    checkpointer: Any,
    graph: Any = None,
    internal_thread_id: str,
    corrected_answer: str,
) -> None:
    """Overwrite the latest AI message content in the LangGraph checkpoint with the corrected answer."""
    if not corrected_answer or checkpointer is None or graph is None:
        return
    try:
        config = {"configurable": {"thread_id": internal_thread_id}}
        checkpoint = await checkpointer.aget(config)
        if not checkpoint:
            return
        channel_values = checkpoint.get("channel_values", {}) or {}
        messages = list(channel_values.get("messages", []) or [])
        if not messages:
            return

        last_idx = None
        for idx in range(len(messages) - 1, -1, -1):
            msg = messages[idx]
            role = str(getattr(msg, "type", "") or "").lower()
            if role in {"ai", "assistant"}:
                last_idx = idx
                break
        if last_idx is None:
            return

        msg = messages[last_idx]
        if hasattr(msg, "model_copy"):
            messages[last_idx] = msg.model_copy(update={"content": corrected_answer})
        else:
            msg.content = corrected_answer

        from langgraph.types import Overwrite
        state = await graph.aget_state(config)
        as_node = state.next[0] if state and state.next else None
        if not as_node and state and getattr(state, "created_by", None):
            as_node = state.created_by
        if not as_node:
            node_names = list(getattr(graph, "nodes", {}).keys())
            as_node = next((n for n in node_names if not n.startswith("__")), None)

        update_kwargs: dict[str, Any] = {
            "values": {"messages": Overwrite(value=messages)},
        }
        if as_node:
            update_kwargs["as_node"] = as_node
        await graph.aupdate_state(config, **update_kwargs)
        logger.info("[FollowUp] Successfully updated checkpointer with corrected answer")
    except Exception as exc:
        logger.warning("[FollowUp] Failed to persist corrected answer to checkpoint: %s", exc)


def _resolve_model_name(model_name: str | None) -> str | None:
    """与主对话一致：优先使用前端传入的模型，否则取系统默认模型。"""
    resolved = str(model_name or "").strip()
    if resolved:
        return resolved
    try:
        from backend.core.agents.config_compat import get_default_model_name

        return get_default_model_name()
    except Exception:
        return None

def _truncate(text: str, limit: int) -> str:
    value = str(text or "").strip()
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def _parse_suggestions(content: str) -> list[str]:
    text = str(content or "").strip()
    if not text:
        return []

    for candidate in _json_candidates(text):
        for pattern in (
            reversed(list(_SUGGESTIONS_OBJ_RE.finditer(candidate))),
            reversed(list(_JSON_ARRAY_RE.finditer(candidate))),
        ):
            for match in pattern:
                try:
                    parsed = json.loads(match.group(0))
                    suggestions = _suggestions_from_parsed_json(parsed)
                    if suggestions:
                        return suggestions
                except json.JSONDecodeError:
                    continue
    return []


def _json_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    value = str(text or "").strip()
    if not value:
        return candidates

    close_matches = list(_THINK_CLOSE_RE.finditer(value))
    if close_matches:
        candidates.append(value[close_matches[-1].end() :].strip())

    for fence in _FENCED_BLOCK_RE.finditer(value):
        candidates.append(fence.group(1).strip())

    stripped = _THINK_TAG_RE.sub("", _THINK_BLOCK_RE.sub("", value)).strip()
    candidates.extend([stripped, value])

    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _suggestions_from_parsed_json(parsed: Any) -> list[str]:
    if isinstance(parsed, dict) and isinstance(parsed.get("suggestions"), list):
        return [str(x).strip() for x in parsed["suggestions"] if str(x).strip()]
    if isinstance(parsed, list):
        return [str(x).strip() for x in parsed if str(x).strip()]
    return []


def _post_filter(suggestions: list[str], answer: str, count: int) -> list[str]:
    answer_norm = re.sub(r"\s+", "", answer.lower())
    seen: set[str] = set()
    filtered: list[str] = []
    for raw in suggestions:
        text = str(raw or "").strip()
        if not text or len(text) > 60:
            continue
        # 过滤典型占位符或示例问句以确保用户体验的严谨性
        if any(p in text for p in ("问句", "占位符", "追问问句", "具体追问", "placeholder", "question1", "question2", "example", "具体问题", "问句A", "问句B", "问句C")):
            continue
        norm = re.sub(r"\s+", "", text.lower())
        if norm in seen:
            continue
        if norm and norm in answer_norm:
            continue
        if text in {"还有问题吗？", "还有其他问题吗？"}:
            continue
        seen.add(norm)
        filtered.append(text)
        if len(filtered) >= count:
            break
    return filtered


_follow_up_service: FollowUpSuggestionService | None = None


