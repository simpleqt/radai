from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from backend.application.conversations.conversation_store import (
    ConversationAgentMismatchError,
    ConversationStore,
)
from backend.application.conversations.errors import ConversationDeletedError
from backend.application.conversations.local_catalog import (
    SQLiteConversationCatalog,
    get_local_conversation_catalog,
)
from backend.application.conversations.session_memory import ConversationMemoryStore


def configure_local_catalog(monkeypatch, tmp_path: Path) -> Path:
    catalog_path = tmp_path / "data" / "conversations" / "conversations.db"
    monkeypatch.setenv("DOC_AGENT_TEST_RUNTIME_ROOT", str(tmp_path))
    monkeypatch.setenv("AGENT_CONVERSATION_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENT_CONVERSATION_SQLITE_PATH", str(catalog_path))
    monkeypatch.setenv("AGENT_CONVERSATION_WRITES_ENABLED", "true")
    return catalog_path


async def test_local_catalog_supports_conversation_and_memory_flow(
    monkeypatch,
    tmp_path,
):
    catalog_path = configure_local_catalog(monkeypatch, tmp_path)
    store = ConversationStore()

    await store.ensure_runtime_conversation(
        user_id="user-1",
        thread_id="thread-1",
        question="第一轮测试问题",
        agent_code="ne",
        workflow_name="lightweight",
        metadata={"source": "test"},
    )
    await store.ensure_runtime_conversation(
        user_id="user-1",
        thread_id="thread-1",
        question="第二轮测试问题",
        agent_code="ne",
        workflow_name="lightweight",
    )

    catalog = get_local_conversation_catalog()
    assert isinstance(catalog, SQLiteConversationCatalog)
    assert catalog.path == catalog_path.resolve()
    conversations = await catalog.list_for_user(user_id="user-1", limit=20, offset=0)
    assert len(conversations) == 1
    assert conversations[0].title == "第一轮测试问题"
    assert (
        conversations[0].metadata_["agent_runtime"]["last_question"] == "第二轮测试问题"
    )

    memory_store = ConversationMemoryStore()
    assert await memory_store.save(
        user_id="user-1",
        thread_id="thread-1",
        memory={"summary": "已确认隔离会话可持久保存"},
    )
    memory = await memory_store.load(user_id="user-1", thread_id="thread-1")
    assert memory is not None
    assert memory["summary"] == "已确认隔离会话可持久保存"

    await store.update_conversation_images(
        user_id="user-1",
        thread_id="thread-1",
        registered_images={"[IMAGE:001]": {"url": "/isolated/image.png"}},
    )
    record = await catalog.get(user_id="user-1", thread_id="thread-1")
    assert record is not None
    assert (
        record.metadata_["registered_images"]["[IMAGE:001]"]["url"]
        == "/isolated/image.png"
    )
    assert await store.get_registered_images(
        user_id="user-1",
        thread_id="thread-1",
    ) == {"[IMAGE:001]": {"url": "/isolated/image.png"}}

    await catalog.rename(user_id="user-1", thread_id="thread-1", title="测试会话")
    renamed = await catalog.get(user_id="user-1", thread_id="thread-1")
    assert renamed is not None
    assert renamed.title == "测试会话"

    await catalog.set_deleted(user_id="user-1", thread_id="thread-1", deleted=True)
    assert (
        await catalog.get(
            user_id="user-1",
            thread_id="thread-1",
            include_deleted=False,
        )
        is None
    )


async def test_local_catalog_keeps_agent_ownership(monkeypatch, tmp_path):
    configure_local_catalog(monkeypatch, tmp_path)
    store = ConversationStore()
    await store.ensure_runtime_conversation(
        user_id="user-1",
        thread_id="thread-1",
        question="hello",
        agent_code="ne",
        workflow_name="full",
    )

    with pytest.raises(ConversationAgentMismatchError):
        await store.ensure_runtime_conversation(
            user_id="user-1",
            thread_id="thread-1",
            question="reuse",
            agent_code="csh",
            workflow_name="full",
        )


async def test_local_catalog_merges_concurrent_image_registrations(
    monkeypatch,
    tmp_path,
):
    configure_local_catalog(monkeypatch, tmp_path)
    store = ConversationStore()
    await store.ensure_runtime_conversation(
        user_id="user-1",
        thread_id="thread-1",
        question="image test",
        agent_code="ne",
        workflow_name="full",
    )

    await asyncio.gather(
        *(
            store.update_conversation_images(
                user_id="user-1",
                thread_id="thread-1",
                registered_images={f"[IMAGE:{index:03d}]": {"index": index}},
            )
            for index in range(10)
        )
    )

    catalog = get_local_conversation_catalog()
    assert catalog is not None
    record = await catalog.get(user_id="user-1", thread_id="thread-1")
    assert record is not None
    assert len(record.metadata_["registered_images"]) == 10


