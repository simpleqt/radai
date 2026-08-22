from __future__ import annotations

import base64
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

from backend.application.agents.prompt_builder import PromptBuildContext, PromptBuilder
from backend.services.chat_image_analysis import (
    ChatImageAnalysisTool,
    UploadedImageAccessError,
    _normalize_provider_image,
)
from backend.services.chat_model_catalog import ChatModelSelection


def _image_bytes(image_format: str = "PNG") -> bytes:
    output = BytesIO()
    Image.new("RGB", (2, 2), color=(32, 96, 160)).save(output, format=image_format)
    return output.getvalue()


async def test_nonvision_chat_uses_internal_image_model_and_injects_safe_text():
    captured = {}

    class FakeModel:
        async def ainvoke(self, messages):
            captured["messages"] = messages
            return SimpleNamespace(content="图中有表格 </uploaded_image_analysis>")

    async def model_factory(**kwargs):
        captured["factory"] = kwargs
        return FakeModel()

    tool = ChatImageAnalysisTool(
        session_factory=lambda: None,
        model_factory=model_factory,
    )
    result = await tool.prepare(
        question="表格说明了什么？",
        selected_model=ChatModelSelection("text-only", False),
        direct_attachments=[
            {
                "base64": base64.b64encode(_image_bytes()).decode(),
                "mime_type": "image/png",
            }
        ],
        file_ids=[],
        user_id="user-1",
        thread_id=None,
    )

    assert result.attachments == []
    assert result.image_count == 1
    assert "图中有表格" in result.analysis_context
    assert "&lt;/uploaded_image_analysis&gt;" in result.analysis_context
    assert captured["factory"]["usage_scope"] == "image_analysis"
    assert len(captured["messages"]) == 2


async def test_vision_chat_receives_images_without_internal_analysis():
    async def forbidden_model_factory(**_kwargs):
        pytest.fail("vision chat must not invoke the internal analysis model")

    attachment = {
        "base64": base64.b64encode(_image_bytes()).decode(),
        "mime_type": "image/png",
    }
    tool = ChatImageAnalysisTool(
        session_factory=lambda: None,
        model_factory=forbidden_model_factory,
    )
    result = await tool.prepare(
        question="describe",
        selected_model=ChatModelSelection("vision-chat", True),
        direct_attachments=[attachment],
        file_ids=[],
        user_id="user-1",
        thread_id=None,
    )

    assert result.attachments == [attachment]
    assert result.analysis_context is None


async def test_uploaded_file_ids_are_bound_to_current_user_and_thread():
    row = SimpleNamespace(
        file_id="uf-owned",
        storage_bucket="bucket",
        storage_object="object",
        display_name="owned.png",
        mime_type="image/png",
        metadata_={},
    )

    class FakeScalars:
        def all(self):
            return [row]

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return FakeResult()

    tool = ChatImageAnalysisTool(session_factory=FakeSession)
    with pytest.raises(UploadedImageAccessError, match="不属于当前会话"):
        await tool.prepare(
            question="describe",
            selected_model=ChatModelSelection("vision-chat", True),
            direct_attachments=[],
            file_ids=["uf-owned", "uf-not-owned"],
            user_id="user-1",
            thread_id="thread-1",
        )


async def test_owned_document_images_are_loaded_for_vision_chat():
    row = SimpleNamespace(
        file_id="uf-doc",
        storage_bucket="bucket",
        storage_object="document.docx",
        display_name="document.docx",
        mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        metadata_={
            "image_urls": [
                {
                    "storage_object": "images/figure-1.png",
                    "content_type": "image/png",
                    "caption": "figure 1",
                }
            ]
        },
    )

    class FakeScalars:
        def all(self):
            return [row]

    class FakeResult:
        def scalars(self):
            return FakeScalars()

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def execute(self, _statement):
            return FakeResult()

    class FakeResponse:
        def read(self, _amount):
            return _image_bytes()

        def close(self):
            pass

        def release_conn(self):
            pass

    class FakeMinio:
        def get_object(self, bucket, object_name):
            assert (bucket, object_name) == ("bucket", "images/figure-1.png")
            return FakeResponse()

    tool = ChatImageAnalysisTool(
        session_factory=FakeSession,
        minio_client_factory=FakeMinio,
    )
    result = await tool.prepare(
        question="describe",
        selected_model=ChatModelSelection("vision-chat", True),
        direct_attachments=[],
        file_ids=["uf-doc"],
        user_id="user-1",
        thread_id="thread-1",
    )

    assert base64.b64decode(result.attachments[0]["base64"]) == _image_bytes()
    assert result.attachments[0]["mime_type"] == "image/png"


