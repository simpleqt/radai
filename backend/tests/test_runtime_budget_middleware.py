from types import SimpleNamespace

import pytest
from langchain.agents.middleware.types import ModelResponse
from langchain_core.messages import AIMessage

from backend.application.tools.runtime_invoker import RuntimeToolInvoker
from backend.core.agents.control import (
    AgentBudgetExceededError,
    AgentControl,
    RunBudget,
    agent_runtime_manager_scope,
)
from backend.core.agents.factory import _assemble_from_features
from backend.core.agents.features import RuntimeFeatures
from backend.core.agents.middlewares.runtime_budget import (
    RuntimeBudgetMiddleware,
    model_response_token_usage,
)


def test_model_response_usage_supports_provider_metadata_shapes():
    response = ModelResponse(
        result=[
            AIMessage(
                content="first",
                usage_metadata={
                    "input_tokens": 3,
                    "output_tokens": 4,
                    "total_tokens": 7,
                },
            ),
            AIMessage(
                content="second",
                response_metadata={
                    "token_usage": {
                        "prompt_tokens": "2",
                        "completion_tokens": 5,
                    }
                },
            ),
        ]
    )

    assert model_response_token_usage(response) == 14


@pytest.mark.asyncio
async def test_async_model_boundary_charges_shared_tree_tokens():
    manager = AgentControl("root", budget=RunBudget(max_tokens=10))
    middleware = RuntimeBudgetMiddleware()
    response = ModelResponse(
        result=[
            AIMessage(
                content="answer",
                usage_metadata={
                    "input_tokens": 3,
                    "output_tokens": 4,
                    "total_tokens": 7,
                },
            )
        ]
    )

    with agent_runtime_manager_scope(manager):
        result = await middleware.awrap_model_call(
            SimpleNamespace(),
            lambda _request: _async_value(response),
        )

    assert result is response
    assert (await manager.budget_snapshot()).tokens_used == 7


@pytest.mark.asyncio
async def test_async_model_boundary_cancels_tree_on_token_overrun():
    manager = AgentControl("root", budget=RunBudget(max_tokens=3))
    middleware = RuntimeBudgetMiddleware()
    response = ModelResponse(
        result=[
            AIMessage(
                content="answer",
                usage_metadata={
                    "input_tokens": 2,
                    "output_tokens": 2,
                    "total_tokens": 4,
                },
            )
        ]
    )

    with agent_runtime_manager_scope(manager):
        with pytest.raises(AgentBudgetExceededError) as exc_info:
            await middleware.awrap_model_call(
                SimpleNamespace(),
                lambda _request: _async_value(response),
            )

    assert exc_info.value.dimension == "tokens"
    assert manager.cancellation.cancelled is True
    assert (await manager.budget_snapshot()).tokens_used == 0


@pytest.mark.asyncio
async def test_graph_and_direct_tool_boundaries_charge_tool_calls():
    manager = AgentControl("root", budget=RunBudget(max_tool_calls=2))
    middleware = RuntimeBudgetMiddleware()
    request = SimpleNamespace()

    with agent_runtime_manager_scope(manager):
        assert (
            await middleware.awrap_tool_call(
                request,
                lambda _request: _async_value("graph-result"),
            )
            == "graph-result"
        )
        await RuntimeToolInvoker._charge_tool_budget()

        with pytest.raises(AgentBudgetExceededError) as exc_info:
            await RuntimeToolInvoker._charge_tool_budget()

    assert exc_info.value.dimension == "tool_calls"
    snapshot = await manager.budget_snapshot()
    assert snapshot.tool_calls_used == 2
    assert snapshot.exceeded is True


def test_auto_assembled_graph_always_installs_runtime_budget_before_tool_errors():
    middleware, _ = _assemble_from_features(RuntimeFeatures())
    names = [type(item).__name__ for item in middleware]

    assert "RuntimeBudgetMiddleware" in names
    assert names.index("RuntimeBudgetMiddleware") < names.index(
        "ToolErrorHandlingMiddleware"
    )


async def _async_value(value):
    return value
