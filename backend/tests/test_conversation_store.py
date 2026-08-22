from types import SimpleNamespace

import pytest

from backend.application.conversations import conversation_store as store_module
from backend.application.conversations.conversation_store import (
    ConversationAgentMismatchError,
    ConversationStore,
)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self):
        self.execute_calls = 0
        self.add_calls = 0
        self.commit_calls = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def execute(self, statement):
        self.execute_calls += 1
        return _ScalarResult(None)

    def add(self, value):
        self.add_calls += 1

    async def commit(self):
        self.commit_calls += 1

    async def rollback(self):
        pass


async def test_unknown_user_skips_conversation_persistence(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(
        store_module,
        "get_async_session_factory",
        lambda: lambda: session,
    )

    await ConversationStore().ensure_runtime_conversation(
        user_id="guest",
        thread_id="thread-1",
        question="hello",
        agent_code="ne",
        workflow_name="lightweight",
        metadata={"source": "test"},
    )

    assert session.execute_calls == 1
    assert session.add_calls == 0
    assert session.commit_calls == 0


async def test_runtime_conversation_writes_can_be_disabled(monkeypatch):
    monkeypatch.setenv("AGENT_CONVERSATION_WRITES_ENABLED", "false")

    def unexpected_session_factory():
        raise AssertionError("disabled persistence must not open a MySQL session")

    monkeypatch.setattr(
        store_module,
        "get_async_session_factory",
        unexpected_session_factory,
    )

    store = ConversationStore()
    await store.ensure_runtime_conversation(
        user_id="user-1",
        thread_id="isolated-thread",
        question="hello",
        agent_code="ne",
        workflow_name="lightweight",
    )
    await store.update_conversation_images(
        user_id="user-1",
        thread_id="isolated-thread",
        registered_images={"[IMAGE:001]": {"url": "https://example.test/image.png"}},
    )


async def test_existing_thread_cannot_change_agent_owner(monkeypatch):
    conversation = SimpleNamespace(
        is_deleted=False,
        title="existing",
        metadata_={"agent_runtime": {"agent_code": "ne"}},
    )

    class ExistingSession(_FakeSession):
        async def execute(self, statement):
            self.execute_calls += 1
            if self.execute_calls == 1:
                return _ScalarResult("user-1")
            return _ScalarResult(conversation)

    session = ExistingSession()
    monkeypatch.setattr(
        store_module,
        "get_async_session_factory",
        lambda: lambda: session,
    )

    with pytest.raises(ConversationAgentMismatchError):
        await ConversationStore().ensure_runtime_conversation(
            user_id="user-1",
            thread_id="thread-1",
            question="hello",
            agent_code="csh",
            workflow_name="lightweight",
        )

    assert session.commit_calls == 0


async def test_runtime_conversation_persistence_propagates_database_failure(
    monkeypatch,
):
    class BrokenSession(_FakeSession):
        async def execute(self, statement):
            raise RuntimeError("database unavailable")

    monkeypatch.setattr(
        store_module,
        "get_async_session_factory",
        lambda: lambda: BrokenSession(),
    )

    with pytest.raises(RuntimeError, match="database unavailable"):
        await ConversationStore().ensure_runtime_conversation(
            user_id="user-1",
            thread_id="thread-1",
            question="hello",
            agent_code="ne",
            workflow_name="full",
        )


def test_new_conversation_metadata_keeps_the_last_question():
    metadata = ConversationStore()._build_metadata(
        agent_code="groundwater",
        workflow_name="full",
        question="六塘片区地下水污染风险如何？",
        metadata={"groundwater_project_id": "liutang-groundwater"},
    )

    assert metadata["agent_runtime"]["last_question"] == "六塘片区地下水污染风险如何？"


async def test_update_conversation_images(monkeypatch):
    conversation = SimpleNamespace(
        metadata_={"registered_images": {"[IMAGE:001]": "http://old"}},
        updated_at=None,
    )

    class CustomSession(_FakeSession):
        async def execute(self, statement):
            return _ScalarResult(conversation)

    session = CustomSession()
    monkeypatch.setattr(
        store_module,
        "get_async_session_factory",
        lambda: lambda: session,
    )

    new_images = {"[IMAGE:002]": {"url": "http://new", "caption": "img2", "index": 2}}
    await ConversationStore().update_conversation_images(
        user_id="user-1",
        thread_id="thread-1",
        registered_images=new_images,
    )

    assert conversation.metadata_["registered_images"]["[IMAGE:001]"] == "http://old"
    assert (
        conversation.metadata_["registered_images"]["[IMAGE:002]"]["url"]
        == "http://new"
    )
    assert conversation.updated_at is not None
    assert session.commit_calls == 1


async def test_get_registered_images_from_database_catalog(monkeypatch):
    metadata = {
        "registered_images": {
            "[IMAGE:001]": {
                "object_name": "staging/kb-id/job/images/figure.jpg",
                "index": 1,
            }
        }
    }

    class CustomSession(_FakeSession):
        async def execute(self, statement):
            return _ScalarResult(metadata)

    monkeypatch.setattr(
        store_module,
        "get_async_session_factory",
        lambda: lambda: CustomSession(),
    )

    images = await ConversationStore().get_registered_images(
        user_id="user-1",
        thread_id="thread-1",
    )

    assert images == metadata["registered_images"]
