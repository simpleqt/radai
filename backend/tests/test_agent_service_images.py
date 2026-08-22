from backend.application.agent_executors.image_url_registry import (
    get_registry,
    register_image_url,
    reset_registry,
    resolve_image_tokens,
)
from backend.application.agent_service import AgentService
from backend.domain.agents.events import AgentEvent
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext


async def test_agent_service_restores_and_persists_durable_images_across_turns(
    monkeypatch,
) -> None:
    profile = AgentProfile(code="ne", name="NE", domain="nuclear-electronics")
    context = AgentRuntimeContext(
        request_id="req-1",
        user_id="user-1",
        external_thread_id="thread-1",
        internal_thread_id="user-1:thread-1",
        agent_profile=profile,
        workflow_name="lightweight",
        model_name=None,
    )
    expired = (
        "/api/v2/files/kb-image?object_name=staging%2Fkb-id%2Fjob%2Fimages%2Fold.jpg"
        "&exp=1&sig=expired"
    )
    persisted: list[dict[str, object]] = []

    class FakeConversationStore:
        async def ensure_runtime_conversation(self, **kwargs) -> None:
            return None

        async def get_registered_images(self, **kwargs) -> dict[str, object]:
            return {
                "[IMAGE:001]": {
                    "url": expired,
                    "caption": "上轮图片",
                    "index": 1,
                }
            }

        async def update_conversation_images(self, **kwargs) -> None:
            persisted.append(kwargs["registered_images"])

    class FakeWorkflow:
        async def run(self, request, runtime_context):
            # Real workflows reset their per-turn entries before retrieval.
            # The persisted seed must survive that reset.
            reset_registry()
            restored = resolve_image_tokens("![再次引用]([IMAGE:001])")
            next_token = register_image_url(
                "/files/staging/kb-id/job/images/new.jpg",
                "本轮图片",
            )
            yield AgentEvent(
                type="message.replace",
                request_id=runtime_context.request_id,
                thread_id=runtime_context.external_thread_id,
                sequence=1,
                payload={"content": restored, "next_token": next_token},
            )

    registry = get_registry()
    registry.replace_persisted({})
    try:
        service = AgentService(conversation_store=FakeConversationStore())
        monkeypatch.setattr(
            service,
            "_resolve_workflow",
            lambda request: (FakeWorkflow(), context),
        )

        events = [
            event
            async for event in service.run_stream(
                AgentRequest(
                    question="再次展示上轮图片",
                    agent_code="ne",
                    user_id="user-1",
                    conversation_id="thread-1",
                )
            )
        ]

        assert "sig=expired" not in events[0].payload["content"]
        assert events[0].payload["next_token"] == "[IMAGE:002]"
        assert persisted == [
            {
                "[IMAGE:001]": {
                    "object_name": "staging/kb-id/job/images/old.jpg",
                    "caption": "上轮图片",
                    "index": 1,
                },
                "[IMAGE:002]": {
                    "object_name": "staging/kb-id/job/images/new.jpg",
                    "caption": "本轮图片",
                    "index": 2,
                },
            }
        ]
    finally:
        registry.replace_persisted({})
