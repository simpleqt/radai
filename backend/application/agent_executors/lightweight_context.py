"""轻量问答检索意图与上下文组装方法实现(首参 self 为 LightweightQAAgent)。"""

from __future__ import annotations

import logging
import asyncio

from backend.application.retrieval.query_resolution import compose_retrieval_query
from backend.config.log_privacy import text_fingerprint
from backend.core.agents.context import make_evidence_record
from backend.core.agents.middlewares.runtime_budget import (
    charge_agent_model_response,
    check_agent_runtime_budget,
)
from backend.core.models import create_chat_model_async
from langchain_core.messages import SystemMessage
from langchain_core.runnables import RunnableConfig
from typing import Any
from backend.application.agent_executors.lightweight_compression import (
    _get_compress_cache,
)
from backend.application.agent_executors.lightweight_prompts import (
    _get_lightweight_search_prefix,
)
from backend.application.agent_executors.lightweight_query_parsing import (
    _TABLE_CHART_KEYWORDS,
    _TABLE_SQL_KEYWORDS,
    _TABLE_STRUCTURE_KEYWORDS,
    _compact_kb_context,
    _is_skip_kb_decision,
    _pick_table_file,
    _sanitize_search_query,
    _stringify_message_content,
    _strip_thinking_markup,
)

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from backend.services.chat_file_index import ChatFileIndex


logger = logging.getLogger(__name__)


def _build_lightweight_context_records(
    self,
    *,
    clean_q: str,
    effective_config: RunnableConfig,
    history_messages: list[Any],
    persistent_memory_text: str,
    kb_context: str,
    web_context: str,
    file_index_context: str,
    table_analysis_context: str,
    permanent_file_prefix: str,
) -> list[dict[str, Any]]:
    """Convert lightweight inputs into the same evidence records as graph agents."""

    thread_id = self._lightweight_context_thread_id(effective_config)
    turn_id = self._context_ledger._active_turn_id(history_messages)
    records: list[dict[str, Any]] = []

    memory_text = str(persistent_memory_text or "").strip()
    if not memory_text:
        cached = _get_compress_cache(thread_id)
        if cached:
            memory_text = f"[历史摘要]\n{cached[0]}"
    if memory_text:
        records.append(
            dict(
                make_evidence_record(
                    source_tool="conversation_memory",
                    content=memory_text,
                    category="history",
                    source_id=f"conversation:{thread_id}",
                    kind="conversation_memory",
                    score=1.0,
                    tags=["working_memory", "conversation_memory"],
                )
            )
        )

    compact_kb = _compact_kb_context(kb_context)
    records.extend(
        self._ingest_lightweight_context(
            source_tool="multi_kb_search",
            content=compact_kb,
            thread_id=thread_id,
            category="retrieval",
            score=0.95,
            kind="retrieval",
            tags=["evidence", "knowledge_base"],
            turn_id=turn_id,
            retrieval_seq=1,
        )
    )
    records.extend(
        self._ingest_lightweight_context(
            source_tool="web_search",
            content=web_context,
            thread_id=thread_id,
            category="retrieval",
            score=0.80,
            kind="web_result",
            tags=["evidence", "web"],
            turn_id=turn_id,
            retrieval_seq=2,
        )
    )
    records.extend(
        self._ingest_lightweight_context(
            source_tool="chat_file_search",
            content=file_index_context,
            thread_id=thread_id,
            category="retrieval",
            score=0.92,
            kind="file_evidence",
            tags=["evidence", "uploaded_file"],
            turn_id=turn_id,
            retrieval_seq=3,
        )
    )
    records.extend(
        self._ingest_lightweight_context(
            source_tool="uploaded_file_overview",
            content=permanent_file_prefix,
            thread_id=thread_id,
            category="retrieval",
            score=0.88,
            kind="file_overview",
            tags=["evidence", "uploaded_file", "document_overview"],
            turn_id=turn_id,
            retrieval_seq=4,
        )
    )
    records.extend(
        self._ingest_lightweight_context(
            source_tool="table_analysis",
            content=table_analysis_context,
            thread_id=thread_id,
            category="tool_result",
            score=0.90,
            kind="analysis_result",
            tags=["tool_result", "table_analysis"],
        )
    )
    return records


