"""LLM enrichment for table/formula blocks before chunk construction."""

from __future__ import annotations

import asyncio
import json
from typing import Any

from backend.config import settings
from backend.config.loggers import get_logger
from backend.rag.ingestion.concurrency import generation_model_concurrency, semaphore
from backend.rag.ingestion.extraction.json_repair import loads_json_object
from backend.rag.ingestion.extraction.llm_extractor import _message_content, _resolve_llm_model_name
from backend.rag.ingestion.models import IngestBlock


logger = get_logger(__name__)

_MAX_BLOCK_CHARS = 6000


class MultimodalSummaryError(RuntimeError):
    """Raised when table/formula LLM summaries are required but unavailable."""


def _clean_text(value: Any, *, max_len: int = 1200) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return text[:max_len]


def _compact_units(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, str):
        return _clean_text(value, max_len=500)
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, dict):
                name = _clean_text(item.get("name") or item.get("column") or item.get("symbol"), max_len=80)
                unit = _clean_text(item.get("unit") or item.get("value"), max_len=80)
                if name and unit:
                    parts.append(f"{name}: {unit}")
                elif unit:
                    parts.append(unit)
            else:
                item_text = _clean_text(item, max_len=120)
                if item_text:
                    parts.append(item_text)
        return "; ".join(parts)[:500]
    if isinstance(value, dict):
        parts = [
            f"{_clean_text(key, max_len=80)}: {_clean_text(unit, max_len=80)}"
            for key, unit in value.items()
            if _clean_text(unit, max_len=80)
        ]
        return "; ".join(parts)[:500]
    return _clean_text(value, max_len=500)


def _summarize_prompt(block: IngestBlock, *, subject: str | None) -> str:
    meta = block.metadata or {}
    payload = {
        "block_id": block.block_id,
        "block_type": block.block_type,
        "subject": subject or "",
        "section_path": list(block.section_path or []),
        "page_num": block.page_num,
        "caption": meta.get("caption") or meta.get("title") or "",
        "latex": meta.get("latex") or "",
        "surrounding_context": meta.get("surrounding_context") or "",
        "content": (block.content or "")[:_MAX_BLOCK_CHARS],
    }
    if block.block_type == "table":
        schema = (
            "{\n"
            '  "table_summary": "用 1-3 句话说明表格主题、关键字段、关键事实",\n'
            '  "units": "能从表头或上下文判断出的单位信息；没有则空字符串",\n'
            '  "key_columns": ["关键列名"],\n'
            '  "key_observations": ["重要数值、趋势或对比，最多 5 条"]\n'
            "}"
        )
        task = "为知识库检索生成表格语义摘要。不要编造表格中没有的信息。"
    else:
        schema = (
            "{\n"
            '  "variable_explanation": "逐项解释公式变量、常量和单位；不确定则说明未知",\n'
            '  "formula_summary": "用 1-2 句话说明公式表达的关系或用途",\n'
            '  "variables": [{"symbol": "变量符号", "meaning": "含义", "unit": "单位或空字符串"}]\n'
            "}"
        )
        task = "为知识库检索生成公式语义摘要和变量说明。不要编造上下文不存在的含义。"
    return (
        f"{task}\n"
        "只输出 JSON，不要解释，不要 Markdown。\n"
        f"输出格式：\n{schema}\n\n"
        f"block:\n{json.dumps(payload, ensure_ascii=False)}"
    )


def _needs_summary(block: IngestBlock) -> bool:
    meta = block.metadata or {}
    if block.block_type == "table":
        return not _clean_text(meta.get("table_summary"))
    if block.block_type == "formula":
        return not _clean_text(meta.get("variable_explanation"))
    return False


def _mark_failed(block: IngestBlock, error: str) -> None:
    block.metadata = dict(block.metadata or {})
    block.metadata["llm_summary_status"] = "failed"
    block.metadata["llm_summary_error"] = error[:300]


