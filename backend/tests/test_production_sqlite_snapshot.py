from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from deploy.production import sqlite_snapshot


def _database(path: Path, schema: str, insert: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as database:
        database.executescript(schema)
        database.execute(insert)
        database.commit()


def test_snapshot_preserves_nested_conversation_catalog(tmp_path: Path) -> None:
    source = tmp_path / "data"
    _database(
        source / "checkpoints.db",
        "CREATE TABLE checkpoints(thread_id TEXT);",
        "INSERT INTO checkpoints VALUES ('thread-1')",
    )
    _database(
        source / "conversations" / "conversations.db",
        "CREATE TABLE conversations(id TEXT);",
        "INSERT INTO conversations VALUES ('conversation-1')",
    )

    pointer = sqlite_snapshot.snapshot(source, tmp_path / "backups", tmp_path / "state")
    snapshot_dir = Path(pointer["latest_snapshot"])
    verified = sqlite_snapshot.verify(snapshot_dir)

    assert (snapshot_dir / "checkpoints.db").is_file()
    assert (snapshot_dir / "conversations" / "conversations.db").is_file()
    assert set(verified["databases"]) == {
        "checkpoints.db",
        "conversations/conversations.db",
    }


def test_verify_rejects_manifest_path_escape(tmp_path: Path) -> None:
    source = tmp_path / "data"
    _database(
        source / "checkpoints.db",
        "CREATE TABLE checkpoints(thread_id TEXT);",
        "INSERT INTO checkpoints VALUES ('thread-1')",
    )
    _database(
        source / "conversations" / "conversations.db",
        "CREATE TABLE conversations(id TEXT);",
        "INSERT INTO conversations VALUES ('conversation-1')",
    )
    pointer = sqlite_snapshot.snapshot(source, tmp_path / "backups", tmp_path / "state")
    snapshot_dir = Path(pointer["latest_snapshot"])
    manifest_path = snapshot_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    first_details = next(iter(manifest["databases"].values()))
    manifest["databases"] = {"../outside.db": first_details}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuntimeError, match="not confined"):
        sqlite_snapshot.verify(snapshot_dir)
