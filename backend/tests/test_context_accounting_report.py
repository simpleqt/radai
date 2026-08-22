"""Read-only aggregation of per-thread context accounting from checkpoints."""

import sqlite3

import pytest
from langgraph.checkpoint.base import empty_checkpoint
from langgraph.checkpoint.sqlite import SqliteSaver

from backend.application.checkpointing.accounting_report import (
    collect_context_accounting,
)


def _write_checkpoint(saver, *, thread_id: str, channel_values: dict) -> None:
    # empty_checkpoint() mints a time-ordered uuid6 id, matching the saver's
    # "latest = MAX(checkpoint_id)" ordering contract.
    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = channel_values
    config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    saver.put(
        config,
        checkpoint,
        {"source": "test", "step": 1, "writes": {}, "parents": {}},
        {},
    )


@pytest.fixture()
def checkpoint_db(tmp_path):
    db_path = tmp_path / "checkpoints.db"
    conn = sqlite3.connect(str(db_path))
    saver = SqliteSaver(conn)
    saver.setup()
    yield db_path, saver
    conn.close()


def test_report_aggregates_budget_and_accounting(checkpoint_db):
    db_path, saver = checkpoint_db
    _write_checkpoint(
        saver,
        thread_id="thread-budget-only",
        channel_values={
            "context_budget": {
                "tokens_used": 1000,
                "dynamic_token_budget": 4000,
                "dynamic_tokens_used": 1000,
                "items_injected": 3,
            }
        },
    )
    _write_checkpoint(
        saver,
        thread_id="thread-full",
        channel_values={
            "context_budget": {
                "tokens_used": 3000,
                "dynamic_token_budget": 4000,
                "dynamic_tokens_used": 2000,
                "items_injected": 5,
            },
            "context_accounting": {
                "remaining_tokens": 90000,
                "jit_body_tokens": 5000,
                "navigator_tokens": 1200,
                "registry": {
                    "object_cache_hits": 7,
                    "objects_read": 9,
                    "selected_segments": 42,
                    "dropped_by_topk": 6,
                    "state_object_refs": 11,
                },
            },
        },
    )
    _write_checkpoint(saver, thread_id="thread-empty", channel_values={})

    report = collect_context_accounting(db_path)

    assert report.threads_total == 3
    assert report.threads_with_budget == 2
    assert report.threads_with_accounting == 1
    assert report.budget["tokens_used"]["count"] == 2
    assert report.budget["tokens_used"]["max"] == 3000
    assert report.budget["dynamic_utilization"]["min"] == 0.25
    assert report.budget["dynamic_utilization"]["max"] == 0.5
    assert report.accounting["selected_segments"]["max"] == 42
    assert report.accounting["state_object_refs"]["median"] == 11
    payload = report.as_dict()
    assert payload["db_path"] == str(db_path)


def test_report_uses_only_the_latest_checkpoint_per_thread(checkpoint_db):
    db_path, saver = checkpoint_db
    _write_checkpoint(
        saver,
        thread_id="thread-1",
        channel_values={"context_budget": {"tokens_used": 111}},
    )
    _write_checkpoint(
        saver,
        thread_id="thread-1",
        channel_values={"context_budget": {"tokens_used": 999}},
    )

    report = collect_context_accounting(db_path)

    assert report.threads_total == 1
    assert report.budget["tokens_used"]["min"] == 999
    assert report.budget["tokens_used"]["max"] == 999


def test_report_respects_thread_limit(checkpoint_db):
    db_path, saver = checkpoint_db
    for index in range(4):
        _write_checkpoint(
            saver,
            thread_id=f"thread-{index}",
            channel_values={"context_budget": {"tokens_used": 100 + index}},
        )

    report = collect_context_accounting(db_path, limit=2)

    assert report.threads_total == 4
    assert report.threads_with_budget == 2


def test_report_skips_damaged_tip_blobs_and_counts_them(checkpoint_db):
    db_path, saver = checkpoint_db
    _write_checkpoint(
        saver,
        thread_id="thread-good",
        channel_values={"context_budget": {"tokens_used": 500}},
    )
    _write_checkpoint(
        saver,
        thread_id="thread-bad-eof",
        channel_values={"context_budget": {"tokens_used": 700}},
    )
    _write_checkpoint(
        saver,
        thread_id="thread-bad-type",
        channel_values={"context_budget": {"tokens_used": 900}},
    )
    conn = sqlite3.connect(str(db_path))
    with conn:
        # Truncated msgpack body → "unexpected end of file" during parsing,
        # the damage shape observed in long-lived runtime databases.
        conn.execute(
            "UPDATE checkpoints SET checkpoint = x'92' "
            "WHERE thread_id = 'thread-bad-eof'"
        )
        # Decodable-but-wrong-type blob (msgpack int 0).
        conn.execute(
            "UPDATE checkpoints SET checkpoint = x'00' "
            "WHERE thread_id = 'thread-bad-type'"
        )
    conn.close()

    report = collect_context_accounting(db_path)

    assert report.threads_total == 3
    assert report.threads_failed == 2
    assert report.failure_sample
    assert report.threads_with_budget == 1
    assert report.budget["tokens_used"]["max"] == 500


def test_report_rejects_missing_or_foreign_database(tmp_path):
    with pytest.raises(FileNotFoundError):
        collect_context_accounting(tmp_path / "absent.db")

    other = tmp_path / "other.db"
    conn = sqlite3.connect(str(other))
    conn.execute("CREATE TABLE misc (id INTEGER PRIMARY KEY)")
    conn.close()
    with pytest.raises(ValueError):
        collect_context_accounting(other)
