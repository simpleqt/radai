"""Retrieval runtime policy limits (context_read call allowance)."""

from __future__ import annotations

from backend.domain.agents.retrieval_policy import RetrievalRuntimePolicy


def test_context_read_call_budget_default_is_five():
    policy = RetrievalRuntimePolicy.for_mode()
    assert policy.context_navigator_tokens == 10000
    assert policy.tool_budgets()["context_read"] == 5


def test_context_read_call_budget_honors_env_override(monkeypatch):
    monkeypatch.setenv("CONTEXT_NAVIGATOR_TOKENS", "8000")
    policy = RetrievalRuntimePolicy.for_mode()
    assert policy.tool_budgets()["context_read"] == 4

    monkeypatch.setenv("CONTEXT_NAVIGATOR_TOKENS", "not-a-number")
    fallback = RetrievalRuntimePolicy.for_mode()
    assert fallback.tool_budgets()["context_read"] == 5


def test_context_read_call_budget_applies_to_all_modes():
    for mode in (
        RetrievalRuntimePolicy.for_mode(),
        RetrievalRuntimePolicy.for_mode(lightweight=True),
        RetrievalRuntimePolicy.for_mode(deep_research=True),
    ):
        assert mode.tool_budgets()["context_read"] == 5
