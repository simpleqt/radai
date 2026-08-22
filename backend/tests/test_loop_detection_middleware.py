from __future__ import annotations

from types import SimpleNamespace

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from backend.core.agents.middlewares.loop_detection import LoopDetectionMiddleware


def _runtime(**context):
    return SimpleNamespace(context=context)


def _tool_call(name: str, call_number: int, *, query: str | None = None) -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[
            {
                "name": name,
                "args": {"query": query or name},
                "id": f"call-{call_number}",
                "type": "tool_call",
            }
        ],
    )


def _tool_result(name: str, call_number: int, content: str) -> ToolMessage:
    return ToolMessage(
        name=name,
        content=content,
        tool_call_id=f"call-{call_number}",
    )


def test_non_consecutive_calls_do_not_accumulate_into_a_loop():
    middleware = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    messages = [
        HumanMessage(content="research the topic"),
        _tool_call("search", 1),
        _tool_result("search", 1, "result-1"),
        _tool_call("read", 2),
        _tool_result("read", 2, "result-2"),
        _tool_call("search", 3),
        _tool_result("search", 3, "result-3"),
        _tool_call("read", 4),
        _tool_result("read", 4, "result-4"),
        _tool_call("search", 5),
    ]

    assert middleware.after_model(
        {"messages": messages}, _runtime(thread_id="thread-1", run_id="run-1")
    ) is None


def test_consecutive_same_calls_warn_and_then_hard_stop():
    middleware = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    first_three = [HumanMessage(content="research the topic")]
    for call_number in range(1, 3):
        first_three.extend(
            [
                _tool_call("search", call_number),
                _tool_result("search", call_number, f"changing-result-{call_number}"),
            ]
        )
    first_three.append(_tool_call("search", 3))

    warning_update = middleware.after_model(
        {"messages": first_three}, _runtime(thread_id="thread-1", run_id="run-1")
    )

    assert warning_update is not None
    warning = warning_update["messages"][0]
    assert warning.type == "human"
    assert "LOOP DETECTED" in warning.content

    first_five = [HumanMessage(content="research the topic")]
    for call_number in range(1, 5):
        first_five.extend(
            [
                _tool_call("search", call_number),
                _tool_result("search", call_number, f"changing-result-{call_number}"),
            ]
        )
    last_message = _tool_call("search", 5)
    last_message.content = "still trying"
    first_five.append(last_message)

    hard_stop_update = middleware.after_model(
        {"messages": first_five}, _runtime(thread_id="thread-1", run_id="run-1")
    )

    assert hard_stop_update is not None
    stopped_message = hard_stop_update["messages"][0]
    assert stopped_message.tool_calls == []
    assert "still trying" in stopped_message.content
    assert "FORCED STOP" in stopped_message.content


def test_alternating_calls_with_repeated_results_are_no_progress_loop():
    middleware = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    messages = [
        HumanMessage(content="research the topic"),
        _tool_call("search", 1),
        _tool_result("search", 1, "same-search-result"),
        _tool_call("read", 2),
        _tool_result("read", 2, "same-read-result"),
        _tool_call("search", 3),
        _tool_result("search", 3, "same-search-result"),
        _tool_call("read", 4),
        _tool_result("read", 4, "same-read-result"),
        _tool_call("search", 5),
    ]

    update = middleware.after_model(
        {"messages": messages}, _runtime(thread_id="thread-1", run_id="run-1")
    )

    assert update is not None
    assert "LOOP DETECTED" in update["messages"][0].content


def test_results_differing_only_by_timestamp_or_uuid_are_no_progress():
    """仅时间戳/追踪 UUID 不同的重复结果不含新信息,必须计入无进展 streak。

    此前这类逐次生成噪声让每个结果 hash 都不同,重复循环被当作持续进展,
    只能等递归上限兜底。
    """

    middleware = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    messages = [
        HumanMessage(content="research the topic"),
        _tool_call("search", 1),
        _tool_result(
            "search",
            1,
            '{"rows": 3, "generated_at": "2026-08-14T02:10:11Z", '
            '"trace_id": "6f9619ff-8b86-4d11-b42d-00c04fc964ff"}',
        ),
        _tool_call("read", 2),
        _tool_result("read", 2, "same-read-result"),
        _tool_call("search", 3),
        _tool_result(
            "search",
            3,
            '{"rows": 3, "generated_at": "2026-08-14T02:10:47Z", '
            '"trace_id": "0e984725-c51c-4bf4-9960-e1c80e27aba0"}',
        ),
        _tool_call("read", 4),
        _tool_result("read", 4, "same-read-result"),
        _tool_call("search", 5),
    ]

    update = middleware.after_model(
        {"messages": messages}, _runtime(thread_id="thread-1", run_id="run-1")
    )

    assert update is not None
    assert "LOOP DETECTED" in update["messages"][0].content


