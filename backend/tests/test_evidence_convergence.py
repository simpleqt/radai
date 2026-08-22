from __future__ import annotations

from types import SimpleNamespace

from backend.application.agent_executors.evidence_convergence import (
    EVIDENCE_CONVERGENCE_MAX_QUERIES_ENV,
    LEGACY_WRITER_HANDOFF_QUERY_LIMIT_ENV,
    EvidenceConvergenceController,
    EvidenceConvergencePolicy,
)


class FakeEvidenceState:
    project_evidence_text = "project evidence"
    retrieved_evidence_text = "retrieved evidence"

    def __init__(
        self,
        *,
        force: bool = False,
        evaluate: bool = True,
    ) -> None:
        self.force = force
        self.evaluate = evaluate
        self.marked_fingerprints: list[str] = []
        self.recorded_gaps: list[tuple[list[str], str]] = []

    def should_force_evidence_convergence(self, *, max_queries: int) -> bool:
        assert max_queries >= 1
        return self.force

    def should_evaluate_evidence(self, *, min_retrieval_blocks: int) -> bool:
        assert min_retrieval_blocks >= 1
        return self.evaluate

    def evidence_fingerprint(self) -> str:
        return "evidence-v1"

    def mark_evidence_evaluated(self, fingerprint: str) -> None:
        self.marked_fingerprints.append(fingerprint)

    def record_evidence_gap(
        self,
        missing_evidence: list[str],
        next_action: str,
    ) -> None:
        self.recorded_gaps.append((missing_evidence, next_action))


class RecordingEvaluator:
    def __init__(self, result) -> None:
        self.result = result
        self.calls: list[dict] = []

    async def evaluate(self, **kwargs):
        self.calls.append(kwargs)
        return self.result


def test_policy_prefers_generic_env_and_keeps_legacy_limit(monkeypatch):
    monkeypatch.delenv("EVIDENCE_CONVERGENCE_ENABLED", raising=False)
    monkeypatch.setenv(LEGACY_WRITER_HANDOFF_QUERY_LIMIT_ENV, "18")
    monkeypatch.setenv(EVIDENCE_CONVERGENCE_MAX_QUERIES_ENV, "7")

    generic = EvidenceConvergencePolicy.from_runtime(
        prompt_domain="groundwater"
    )
    monkeypatch.delenv(EVIDENCE_CONVERGENCE_MAX_QUERIES_ENV)
    legacy = EvidenceConvergencePolicy.from_runtime(
        prompt_domain="groundwater"
    )

    assert generic.enabled is True
    assert generic.max_queries == 7
    assert legacy.max_queries == 18


def test_explicit_generic_policy_can_enable_other_domains(monkeypatch):
    monkeypatch.delenv("EVIDENCE_CONVERGENCE_ENABLED", raising=False)

    disabled = EvidenceConvergencePolicy.from_runtime(prompt_domain="general")
    enabled = EvidenceConvergencePolicy.from_runtime(
        prompt_domain="general",
        request_metadata={
            "evidence_convergence": {
                "enabled": True,
                "max_queries": 3,
                "min_retrieval_blocks": 2,
            }
        },
    )

    assert disabled.enabled is False
    assert enabled == EvidenceConvergencePolicy(
        enabled=True,
        max_queries=3,
        min_retrieval_blocks=2,
    )


async def test_query_limit_converges_without_calling_model_evaluator():
    state = FakeEvidenceState(force=True)
    evaluator = RecordingEvaluator(
        SimpleNamespace(complete=False, missing_evidence=[], next_action="")
    )
    controller = EvidenceConvergenceController(
        policy=EvidenceConvergencePolicy(
            enabled=True,
            max_queries=4,
        ),
        evaluator=evaluator,
    )

    updates = [
        update
        async for update in controller.on_tool_event(
            question="question",
            state=state,
        )
    ]

    assert [(update.reason, update.converged) for update in updates] == [
        ("query_limit", True)
    ]
    assert updates[0].message == (
        "已完成多轮项目数据查询，正在直接生成阶段性分析结论..."
    )
    assert evaluator.calls == []


async def test_incomplete_evaluation_keeps_retrieving_and_marks_fingerprint():
    state = FakeEvidenceState()
    evaluator = RecordingEvaluator(
        SimpleNamespace(
            complete=False,
            missing_evidence=["缺少项目实测值", "缺少标准限值"],
            next_action="继续查询",
        )
    )
    controller = EvidenceConvergenceController(
        policy=EvidenceConvergencePolicy(enabled=True),
        evaluator=evaluator,
    )

    updates = [
        update
        async for update in controller.on_tool_event(
            question="question",
            state=state,
        )
    ]

    assert [update.reason for update in updates] == [
        "evaluating",
        "insufficient",
    ]
    assert updates[-1].message == (
        "现有证据不足，继续查询：缺少项目实测值；缺少标准限值"
    )
    assert updates[-1].converged is False
    assert state.marked_fingerprints == ["evidence-v1"]
    assert state.recorded_gaps == [
        (["缺少项目实测值", "缺少标准限值"], "继续查询")
    ]


async def test_repeated_incomplete_evaluations_converge_with_explicit_gap():
    state = FakeEvidenceState()
    evaluator = RecordingEvaluator(
        SimpleNamespace(
            complete=False,
            missing_evidence=["项目包没有抽水试验时序数据"],
            next_action="补充流量与降深时间序列",
        )
    )
    controller = EvidenceConvergenceController(
        policy=EvidenceConvergencePolicy(
            enabled=True,
            max_queries=20,
            max_incomplete_evaluations=2,
        ),
        evaluator=evaluator,
    )

    first = [
        update
        async for update in controller.on_tool_event(
            question="如何利用抽水试验数据降低参数不确定性？",
            state=state,
        )
    ]
    second = [
        update
        async for update in controller.on_tool_event(
            question="如何利用抽水试验数据降低参数不确定性？",
            state=state,
        )
    ]

    assert first[-1].reason == "insufficient"
    assert first[-1].converged is False
    assert second[-1].reason == "insufficient_limit"
    assert second[-1].converged is True
    assert "明确数据缺口" in second[-1].message


async def test_disabled_policy_is_a_noop_for_every_tool_event():
    state = FakeEvidenceState(force=True)
    evaluator = RecordingEvaluator(
        SimpleNamespace(complete=True, missing_evidence=[], next_action="")
    )
    controller = EvidenceConvergenceController(
        policy=EvidenceConvergencePolicy(enabled=False),
        evaluator=evaluator,
    )

    updates = [
        update
        async for update in controller.on_tool_event(
            question="question",
            state=state,
        )
    ]

    assert updates == []
    assert evaluator.calls == []
