from __future__ import annotations

import gzip
import json
import stat
from pathlib import Path

from sqlalchemy import create_engine, text

from deploy.production import mysql_table_snapshot


def test_mysql_table_snapshot_format_is_private_and_verifiable(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE extraction_results ("
                    "extraction_id TEXT PRIMARY KEY, task_id TEXT, payload BLOB)"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO extraction_results VALUES "
                    "('result-1', 'task-1', :payload)"
                ),
                {"payload": b"private-test-payload"},
            )

        payload = mysql_table_snapshot.snapshot_table(
            engine, "extraction_results", tmp_path / "backups"
        )
        snapshot_dir = Path(payload["snapshot_dir"])
        verified = mysql_table_snapshot.verify_snapshot(snapshot_dir)

        assert verified["row_count"] == 1
        assert stat.S_IMODE((snapshot_dir / "manifest.json").stat().st_mode) == 0o600
        data_path = snapshot_dir / "extraction_results.jsonl.gz"
        assert stat.S_IMODE(data_path.stat().st_mode) == 0o600
        with gzip.open(data_path, "rt", encoding="utf-8") as source:
            header = json.loads(source.readline())
            row = json.loads(source.readline())
        assert header["table"] == "extraction_results"
        assert row["values"][2]["$type"] == "bytes-base64"
    finally:
        engine.dispose()


def test_mysql_table_snapshot_rejects_unsafe_identifier(tmp_path: Path) -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    try:
        try:
            mysql_table_snapshot.snapshot_table(
                engine, "extraction_results; DROP TABLE users", tmp_path
            )
        except ValueError as exc:
            assert "safe SQL identifier" in str(exc)
        else:
            raise AssertionError("unsafe SQL identifier was accepted")
    finally:
        engine.dispose()


def test_mysql_table_snapshot_rejects_manifest_path_escape(tmp_path: Path) -> None:
    snapshot_dir = tmp_path / "snapshot"
    snapshot_dir.mkdir()
    (snapshot_dir / "manifest.json").write_text(
        json.dumps({"data_file": "../outside.jsonl.gz"}), encoding="utf-8"
    )

    try:
        mysql_table_snapshot.verify_snapshot(snapshot_dir)
    except RuntimeError as exc:
        assert "not confined" in str(exc)
    else:
        raise AssertionError("manifest path escape was accepted")