def test_results_with_new_data_still_count_as_progress_after_masking():
    """真实数据变化(此处 rows 3→5)仍视为进展;只有挥发性标识被屏蔽。"""

    middleware = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    messages = [
        HumanMessage(content="research the topic"),
        _tool_call("search", 1),
        _tool_result(
            "search",
            1,
            '{"rows": 3, "generated_at": "2026-08-14T02:10:11Z"}',
        ),
        _tool_call("read", 2),
        _tool_result("read", 2, "same-read-result"),
        _tool_call("search", 3),
        _tool_result(
            "search",
            3,
            '{"rows": 5, "generated_at": "2026-08-14T02:10:47Z"}',
        ),
        _tool_call("read", 4),
    ]

    assert middleware.after_model(
        {"messages": messages}, _runtime(thread_id="thread-1", run_id="run-1")
    ) is None


def test_volatile_masking_preserves_date_only_literals():
    from backend.core.agents.middlewares.loop_detection import (
        _mask_volatile_identifiers,
    )

    text = (
        '{"sample_date": "2026-08-14", '
        '"generated_at": "2026-08-14T02:10:11+08:00"}'
    )
    masked = _mask_volatile_identifiers(text)

    assert '"sample_date": "2026-08-14"' in masked
    assert "02:10:11" not in masked
    assert "<ts>" in masked


def test_new_tool_result_resets_no_progress_streak():
    middleware = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    messages = [
        HumanMessage(content="research the topic"),
        _tool_call("search", 1),
        _tool_result("search", 1, "search-result-v1"),
        _tool_call("read", 2),
        _tool_result("read", 2, "read-result-v1"),
        _tool_call("search", 3),
        _tool_result("search", 3, "search-result-v2"),
        _tool_call("read", 4),
    ]

    assert middleware.after_model(
        {"messages": messages}, _runtime(thread_id="thread-1", run_id="run-1")
    ) is None


def test_different_structured_tool_errors_do_not_count_as_progress():
    middleware = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    messages = [
        HumanMessage(content="analyze the project"),
        _tool_call("bootstrap", 1),
        _tool_result("bootstrap", 1, '{"error":"path denied"}'),
        _tool_call("inspect", 2),
        _tool_result("inspect", 2, '{"error":"path denied"}'),
        _tool_call("kml_summary", 3),
    ]

    update = middleware.after_model(
        {"messages": messages}, _runtime(thread_id="thread-1", run_id="run-1")
    )

    assert update is not None
    assert "LOOP DETECTED" in update["messages"][0].content


def test_distinct_call_arguments_count_as_progress_even_with_same_result_text():
    middleware = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    messages = [HumanMessage(content="read successive pages")]
    for call_number in range(1, 4):
        messages.extend(
            [
                _tool_call("read", call_number, query=f"page-{call_number}"),
                _tool_result("read", call_number, "success"),
            ]
        )
    messages.append(_tool_call("read", 4, query="page-4"))

    assert middleware.after_model(
        {"messages": messages}, _runtime(thread_id="thread-1", run_id="run-1")
    ) is None


def test_new_user_turn_resets_detection_history():
    middleware = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    messages = [HumanMessage(content="first request")]
    for call_number in range(1, 5):
        messages.extend(
            [
                _tool_call("search", call_number),
                _tool_result("search", call_number, "same result"),
            ]
        )
    messages.extend(
        [
            _tool_call("search", 5),
            HumanMessage(content="new request"),
            _tool_call("search", 6),
        ]
    )

    assert middleware.after_model(
        {"messages": messages}, _runtime(thread_id="thread-1", run_id="run-2")
    ) is None


def test_same_thread_different_runs_do_not_share_detection_state():
    middleware = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)
    run_one_first = [HumanMessage(content="request one"), _tool_call("search", 1)]
    run_one_second = [
        *run_one_first,
        _tool_result("search", 1, "result-1"),
        _tool_call("search", 2),
    ]
    run_two_first = [HumanMessage(content="request two"), _tool_call("search", 3)]

    assert middleware.after_model(
        {"messages": run_one_first}, _runtime(thread_id="shared", run_id="run-1")
    ) is None
    assert middleware.after_model(
        {"messages": run_one_second}, _runtime(thread_id="shared", run_id="run-1")
    ) is None
    assert middleware.after_model(
        {"messages": run_two_first}, _runtime(thread_id="shared", run_id="run-2")
    ) is None


def test_anonymous_runs_do_not_share_a_default_history_bucket():
    middleware = LoopDetectionMiddleware(warn_threshold=3, hard_limit=5)

    for call_number in range(1, 7):
        state = {
            "messages": [
                HumanMessage(content=f"independent request {call_number}"),
                _tool_call("search", call_number),
            ]
        }
        assert middleware.after_model(state, _runtime()) is None
