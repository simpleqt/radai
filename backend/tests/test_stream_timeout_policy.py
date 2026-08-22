from __future__ import annotations

from backend.application.agent_executors.stream_decisions import (
    StreamExitReason,
    timeout_reached_for,
)
from backend.application.agent_executors.stream_timeouts import (
    StreamTimeoutPolicy,
    load_stream_timeout_policy,
)
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.domain.agents.profile import AgentProfile


def test_default_policy_values() -> None:
    policy = StreamTimeoutPolicy()

    assert policy.stream_idle_timeout_seconds == 180
    assert policy.model_round_idle_timeout_seconds == 180
    assert policy.heartbeat_interval_seconds == 30
    assert policy.incomplete_round_retry_max == 2
    assert policy.normal_stream_total_timeout_seconds == 600
    assert policy.deep_research_stream_timeout_seconds == 1800
    assert policy.deep_research_idle_timeout_seconds == 600
    assert policy.deep_research_finalize_idle_timeout_seconds == 300
    assert policy.deep_research_finalize_grace_seconds == 300
    assert policy.deep_research_task_stall_grace_seconds == 300
    assert policy.recursion_limit is None


def test_env_overrides(monkeypatch) -> None:
    monkeypatch.setenv("STREAM_IDLE_TIMEOUT_SECONDS", "45")
    monkeypatch.setenv("STREAM_TOTAL_TIMEOUT_SECONDS", "120")
    monkeypatch.setenv("DEEP_RESEARCH_STREAM_TIMEOUT_SECONDS", "900")
    monkeypatch.setenv("STREAM_RECURSION_LIMIT", "200")

    policy = StreamTimeoutPolicy.from_env()

    assert policy.stream_idle_timeout_seconds == 45
    assert policy.normal_stream_total_timeout_seconds == 120
    assert policy.deep_research_stream_timeout_seconds == 900
    assert policy.recursion_limit == 200


def test_env_total_timeout_zero_disables_limit(monkeypatch) -> None:
    monkeypatch.setenv("STREAM_TOTAL_TIMEOUT_SECONDS", "0")

    policy = StreamTimeoutPolicy.from_env()

    assert policy.normal_stream_total_timeout_seconds is None


def test_profile_override_wins_over_defaults() -> None:
    profile = AgentProfile(
        code="fmca",
        name="FMCA",
        domain="fundamental-mechanics-course",
        stream_timeout_policy={
            "normal_stream_total_timeout_seconds": 120,
            "heartbeat_interval_seconds": 10,
        },
    )

    policy = load_stream_timeout_policy(profile)

    assert policy.normal_stream_total_timeout_seconds == 120
    assert policy.heartbeat_interval_seconds == 10
    assert policy.stream_idle_timeout_seconds == 180


def test_agent_yaml_exposes_stream_timeout_policy() -> None:
    profile = AgentProfileLoader().load("fmca")

    assert profile.stream_timeout_policy is not None
    assert profile.stream_timeout_policy["normal_stream_total_timeout_seconds"] == 600


def test_resolve_normal_idle_timeout_uses_model_round_budget() -> None:
    policy = StreamTimeoutPolicy(
        stream_idle_timeout_seconds=60,
        model_round_idle_timeout_seconds=90,
    )

    assert policy.resolve_normal_idle_timeout(False) == 60
    assert policy.resolve_normal_idle_timeout(True) == 90


def test_from_mapping_ignores_unknown_keys() -> None:
    policy = StreamTimeoutPolicy.from_mapping(
        {
            "stream_idle_timeout_seconds": 30,
            "unknown_field": 1,
        }
    )

    assert policy.stream_idle_timeout_seconds == 30
    assert policy.model_round_idle_timeout_seconds == 180


def test_stream_total_timeout_counts_as_timeout_reason() -> None:
    assert (
        timeout_reached_for(StreamExitReason.STREAM_TOTAL_TIMEOUT) is True
    )