def _apply_payload(block: IngestBlock, payload: dict[str, Any], *, model_name: str) -> bool:
    block.metadata = dict(block.metadata or {})
    meta = block.metadata
    if block.block_type == "table":
        summary = _clean_text(payload.get("table_summary"))
        if not summary:
            return False
        meta["table_summary"] = summary
        units = _compact_units(payload.get("units"))
        if units:
            meta["units"] = units
        if isinstance(payload.get("key_columns"), list):
            meta["key_columns"] = [_clean_text(item, max_len=120) for item in payload["key_columns"] if _clean_text(item)]
        if isinstance(payload.get("key_observations"), list):
            meta["key_observations"] = [
                _clean_text(item, max_len=240)
                for item in payload["key_observations"]
                if _clean_text(item)
            ][:5]
    elif block.block_type == "formula":
        explanation = _clean_text(payload.get("variable_explanation"))
        if not explanation:
            return False
        meta["variable_explanation"] = explanation
        formula_summary = _clean_text(payload.get("formula_summary"))
        if formula_summary:
            meta["formula_summary"] = formula_summary
        if isinstance(payload.get("variables"), list):
            meta["variables"] = payload["variables"][:20]
    else:
        return False
    meta["llm_summary_model"] = model_name
    meta["llm_summary_status"] = "ok"
    return True


async def enrich_table_formula_blocks(
    blocks: list[IngestBlock],
    *,
    subject: str | None = None,
    require_llm: bool = False,
) -> dict[str, Any]:
    """Populate table_summary / variable_explanation metadata for table/formula blocks."""

    table_blocks = [b for b in blocks if b.block_type == "table"]
    formula_blocks = [b for b in blocks if b.block_type == "formula"]
    targets = [b for b in table_blocks + formula_blocks if _needs_summary(b)]
    audit: dict[str, Any] = {
        "enabled": False,
        "table_count": len(table_blocks),
        "formula_count": len(formula_blocks),
        "target_count": len(targets),
        "skipped_existing_count": len(table_blocks) + len(formula_blocks) - len(targets),
        "table_summarized": 0,
        "formula_summarized": 0,
        "failed_count": 0,
    }
    if not targets:
        audit["enabled"] = True
        return audit

    model_name = _resolve_llm_model_name()
    if not model_name:
        message = "表格/公式摘要需要配置 SELF_EXTRACTION_LLM_MODEL 或 ingestion_llm_provider 对应模型"
        audit["reason"] = "llm_model_not_configured"
        if require_llm:
            raise MultimodalSummaryError(message)
        logger.warning("[self-ingestion] %s", message)
        return audit

    try:
        from backend.core.models import create_chat_model
    except Exception as exc:
        if require_llm:
            raise MultimodalSummaryError(f"模型工厂不可用: {exc}") from exc
        audit["reason"] = "model_factory_unavailable"
        audit["error"] = str(exc)[:300]
        logger.warning("[self-ingestion] table/formula summary model factory unavailable: %s", exc)
        return audit

    audit["enabled"] = True
    audit["model"] = model_name
    model = create_chat_model(name=model_name, thinking_enabled=False)
    timeout = float(getattr(settings, "self_extraction_llm_timeout", 45) or 45)
    batch_semaphore = await semaphore(
        "self_ingestion_generation_model",
        generation_model_concurrency(),
    )

    async def run_one(block: IngestBlock) -> tuple[str, bool, str | None]:
        try:
            async with batch_semaphore:
                response = await asyncio.wait_for(
                    model.ainvoke(_summarize_prompt(block, subject=subject)),
                    timeout=timeout,
                )
            payload = loads_json_object(_message_content(response))
            if not _apply_payload(block, payload, model_name=model_name):
                _mark_failed(block, "LLM response missing required summary field")
                return block.block_type, False, "missing_required_summary"
            return block.block_type, True, None
        except Exception as exc:  # per-block best effort
            _mark_failed(block, str(exc))
            logger.warning(
                "[self-ingestion] table/formula summary failed: block_id=%s type=%s error=%s",
                block.block_id,
                block.block_type,
                exc,
            )
            return block.block_type, False, str(exc)[:120]

    results = await asyncio.gather(*(run_one(block) for block in targets))
    errors: list[str] = []
    for block_type, ok, error in results:
        if ok and block_type == "table":
            audit["table_summarized"] += 1
        elif ok and block_type == "formula":
            audit["formula_summarized"] += 1
        else:
            audit["failed_count"] += 1
            if error:
                errors.append(error)
    if errors:
        audit["errors"] = errors[:5]
    if require_llm and audit["failed_count"]:
        raise MultimodalSummaryError(f"表格/公式摘要失败: {audit['failed_count']} blocks")
    return audit
