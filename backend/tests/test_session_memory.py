from backend.application.conversations.session_memory import (
    build_memory_from_summary,
    format_memory_for_prompt,
    validate_memory_summary,
)
from langchain_core.messages import HumanMessage
from types import SimpleNamespace


def test_memory_summary_validation_rejects_invented_numbers_and_refs():
    source = "用户确认阈值为 17 mg/L，引用 [doc-a]，网址 https://example.com/a"

    assert validate_memory_summary(
        source_text=source,
        summary="用户确认阈值为 17 mg/L，引用 [doc-a]，网址 https://example.com/a",
    )
    assert not validate_memory_summary(
        source_text=source,
        summary="用户确认阈值为 18 mg/L，引用 [doc-a]，网址 https://example.com/a",
    )
    assert not validate_memory_summary(
        source_text=source,
        summary="用户确认阈值为 17 mg/L，引用 [doc-b]，网址 https://example.com/a",
    )


def test_memory_summary_validation_allows_dropping_noncritical_numbers():
    source = "\n".join(
        [
            "第 2024 轮普通闲聊提到页面 88、任务 991、草稿 12345。",
            "用户确认阈值为 17 mg/L，引用 [doc-a]。",
        ]
    )

    assert validate_memory_summary(
        source_text=source,
        summary="用户确认阈值为 17 mg/L，引用 [doc-a]。",
    )


def test_memory_summary_validation_keeps_critical_numbers_and_refs():
    source = "用户确认阈值为 17 mg/L，引用 [doc-a]。"

    assert not validate_memory_summary(
        source_text=source,
        summary="用户确认了阈值。",
    )


def test_memory_prompt_format_includes_persistent_fields():
    memory = build_memory_from_summary(summary="保留 17 mg/L 和 [doc-a]", message_count=4)
    memory["confirmed_facts"] = [{"fact": "标准 A 已确认"}]
    memory["open_questions"] = ["还缺少标准 B"]

    rendered = format_memory_for_prompt(memory)

    assert "[持久会话摘要]" in rendered
    assert "标准 A 已确认" in rendered
    assert "还缺少标准 B" in rendered


def test_memory_summary_populates_structured_fields():
    memory = build_memory_from_summary(
        summary="\n".join(
            [
                "关键实体：项目 Alpha、标准 GB 1",
                "用户偏好：回答保持简洁",
                "已确认事实：阈值为 17 mg/L",
                "待确认：标准 B 是否适用",
            ]
        ),
        message_count=8,
    )

    assert memory["key_entities"] == ["项目 Alpha", "标准 GB 1"]
    assert memory["user_preferences"] == ["用户偏好：回答保持简洁"]
    assert memory["confirmed_facts"] == ["已确认事实：阈值为 17 mg/L"]
    assert memory["open_questions"] == ["待确认：标准 B 是否适用"]


def test_memory_summary_tracks_message_id_watermark():
    memory = build_memory_from_summary(
        summary="已确认事实：阈值为 17 mg/L",
        message_count=8,
        message_id="message-8",
    )

    assert memory["last_compressed_message_id"] == "message-8"


def test_memory_summary_rejects_unsupported_textual_fact():
    assert not validate_memory_summary(
        source_text="用户要求回答保持简洁。",
        summary="用户要求回答保持简洁，并确认项目已经验收。",
    )


async def test_lightweight_history_loader_keeps_watermark_beyond_64_messages():
    from backend.application.agent_executors.lightweight_qa_agent import LightweightQAAgent

    messages = [HumanMessage(content=f"message-{index}", id=f"id-{index}") for index in range(100)]

    class State:
        values = {"messages": messages}

    class Graph:
        async def aget_state(self, config):
            return State()

    agent = object.__new__(LightweightQAAgent)
    agent._thread_id = "thread-1"
    agent._graph = Graph()

    loaded = await agent._load_recent_messages({"configurable": {"thread_id": "thread-1"}})

    assert len(loaded) == 100
    assert agent._message_id(loaded[20]) == "id-20"


