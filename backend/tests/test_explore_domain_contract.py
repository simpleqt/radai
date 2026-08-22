"""Explore 领域解析的 fail-fast 契约。"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import BaseModel, ValidationError

from backend.api.dependencies.auth import ApiKeyAuth
from backend.api.routers.explore import (
    DetailChatRequest,
    DetailRequest,
    ExpandRequest,
    GradeRequest,
    MapRequest,
    QuizRequest,
    ResourcesRequest,
    TopicsRequest,
    explore_detail,
    explore_detail_chat,
    explore_expand,
    explore_map,
    explore_quiz,
    explore_quiz_grade,
    explore_topics,
)
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.explore import service
from backend.domain.agents.events import AgentEvent

RequestFactory = Callable[[dict[str, Any]], BaseModel]


def _request_factory(model: type[BaseModel], **required: Any) -> RequestFactory:
    return lambda values: model.model_validate({**required, **values})


REQUEST_FACTORIES = [
    pytest.param(_request_factory(TopicsRequest), id="topics"),
    pytest.param(_request_factory(MapRequest, topic="topic"), id="map"),
    pytest.param(_request_factory(ExpandRequest, node_title="node"), id="expand"),
    pytest.param(_request_factory(DetailRequest, node_title="node"), id="detail"),
    pytest.param(
        _request_factory(DetailChatRequest, node_title="node", question="why"),
        id="detail-chat",
    ),
    pytest.param(_request_factory(ResourcesRequest, node_title="node"), id="resources"),
    pytest.param(_request_factory(QuizRequest, node_title="node"), id="quiz"),
    pytest.param(_request_factory(GradeRequest, question={"id": "q1"}), id="grade"),
]


@pytest.mark.parametrize("factory", REQUEST_FACTORIES)
def test_explore_request_defaults_omitted_domain_to_ne(factory: RequestFactory) -> None:
    assert factory({}).domain == "ne"


@pytest.mark.parametrize("factory", REQUEST_FACTORIES)
@pytest.mark.parametrize("domain", [None, "", " \t "])
def test_explore_request_defaults_empty_domain_to_ne(
    factory: RequestFactory,
    domain: str | None,
) -> None:
    assert factory({"domain": domain}).domain == "ne"


@pytest.mark.parametrize("factory", REQUEST_FACTORIES)
@pytest.mark.parametrize(
    ("domain", "expected"),
    [(" NE ", "ne"), ("CsH", "csh"), (" FMCA ", "fmca")],
)
def test_explore_request_normalizes_registered_domain(
    factory: RequestFactory,
    domain: str,
    expected: str,
) -> None:
    assert factory({"domain": domain}).domain == expected


@pytest.mark.parametrize("factory", REQUEST_FACTORIES)
def test_explore_request_rejects_explicit_unknown_domain(
    factory: RequestFactory,
) -> None:
    with pytest.raises(ValidationError, match="Unsupported explore domain 'materials'"):
        factory({"domain": "materials"})


def test_service_domain_resolution_is_strict_and_exhaustive() -> None:
    assert service.resolve_domain(None) == "ne"
    assert service.resolve_domain("  ") == "ne"
    assert service.resolve_subject("ne") == "核电子学"
    assert service.resolve_subject("csh") == "场地污染水文地质"
    assert service.resolve_subject("fmca") == "基础力学"
    assert service.resolve_agent_code("ne") == "ne"
    assert service.resolve_agent_code("csh") == "csh"
    assert service.resolve_agent_code("fmca") == "fmca"

    with pytest.raises(ValueError, match="Unsupported explore domain 'materials'"):
        service.resolve_domain("materials")
    with pytest.raises(ValueError, match="Unsupported explore domain 'materials'"):
        service.resolve_subject("materials")
    with pytest.raises(ValueError, match="Unsupported explore domain 'materials'"):
        service.resolve_subject("materials", subject="显式学科")
    with pytest.raises(ValueError, match="Unsupported explore domain 'materials'"):
        service.resolve_agent_code("materials")


def test_all_content_routes_fail_fast_if_validation_is_bypassed() -> None:
    """Defence in depth: model_construct 绕过 Pydantic 时仍不得进入 NE 执行。"""
    auth = ApiKeyAuth(api_key="", user_id="guest", role="user")

    async def run() -> None:
        calls = [
            explore_topics(
                TopicsRequest.model_construct(domain="materials"),
                auth=auth,
                session=None,
            ),
            explore_map(
                MapRequest.model_construct(topic="topic", domain="materials"),
                auth=auth,
                session=None,
            ),
            explore_expand(
                ExpandRequest.model_construct(node_title="node", domain="materials"),
                auth=auth,
                session=None,
            ),
            explore_quiz(
                QuizRequest.model_construct(node_title="node", domain="materials"),
                auth=auth,
                session=None,
            ),
            explore_quiz_grade(
                GradeRequest.model_construct(question={"id": "q1"}, domain="materials")
            ),
            explore_detail(
                DetailRequest.model_construct(node_title="node", domain="materials"),
                request=object(),
                auth=auth,
                session=None,
            ),
            explore_detail_chat(
                DetailChatRequest.model_construct(
                    node_title="node",
                    question="why",
                    domain="materials",
                ),
                request=object(),
                auth=auth,
                session=None,
            ),
        ]
        for call in calls:
            with pytest.raises(
                ValueError,
                match="Unsupported explore domain 'materials'",
            ):
                await call

    asyncio.run(run())


@pytest.mark.parametrize(
    ("domain", "expected_agent_code"),
    [("ne", "ne"), ("csh", "csh"), ("fmca", "fmca")],
)
def test_explore_detail_uses_isolated_lightweight_workflow(
    monkeypatch: pytest.MonkeyPatch,
    domain: str,
    expected_agent_code: str,
) -> None:
    """所有领域的详解都走 lightweight，不受普通问答默认工作流影响。"""
    captured_requests = []

    class FakeAgentService:
        def __init__(self, *, checkpointer: object) -> None:
            assert checkpointer == "test-checkpointer"

        async def run_stream(self, agent_request):
            captured_requests.append(agent_request)
            yield AgentEvent(
                type="done",
                request_id="request-id",
                thread_id=agent_request.conversation_id,
                sequence=0,
                payload={},
            )

    class FakeRequest:
        app = SimpleNamespace(
            state=SimpleNamespace(checkpointer="test-checkpointer")
        )
        state = SimpleNamespace(user=None, user_id=None)

        async def is_disconnected(self) -> bool:
            return False

    monkeypatch.setattr(
        "backend.application.agent_service.AgentService",
        FakeAgentService,
    )
    auth = ApiKeyAuth(api_key="", user_id="test-user", role="user")

    async def run() -> None:
        response = await explore_detail(
            DetailRequest(node_title="node", domain=domain),
            request=FakeRequest(),
            auth=auth,
            session=None,
        )
        chunks = [chunk async for chunk in response.body_iterator]
        assert chunks

    asyncio.run(run())

    assert len(captured_requests) == 1
    agent_request = captured_requests[0]
    assert agent_request.agent_code == expected_agent_code
    assert agent_request.requested_workflow == "lightweight"
    assert agent_request.metadata["runtime_config_scope"] == f"explore.detail.{domain}"


@pytest.mark.parametrize("agent_code", sorted(service.AGENT_CODE_BY_DOMAIN.values()))
def test_every_explore_agent_registers_lightweight_workflow(agent_code: str) -> None:
    """新增 Explore 领域时必须同时注册详解所需的 lightweight 工作流。"""
    profile = AgentProfileLoader().load(agent_code)

    assert "lightweight" in profile.allowed_workflows


def test_detail_model_configuration_is_isolated_by_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXPLORE_DETAIL_MODEL_NAME", "detail-shared-model")
    monkeypatch.setenv("EXPLORE_DETAIL_CSH_MODEL_NAME", "detail-csh-model")

    assert service.resolve_detail_runtime_config("csh").model_name == "detail-csh-model"
    assert (
        service.resolve_detail_runtime_config("fmca").model_name
        == "detail-shared-model"
    )
    assert (
        service.resolve_detail_runtime_config("ne", "request-model").model_name
        == "request-model"
    )


def test_quiz_request_accepts_frontend_default_four_types() -> None:
    """前端默认「全部」发送 choice/judge/short/calc 四种题型（H1 回归守护）。

    router 曾按元素个数设 types max_length=3，默认出题请求被整体 422。
    """
    request = QuizRequest.model_validate(
        {"node_title": "node", "types": ["choice", "judge", "short", "calc"]}
    )
    assert request.types == ["choice", "judge", "short", "calc"]

    with pytest.raises(ValidationError):
        QuizRequest.model_validate(
            {
                "node_title": "node",
                "types": ["choice"] * 6,
            }
        )


def test_kb_ids_allow_large_agent_bindings() -> None:
    """智能体 KB 绑定无条数上限，explore 上界须容纳（>8 合法，>50 拒绝）。"""
    request = TopicsRequest.model_validate({"kb_ids": [f"kb-{i}" for i in range(30)]})
    assert len(request.kb_ids) == 30

    with pytest.raises(ValidationError):
        TopicsRequest.model_validate({"kb_ids": [f"kb-{i}" for i in range(51)]})


def test_detail_chat_accepts_long_assistant_echo_history() -> None:
    """前端把助手长回答原样回显进 history，router 上界须与服务层截断语义相容。"""
    request = DetailChatRequest.model_validate(
        {
            "node_title": "node",
            "question": "why",
            "detail_content": "x" * 64000,
            "history": [{"role": "assistant", "content": "y" * 20000}],
        }
    )
    assert request.history[0].content == "y" * 20000

    with pytest.raises(ValidationError):
        DetailChatRequest.model_validate(
            {
                "node_title": "node",
                "question": "why",
                "history": [{"role": "assistant", "content": "y" * 20001}],
            }
        )