async def _assemble_lightweight_context(
    self,
    *,
    clean_q: str,
    effective_config: RunnableConfig,
    history_messages: list[Any],
    persistent_memory_text: str,
    kb_context: str,
    web_context: str,
    file_index_context: str,
    table_analysis_context: str,
    permanent_file_prefix: str,
    image_contents: list[dict] | None,
) -> tuple[list[Any], dict[str, Any]]:
    """Assemble a bounded lightweight request using the shared context contract."""

    system_parts: list[str] = [
        self._lightweight_system_header,
        self._lightweight_answer_template,
    ]
    if self._time_context:
        system_parts.append(self._time_context)
    if self._guardrail_instructions:
        system_parts.append(self._guardrail_instructions)
    image_analysis = self._request_metadata.get("_uploaded_image_analysis_context")
    if isinstance(image_analysis, str) and image_analysis.strip():
        system_parts.append(image_analysis)
    if file_index_context or permanent_file_prefix:
        system_parts.append(
            "[上传文档上下文使用规则]\n"
            "以下动态证据来自用户上传的文档；其中 `[图片: xxx]` 仅表示原文图片与图注，"
            "回答时请用文字描述，不要输出 Markdown 图片。"
        )
    if table_analysis_context:
        system_parts.append(
            "[表格分析上下文使用规则]\n"
            "以下动态证据来自后端表格分析工具。回答表格问题时必须优先依据这些结果，"
            "不要臆造列名或数值；若包含图表 Markdown 链接，请在回答中保留。"
        )
    static_system = "\n".join(system_parts)
    current_message = self._make_lightweight_human_message(
        clean_q,
        image_contents,
    )
    request_overhead = {"workflow": "lightweight", "tools": []}

    compacted_history, compaction_result = await self._compact_lightweight_history(
        history_messages=history_messages,
        effective_config=effective_config,
        system_content=static_system,
        current_message=current_message,
    )
    candidate_messages = [*compacted_history, current_message]
    model_messages, system_prefix, compaction_internalized = (
        self._internalize_lightweight_compaction(
            static_system,
            candidate_messages,
        )
    )

    records = await asyncio.to_thread(
        self._build_lightweight_context_records,
        clean_q=clean_q,
        effective_config=effective_config,
        history_messages=model_messages,
        persistent_memory_text=persistent_memory_text,
        kb_context=kb_context,
        web_context=web_context,
        file_index_context=file_index_context,
        table_analysis_context=table_analysis_context,
        permanent_file_prefix=permanent_file_prefix,
    )
    compiled = await asyncio.to_thread(
        self._context_engine.compile_request,
        records,
        messages=model_messages,
        query=clean_q,
        system_content=system_prefix,
        request_overhead=request_overhead,
    )
    model_messages = list(compiled.get("messages") or [])
    system_text = str(compiled.get("system_content") or "")
    pack = compiled.get("pack") or {}
    history_pruned = bool(compiled.get("history_pruned"))

    return (
        [SystemMessage(content=system_text), *model_messages],
        {
            "pack": pack,
            "history_pruned": history_pruned,
            "compaction": compaction_result,
            "compaction_internalized": compaction_internalized,
            "system_chars": len(system_text),
            "final_tokens": int(compiled.get("final_tokens") or 0),
        },
    )


