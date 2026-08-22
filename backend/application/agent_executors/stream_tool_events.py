"""工具/链/任务运行时事件处理实现(拆自 NEAgentStreamHandler,首参 self 为 handler 实例)。"""

from __future__ import annotations

import asyncio
import json
import time

from backend.application.agent_executors.stream_utils import (
    build_task_runtime_metadata,
    extract_report_metadata,
    extract_task_message_text,
    extract_task_result_metadata,
    extract_task_runtime_event_payload,
    extract_web_search_metadata,
    make_process_event,
    summarize_tool_output,
)
from backend.application.agent_executors.tool_presenters import (
    build_display_payload,
    has_presenter,
    output_has_error,
    resolve_phase,
)
from backend.application.agents.prompt_templates.ne import SUPPRESSED_TOOL_RESULT_TOOLS
from backend.application.artifacts.artifact_utils import (
    build_registered_artifact_metadata,
)
from backend.application.tools.result_metadata import parse_tool_result_metadata
from backend.config.loggers import get_logger
from typing import Any, AsyncIterator
from backend.application.agent_executors.stream_tool_evidence import (
    _GROUNDWATER_PROJECT_EVIDENCE_TOOLS,
    _coordinated_tool_evidence_records,
    _counts_toward_project_query_budget,
    _display_compaction_summary,
    _extract_context_compaction,
)


logger = get_logger(__name__)


