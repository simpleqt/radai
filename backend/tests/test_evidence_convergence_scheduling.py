"""研究调度、结构化收敛与写作投影契约（拆自 test_evidence_matrix.py）。"""

import asyncio
import json
import time

from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
    CoordinationUnitResult,
    ResearchTask,
)
from backend.domain.agents.request import AgentRequest


def test_fallback_report_aligns_branch_results_by_task_key():
    tasks = [
        ResearchTask("t1", "base", "base", priority=10),
        ResearchTask("t2", "high", "high", priority=100),
    ]
    report = CoordinatedAgentWorkflow._build_fallback_report(
        request=AgentRequest(question="q", agent_code="a", user_id="u"),
        planner=CoordinationUnitResult(role="planner", answer="plan"),
        tasks=tasks,
        branch_results=[
            CoordinationUnitResult(
                role="researcher_2",
                answer="t2 answer",
                task_key="t2",
                task_title="high",
            )
        ],
    )

    base_index = report.index("## base")
    high_index = report.index("## high")
    assert "尚未完成" in report[base_index:high_index]
    assert "t2 answer" in report[high_index:]


async def test_scheduler_respects_task_dependencies():
    seen: list[str] = []

    class DummyWorkflow(CoordinatedAgentWorkflow):
        async def _run_research_task(self, *, task, index, **kwargs):  # type: ignore[override]
            seen.append(task.key)
            return CoordinationUnitResult(role=f"researcher_{index}", answer=task.key)

    workflow = DummyWorkflow(name="deep_research")
    tasks = [
        (1, ResearchTask("t1", "base", "base", priority=10)),
        (
            2,
            ResearchTask(
                "t2", "dependent", "dependent", priority=100, dependencies=("t1",)
            ),
        ),
    ]

    results = await workflow._run_scheduled_research_tasks(
        scheduled_tasks=tasks,
        used_fallback=False,
        kb_circuit_open=asyncio.Event(),
        request=AgentRequest(question="q", agent_code="a", user_id="u"),
        context=None,  # type: ignore[arg-type]
        event_sink=None,
        analysis_gate=None,
    )

    assert seen == ["t1", "t2"]
    assert [result.answer for result in results] == ["t1", "t2"]


async def test_scheduler_partial_handoff_does_not_discard_remaining_tasks():
    seen: list[str] = []
    partial_snapshots: list[tuple[list[str], list[str]]] = []

    async def capture_partial(results):
        partial_snapshots.append(([result.answer for result in results], list(seen)))

    class DummyWorkflow(CoordinatedAgentWorkflow):
        async def _run_research_task(self, *, task, index, **kwargs):  # type: ignore[override]
            if task.key == "t2":
                await asyncio.sleep(0.01)
            seen.append(task.key)
            return CoordinationUnitResult(role=f"researcher_{index}", answer=task.key)

    workflow = DummyWorkflow(name="deep_research")
    tasks = [
        (1, ResearchTask("t1", "high", "high", priority=100)),
        (2, ResearchTask("t2", "low", "low", priority=10)),
    ]

    results = await workflow._run_scheduled_research_tasks(
        scheduled_tasks=tasks,
        used_fallback=False,
        kb_circuit_open=asyncio.Event(),
        request=AgentRequest(question="q", agent_code="a", user_id="u"),
        context=None,  # type: ignore[arg-type]
        event_sink=None,
        analysis_gate=None,
        partial_result_limit=1,
        partial_result_sink=capture_partial,
    )

    assert seen == ["t1", "t2"]
    assert [result.answer for result in results] == ["t1", "t2"]
    assert partial_snapshots == [(["t1"], ["t1"])]


async def test_scheduler_runs_dependent_with_explicit_failed_dependency():
    seen: list[str] = []
    dependent_inputs: list[list[CoordinationUnitResult]] = []

    class DummyWorkflow(CoordinatedAgentWorkflow):
        async def _run_research_task(self, *, task, index, **kwargs):  # type: ignore[override]
            seen.append(task.key)
            if task.key == "t2":
                dependent_inputs.append(list(kwargs.get("dependency_results") or []))
            return CoordinationUnitResult(
                role=f"researcher_{index}",
                answer="" if task.key == "t1" else task.key,
                error="failed" if task.key == "t1" else None,
            )

    workflow = DummyWorkflow(name="deep_research")
    results = await workflow._run_scheduled_research_tasks(
        scheduled_tasks=[
            (1, ResearchTask("t1", "base", "base", priority=100)),
            (2, ResearchTask("t2", "dependent", "dependent", dependencies=("t1",))),
        ],
        used_fallback=False,
        kb_circuit_open=asyncio.Event(),
        request=AgentRequest(question="q", agent_code="a", user_id="u"),
        context=None,  # type: ignore[arg-type]
        event_sink=None,
        analysis_gate=None,
    )

    assert seen == ["t1", "t2"]
    assert results[-1].answer == "t2"
    assert results[-1].task_key == "t2"
    assert results[-1].error is None
    assert len(dependent_inputs) == 1
    assert dependent_inputs[0][0].task_key == "t1"
    assert dependent_inputs[0][0].error == "failed"


