"""Model-based evidence completeness evaluation before writer handoff."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvidenceEvaluation:
    complete: bool
    missing_evidence: list[str]
    next_action: str
    rationale: str


class EvidenceCompletenessEvaluator:
    """Ask a no-tool model whether collected evidence is ready for final writing."""

    def __init__(
        self,
        *,
        model_name: str | None,
        timeout_seconds: float | None = None,
    ):
        self._model_name = model_name
        configured_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else float(os.getenv("EVIDENCE_EVALUATOR_TIMEOUT_SECONDS", "30") or "30")
        )
        self._timeout_seconds = max(1.0, configured_timeout)

    @staticmethod
    def _trim(text: str, *, max_chars: int) -> str:
        text = str(text or "").strip()
        return text if len(text) <= max_chars else text[-max_chars:]

    @staticmethod
    def _message_text(message: Any) -> str:
        content = getattr(message, "content", message)
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    value = item.get("text") or item.get("content")
                    if value:
                        parts.append(str(value))
            return "\n".join(parts).strip()
        return str(content or "").strip()

    @staticmethod
    def _parse(text: str) -> EvidenceEvaluation:
        candidate = text.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            candidate = "\n".join(lines).strip()
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            start = candidate.find("{")
            end = candidate.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("证据评估模型未返回 JSON") from exc
            payload = json.loads(candidate[start : end + 1])
        if not isinstance(payload, dict) or not isinstance(payload.get("complete"), bool):
            raise ValueError("证据评估结果缺少布尔字段 complete")
        missing = payload.get("missing_evidence") or []
        if not isinstance(missing, list):
            missing = [str(missing)]
        return EvidenceEvaluation(
            complete=payload["complete"],
            missing_evidence=[str(item).strip() for item in missing if str(item).strip()],
            next_action=str(payload.get("next_action") or "").strip(),
            rationale=str(payload.get("rationale") or "").strip(),
        )

    async def evaluate(
        self,
        *,
        question: str,
        project_evidence: str,
        retrieved_evidence: str,
    ) -> EvidenceEvaluation:
        from backend.core.models import create_chat_model

        prompt = f"""
判断现有证据是否足以让另一个无工具撰写模型完整、准确地回答用户问题。

判断原则：
1. 依据用户问题的实际目标进行语义判断，不使用固定点位、固定字段或固定指标规则。
2. 只检查当前问题真正需要的证据，不得机械要求数值、标准限值或超标计算。方法说明、方案分析类问题不一定需要这些数据。
3. 清单、表结构、数据存在性说明只能证明数据可能存在，不能代替实际查询结果。
4. 工具报错、空结果、只提到文档名称但没有问题所需内容，均不能视为证据充分。
5. 知识库内容必须与问题的核心概念直接相关；仅有同领域但无关的规范，不算有效依据。
6. 如果项目缺少精确数据，但现有证据已经足以给出有用的方法、定性分析并准确说明限制，可以判定 complete=true；不要为了追求不存在的数据无限查询。
7. 只有继续调用工具有明确机会获得回答所必需的证据时，complete 才为 false，并指出缺少什么以及下一步应获取什么证据。
8. 不要回答用户问题，只输出一个 JSON 对象，不要输出 Markdown。

JSON 格式：
{{
  "complete": false,
  "missing_evidence": ["缺失证据"],
  "next_action": "下一步应获取的证据或应执行的查询",
  "rationale": "简短判断理由"
}}

用户问题：
{question}

<project_evidence>
{self._trim(project_evidence, max_chars=16000)}
</project_evidence>

<reference_evidence>
{self._trim(retrieved_evidence, max_chars=12000)}
</reference_evidence>
""".strip()
        model = create_chat_model(name=self._model_name or None, thinking_enabled=False)
        response = await asyncio.wait_for(
            model.ainvoke(
                [
                    SystemMessage(
                        content="你是证据完整性评估器，只做严格的证据充分性判断。"
                    ),
                    HumanMessage(content=prompt),
                ]
            ),
            timeout=self._timeout_seconds,
        )
        evaluation = self._parse(self._message_text(response))
        logger.info(
            "[EvidenceEvaluator] complete=%s missing=%s next_action=%s",
            evaluation.complete,
            evaluation.missing_evidence,
            evaluation.next_action,
        )
        return evaluation
