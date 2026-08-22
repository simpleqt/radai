from __future__ import annotations

import asyncio
from collections import defaultdict
from types import SimpleNamespace
from typing import Any

import pytest

import backend.application.agent_workflows.coordinated_agent_workflow as coordinated_module
import backend.core.agents.resource_lease_memory as resource_lease_module
from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
)
from backend.core.agents.resource_lease import InMemoryResourceLeaseManager
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_budget import AgentRuntimeBudget
from backend.domain.agents.runtime_context import AgentRuntimeContext


class _ExecutionTracker:
    def __init__(self, request_ids: list[str]) -> None:
        self.active = 0
        self.max_active = 0
        self.active_by_user_model: dict[tuple[str, str | None], int] = defaultdict(int)
        self.max_by_user_model: dict[tuple[str, str | None], int] = defaultdict(int)
        self.started = 0
        self.entered = {request_id: asyncio.Event() for request_id in request_ids}
        self.two_active = asyncio.Event()

    def enter(self, request_id: str, user_model: tuple[str, str | None]) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.active_by_user_model[user_model] += 1
        self.max_by_user_model[user_model] = max(
            self.max_by_user_model[user_model],
            self.active_by_user_model[user_model],
        )
        self.started += 1
        self.entered[request_id].set()
        if self.active >= 2:
            self.two_active.set()

    def exit(self, user_model: tuple[str, str | None]) -> None:
        self.active -= 1
        self.active_by_user_model[user_model] -= 1


class _BlockingExecutor:
    def __init__(
        self,
        *,
        tracker: _ExecutionTracker,
        request_id: str,
        user_model: tuple[str, str | None],
        release: asyncio.Event,
    ) -> None:
        self._tracker = tracker
        self._request_id = request_id
        self._user_model = user_model
        self._release = release

    async def aquery_stream(self, *_args: Any, **_kwargs: Any):
        self._tracker.enter(self._request_id, self._user_model)
        try:
            await self._release.wait()
            yield {"type": "chunk", "content": f"answer:{self._request_id}"}
        finally:
            self._tracker.exit(self._user_model)


