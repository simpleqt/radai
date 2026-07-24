"""Record delegated subagent bursts; execution is queued by AgentControl."""

import logging
from typing_extensions import override

from langchain.agents import AgentState
from langchain.agents.middleware import AgentMiddleware
from langgraph.runtime import Runtime

logger = logging.getLogger(__name__)

DEFAULT_MAX_CONCURRENT_SUBAGENTS = 5
MIN_SUBAGENT_LIMIT = 1
MAX_SUBAGENT_LIMIT = 5


def _clamp_subagent_limit(value: int) -> int:
    return max(MIN_SUBAGENT_LIMIT, min(MAX_SUBAGENT_LIMIT, value))


class SubagentLimitMiddleware(AgentMiddleware[AgentState]):
    """Preserve task calls and let the request-tree control plane bound them.

    Previous behavior removed calls beyond the configured limit.  That made a
    model's fifth task disappear when the product was configured for five
    workers, and lost work rather than applying backpressure.
    """

    def __init__(self, max_concurrent: int = DEFAULT_MAX_CONCURRENT_SUBAGENTS):
        super().__init__()
        self.max_concurrent = _clamp_subagent_limit(max_concurrent)

    def _observe_task_calls(self, state: AgentState) -> dict | None:
        messages = state.get("messages", [])
        if not messages:
            return None
        last_msg = messages[-1]
        if getattr(last_msg, "type", None) != "ai":
            return None
        tool_calls = getattr(last_msg, "tool_calls", None)
        if not tool_calls:
            return None

        task_indices = [i for i, tc in enumerate(tool_calls) if tc.get("name") == "task"]
        if len(task_indices) <= self.max_concurrent:
            return None
        logger.warning(
            "Received %d task tool calls (concurrency=%d); preserving all calls and queueing excess work",
            len(task_indices),
            self.max_concurrent,
        )
        return None

    @override
    def after_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._observe_task_calls(state)

    @override
    async def aafter_model(self, state: AgentState, runtime: Runtime) -> dict | None:
        return self._observe_task_calls(state)
