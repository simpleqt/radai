from __future__ import annotations

from backend.application.agent_executors.stream_utils import (
    extract_task_result_metadata,
)


def test_extract_task_result_metadata_keeps_single_trailer_wire_shape():
    raw_output = (
        "Task Succeeded. Result: completed\n"
        'Metadata: {"result_preview":"final","result_sections":["summary"],'
        '"urls":["https://example.com"],"domains":["example.com"],'
        '"tool_calls":["search"],"search_queries":["query"],'
        '"message_count":2,"ignored":"value"}'
    )

    assert extract_task_result_metadata(raw_output) == {
        "result_preview": "final",
        "result_sections": ["summary"],
        "urls": ["https://example.com"],
        "domains": ["example.com"],
        "tool_calls": ["search"],
        "search_queries": ["query"],
        "message_count": 2,
    }


def test_extract_task_result_metadata_uses_only_last_formal_trailer():
    raw_output = (
        'Draft text containing Metadata: {"result_preview":"draft"}\n'
        "More task output\n"
        'Metadata: {"result_preview":"official","message_count":3}'
    )

    assert extract_task_result_metadata(raw_output) == {
        "result_preview": "official",
        "message_count": 3,
    }


def test_extract_task_result_metadata_does_not_fall_back_to_earlier_marker():
    raw_output = (
        'Metadata: {"result_preview":"untrusted-earlier-value"}\n'
        'Metadata: {"result_preview":"incomplete"'
    )

    assert extract_task_result_metadata(raw_output) == {}


def test_extract_task_result_metadata_requires_json_to_end_the_output():
    raw_output = (
        'Task result\nMetadata: {"result_preview":"not-a-trailer"}\n'
        "unexpected trailing output"
    )

    assert extract_task_result_metadata(raw_output) == {}


def test_extract_task_result_metadata_keeps_legacy_dict_output_compatible():
    raw_output = {
        "result_preview": "from-dict",
        "message_count": 0,
        "ignored": "value",
    }

    assert extract_task_result_metadata(raw_output) == {
        "result_preview": "from-dict",
        "message_count": 0,
    }
