"""工具调用轮「幽灵正文」对账测试。

部分网关（如 glm）在发起工具调用前把预演 JSON 灌进 content 流：文本被实时
转发到浏览器，但不进入该轮留存消息——直播气泡多出一段刷新后会消失的内容
（2026-08-13 课程评价生产实录）。轮次结束时应剪掉幽灵尾巴并下发
message.replace，使直播与历史回放一致。
"""

from __future__ import annotations

from langchain_core.messages import AIMessage, AIMessageChunk

from backend.application.agent_executors.stream_handler import NEAgentStreamHandler


async def _drive(handler, texts: list[str], output: AIMessage) -> list[dict]:
    events: list[dict] = []
    for text in texts:
        async for ev in handler._on_model_stream(
            {"data": {"chunk": AIMessageChunk(content=text)}}
        ):
            events.append(ev)
    async for ev in handler._on_model_end({"data": {"output": output}}):
        events.append(ev)
    return events


async def test_tool_round_ghost_text_is_trimmed_and_replaced():
    handler = NEAgentStreamHandler(
        is_deep_research=False, thread_id="t-ghost", question="q"
    )
    handler._start_model_round()

    ghost = '```json { "course": { "name": "材料力学" }, "goals": [] }'
    output = AIMessage(
        content="材料就绪，现在开始解析成绩单和教学大纲。",
        response_metadata={"finish_reason": "tool_calls"},
        tool_calls=[
            {"name": "extract_syllabus", "args": {}, "id": "c1", "type": "tool_call"}
        ],
    )
    events = await _drive(
        handler, ["材料就绪，现在开始解析成绩单和教学大纲。", ghost], output
    )

    replaces = [e for e in events if e.get("type") == "message.replace"]
    assert replaces, "工具轮幽灵正文应触发 message.replace 收敛"
    assert replaces[-1]["content"] == "材料就绪，现在开始解析成绩单和教学大纲。"
    # 持久化片段与直播收敛后的文本一致（刷新后不再多出/少掉内容）
    assert "".join(handler.answer_fragments) == "材料就绪，现在开始解析成绩单和教学大纲。"


async def test_tool_round_without_ghost_emits_no_replace():
    handler = NEAgentStreamHandler(
        is_deep_research=False, thread_id="t-clean", question="q"
    )
    handler._start_model_round()

    output = AIMessage(
        content="正在解析材料。",
        response_metadata={"finish_reason": "tool_calls"},
        tool_calls=[
            {"name": "extract_grade_sheet", "args": {}, "id": "c2", "type": "tool_call"}
        ],
    )
    events = await _drive(handler, ["正在解析材料。"], output)

    assert not [e for e in events if e.get("type") == "message.replace"]
    assert "".join(handler.answer_fragments) == "正在解析材料。"


async def test_tool_internal_llm_stream_is_fully_suppressed():
    """工具内部 LLM 子调用（tool-internal-llm 标签）的流事件不得进入可见输出。

    生产根因：suggest_goal_mapping / extract_syllabus 的 JSON 子调用流被当成
    主模型正文转发，聊天气泡出现 ```json {"mappings": ...} 幽灵段。
    """
    handler = NEAgentStreamHandler(
        is_deep_research=False, thread_id="t-internal", question="q"
    )
    handler._start_model_round()

    events = []
    for kind, payload in (
        ("on_chat_model_start", {}),
        (
            "on_chat_model_stream",
            {"chunk": AIMessageChunk(content='```json\n{"mappings": []}\n```')},
        ),
        ("on_chat_model_end", {"output": AIMessage(content='{"mappings": []}')}),
    ):
        async for ev in handler.handle(
            {
                "event": kind,
                "tags": ["tool-internal-llm", "course-eval-internal"],
                "metadata": {"purpose": "course_eval_internal_json"},
                "data": payload,
            }
        ):
            events.append(ev)

    assert events == []
    assert handler.answer_fragments == []
    assert handler._round_streamed_visible_text == ""


async def test_normal_round_missing_tail_recovery_still_works():
    """无工具轮的 missing-text 补发路径不受对账逻辑影响。"""
    handler = NEAgentStreamHandler(
        is_deep_research=False, thread_id="t-tail", question="q"
    )
    handler._start_model_round()

    output = AIMessage(
        content="前半段后半段",
        response_metadata={"finish_reason": "stop"},
    )
    events = await _drive(handler, ["前半段"], output)

    assert not [e for e in events if e.get("type") == "message.replace"]
    chunks = [e["content"] for e in events if e.get("type") == "chunk"]
    assert "".join(chunks).endswith("后半段")
    assert "".join(handler.answer_fragments) == "前半段后半段"