async def test_background_compression_does_not_cache_failed_persistence(monkeypatch):
    # The background-compression implementation (module-level cache, retry
    # constants, and its create_chat_model_async binding) was split into
    # lightweight_compression; patches must target the module the code reads.
    import backend.application.agent_executors.lightweight_compression as module
    from backend.application.agent_executors.lightweight_qa_agent import LightweightQAAgent

    class Store:
        async def load(self, **kwargs):
            return None

        async def save(self, **kwargs):
            return False

    class Model:
        async def ainvoke(self, prompt):
            return SimpleNamespace(content="用户确认阈值为 17 mg/L，引用 [doc-a]。")

    async def load_messages(config):
        return [
            HumanMessage(
                content="用户确认阈值为 17 mg/L，引用 [doc-a]。",
                id="message-1",
            )
        ]

    async def create_model(**kwargs):
        return Model()

    agent = object.__new__(LightweightQAAgent)
    agent._memory_store = Store()
    agent._user_id = "user-1"
    agent._thread_id = "internal-thread-1"
    agent._uploaded_files_thread_id = "thread-1"
    agent._history_compress_chars = 1
    agent._history_limit = 16
    agent._model_name = "model-1"
    agent._load_recent_messages = load_messages
    monkeypatch.setattr(module, "create_chat_model_async", create_model)
    module._compress_cache.pop("internal-thread-1", None)

    await agent._do_background_compression({}, "internal-thread-1")

    assert "internal-thread-1" not in module._compress_cache


async def test_background_compression_watermark_does_not_skip_messages_added_during_generation(
    monkeypatch,
):
    # The background-compression implementation (module-level cache, retry
    # constants, and its create_chat_model_async binding) was split into
    # lightweight_compression; patches must target the module the code reads.
    import backend.application.agent_executors.lightweight_compression as module
    from backend.application.agent_executors.lightweight_qa_agent import LightweightQAAgent

    messages = [
        HumanMessage(
            content="用户确认阈值为 17 mg/L，引用 [doc-a]。",
            id="message-1",
        ),
        HumanMessage(
            content="继续追问阈值依据 " + "x" * 200,
            id="message-2",
        ),
    ]

    class Store:
        def __init__(self):
            self.saved = []

        async def load(self, **kwargs):
            return None

        async def save(self, **kwargs):
            self.saved.append(kwargs["memory"])
            return True

    class Model:
        async def ainvoke(self, prompt):
            messages.append(
                HumanMessage(
                    content="压缩期间新增的问题",
                    id="message-3",
                )
            )
            return SimpleNamespace(content="用户确认阈值为 17 mg/L，引用 [doc-a]。")

    async def load_messages(config):
        return messages

    async def create_model(**kwargs):
        return Model()

    agent = object.__new__(LightweightQAAgent)
    agent._memory_store = Store()
    agent._user_id = "user-1"
    agent._thread_id = "internal-thread-1"
    agent._uploaded_files_thread_id = "thread-1"
    agent._history_compress_chars = 1
    agent._history_limit = 16
    agent._model_name = "model-1"
    agent._load_recent_messages = load_messages
    monkeypatch.setattr(module, "create_chat_model_async", create_model)
    monkeypatch.setattr(module, "_COMPRESSION_PERSIST_RETRY_DELAY_SECONDS", 0.0)
    module._compress_cache.pop("internal-thread-1", None)

    await agent._do_background_compression({}, "internal-thread-1")

    assert agent._memory_store.saved
    assert agent._memory_store.saved[0]["last_compressed_message_id"] == "message-2"
    assert module._compress_cache.get("internal-thread-1") == (
        "用户确认阈值为 17 mg/L，引用 [doc-a]。",
        "message-2",
    )


async def test_background_compression_retries_memory_save(monkeypatch):
    # The background-compression implementation (module-level cache, retry
    # constants, and its create_chat_model_async binding) was split into
    # lightweight_compression; patches must target the module the code reads.
    import backend.application.agent_executors.lightweight_compression as module
    from backend.application.agent_executors.lightweight_qa_agent import LightweightQAAgent

    class Store:
        def __init__(self):
            self.attempts = 0

        async def load(self, **kwargs):
            return None

        async def save(self, **kwargs):
            self.attempts += 1
            return self.attempts >= 3

    class Model:
        async def ainvoke(self, prompt):
            return SimpleNamespace(content="用户确认阈值为 17 mg/L，引用 [doc-a]。")

    async def load_messages(config):
        return [
            HumanMessage(
                content="用户确认阈值为 17 mg/L，引用 [doc-a]。",
                id="message-1",
            )
        ]

    async def create_model(**kwargs):
        return Model()

    agent = object.__new__(LightweightQAAgent)
    agent._memory_store = Store()
    agent._user_id = "user-1"
    agent._thread_id = "internal-thread-1"
    agent._uploaded_files_thread_id = "thread-1"
    agent._history_compress_chars = 1
    agent._history_limit = 16
    agent._model_name = "model-1"
    agent._load_recent_messages = load_messages
    monkeypatch.setattr(module, "create_chat_model_async", create_model)
    monkeypatch.setattr(module, "_COMPRESSION_PERSIST_RETRY_DELAY_SECONDS", 0.0)
    module._compress_cache.pop("internal-thread-1", None)

    await agent._do_background_compression({}, "internal-thread-1")

    assert agent._memory_store.attempts == 3
    assert "internal-thread-1" in module._compress_cache