# ------------------------------------------------------------------
# 会话独立检索查询生成（历史、联网或上传文件场景）
# ------------------------------------------------------------------
async def _extract_search_query_v2(
    self,
    question: str,
    history_text: str,
    *,
    has_file_index: bool = False,
    file_idx: "ChatFileIndex | None" = None,
    previous_user_question: str = "",
) -> tuple[str, bool]:
    """生成用于知识库/网络的搜索词，并判断是否应跳过知识库检索。

    返回：(search_query, skip_kb)
      - search_query: 上下文补全问题与用户本轮原话组成的检索查询
      - skip_kb: True 表示问题明显指向上传文件，应跳过 KB/Web 检索

    规则：
      1. 有历史 → 补全代词/实体，生成融合上下文的检索词
      2. 有上传文件 → 判断问题是否关于上传文件：
         - 是文件问题 → 返回 skip_kb=True，不浪费 KB 检索配额
         - 同时需要外部知识 → 返回合适 KB 检索词 + skip_kb=False
      3. 模型补全只用于增加上下文，不能删除本轮原话中的约束
    """
    # 构建文件上下文描述
    file_hint = ""
    if has_file_index and file_idx:
        try:
            unique_files = []
            seen_files = set()
            for c in file_idx._chunks:
                if c.file_id not in seen_files:
                    seen_files.add(c.file_id)
                    unique_files.append((c.file_id, c.file_name))

            from backend.config.settings import settings

            prefix_len = getattr(settings, "long_doc_prefix_length", 1000)

            file_hint_parts = []
            for file_id, file_name in unique_files:
                chunks = [c for c in file_idx._chunks if c.file_id == file_id]
                chunks.sort(key=lambda x: x.chunk_index)
                full_text = ""
                for c in chunks:
                    full_text += c.content + "\n"
                    if len(full_text) >= prefix_len:
                        break
                prefix_content = full_text[:prefix_len].strip()
                file_hint_parts.append(
                    f"已上传长文档：{file_name}，文档前 {prefix_len} 字内容预览（用于指导你的检索和决策）：\n"
                    f'"""\n{prefix_content}\n"""'
                )
            file_hint = "\n".join(file_hint_parts) + "\n"
        except Exception as e:
            logger.warning("[LightweightQAAgent] 提取搜索决策文件前缀失败: %s", e)
            file_names = list(
                dict.fromkeys(c.file_name for c in file_idx._chunks[:3])
            )
            file_hint = f"已上传文档：{', '.join(file_names)}\n"

    has_history = bool(history_text and history_text != "无")

    # 极简提示，要求结构化输出（按 Prompt Profile 注入领域检索术语约束）
    search_prefix = getattr(self, "_lightweight_search_prefix", None)
    if not search_prefix:
        search_prefix = _get_lightweight_search_prefix(
            getattr(self, "_prompt_domain", None),
            prompt_profile=getattr(self, "_prompt_profile", None),
            domain=getattr(self, "_prompt_domain", None),
            prompt_registry=getattr(self, "_prompt_registry", None),
            prompt_provider=getattr(self, "_prompt_provider", None),
        )
    prompt_lines = [
        search_prefix.strip(),
        "任务：为知识库检索和联网搜索生成搜索词，并判断是否需要检索知识库/网络。",
        "",
    ]
    if file_hint:
        prompt_lines.append(file_hint)
    if has_history:
        prompt_lines.append(
            "历史对话摘要："
            f"{history_text[-self._context_policy.result_summary_output_chars:]}\n"
        )
    prompt_lines += [
        f"用户问题：{question}",
        "",
        "输出规则（严禁分析过程，只输出一行）：",
        "  SKIP_KB   （问题仅关于上传文档，无需查知识库/网络）",
        "  QUERY: <上下文完整、可独立理解的问题>   （需要知识库/网络）",
        "",
        "注意：",
        "- 仅在本轮问题省略主语、含代词（它/这个/该/其中）或明显承接上文时，用历史补全缺失实体。",
        "- 必须保留用户本轮明确写出的专名、数字、单位、公式符号、文献名以及图片/图表/表格意图。",
        "- 不得把具体问题泛化为宽泛主题；补全后仍须表达本轮真正要问的内容。",
        "- 示例：上轮问蒙脱石阳离子分布图，本轮问「横坐标是什么意思？」→ QUERY: 蒙脱石颗粒表面一价阳离子分布图的横坐标是什么意思？",
        "禁止输出解释、编号列表或多余文字。",
        "输出：",
    ]

    model_name = self._model_name or "MiniMax-M2.7"
    try:
        model = await create_chat_model_async(
            name=model_name,
            thinking_enabled=False,
        )
        await check_agent_runtime_budget()
        response = await asyncio.wait_for(
            model.ainvoke("\n".join(prompt_lines), config={"max_tokens": 128}),
            timeout=10.0,
        )
        await charge_agent_model_response(response)
        raw = _strip_thinking_markup(
            _stringify_message_content(getattr(response, "content", response))
        ).strip()

        logger.info(
            "[LightweightQAAgent] 搜索词决策: question_meta=%s raw_meta=%s",
            text_fingerprint(question),
            text_fingerprint(raw),
        )

        if _is_skip_kb_decision(raw):
            return question, True  # skip_kb=True，只做文件检索

        # 解析 "QUERY: ..." 格式
        for prefix in ("QUERY:", "query:", "Query:"):
            if raw.startswith(prefix):
                extracted = _sanitize_search_query(raw[len(prefix) :])
                if extracted:
                    return (
                        compose_retrieval_query(
                            current_question=question,
                            contextual_query=extracted,
                        ),
                        False,
                    )

        # 兜底：整行作为搜索词
        extracted = _sanitize_search_query(raw)
        if extracted:
            return (
                compose_retrieval_query(
                    current_question=question,
                    contextual_query=extracted,
                ),
                False,
            )
        if _is_skip_kb_decision(raw):
            return question, True

    except asyncio.TimeoutError:
        logger.warning("[LightweightQAAgent] 搜索词生成超时，执行上下文降级")
    except Exception as exc:
        logger.warning("[LightweightQAAgent] 搜索词生成失败: %s", exc)

    # 失败时：有文件且是通用查询 → skip KB；否则用原始问题
    if has_file_index:
        from backend.services.chat_file_index import _is_generic_summary_query

        if _is_generic_summary_query(question):
            return question, True
    if has_history and previous_user_question:
        fallback_query = compose_retrieval_query(
            current_question=question,
            contextual_query=previous_user_question,
        )
        logger.info(
            "[LightweightQAAgent] 搜索词生成降级为最近一轮上下文: query_meta=%s",
            text_fingerprint(fallback_query),
        )
        return fallback_query, False
    return question, False