def test_local_catalog_must_stay_inside_test_runtime(monkeypatch, tmp_path):
    runtime_root = tmp_path / "runtime"
    monkeypatch.setenv("DOC_AGENT_TEST_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("AGENT_CONVERSATION_STORE_BACKEND", "sqlite")
    monkeypatch.setenv(
        "AGENT_CONVERSATION_SQLITE_PATH",
        str(tmp_path / "outside" / "conversations.db"),
    )

    with pytest.raises(RuntimeError, match="must stay inside"):
        get_local_conversation_catalog()


async def test_local_catalog_admin_listing_and_stats(monkeypatch, tmp_path):
    configure_local_catalog(monkeypatch, tmp_path)
    catalog = get_local_conversation_catalog()
    assert catalog is not None

    for user_id, thread_id, question in (
        ("user-1", "thread-1", "one"),
        ("user-1", "thread-2", "two"),
        ("user-2", "thread-3", "three"),
    ):
        await catalog.ensure_runtime_conversation(
            user_id=user_id,
            thread_id=thread_id,
            title=question,
            agent_code="ne",
            workflow_name="lightweight",
            question=question,
            metadata=None,
        )
    await catalog.set_deleted(user_id="user-1", thread_id="thread-2", deleted=True)

    active_rows, active_total = await catalog.list_all(limit=20, offset=0)
    all_rows, all_total = await catalog.list_all(
        limit=20,
        offset=0,
        include_deleted=True,
    )
    user_rows, user_total = await catalog.list_all(
        limit=20,
        offset=0,
        include_deleted=True,
        user_ids=["user-1"],
    )
    stats = await catalog.stats()

    assert active_total == len(active_rows) == 2
    assert all_total == len(all_rows) == 3
    assert user_total == len(user_rows) == 2
    assert {row.user_id for row in user_rows} == {"user-1"}
    assert stats.total == 3
    assert stats.active == 2
    assert stats.deleted == 1
    assert stats.unique_users == 2
    assert stats.latest_updated_at is not None


def test_local_catalog_accepts_generic_runtime_root(monkeypatch, tmp_path):
    runtime_root = tmp_path / "production-runtime"
    catalog_path = runtime_root / "data" / "conversations" / "conversations.db"
    monkeypatch.delenv("DOC_AGENT_TEST_RUNTIME_ROOT", raising=False)
    monkeypatch.setenv("DOC_AGENT_RUNTIME_ROOT", str(runtime_root))
    monkeypatch.setenv("AGENT_CONVERSATION_STORE_BACKEND", "sqlite")
    monkeypatch.setenv("AGENT_CONVERSATION_SQLITE_PATH", str(catalog_path))

    catalog = get_local_conversation_catalog()

    assert catalog is not None
    assert catalog.path == catalog_path.resolve()


def _direct_catalog(tmp_path: Path) -> SQLiteConversationCatalog:
    return SQLiteConversationCatalog(tmp_path / "catalog" / "conversations.db")


async def _ensure(
    catalog: SQLiteConversationCatalog,
    *,
    thread_id: str = "thread-1",
    title: str = "新对话",
    question: str = "hello",
    agent_code: str = "ne",
) -> None:
    await catalog.ensure_runtime_conversation(
        user_id="user-1",
        thread_id=thread_id,
        title=title,
        agent_code=agent_code,
        workflow_name="full",
        question=question,
        metadata=None,
    )


async def test_local_catalog_rejects_reuse_of_deleted_thread(tmp_path):
    catalog = _direct_catalog(tmp_path)
    await _ensure(catalog)
    await catalog.set_deleted(user_id="user-1", thread_id="thread-1", deleted=True)

    with pytest.raises(ConversationDeletedError):
        await _ensure(catalog, question="reuse after delete")


async def test_local_catalog_concurrent_ensure_is_idempotent(tmp_path):
    catalog = _direct_catalog(tmp_path)

    await asyncio.gather(
        *(_ensure(catalog, question=f"round-{index}") for index in range(5))
    )

    rows, total = await catalog.list_all(limit=20, offset=0, include_deleted=True)
    assert total == len(rows) == 1
    record = rows[0]
    assert record.thread_id == "thread-1"
    assert record.metadata_ is not None
    assert record.metadata_["agent_runtime"]["agent_code"] == "ne"


async def test_local_catalog_preserves_custom_title_and_replaces_default(tmp_path):
    catalog = _direct_catalog(tmp_path)

    # 用户已有自定义标题:后续轮次不得覆盖。
    await _ensure(catalog, thread_id="custom", title="自定义标题")
    await _ensure(catalog, thread_id="custom", title="第二轮问题")
    record = await catalog.get(user_id="user-1", thread_id="custom")
    assert record is not None
    assert record.title == "自定义标题"

    # 占位默认标题「新对话」:下一轮真实问题应顶替它。
    await catalog.rename(user_id="user-1", thread_id="placeholder", title="新对话")
    await _ensure(catalog, thread_id="placeholder", title="真实问题标题")
    record = await catalog.get(user_id="user-1", thread_id="placeholder")
    assert record is not None
    assert record.title == "真实问题标题"


async def test_local_catalog_truncates_last_question_to_500_chars(tmp_path):
    catalog = _direct_catalog(tmp_path)
    await _ensure(catalog, question="问" * 800)

    record = await catalog.get(user_id="user-1", thread_id="thread-1")
    assert record is not None
    assert len(record.metadata_["agent_runtime"]["last_question"]) == 500


async def test_local_catalog_rename_missing_thread_creates_placeholder(tmp_path):
    catalog = _direct_catalog(tmp_path)

    await catalog.rename(user_id="user-1", thread_id="ghost", title="占位标题")

    record = await catalog.get(user_id="user-1", thread_id="ghost")
    assert record is not None
    assert record.title == "占位标题"
    assert record.is_deleted is False


async def test_local_catalog_metadata_write_return_values(tmp_path):
    catalog = _direct_catalog(tmp_path)

    # 行不存在时必须返回 False,调用方以此决定是否回退。
    assert (
        await catalog.merge_metadata_value(
            user_id="user-1",
            thread_id="missing",
            key="k",
            value="v",
        )
        is False
    )
    assert (
        await catalog.merge_registered_images(
            user_id="user-1",
            thread_id="missing",
            registered_images={"[IMAGE:001]": {"url": "/x.png"}},
        )
        is False
    )

    await _ensure(catalog)
    assert (
        await catalog.merge_metadata_value(
            user_id="user-1",
            thread_id="thread-1",
            key="k",
            value="v",
        )
        is True
    )

    # 已删除的行对 merge 系列写入不可见。
    await catalog.set_deleted(user_id="user-1", thread_id="thread-1", deleted=True)
    assert (
        await catalog.merge_metadata_value(
            user_id="user-1",
            thread_id="thread-1",
            key="k2",
            value="v2",
        )
        is False
    )
    assert (
        await catalog.merge_registered_images(
            user_id="user-1",
            thread_id="thread-1",
            registered_images={"[IMAGE:001]": {"url": "/x.png"}},
        )
        is False
    )


async def test_local_catalog_tolerates_corrupted_metadata_rows(tmp_path):
    import aiosqlite

    catalog = _direct_catalog(tmp_path)
    await _ensure(catalog)

    # 直接把损坏的/非对象的 JSON 写进存量行,读取路径必须降级为 None 而不是抛错。
    async with aiosqlite.connect(str(catalog.path)) as connection:
        await connection.execute(
            "UPDATE conversations SET metadata = ? WHERE thread_id = ?",
            ("{not-json", "thread-1"),
        )
        await connection.commit()
    record = await catalog.get(user_id="user-1", thread_id="thread-1")
    assert record is not None
    assert record.metadata_ is None

    async with aiosqlite.connect(str(catalog.path)) as connection:
        await connection.execute(
            "UPDATE conversations SET metadata = ? WHERE thread_id = ?",
            ('["list", "not", "dict"]', "thread-1"),
        )
        await connection.commit()
    record = await catalog.get(user_id="user-1", thread_id="thread-1")
    assert record is not None
    assert record.metadata_ is None


async def test_local_catalog_metadata_roundtrips_non_ascii(tmp_path):
    catalog = _direct_catalog(tmp_path)
    await _ensure(catalog)

    payload = {"摘要": "地下水位持续下降", "nested": {"单位": "米"}}
    assert (
        await catalog.merge_metadata_value(
            user_id="user-1",
            thread_id="thread-1",
            key="报告",
            value=payload,
        )
        is True
    )

    record = await catalog.get(user_id="user-1", thread_id="thread-1")
    assert record is not None
    assert record.metadata_ is not None
    assert record.metadata_["报告"] == payload