async def _on_tool_start(self, event: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    tool_name = event.get("name")
    tool_run_id = self._tool_run_id(event)
    tool_run_key = tool_run_id or str(tool_name or "unknown")
    if tool_name:
        self._tools_invoked.append(str(tool_name))
    self._tool_started_at[tool_run_key] = time.monotonic()
    data = event.get("data", {}) or {}
    tool_input = data.get("input", {})

    if isinstance(tool_input, str):
        try:
            tool_input = json.loads(tool_input)
        except Exception:
            tool_input = {"query": tool_input}
    if not isinstance(tool_input, dict):
        tool_input = {"input": tool_input}
    self._tool_inputs[tool_run_key] = dict(tool_input)

    if tool_name == "multi_kb_search":
        q = tool_input.get("query", "未知")
        kb_ids = tool_input.get("kb_ids")
        effective_kb_ids = kb_ids if kb_ids else self._kb_ids
        from backend.application.knowledge_bases.catalog_service import (
            resolve_knowledge_base_labels,
        )

        try:
            kb_names = await asyncio.wait_for(
                resolve_knowledge_base_labels(effective_kb_ids),
                timeout=5.0,
            )
        except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041
            logger.warning("知识库标签解析超时，使用降级标签")
            kb_names = []
        kb_names_str = "、".join(kb_names) or "已授权知识库"
        title = "检索专业知识库"
        detail = f"正在{kb_names_str}中检索: '{q}'"
    elif tool_name == "web_search":
        q = tool_input.get("query", "未知")
        title = "联网搜索"
        detail = f"正在搜索全网资讯: '{q}'"
    elif tool_name == "academic_search":
        q = tool_input.get("query", "未知")
        title = "学术文献检索"
        detail = f"正在检索学术文献: '{q}'"
    elif tool_name == "task":
        desc = tool_input.get("description", "未知任务")
        title = "子智能体并行执行"
        detail = f"委派子任务: {desc}"
    elif tool_name == "save_deep_research_report":
        title = "生成研究报告"
        detail = "正在保存研究报告并上传 MinIO"
    else:
        title = "工具调用"
        detail = f"使用工具: {tool_name}"

    event_metadata: dict[str, Any] = {"input": tool_input}
    if tool_run_id:
        event_metadata["tool_run_id"] = tool_run_id
    yield {
        "type": "event",
        "event": make_process_event(
            kind="tool_start",
            title=title,
            detail=detail,
            tool=tool_name,
            level="active",
            metadata=event_metadata,
            event_key=(f"tool_start:{tool_run_id}" if tool_run_id else None),
        ),
    }


async def _on_tool_end(self, event: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    tool_name = event.get("name")
    tool_run_id = self._tool_run_id(event)
    tool_run_key = tool_run_id or str(tool_name or "unknown")
    elapsed_ms = None
    started_at = self._tool_started_at.pop(tool_run_key, None)
    tool_input = self._tool_inputs.pop(tool_run_key, {})
    if started_at is not None:
        elapsed_ms = int((time.monotonic() - started_at) * 1000)

    if not self._is_deep_research:
        self._any_tool_executed = True

    data = event.get("data", {}) or {}
    raw_output = data.get("output") or data.get("content") or data.get("result")
    summary = summarize_tool_output(raw_output)
    parsed_metadata = parse_tool_result_metadata(tool_name, raw_output)
    result_metadata: dict[str, Any] = parsed_metadata.as_dict()
    if tool_run_id:
        result_metadata["tool_run_id"] = tool_run_id

    if not self._is_deep_research:
        if tool_name in _GROUNDWATER_PROJECT_EVIDENCE_TOOLS:
            keep_as_project_evidence = True
            if tool_name == "query_project_tables":
                keep_as_project_evidence = _counts_toward_project_query_budget(raw_output)
                if keep_as_project_evidence:
                    self._project_table_query_count += 1
            if keep_as_project_evidence:
                self._append_project_evidence(
                    tool_name=str(tool_name or "unknown"),
                    raw_output=raw_output,
                    tool_input=tool_input,
                )
        elif tool_name == "multi_kb_search":
            evidence = self._stringify_evidence(raw_output, max_chars=10000)
            self._knowledge_target_missed = parsed_metadata.target_document_match is False
            self._append_evidence(
                self._retrieved_evidence_fragments,
                f"<knowledge_tool_result name=\"multi_kb_search\">\n{evidence}\n</knowledge_tool_result>",
                max_items=4,
            )
            if parsed_metadata.budget_exhausted and self._project_evidence_fragments:
                self._evidence_convergence_ready = True
        elif tool_name == "web_search":
            evidence = self._stringify_evidence(raw_output, max_chars=10000)
            self._append_evidence(
                self._web_evidence_fragments,
                f"<web_tool_result name=\"{tool_name}\">\n{evidence}\n</web_tool_result>",
                max_items=2,
            )

    if tool_name == "web_search":
        result_metadata.update(extract_web_search_metadata(raw_output))
    elif tool_name in {"save_deep_research_report", "generate_html_ppt"}:
        result_metadata.update(extract_report_metadata(raw_output))
    if self._capture_tool_evidence:
        evidence_records = _coordinated_tool_evidence_records(
            tool_name,
            raw_output,
            tool_run_id=tool_run_id,
        )
        if evidence_records:
            # The coordinator consumes this private field and removes it
            # before emitting the parent process event to clients.
            result_metadata["_coordination_evidence_records"] = evidence_records

    status_title = "工具调用完成"
    status_detail = f"{tool_name} 调用完成"

    if tool_name == "multi_kb_search":
        if parsed_metadata.target_document_match is False:
            status_title = "目标标准未命中"
            status_detail = "知识库仅返回相关资料，将继续使用联网搜索核验目标标准"
        else:
            status_title = "知识库检索完成"
            status_detail = "已成功获取相关的知识库文档"
    elif tool_name == "web_search":
        status_title = "网络搜索完成"
        status_detail = "已获取网页参考资讯"
    elif tool_name == "academic_search":
        status_title = "文献检索完成"
        status_detail = "已获取学术论文检索结果"
    elif tool_name == "task":
        status_title = "子任务完成"
        task_meta = extract_task_result_metadata(raw_output)
        if task_meta:
            result_metadata.update(task_meta)
            preview = str(task_meta.get("result_preview") or "").strip()
            if preview:
                summary = summarize_tool_output(preview, max_length=500)
            query_count = len(task_meta.get("search_queries") or []) if isinstance(task_meta.get("search_queries"), list) else 0
            url_count = len(task_meta.get("urls") or []) if isinstance(task_meta.get("urls"), list) else 0
            if query_count or url_count:
                status_detail = f"子任务执行完成（检索关键词 {query_count} 条，来源链接 {url_count} 条）"
            else:
                status_detail = "子任务执行并获取结果完成"
        if self._is_deep_research and not self.deep_research_grace_applied:
            self.deep_research_grace_applied = True
    elif tool_name == "save_deep_research_report":
        artifact_path = str(result_metadata.get("artifact_path") or "").strip()
        if artifact_path:
            status_title = "研究报告已保存"
            status_detail = "报告已写入文档并生成可访问链接"
            self.report_saved = True
        else:
            status_title = "研究报告保存失败"
            status_detail = "未获取到 artifact_path，稍后将触发自动兜底归档"
            self.report_saved = False
    elif tool_name == "generate_html_ppt":
        artifact_path = str(result_metadata.get("artifact_path") or "").strip()
        if artifact_path:
            status_title = "HTML PPT 已生成"
            status_detail = "演示文稿已按用户隔离流程保存并生成可预览链接"
        else:
            status_title = "HTML PPT 生成失败"
            status_detail = "未获取到 HTML 工件信息"

    tool_end_warning = (tool_name == "save_deep_research_report" and not self.report_saved) or (
        tool_name == "generate_html_ppt" and not str(result_metadata.get("artifact_path") or "").strip()
    )

    # presenter 把工具返回值翻译成结构化展示语义；没有 presenter 时返回 None，
    # 前端继续走降级映射，行为与改造前一致。
    display_payload = build_display_payload(
        tool_name,
        tool_input=tool_input if isinstance(tool_input, dict) else None,
        raw_output=raw_output,
        duration_ms=elapsed_ms,
    )
    if display_payload and output_has_error(raw_output):
        tool_end_warning = True

    yield {
        "type": "event",
        "event": make_process_event(
            kind="tool_end",
            title=status_title,
            detail=status_detail,
            tool=tool_name,
            level="warning" if tool_end_warning else "success",
            duration_ms=elapsed_ms,
            metadata=(
                {"tool_run_id": tool_run_id}
                if tool_run_id
                else None
            ),
            event_key=(f"tool_end:{tool_run_id}" if tool_run_id else None),
            phase=resolve_phase(tool_name) or None,
            display=display_payload,
        ),
    }

    # 有专属 presenter 时不再补发「结果摘要」：那条事件的 detail 就是原始 JSON 前 320 字符。
    # 只对已登记的工具抑制——JSON 报错兜底会命中检索类工具，那条事件仍要保留证据链路。
    should_emit_tool_result = tool_name not in SUPPRESSED_TOOL_RESULT_TOOLS and not (
        display_payload and has_presenter(tool_name)
    )
    if summary and should_emit_tool_result:
        if tool_name in {"multi_kb_search", "web_search", "academic_search", "task"}:
            self.collected_research_notes.append(f"- [{tool_name}] {summary}")
        tool_result_detail = summary
        if tool_name == "web_search":
            total = result_metadata.get("total_results")
            url_count = len(result_metadata.get("urls") or [])
            if isinstance(total, int) and url_count:
                tool_result_detail = f"共 {total} 条结果，{url_count} 个网页来源"
            elif url_count:
                tool_result_detail = f"共 {url_count} 个网页来源"
        yield {
            "type": "event",
            "event": make_process_event(
                kind="tool_result",
                title="结果摘要",
                detail=tool_result_detail,
                tool=tool_name,
                level="info",
                duration_ms=elapsed_ms,
                summary=summary,
                metadata=result_metadata,
                event_key=(f"tool_result:{tool_run_id}" if tool_run_id else None),
            ),
        }

    if tool_name in {"save_deep_research_report", "generate_html_ppt"}:
        artifact_path = str(result_metadata.get("artifact_path") or "").strip()
        if artifact_path and artifact_path not in self.sent_artifacts:
            self.sent_artifacts.add(artifact_path)
            artifact_title = str(
                result_metadata.get("title")
                or ("深度研究报告" if tool_name == "save_deep_research_report" else "HTML PPT")
            )
            try:
                artifact_metadata = await asyncio.wait_for(
                    build_registered_artifact_metadata(
                        internal_thread_id=self._thread_id,
                        artifact_path=artifact_path,
                        title=artifact_title,
                        minio_object=(
                            str(result_metadata.get("minio_object") or "") or None
                        ),
                    ),
                    timeout=10.0,
                )
            except (asyncio.TimeoutError, TimeoutError):  # noqa: UP041
                logger.warning(
                    "工具工件元数据注册超时: tool=%s path=%s",
                    tool_name,
                    artifact_path,
                )
                artifact_metadata = {
                    **result_metadata,
                    "id": artifact_path,
                    "title": artifact_title,
                    "type": "file",
                }
            yield {
                "type": "event",
                "event": make_process_event(
                    kind="artifact",
                    title=artifact_title,
                    detail="已生成工件，可通过服务器中转安全下载/预览",
                    level="success",
                    metadata=artifact_metadata,
                ),
            }
            if tool_name == "generate_html_ppt":
                # 模型在纯工具调用回合常不发可见正文，前端会显示 0 chunk；补一句落气泡的说明
                user_ack = f"已生成 HTML 演示文稿「{artifact_title}」，请在工件/附件区域打开预览或下载。\n"
                self.answer_fragments.append(user_ack)
                self.chunk_count += 1
                self._normal_output_chars += len(user_ack)
                self._html_ppt_succeeded = True
                yield {"type": "chunk", "content": user_ack}
        elif tool_name == "generate_html_ppt" and not artifact_path:
            fail_msg = (
                f"{status_title}：{status_detail}。\n\n"
                "请先 `read_local_skill_file(\"html-ppt\")` 核对 template/theme 名称与 `slides_outline` 格式，"
                "再重试 `generate_html_ppt`。\n"
            )
            self.answer_fragments.append(fail_msg)
            self.chunk_count += 1
            self._normal_output_chars += len(fail_msg)
            yield {"type": "chunk", "content": fail_msg}


async def _on_chain_end(self, event: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    compaction = _extract_context_compaction(event)
    if compaction is not None:
        window_id = str(compaction.get("window_id") or "").strip()
        if window_id and window_id not in self._seen_context_window_ids:
            self._seen_context_window_ids.add(window_id)
            tokens_before = int(compaction.get("tokens_before") or 0)
            tokens_after = int(compaction.get("tokens_after") or 0)
            removed_messages = int(compaction.get("removed_messages") or 0)
            summary = _display_compaction_summary(compaction.get("summary"))
            detail = (
                f"上下文窗口 {window_id}：约 {tokens_before} → {tokens_after} tokens，"
                f"归档 {removed_messages} 条历史消息"
            )
            yield {
                "type": "event",
                "event": make_process_event(
                    kind="context_compaction",
                    title="上下文窗口已压缩",
                    detail=detail,
                    tool="context_compression",
                    level="success",
                    summary=summary or None,
                    metadata={
                        key: value
                        for key, value in compaction.items()
                        if key != "summary"
                    },
                ),
            }
    chain_name = event.get("name", "chain")
    if "task" in str(chain_name).lower():
        yield {
            "type": "event",
            "event": make_process_event(
                kind="task_end",
                title="任务完成",
                detail=f"子任务执行完成: {chain_name}",
                tool=chain_name,
                level="success",
            ),
        }
    elif str(chain_name) == "ne-full-feature":
        yield {"type": "status", "content": "回答生成完成"}


async def _on_task_runtime(self, event: dict[str, Any]) -> AsyncIterator[dict[str, Any]]:
    task_runtime_event = extract_task_runtime_event_payload(event)
    if task_runtime_event is None:
        return

    runtime_type = str(task_runtime_event.get("type", "")).strip()
    task_id = str(task_runtime_event.get("task_id", "")).strip() or None
    description = str(task_runtime_event.get("description", "")).strip()

    if runtime_type.startswith("task_"):
        self.deep_research_last_task_runtime_at = time.monotonic()
        if task_id and runtime_type in {"task_started", "task_running", "task_tool_start", "task_tool_end"}:
            self.deep_research_active_task_ids.add(task_id)
        if task_id and runtime_type in {
            "task_completed",
            "task_failed",
            "task_timed_out",
            "task_cancelled",
        }:
            self.deep_research_active_task_ids.discard(task_id)

    runtime_metadata = build_task_runtime_metadata(
        task_runtime_event,
        task_id=task_id,
        description=description or None,
    )

    if runtime_type == "task_started":
        yield {
            "type": "event",
            "event": make_process_event(
                kind="task_start",
                title="子智能体已启动",
                detail=description or f"任务已启动: {task_id or 'unknown'}",
                tool="task",
                level="active",
                metadata=runtime_metadata,
            ),
        }
    elif runtime_type == "task_running":
        raw_message = task_runtime_event.get("message", "")
        message = extract_task_message_text(raw_message)
        message_text = str(task_runtime_event.get("message_text", "")).strip()
        if message_text:
            message = message_text
        if message:
            yield {
                "type": "event",
                "event": make_process_event(
                    kind="task_progress",
                    title="子智能体进展",
                    detail=message,
                    tool="task",
                    level="info",
                    metadata={
                        **runtime_metadata,
                        "message_index": task_runtime_event.get("message_index"),
                        "total_messages": task_runtime_event.get("total_messages"),
                    },
                ),
            }
    elif runtime_type in {"task_tool_start", "task_tool_end"}:
        tool_name = str(task_runtime_event.get("tool", "")).strip() or "unknown_tool"
        detail = str(task_runtime_event.get("detail", "")).strip()
        summary = str(task_runtime_event.get("summary", "")).strip()
        event_kind = "tool_start" if runtime_type == "task_tool_start" else "tool_result"
        yield {
            "type": "event",
            "event": make_process_event(
                kind=event_kind,
                title="子智能体工具调用" if runtime_type == "task_tool_start" else "子智能体工具结果",
                detail=detail or (f"{tool_name} started" if runtime_type == "task_tool_start" else f"{tool_name} completed"),
                tool=tool_name,
                level="active" if runtime_type == "task_tool_start" else "info",
                summary=summary if runtime_type == "task_tool_end" and summary else None,
                metadata={
                    **runtime_metadata,
                    "tool": tool_name,
                    "detail": detail,
                    "runtime_type": runtime_type,
                },
            ),
        }
    elif runtime_type == "task_completed":
        result_preview = str(task_runtime_event.get("result_preview", "")).strip()
        result_text = result_preview or summarize_tool_output(task_runtime_event.get("result"), max_length=2000)
        yield {
            "type": "event",
            "event": make_process_event(
                kind="task_end",
                title="子智能体已完成",
                detail=result_text or "子任务执行完成",
                tool="task",
                level="success",
                metadata={
                    **runtime_metadata,
                    "result": result_text,
                    "runtime_type": runtime_type,
                },
            ),
        }
    elif runtime_type in {"task_failed", "task_timed_out", "task_cancelled"}:
        error = str(task_runtime_event.get("error", "")).strip() or runtime_type
        yield {
            "type": "event",
            "event": make_process_event(
                kind="task_end",
                title=(
                    "子智能体已取消"
                    if runtime_type == "task_cancelled"
                    else "子智能体执行异常"
                ),
                detail=error,
                tool="task",
                level="warning",
                metadata={
                    **runtime_metadata,
                    "runtime_type": runtime_type,
                    "error": error,
                },
            ),
        }
