"""Profile-driven agent executor registry."""

from __future__ import annotations

from typing import Any

from backend.application.agent_executors.profiled_executor import ProfiledAgentExecutor
from backend.domain.agents.profile import AgentProfile


class AgentExecutorRegistry:
    """Build an executor for a configured agent profile."""

    def build(self, profile: AgentProfile, **kwargs: Any):
        return ProfiledAgentExecutor(profile=profile, **kwargs)
