"""Docker 测试状态种子守卫回归。

守卫的目的是拦截「换库/截断」式状态丢失;检查点保留 GC 按线程裁剪历史
(每线程至少保留最新一个检查点)是合法维护,总行数下降不得卡死部署。
"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

_SEED_STATE_PATH = (
    Path(__file__).resolve().parents[2] / "deploy" / "docker-test" / "seed_state.py"
)
_spec = importlib.util.spec_from_file_location("docker_test_seed_state", _SEED_STATE_PATH)
seed_state = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_state)


def _make_checkpoint_db(path: Path, *, threads: int, rows_per_thread: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE TABLE checkpoints ("
            "thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT)"
        )
        for thread_index in range(threads):
            for row_index in range(rows_per_thread):
                database.execute(
                    "INSERT INTO checkpoints VALUES (?, '', ?)",
                    (f"thread-{thread_index}", f"ckpt-{row_index}"),
                )


def _make_catalog_db(path: Path, *, rows: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE TABLE conversations (user_id TEXT, is_deleted INTEGER)"
        )
        for index in range(rows):
            database.execute(
                "INSERT INTO conversations VALUES (?, 0)", (f"user-{index}",)
            )


def _write_markers(
    state_dir: Path,
    *,
    checkpoint_rows: int,
    thread_rows: int,
    catalog_rows: int,
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "checkpoint-migration.json").write_text(
        json.dumps(
            {
                "databases": {
                    "checkpoints.db": {
                        "checkpoint_rows": checkpoint_rows,
                        "thread_rows": thread_rows,
                    },
                    "guest_checkpoints.db": {
                        "checkpoint_rows": 0,
                        "thread_rows": 0,
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    (state_dir / "conversation-catalog-migration.json").write_text(
        json.dumps({"catalog": {"rows": catalog_rows}}),
        encoding="utf-8",
    )


def _args(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        force=False,
        target_state=tmp_path / "state",
        target_checkpoints=tmp_path / "checkpoints",
        target_conversations=tmp_path / "conversations",
    )


def _seed_fixture(
    tmp_path: Path,
    *,
    live_threads: int,
    live_rows_per_thread: int,
    baseline_rows: int,
    baseline_threads: int,
    catalog_rows: int = 5,
    baseline_catalog_rows: int = 5,
) -> SimpleNamespace:
    args = _args(tmp_path)
    _make_checkpoint_db(
        args.target_checkpoints / "checkpoints.db",
        threads=live_threads,
        rows_per_thread=live_rows_per_thread,
    )
    _make_checkpoint_db(
        args.target_checkpoints / "guest_checkpoints.db",
        threads=1,
        rows_per_thread=1,
    )
    _make_catalog_db(
        args.target_conversations / "conversations.db", rows=catalog_rows
    )
    _write_markers(
        args.target_state,
        checkpoint_rows=baseline_rows,
        thread_rows=baseline_threads,
        catalog_rows=baseline_catalog_rows,
    )
    return args


def test_gc_pruned_rows_do_not_block_preserved_state(tmp_path):
    # 基线 3 线程 × 10 行 = 30 行;GC 后每线程只剩 2 行(共 6 行),线程数不变
    args = _seed_fixture(
        tmp_path,
        live_threads=3,
        live_rows_per_thread=2,
        baseline_rows=30,
        baseline_threads=3,
    )
    result = seed_state.verify_existing(args)
    assert result is not None
    assert result["status"] == "preserved"
    assert result["checkpoint_rows"]["checkpoints.db"] == 6


def test_missing_threads_still_fail_the_guard(tmp_path):
    # 线程从 3 掉到 2 = 真正的状态丢失,必须拦截
    args = _seed_fixture(
        tmp_path,
        live_threads=2,
        live_rows_per_thread=10,
        baseline_rows=30,
        baseline_threads=3,
    )
    with pytest.raises(seed_state.SeedError, match="threads fell below baseline"):
        seed_state.verify_existing(args)


def test_catalog_row_loss_still_fails_the_guard(tmp_path):
    args = _seed_fixture(
        tmp_path,
        live_threads=3,
        live_rows_per_thread=10,
        baseline_rows=30,
        baseline_threads=3,
        catalog_rows=3,
        baseline_catalog_rows=5,
    )
    with pytest.raises(seed_state.SeedError, match="conversation catalog rows"):
        seed_state.verify_existing(args)