async def test_scheduler_does_not_start_new_wave_below_admission_floor():
    seen: list[str] = []

    class DummyWorkflow(CoordinatedAgentWorkflow):
        async def _run_research_task(self, *, task, **kwargs):  # type: ignore[override]
            seen.append(task.key)
            return CoordinationUnitResult(role="researcher", answer="unexpected")

    workflow = DummyWorkflow(name="deep_research")
    results = await workflow._run_scheduled_research_tasks(
        scheduled_tasks=[
            (1, ResearchTask("late", "late branch", "collect evidence")),
        ],
        used_fallback=False,
        kb_circuit_open=asyncio.Event(),
        request=AgentRequest(question="q", agent_code="a", user_id="u"),
        context=None,  # type: ignore[arg-type]
        event_sink=None,
        analysis_gate=None,
        research_deadline=time.monotonic() + 0.01,
    )

    assert seen == []
    assert results[0].task_key == "late"
    assert results[0].timed_out is True


async def test_no_deadline_wave_isolates_unexpected_branch_error():
    """plan_solve (research_deadline=None) 波次:一个分支抛未预期异常,
    不得中止整波、丢弃兄弟分支证据——须与截止分支一样逐分支降级。"""

    class DummyWorkflow(CoordinatedAgentWorkflow):
        async def _run_research_task(self, *, task, index, **kwargs):  # type: ignore[override]
            if task.key == "boom":
                raise RuntimeError("unexpected branch crash")
            return CoordinationUnitResult(role=f"researcher_{index}", answer=task.key)

    workflow = DummyWorkflow(name="plan_solve")
    results = await workflow._run_scheduled_research_tasks(
        scheduled_tasks=[
            (1, ResearchTask("boom", "crashing", "crashing")),
            (2, ResearchTask("ok", "healthy", "healthy")),
        ],
        used_fallback=False,
        kb_circuit_open=asyncio.Event(),
        request=AgentRequest(question="q", agent_code="a", user_id="u"),
        context=None,  # type: ignore[arg-type]
        event_sink=None,
        analysis_gate=None,
    )  # research_deadline 省略 → 走无截止 gather 分支

    by_key = {result.task_key: result for result in results}
    assert set(by_key) == {"boom", "ok"}
    # 兄弟分支证据保留
    assert by_key["ok"].answer == "ok"
    assert by_key["ok"].error is None
    # 崩溃分支降级为带 error 的占位结果,而非整波抛出
    assert by_key["boom"].answer == ""
    assert by_key["boom"].error
    assert by_key["boom"].timed_out is False


async def test_no_deadline_wave_maps_branch_cancellation_to_timeout_result():
    """无截止波次里,分支自身被取消须降级为 timed_out 占位结果而非上抛。"""

    class DummyWorkflow(CoordinatedAgentWorkflow):
        async def _run_research_task(self, *, task, index, **kwargs):  # type: ignore[override]
            if task.key == "cancelled":
                raise asyncio.CancelledError()
            return CoordinationUnitResult(role=f"researcher_{index}", answer=task.key)

    workflow = DummyWorkflow(name="plan_solve")
    results = await workflow._run_scheduled_research_tasks(
        scheduled_tasks=[
            (1, ResearchTask("cancelled", "cancelled branch", "c")),
            (2, ResearchTask("ok", "healthy", "healthy")),
        ],
        used_fallback=False,
        kb_circuit_open=asyncio.Event(),
        request=AgentRequest(question="q", agent_code="a", user_id="u"),
        context=None,  # type: ignore[arg-type]
        event_sink=None,
        analysis_gate=None,
    )

    by_key = {result.task_key: result for result in results}
    assert by_key["ok"].answer == "ok"
    assert by_key["cancelled"].timed_out is True


