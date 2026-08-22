"""第六轮修复回归：访客目录 GC 的删除键与范围。

生产实测：目录行 thread_id 即 guest_checkpoints.db 的内部线程键
（g_<scope>__t_<ext>），删除必须用原值且先 checkpoint 后目录。
"""

import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from backend.services.context_maintenance import ContextMaintenanceScheduler


def _make_dbs(tmp_path: Path, extra_rows: int = 0):
    catalog = tmp_path / "conversations.db"
    conn = sqlite3.connect(catalog)
    conn.execute(
        "CREATE TABLE conversations ("
        "user_id TEXT, thread_id TEXT, updated_at TEXT, title TEXT)"
    )
    old = (datetime.now() - timedelta(days=60)).isoformat()
    rows = [
        ("guest_003fe43e1f24e325", "g_003fe43e1f24e325__t_7d4db2", old),
        ("guest_00a8c253995e6397", "g_00a8c253995e6397__t_8bd640", old),
    ]
    for i in range(extra_rows):
        rows.append((f"guest_scope{i:08d}", f"g_scope{i:08d}__t_t{i}", old))
    conn.executemany(
        "INSERT INTO conversations (user_id, thread_id, updated_at) VALUES (?,?,?)",
        rows,
    )
    # 一条新近行，不得被删
    conn.execute(
        "INSERT INTO conversations VALUES (?,?,?,?)",
        (
            "guest_recent0000000000",
            "g_recent0000000000__t_recent",
            datetime.now().isoformat(),
            "t",
        ),
    )
    conn.commit()
    conn.close()

    guest_db = tmp_path / "guest_checkpoints.db"
    gconn = sqlite3.connect(guest_db)
    gconn.execute("CREATE TABLE checkpoints (thread_id TEXT, payload BLOB)")
    gconn.execute("CREATE TABLE writes (thread_id TEXT, payload BLOB)")
    for _, tid, _ in rows:
        gconn.execute("INSERT INTO checkpoints VALUES (?,?)", (tid, b"x"))
        gconn.execute("INSERT INTO writes VALUES (?,?)", (tid, b"x"))
    gconn.execute(
        "INSERT INTO checkpoints VALUES (?,?)", ("g_recent0000000000__t_recent", b"x")
    )
    gconn.commit()
    gconn.close()
    return catalog, guest_db


def _run(monkeypatch, catalog: Path, guest_db: Path):
    monkeypatch.setenv("AGENT_CONVERSATION_SQLITE_PATH", str(catalog))
    monkeypatch.setenv("GUEST_CHECKPOINT_DB", str(guest_db))
    monkeypatch.setenv("GUEST_CATALOG_RETENTION_DAYS", "30")
    return ContextMaintenanceScheduler._gc_guest_catalog()


def test_guest_gc_deletes_checkpoints_with_catalog_thread_id(tmp_path, monkeypatch):
    catalog, guest_db = _make_dbs(tmp_path)
    result = _run(monkeypatch, catalog, guest_db)

    assert result["removed_threads"] == 2
    gconn = sqlite3.connect(guest_db)
    remaining = {r[0] for r in gconn.execute("SELECT thread_id FROM checkpoints")}
    gconn.close()
    assert remaining == {"g_recent0000000000__t_recent"}
    conn = sqlite3.connect(catalog)
    left = conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE user_id LIKE 'guest_%'"
    ).fetchone()[0]
    conn.close()
    assert left == 1


def test_guest_gc_delete_scope_limited_to_collected_batch(tmp_path, monkeypatch):
    """DELETE 必须限定在本批收集行：超额积压不得删目录留 checkpoint。"""
    total = 600  # > LIMIT 500
    catalog, guest_db = _make_dbs(tmp_path, extra_rows=total - 2)
    result = _run(monkeypatch, catalog, guest_db)

    assert result["removed_threads"] == 500
    conn = sqlite3.connect(catalog)
    left_catalog = conn.execute(
        "SELECT COUNT(*) FROM conversations WHERE user_id LIKE 'guest_%'"
    ).fetchone()[0]
    conn.close()
    gconn = sqlite3.connect(guest_db)
    left_ckpts = gconn.execute("SELECT COUNT(*) FROM checkpoints").fetchone()[0]
    gconn.close()
    # 100 超期 + 1 新近留在目录；checkpoint 同步删 500，剩 100 超期 + 1 新近
    assert left_catalog == 101
    assert left_ckpts == 101