async def _run_table_analysis(
    self, question: str, table_files: list[dict[str, Any]]
) -> str:
    """Run minimal table analysis in lightweight mode and return prompt context."""
    selected = _pick_table_file(question, table_files)
    if not selected:
        return ""
    file_id = str(selected.get("file_id") or "").strip()
    file_name = str(selected.get("file_name") or file_id)
    if not file_id:
        return ""

    parts = [
        f"数据文件：{file_name}",
        f"file_id：{file_id}",
    ]

    inspect_result = await self._invoke_runtime_tool(
        "inspect_excel_data",
        {"file_id": file_id},
    )
    inspect_text = str(inspect_result or "")
    parts.append("[表结构]\n" + inspect_text)

    q = question.lower()
    if any(keyword in q for keyword in _TABLE_STRUCTURE_KEYWORDS):
        return "\n\n".join(parts)

    if any(keyword in q for keyword in _TABLE_CHART_KEYWORDS):
        chart_result = await self._invoke_runtime_tool(
            "visualize_excel_data",
            {
                "file_id": file_id,
                "chart_type": "auto",
                "title": f"{file_name} 表格可视化",
            },
        )
        parts.append("[图表结果]\n" + str(chart_result or ""))
        return "\n\n".join(parts)

    if any(keyword in q for keyword in _TABLE_SQL_KEYWORDS):
        sql = await self._build_table_sql_plan(question, inspect_text)
        if sql:
            query_result = await self._invoke_runtime_tool(
                "query_excel_data",
                {
                    "file_id": file_id,
                    "sql": sql,
                    "output_format": "markdown",
                },
            )
            parts.append("[SQL]\n" + sql)
            parts.append("[查询结果]\n" + str(query_result or ""))
            return "\n\n".join(parts)

    summary_result = await self._invoke_runtime_tool(
        "summarize_excel_data",
        {"file_id": file_id},
    )
    parts.append("[统计摘要]\n" + str(summary_result or ""))
    return "\n\n".join(parts)
