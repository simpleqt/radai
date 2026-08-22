from backend.application.event_stream.thinking_filter import split_thinking_segments


def test_split_thinking_segments_separates_visible_and_thinking():
    visible, thinking, in_thought, carry = split_thinking_segments(
        "可见内容<think>隐藏推理</think>结尾",
        False,
        "",
    )

    assert visible == "可见内容结尾"
    assert thinking == "隐藏推理"
    assert in_thought is False
    assert carry == ""


def test_split_thinking_segments_handles_unclosed_tag_across_chunks():
    visible, thinking, in_thought, carry = split_thinking_segments(
        "正文<think>思考片段",
        False,
        "",
    )
    assert visible == "正文"
    assert thinking == "思考片段"
    assert in_thought is True

    visible2, thinking2, in_thought2, carry2 = split_thinking_segments(
        "继续</think>答案",
        in_thought,
        carry,
    )
    assert visible2 == "答案"
    assert thinking2 == "继续"
    assert in_thought2 is False
    assert carry2 == ""