def test_convergence_prompt_keeps_evidence_json_valid_when_bounded():
    records = [
        {
            "id": f"record-{index}",
            "doc_title": f"测试文献 {index}",
            "chunk_id": f"chunk-{index}",
            "content": ("含有括号、引号和公式 [x] {y} 的证据内容。" * 80),
        }
        for index in range(20)
    ]

    prompt = CoordinatedAgentWorkflow._build_tool_free_convergence_prompt(
        objective="核验研究结论",
        partial_answer="阶段性文本",
        evidence_refs=[],
        evidence_records=records,
    )

    evidence_json = prompt.split("证据记录：\n", 1)[1].split(
        "\n\n已捕获来源：",
        1,
    )[0]
    parsed = json.loads(evidence_json)
    assert parsed
    assert len(evidence_json) <= 24000
    assert all(item["source"].startswith("[测试文献") for item in parsed)


def test_convergence_prompt_selects_later_independent_sources():
    records = [
        {
            "id": f"early-{index}",
            "source_id": "same-source",
            "doc_title": "重复来源",
            "chunk_id": f"chunk-{index}",
            "content": f"重复来源的第 {index} 个片段，" + ("背景材料" * 80),
            "source_tool": "web_search",
            "score": 0.4,
        }
        for index in range(20)
    ]
    records.append(
        {
            "id": "late-authority",
            "source_id": "authority-source",
            "doc_title": "权威来源",
            "chunk_id": "late",
            "content": "权威来源给出直接相关的关键性能数据。" + ("关键证据" * 80),
            "source_tool": "multi_kb_search",
            "score": 0.95,
        }
    )

    prompt = CoordinatedAgentWorkflow._build_tool_free_convergence_prompt(
        objective="核验关键性能",
        partial_answer="",
        evidence_refs=[],
        evidence_records=records,
    )

    evidence_json = prompt.split("证据记录：\n", 1)[1].split("\n\n已捕获来源：", 1)[0]
    parsed = json.loads(evidence_json)
    assert any(item["id"] == "late-authority" for item in parsed)
    assert sum(item["source_id"] == "same-source" for item in parsed) <= 2


def test_structured_convergence_keeps_only_refs_captured_by_branch():
    captured_url = "https://example.com/captured-source"
    invented_url = "https://example.com/invented-source"
    payload = {
        "summary": "包含一条有效结论和一条无效结论",
        "claims": [
            {
                "claim": "捕获证据明确支持性能提升",
                "supporting_refs": [captured_url],
                "confidence": 0.9,
            },
            {
                "claim": "未捕获来源声称效率达到 99%",
                "supporting_refs": [invented_url],
                "confidence": 0.9,
            },
        ],
        "confidence": 0.8,
        "conflicts": [],
        "missing": [],
        "risks": [],
        "suggested_next_actions": [],
    }

    handoff = CoordinatedAgentWorkflow._sanitize_structured_convergence_handoff(
        json.dumps(payload, ensure_ascii=False),
        role="evidence_researcher",
        objective="核验证据",
        answer_fallback="",
        task_id="research:1:convergence",
        evidence_refs=[captured_url],
        evidence_records=[],
    )

    assert handoff is not None
    assert handoff["evidence_refs"] == [captured_url]
    assert [claim["claim"] for claim in handoff["claims"]] == [
        "捕获证据明确支持性能提升"
    ]
    assert handoff["claims"][0]["supporting_refs"] == [captured_url]
    assert invented_url not in json.dumps(handoff, ensure_ascii=False)
    assert "1 条候选结论" in handoff["missing"][-1]


def test_structured_convergence_accepts_title_alias_for_chunk_ref():
    payload = {
        "summary": "标题别名应映射到完整 chunk 引用",
        "claims": [
            {
                "claim": "核技术全书指出热中子探测效率可达 80%",
                "supporting_refs": ["[核技术全书]"],
                "confidence": 0.85,
            }
        ],
        "confidence": 0.85,
        "conflicts": [],
        "missing": [],
        "risks": [],
        "suggested_next_actions": [],
    }
    chunk_ref = "[核技术全书#chunk-30748814e7f97c4c74762cd2f6a3f184]"

    handoff = CoordinatedAgentWorkflow._sanitize_structured_convergence_handoff(
        json.dumps(payload, ensure_ascii=False),
        role="domain_researcher",
        objective="核验效率",
        answer_fallback="",
        task_id="research:2:convergence",
        evidence_refs=[chunk_ref],
        evidence_records=[
            {
                "doc_title": "核技术全书",
                "chunk_id": "chunk-30748814e7f97c4c74762cd2f6a3f184",
                "content": "热中子探测效率可达 80%。",
            }
        ],
    )

    assert handoff is not None
    assert handoff["claims"][0]["supporting_refs"] == [chunk_ref]