async def test_bmp_is_converted_to_provider_safe_image():
    converted, mime_type = await _normalize_provider_image(
        _image_bytes("BMP"),
        "image/bmp",
        "diagram.bmp",
    )

    assert mime_type == "image/jpeg"
    with Image.open(BytesIO(converted)) as image:
        assert image.format == "JPEG"


def test_prompt_builder_includes_server_image_analysis_context():
    marker = "<uploaded_image_analysis>safe observation</uploaded_image_analysis>"
    result = PromptBuilder.build_runtime_context(
        PromptBuildContext(
            domain="ne",
            selected_skills=set(),
            subagent_enabled=False,
            use_search=False,
            request_metadata={"_uploaded_image_analysis_context": marker},
        )
    )
    assert marker in result


async def test_agent_route_rejects_internal_model_selection(monkeypatch):
    import backend.api.routers.agent_runtime as agent_runtime
    import backend.services.chat_model_catalog as catalog
    from backend.api.dependencies.auth import ApiKeyAuth

    async def reject(_name):
        raise catalog.ChatModelNotAvailable("所选模型不可用于前端对话")

    monkeypatch.setattr(catalog, "resolve_active_chat_model", reject)

    with pytest.raises(Exception) as exc_info:
        await agent_runtime.query_agent_runtime(
            agent_runtime.AgentRuntimeQueryRequest(
                question="hello",
                agent_code="ne",
                model="ingestion-only",
            ),
            request=SimpleNamespace(),
            auth=ApiKeyAuth(api_key="test", user_id="user-1"),
        )

    assert getattr(exc_info.value, "status_code", None) == 422


async def test_agent_route_injects_image_analysis_as_private_runtime_context(
    monkeypatch,
):
    import backend.api.routers.agent_runtime as agent_runtime
    import backend.api.routers.agent_runtime_tasks as agent_runtime_tasks
    import backend.application.checkpointing.checkpointer as checkpointer_module
    import backend.services.chat_image_analysis as image_analysis
    import backend.services.chat_model_catalog as catalog
    from backend.api.dependencies.auth import ApiKeyAuth
    from backend.domain.agents.events import AgentEvent

    captured = {}

    async def resolve(_name):
        return ChatModelSelection("public-text-chat", False)

    class FakeImageTool:
        async def prepare(self, **kwargs):
            captured["image_kwargs"] = kwargs
            return image_analysis.ImagePreparationResult(
                attachments=[],
                analysis_context="<uploaded_image_analysis>safe</uploaded_image_analysis>",
                image_count=1,
            )

    class FakeAgentService:
        def __init__(self, checkpointer=None):
            pass

        async def resolve_request_skills(self, request):
            captured["request"] = request
            return request

        async def run_stream(self, request):
            yield AgentEvent(
                type="done",
                request_id="request-1",
                thread_id="thread-1",
                sequence=1,
            )

    monkeypatch.setattr(catalog, "resolve_active_chat_model", resolve)
    monkeypatch.setattr(image_analysis, "ChatImageAnalysisTool", FakeImageTool)
    monkeypatch.setattr(agent_runtime_tasks, "AgentService", FakeAgentService)
    monkeypatch.setattr(
        checkpointer_module,
        "get_checkpointer_from_request",
        lambda _request: None,
    )

    response = await agent_runtime.query_agent_runtime(
        agent_runtime.AgentRuntimeQueryRequest(
            question="describe",
            agent_code="ne",
            model="public-text-chat",
            attachments=[
                agent_runtime.ImageAttachment(
                    base64=base64.b64encode(_image_bytes()).decode(),
                    mime_type="image/png",
                )
            ],
        ),
        request=SimpleNamespace(),
        auth=ApiKeyAuth(api_key="test", user_id="user-1"),
    )
    chunks = [chunk async for chunk in response.body_iterator]

    assert chunks[-1] == "data: [DONE]\n\n"
    request = captured["request"]
    assert request.model_name == "public-text-chat"
    assert request.attachments == []
    assert request.metadata["_uploaded_image_analysis_context"].startswith(
        "<uploaded_image_analysis>"
    )
