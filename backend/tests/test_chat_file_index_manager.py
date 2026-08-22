from backend.services.chat_file_index import ChatFileIndexManager


def test_manager_hydrates_index_from_storage_on_cache_miss(monkeypatch):
    manager = ChatFileIndexManager(max_threads=4)
    load_calls: list[tuple[str, str]] = []

    def fake_load(user_id: str, thread_id: str):
        load_calls.append((user_id, thread_id))
        return [
            ("uf_a", "report-a.md", "alpha beta gamma"),
            ("uf_b", "report-b.md", "delta epsilon zeta"),
        ]

    monkeypatch.setattr(
        manager,
        "_load_thread_documents_from_storage",
        fake_load,
    )

    index = manager.get("thread-hydrate", user_id="user-a")

    assert index is not None
    assert index.file_count == 2
    assert index.chunk_count >= 2
    assert load_calls == [("user-a", "thread-hydrate")]

    cached = manager.get("thread-hydrate", user_id="user-a")
    assert cached is index
    assert load_calls == [("user-a", "thread-hydrate")]


def test_manager_has_files_uses_storage_hydration(monkeypatch):
    manager = ChatFileIndexManager(max_threads=4)

    monkeypatch.setattr(
        manager,
        "_load_thread_documents_from_storage",
        lambda user_id, thread_id: [("uf_a", "report-a.md", "alpha beta gamma")],
    )

    assert manager.has_files("thread-has-files", user_id="user-a") is True


def test_manager_ignores_empty_hydration_results(monkeypatch):
    manager = ChatFileIndexManager(max_threads=4)

    monkeypatch.setattr(
        manager,
        "_load_thread_documents_from_storage",
        lambda user_id, thread_id: [],
    )

    assert manager.get("thread-empty", user_id="user-a") is None
    assert manager.has_files("thread-empty", user_id="user-a") is False


def test_manager_isolates_same_thread_id_between_users():
    manager = ChatFileIndexManager(max_threads=4)

    user_a = manager.get_or_create("shared-thread", user_id="user-a")
    user_b = manager.get_or_create("shared-thread", user_id="user-b")
    user_a.add_document("a", "private alpha", "a.txt")

    assert user_a is not user_b
    assert user_a.file_count == 1
    assert user_b.file_count == 0