def test_structured_convergence_recovers_url_variant_and_source_id_binding():
    captured_url = "https://www.tsinghua.edu.cn/info/1175/103685.htm?from=search"
    payload = {
        "summary": "两条结论均来自已捕获来源",
        "claims": [
            {
                "claim": "清华大学页面披露了探测器原型进展",
                "supporting_refs": [
                    "[清华大学](http://tsinghua.edu.cn/info/1175/103685.htm#result)"
                ],
                "confidence": 0.85,
            },
            {
                "claim": "同一证据记录还给出了样品尺寸",
                "supporting_refs": [],
                "source_ids": ["ev-tsinghua"],
                "confidence": 0.8,
            },
        ],
        "confidence": 0.8,
        "conflicts": [],
        "missing": [],
        "risks": [],
        "suggested_next_actions": [],
    }

    handoff = CoordinatedAgentWorkflow._sanitize_structured_convergence_handoff(
        payload,
        role="evidence_researcher",
        objective="核验探测器进展",
        answer_fallback="",
        task_id="research:url-variant:convergence",
        evidence_refs=[captured_url],
        evidence_records=[
            {
                "id": "ev-tsinghua",
                "source_id": captured_url,
                "doc_title": "清华大学研究进展",
                "chunk_id": "result-1",
                "raw_ref": captured_url,
                "content": "页面披露了探测器原型进展和样品尺寸。",
            }
        ],
    )

    assert handoff is not None
    assert len(handoff["claims"]) == 2
    assert all(
        claim["supporting_refs"] == [captured_url] for claim in handoff["claims"]
    )


def test_quality_recovery_triggers_when_captured_sources_are_underbound():
    from backend.domain.agents.profile import AgentProfile
    from backend.domain.agents.runtime_context import AgentRuntimeContext

    tasks = [
        ResearchTask(
            "t1",
            "现状、案例与最新进展",
            "收集最新进展",
            expected_tools=("multi_kb_search",),
        )
    ]
    branch = CoordinationUnitResult(
        role="researcher_1",
        answer="仅形成少量绑定结论",
        task_key="t1",
        task_title="现状、案例与最新进展",
        handoff={
            "evidence_refs": ["https://example.com/a"],
            "claims": [
                {
                    "claim": "捕获证据支持该探测器用于热中子测量",
                    "supporting_refs": ["https://example.com/a"],
                    "confidence": 0.9,
                }
            ],
            "missing": [],
        },
        captured_evidence_refs=(
            "https://example.com/a",
            "https://example.com/b",
            "https://example.com/c",
            "https://example.com/d",
        ),
    )
    context = AgentRuntimeContext(
        request_id="req-1",
        user_id="u",
        external_thread_id="thread-1",
        internal_thread_id="u:thread-1",
        agent_profile=AgentProfile(code="ne", name="NE", domain="nuclear"),
        workflow_name="deep_research",
        model_name=None,
        kb_ids=["kb-1"],
        use_search=True,
    )

    recovery = CoordinatedAgentWorkflow._prepare_quality_recovery_tasks(
        tasks=tasks,
        branch_results=[branch],
        context=context,
        round_index=1,
        limit=2,
    )

    assert len(recovery) == 1
    assert recovery[0].title.startswith("证据恢复：")
    assert "覆盖不足" in recovery[0].question


def test_writer_projection_hides_uncited_summary_and_claim_text():
    url = "https://example.com/source"
    unsupported_text = "没有逐项来源的性能数字为 99%"
    branch = CoordinationUnitResult(
        role="evidence_researcher",
        answer=f"有效结论。{unsupported_text}",
        handoff={
            "task_id": "research:1",
            "subagent_type": "evidence_researcher",
            "description": "核验证据",
            "summary": f"有效结论。{unsupported_text}",
            "evidence_refs": [url],
            "claims": [
                {
                    "claim": "捕获证据支持有效结论",
                    "supporting_refs": [url],
                    "confidence": 0.9,
                },
                {
                    "claim": unsupported_text,
                    "supporting_refs": [],
                    "confidence": 0.8,
                },
            ],
            "confidence": 0.8,
        },
    )

    projected = CoordinatedAgentWorkflow._writer_safe_branch_results([branch])[0]
    serialized = json.dumps(projected.handoff, ensure_ascii=False)

    assert projected.answer == "该研究分支提供 1 条逐项绑定来源的结论。"
    assert unsupported_text not in serialized
    assert projected.handoff is not None
    assert len(projected.handoff["claims"]) == 1
    assert projected.handoff["evidence_refs"] == [url]
