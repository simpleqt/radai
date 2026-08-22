from backend.db import connection


class _RecordingCursor:
    def __init__(self, statements: list[str]):
        self._statements = statements
        self.closed = False

    def execute(self, statement: str) -> None:
        self._statements.append(statement)

    def close(self) -> None:
        self.closed = True


class _RecordingConnection:
    def __init__(self):
        self.statements: list[str] = []
        self.cursor_instance = _RecordingCursor(self.statements)

    def cursor(self) -> _RecordingCursor:
        return self.cursor_instance


def test_mysql_connections_are_forced_read_only_when_enabled(monkeypatch):
    monkeypatch.setenv("MYSQL_SESSION_READ_ONLY", "true")
    database = _RecordingConnection()

    connection._configure_mysql_connection(database)

    assert database.statements == [
        "SET NAMES utf8mb4",
        "SET SESSION TRANSACTION READ ONLY",
    ]
    assert database.cursor_instance.closed is True


def test_mysql_connections_keep_normal_mode_by_default(monkeypatch):
    monkeypatch.delenv("MYSQL_SESSION_READ_ONLY", raising=False)
    database = _RecordingConnection()

    connection._configure_mysql_connection(database)

    assert database.statements == ["SET NAMES utf8mb4"]
    assert database.cursor_instance.closed is True