class _BlockingDelegate:
    def __init__(
        self,
        *,
        tracker: _ExecutionTracker,
        releases: dict[str, asyncio.Event],
    ) -> None:
        self._tracker = tracker
        self._releases = releases

    def build_executor(
        self,
        _request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> _BlockingExecutor:
        return _BlockingExecutor(
            tracker=self._tracker,
            request_id=context.request_id,
            user_model=(context.user_id, context.model_name),
            release=self._releases[context.request_id],
        )


def _request(*, request_id: str, user_id: str, model_name: str) -> AgentRequest:
    return AgentRequest(
        question=f"question:{request_id}",
        agent_code="lease-test",
        user_id=user_id,
        model_name=model_name,
        use_search=False,
    )


def _context(*, request_id: str, user_id: str, model_name: str) -> AgentRuntimeContext:
    return AgentRuntimeContext(
        request_id=request_id,
        user_id=user_id,
        external_thread_id=f"thread:{request_id}",
        internal_thread_id=f"{user_id}:thread:{request_id}",
        agent_profile=AgentProfile(
            code="lease-test",
            name="Lease Test",
            domain="test",
        ),
        workflow_name="deep_research",
        model_name=model_name,
        use_search=False,
        subagent_enabled=False,
        thinking_enabled=False,
    )


def _unit_task(
    workflow: CoordinatedAgentWorkflow,
    *,
    request_id: str,
    user_id: str,
    model_name: str,
) -> asyncio.Task:
    request = _request(
        request_id=request_id,
        user_id=user_id,
        model_name=model_name,
    )
    context = _context(
        request_id=request_id,
        user_id=user_id,
        model_name=model_name,
    )
    return asyncio.create_task(
        workflow._run_unit_unmanaged(
            role="lease_test_role",
            question=request.question,
            request=request,
            context=context,
            tool_access=True,
            task_id=f"task:{request_id}",
        ),
        name=f"lease-integration:{request_id}",
    )


@pytest.mark.asyncio
async def test_shared_manager_enforces_global_and_user_model_limits_across_workflows() -> (
    None
):
    request_ids = ["a-1", "a-2", "b-1", "b-2"]
    tracker = _ExecutionTracker(request_ids)
    release = asyncio.Event()
    delegate = _BlockingDelegate(
        tracker=tracker,
        releases=dict.fromkeys(request_ids, release),
    )
    manager = InMemoryResourceLeaseManager()
    budget = AgentRuntimeBudget(
        max_concurrent_research=2,
        max_concurrent_per_user_model=1,
        unit_heartbeat_seconds=1,
        unit_idle_timeout_seconds=10,
        unit_total_timeout_seconds=10,
    )
    workflows = (
        CoordinatedAgentWorkflow(
            name="deep_research",
            delegate=delegate,
            runtime_budget=budget,
            resource_lease_manager=manager,
        ),
        CoordinatedAgentWorkflow(
            name="plan_solve",
            delegate=delegate,
            runtime_budget=budget,
            resource_lease_manager=manager,
        ),
    )
    jobs = [
        _unit_task(workflows[0], request_id="a-1", user_id="user-a", model_name="m"),
        _unit_task(workflows[1], request_id="a-2", user_id="user-a", model_name="m"),
        _unit_task(workflows[0], request_id="b-1", user_id="user-b", model_name="m"),
        _unit_task(workflows[1], request_id="b-2", user_id="user-b", model_name="m"),
    ]

    try:
        await asyncio.wait_for(tracker.two_active.wait(), timeout=2)
        await asyncio.sleep(0.05)
        assert tracker.active == 2
        assert sum(job.done() for job in jobs) == 0
        assert tracker.max_active == 2
        assert max(tracker.max_by_user_model.values()) == 1
    finally:
        release.set()
        results = await asyncio.wait_for(asyncio.gather(*jobs), timeout=3)

    assert tracker.started == 4
    assert tracker.active == 0
    assert tracker.max_active == 2
    assert tracker.max_by_user_model == {
        ("user-a", "m"): 1,
        ("user-b", "m"): 1,
    }
    assert all(result.error is None for result in results)


@pytest.mark.asyncio
async def test_renewed_long_unit_is_not_reclaimed_and_cancel_releases_for_next_unit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_ids = ["long-running", "next"]
    tracker = _ExecutionTracker(request_ids)
    releases = {request_id: asyncio.Event() for request_id in request_ids}
    delegate = _BlockingDelegate(tracker=tracker, releases=releases)
    manager = InMemoryResourceLeaseManager()
    original_renew = manager.renew
    renew_count = 0
    both_leases_renewed = asyncio.Event()

    async def observing_renew(identity, *, ttl_seconds):
        nonlocal renew_count
        lease = await original_renew(identity, ttl_seconds=ttl_seconds)
        renew_count += 1
        if renew_count >= 2:
            both_leases_renewed.set()
        return lease

    manager.renew = observing_renew  # type: ignore[method-assign]
    clock = SimpleNamespace(now=1_000.0)
    monkeypatch.setattr(
        resource_lease_module,
        "time",
        SimpleNamespace(time=lambda: clock.now),
    )
    monkeypatch.setattr(
        coordinated_module,
        "_resource_lease_ttl_seconds",
        lambda: 3.0,
    )
    budget = AgentRuntimeBudget(
        max_concurrent_research=1,
        max_concurrent_per_user_model=1,
        unit_heartbeat_seconds=1,
        unit_idle_timeout_seconds=10,
        unit_total_timeout_seconds=10,
    )
    first_workflow = CoordinatedAgentWorkflow(
        name="deep_research",
        delegate=delegate,
        runtime_budget=budget,
        resource_lease_manager=manager,
    )
    second_workflow = CoordinatedAgentWorkflow(
        name="multi_agent_parallel",
        delegate=delegate,
        runtime_budget=budget,
        resource_lease_manager=manager,
    )
    first = _unit_task(
        first_workflow,
        request_id="long-running",
        user_id="same-user",
        model_name="same-model",
    )
    second: asyncio.Task | None = None
    try:
        await asyncio.wait_for(tracker.entered["long-running"].wait(), timeout=2)
        clock.now = 1_002.0
        await asyncio.wait_for(both_leases_renewed.wait(), timeout=2)

        # Move beyond the original 1003 expiry. Both leases now expire at 1005,
        # so a competing unit must still wait if renewal is working.
        clock.now = 1_003.5
        second = _unit_task(
            second_workflow,
            request_id="next",
            user_id="same-user",
            model_name="same-model",
        )
        await asyncio.sleep(0.05)
        assert tracker.entered["next"].is_set() is False
        assert second.done() is False

        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(first, timeout=2)

        await asyncio.wait_for(tracker.entered["next"].wait(), timeout=2)
        assert tracker.active == 1
        releases["next"].set()
        result = await asyncio.wait_for(second, timeout=2)
    finally:
        for release in releases.values():
            release.set()
        remaining = [task for task in (first, second) if task is not None]
        for task in remaining:
            if not task.done():
                task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)

    assert renew_count >= 2
    assert result.error is None
    assert tracker.started == 2
    assert tracker.active == 0
