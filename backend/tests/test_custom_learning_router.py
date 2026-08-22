import asyncio
from datetime import datetime

from backend.api.routers.custom_learning import (
    _build_low_score_adjustment,
    _build_multi_agent_quiz,
    _build_node_explanation,
    _collect_weak_points,
    _explore_learning_node,
    _finalize_node_assessment,
    _grade_quiz,
    _is_pending_review_node,
    _keyword_nodes,
    _merge_node_explanation,
    _module_effects,
    _next_action_hint,
    _node_learning_path,
    _normalize_kb_ids,
    _prewarm_node_content,
    _recalculate_plan,
    _refresh_node_quiz,
    _repair_plan_content,
    _style_summary,
    _style_task_hints,
    _unlock_next_nodes,
)
from backend.db.models import LearningNodeAttempt, LearningPlan, LearningPlanNode
from backend.services.custom_learning_agents import CustomLearningAgentTeam


def test_grade_quiz_scores_choice_and_short_answer() -> None:
    questions = [
        {"id": "q1", "type": "choice", "question": "核心目标？", "answer": "A"},
        {"id": "q2", "type": "short", "question": "说明掌握标准"},
    ]

    score, diagnosis = _grade_quiz(questions, {"q1": "A", "q2": "能说明概念、条件和常见误区"})

    assert score == 95
    assert diagnosis["weak_points"] == []
    assert diagnosis["suggestions"] == ["可以进入下一个节点"]


def test_grade_quiz_returns_per_question_feedback() -> None:
    questions = [
        {"id": "q1", "type": "choice", "question": "核心目标？", "answer": "A", "analysis": "看解析"},
        {"id": "q2", "type": "short", "question": "说明掌握标准", "reference": "概念 条件 误区"},
        {"id": "q3", "type": "judge", "question": "判断题", "answer": "正确"},
    ]

    _score, diagnosis = _grade_quiz(questions, {"q1": "B", "q2": "", "q3": "正确"})

    results = {item["id"]: item for item in diagnosis["question_results"]}
    assert results["q1"]["verdict"] == "wrong"
    assert results["q1"]["expected_answer"] == "A"
    assert results["q1"]["analysis"] == "看解析"
    assert results["q1"]["user_answer"] == "B"
    assert results["q2"]["verdict"] == "wrong"
    assert results["q2"]["missing_points"]
    assert results["q2"]["reference"] == "概念 条件 误区"
    assert results["q3"]["verdict"] == "correct"
    assert results["q3"]["score"] == 100


def test_generate_plan_blueprint_uses_llm_for_generic_goal(monkeypatch) -> None:
    async def run() -> None:
        from backend.application.custom_learning import common

        monkeypatch.delenv("CUSTOM_LEARNING_LLM_PLANNING", raising=False)

        async def fake_llm(prompt: str, *, timeout: float):
            assert "位置传感读出电路" in prompt
            return {
                "title": "位置传感读出专项",
                "summary": "从结构到位置重建的专项路径",
                "nodes": [
                    {"title": "读出结构与编码", "subtitle": "条形与像素", "objective": "区分读出结构", "resource": "结构讲义", "task": "整理对比表", "check": "能比较通道数"},
                    {"title": "电荷分配模型", "subtitle": "相邻通道幅值", "objective": "建立分配模型", "resource": "事件数据", "task": "分析幅值关系", "check": "能识别边缘效应"},
                    {"title": "重心位置重建", "subtitle": "插值与误差", "objective": "完成位置重建", "resource": "算法练习", "task": "计算坐标", "check": "能写出重心公式"},
                    {"title": "联调与验证", "subtitle": "端到端", "objective": "完成联调", "resource": "联调记录", "task": "输出位置谱", "check": "能定位异常"},
                ],
            }

        monkeypatch.setattr(common, "_planning_llm_call", fake_llm)
        blueprint = await common._generate_plan_blueprint(
            goal="学会位置传感读出电路",
            baseline="beginner",
            depth="analysis",
            duration_days=5,
            daily_minutes=60,
            kb_ids=[],
            style_snapshot=None,
        )

        titles = [node.title for node in blueprint.nodes]
        assert "重心位置重建" in titles
        assert all("任务边界" not in title for title in titles)
        assert blueprint.title == "位置传感读出专项"
        assert blueprint.agent_trace[0]["source"] == "llm_blueprint"
        # 基线评估仍然生效（入门级 1.2 倍时长）。
        assert all(node.estimated_minutes >= 60 for node in blueprint.nodes)

    asyncio.run(run())


def test_generate_plan_blueprint_falls_back_on_invalid_llm(monkeypatch) -> None:
    async def run() -> None:
        from backend.application.custom_learning import common

        monkeypatch.delenv("CUSTOM_LEARNING_LLM_PLANNING", raising=False)

        async def fake_llm_bad(prompt: str, *, timeout: float):
            return {"nodes": [{"title": ""}, "not-a-dict", {"subtitle": "无标题"}]}

        monkeypatch.setattr(common, "_planning_llm_call", fake_llm_bad)
        blueprint = await common._generate_plan_blueprint(
            goal="学会位置传感读出电路",
            baseline="unknown",
            depth="analysis",
            duration_days=5,
            daily_minutes=60,
            kb_ids=[],
            style_snapshot=None,
        )
        assert blueprint.agent_trace[0]["source"] == "keyword_rules"
        assert any("任务边界" in node.title for node in blueprint.nodes)

        async def fake_llm_raise(prompt: str, *, timeout: float):
            raise TimeoutError("slow model")

        monkeypatch.setattr(common, "_planning_llm_call", fake_llm_raise)
        blueprint = await common._generate_plan_blueprint(
            goal="学会位置传感读出电路",
            baseline="unknown",
            depth="analysis",
            duration_days=5,
            daily_minutes=60,
            kb_ids=[],
            style_snapshot=None,
        )
        assert blueprint.agent_trace[0]["source"] == "keyword_rules"

    asyncio.run(run())


def test_generate_plan_blueprint_skips_llm_for_curated_goal(monkeypatch) -> None:
    async def run() -> None:
        from backend.application.custom_learning import common

        monkeypatch.delenv("CUSTOM_LEARNING_LLM_PLANNING", raising=False)

        async def fail_llm(prompt: str, *, timeout: float):
            raise AssertionError("命中人工模板时不应调用 LLM")

        monkeypatch.setattr(common, "_planning_llm_call", fail_llm)
        blueprint = await common._generate_plan_blueprint(
            goal="7 天掌握前置放大器原理",
            baseline="unknown",
            depth="analysis",
            duration_days=7,
            daily_minutes=90,
            kb_ids=[],
            style_snapshot=None,
        )
        assert blueprint.agent_trace[0]["source"] == "keyword_rules"
        assert blueprint.nodes[0].title == "前放在测量链路中的作用"

    asyncio.run(run())


def _llm_grading_fixtures() -> tuple[LearningPlanNode, list[dict], dict]:
    node = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="脉冲形成",
        status="learning",
    )
    questions = [
        {"id": "q1", "type": "choice", "question": "选择题", "answer": "A"},
        {"id": "q2", "type": "short", "question": "简答题", "reference": "概念 条件 误区"},
    ]
    answers = {"q1": "A", "q2": "从电荷收集与成形时间常数两方面详细说明了脉冲形成过程"}
    return node, questions, answers


def test_grade_quiz_with_llm_merges_short_answer_scores(monkeypatch) -> None:
    async def run() -> None:
        from backend.application.custom_learning import content_quiz

        monkeypatch.delenv("CUSTOM_LEARNING_LLM_GRADING", raising=False)
        node, questions, answers = _llm_grading_fixtures()

        async def fake_llm(prompt: str, *, timeout: float):
            assert "简答题" in prompt
            assert timeout > 0
            return [{"id": "q2", "score": 88, "missing_points": [], "comment": "答得完整"}]

        monkeypatch.setattr(content_quiz, "_grading_llm_call", fake_llm)
        score, diagnosis = await content_quiz._grade_quiz_with_llm(node, questions, answers)

        assert diagnosis["grading"] == "llm_assisted"
        results = {item["id"]: item for item in diagnosis["question_results"]}
        assert results["q2"]["score"] == 88
        assert results["q2"]["verdict"] == "correct"
        assert results["q2"]["comment"] == "答得完整"
        assert results["q2"]["graded_by"] == "llm"
        assert score == round((100 + 88) / 2)
        assert diagnosis["weak_points"] == []
        assert diagnosis["suggestions"] == ["可以进入下一个节点"]

    asyncio.run(run())


def test_grade_quiz_with_llm_falls_back_to_rules_on_failure(monkeypatch) -> None:
    async def run() -> None:
        from backend.application.custom_learning import content_quiz

        monkeypatch.delenv("CUSTOM_LEARNING_LLM_GRADING", raising=False)
        node, questions, answers = _llm_grading_fixtures()
        rules_score, rules_diagnosis = _grade_quiz(questions, answers)

        async def fake_llm_none(prompt: str, *, timeout: float):
            return None

        monkeypatch.setattr(content_quiz, "_grading_llm_call", fake_llm_none)
        score, diagnosis = await content_quiz._grade_quiz_with_llm(node, questions, answers)

        assert diagnosis["grading"] == "rules_fallback"
        assert score == rules_score
        assert diagnosis["weak_points"] == rules_diagnosis["weak_points"]

        # 非法输出（未知题目 id、坏分数）同样回退。
        async def fake_llm_invalid(prompt: str, *, timeout: float):
            return [{"id": "q999", "score": 90}, {"id": "q2", "score": "abc"}]

        monkeypatch.setattr(content_quiz, "_grading_llm_call", fake_llm_invalid)
        _score, diagnosis = await content_quiz._grade_quiz_with_llm(node, questions, answers)
        assert diagnosis["grading"] == "rules_fallback"

    asyncio.run(run())


def test_grade_quiz_with_llm_skips_llm_without_short_answers(monkeypatch) -> None:
    async def run() -> None:
        from backend.application.custom_learning import content_quiz

        monkeypatch.delenv("CUSTOM_LEARNING_LLM_GRADING", raising=False)
        node, _questions, _answers = _llm_grading_fixtures()
        questions = [{"id": "q1", "type": "choice", "question": "选择题", "answer": "A"}]

        async def fail_llm(prompt: str, *, timeout: float):
            raise AssertionError("没有短答题时不应调用 LLM")

        monkeypatch.setattr(content_quiz, "_grading_llm_call", fail_llm)
        score, diagnosis = await content_quiz._grade_quiz_with_llm(node, questions, {"q1": "A"})
        assert score == 100
        assert "grading" not in diagnosis

        # 短答题留空同样不调用（规则直接判 0）。
        questions_with_blank = [
            {"id": "q1", "type": "choice", "question": "选择题", "answer": "A"},
            {"id": "q2", "type": "short", "question": "简答题"},
        ]
        _score, diagnosis = await content_quiz._grade_quiz_with_llm(node, questions_with_blank, {"q1": "A", "q2": " "})
        assert "grading" not in diagnosis

        # 环境开关关闭时也不调用。
        monkeypatch.setenv("CUSTOM_LEARNING_LLM_GRADING", "off")
        _score, diagnosis = await content_quiz._grade_quiz_with_llm(node, _questions, _answers)
        assert "grading" not in diagnosis

    asyncio.run(run())


def test_refresh_node_quiz_tracks_revision_and_varies_questions() -> None:
    async def run() -> None:
        plan = LearningPlan(
            plan_id="plan-1",
            user_id="user-1",
            title="自定义学习",
            duration_days=3,
            daily_minutes=60,
        )
        node = LearningPlanNode(
            node_id="node-1",
            plan_id="plan-1",
            order_index=0,
            title="核心概念",
            status="learning",
            mastery=40,
            estimated_minutes=60,
            check="能说明关键概念",
        )

        first_questions = await _refresh_node_quiz(plan, node)
        second_questions = await _refresh_node_quiz(plan, node)

        assert first_questions[-1]["id"] == "q_review_1"
        assert second_questions[-1]["id"] == "q_review_2"
        assert "第 2 次练习" in second_questions[-1]["question"]
        assert node.study_signals["quiz_revision"] == 2
        assert node.study_signals["last_agent"] == "quiz_author"

    asyncio.run(run())


def test_explore_learning_node_preserves_existing_detail_cache(monkeypatch) -> None:
    async def fake_generate_map(**_kwargs):
        return {"root": {"title": "核心概念"}, "children": [{"title": "子概念"}]}

    async def fake_gather_resources(**_kwargs):
        return {"resources": [{"doc_id": "doc-1", "title": "资料"}], "grounded": True}

    async def run() -> None:
        from backend.application.explore import service as explore_svc

        monkeypatch.setattr(explore_svc, "resolve_subject", lambda _domain: "核电子学")
        monkeypatch.setattr(explore_svc, "generate_map", fake_generate_map)
        monkeypatch.setattr(explore_svc, "gather_resources", fake_gather_resources)

        plan = LearningPlan(
            plan_id="plan-1",
            user_id="user-1",
            title="自定义学习",
            domain="ne",
            duration_days=3,
            daily_minutes=60,
            kb_ids=["kb-1"],
        )
        node = LearningPlanNode(
            node_id="node-1",
            plan_id="plan-1",
            order_index=0,
            title="核心概念",
            status="learning",
            mastery=40,
            estimated_minutes=60,
            explore_cache={
                "detail_content": "已有知识库详解",
                "detail_sources": [{"title": "详解来源"}],
            },
        )

        await _explore_learning_node(plan, node)

        assert node.explore_cache["detail_content"] == "已有知识库详解"
        assert node.explore_cache["detail_sources"] == [{"title": "详解来源"}]
        assert node.explore_cache["map"]["root"]["title"] == "核心概念"
        assert node.resources == [{"doc_id": "doc-1", "title": "资料"}]

    asyncio.run(run())


def test_low_score_adjustment_inserts_review_node_and_marks_current_node() -> None:
    plan = LearningPlan(
        plan_id="plan-1",
        user_id="user-1",
        title="自定义学习",
        duration_days=3,
        daily_minutes=60,
        progress=0,
        mastery=0,
    )
    node = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="核心概念",
        status="learning",
        mastery=40,
        estimated_minutes=60,
        tasks=["整理笔记"],
        checks=["能说明关键概念"],
    )
    next_node = LearningPlanNode(
        node_id="node-2",
        plan_id="plan-1",
        order_index=1,
        title="案例练习",
        status="locked",
        mastery=0,
        estimated_minutes=60,
    )
    plan.nodes = [node, next_node]

    adjustment = _build_low_score_adjustment(
        plan,
        node,
        score=45,
        diagnosis={"weak_points": ["核心概念掌握不稳"]},
    )

    assert adjustment is not None
    assert adjustment.trigger_type == "low_score"
    assert adjustment.accepted is True
    assert node.status == "review"
    assert node.study_signals["needs_review"] is True
    assert next_node.order_index == 2

    review_nodes = [item for item in plan.nodes if item.node_type == "review"]
    assert len(review_nodes) == 1
    assert review_nodes[0].status == "open"
    assert review_nodes[0].parent_node_id == "node-1"
    assert review_nodes[0].order_index == 1
    assert adjustment.after_snapshot["created_node_id"] == review_nodes[0].node_id


def test_review_child_completion_reopens_parent_without_unlocking_next() -> None:
    plan = LearningPlan(
        plan_id="plan-1",
        user_id="user-1",
        title="自定义学习",
        duration_days=3,
        daily_minutes=60,
    )
    parent = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="核心概念",
        status="review",
        mastery=45,
        estimated_minutes=60,
    )
    review_child = LearningPlanNode(
        node_id="review-1",
        plan_id="plan-1",
        parent_node_id="node-1",
        order_index=1,
        title="复习：核心概念",
        node_type="review",
        status="done",
        mastery=85,
        estimated_minutes=30,
    )
    next_node = LearningPlanNode(
        node_id="node-2",
        plan_id="plan-1",
        order_index=2,
        title="案例练习",
        status="locked",
        mastery=0,
        estimated_minutes=60,
    )
    plan.nodes = [parent, review_child, next_node]

    unlocked = _unlock_next_nodes(plan, review_child)

    assert unlocked == []
    assert parent.status == "open"
    assert next_node.status == "locked"


def test_parent_retry_unlocks_next_main_node() -> None:
    plan = LearningPlan(
        plan_id="plan-1",
        user_id="user-1",
        title="自定义学习",
        duration_days=3,
        daily_minutes=60,
    )
    parent = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="核心概念",
        status="done",
        mastery=85,
        estimated_minutes=60,
    )
    review_child = LearningPlanNode(
        node_id="review-1",
        plan_id="plan-1",
        parent_node_id="node-1",
        order_index=1,
        title="复习：核心概念",
        node_type="review",
        status="done",
        mastery=90,
        estimated_minutes=30,
    )
    next_node = LearningPlanNode(
        node_id="node-2",
        plan_id="plan-1",
        order_index=2,
        title="案例练习",
        status="locked",
        mastery=0,
        estimated_minutes=60,
    )
    plan.nodes = [parent, review_child, next_node]

    unlocked = _unlock_next_nodes(plan, parent)

    assert unlocked == ["node-2"]
    assert next_node.status == "open"


def test_collect_weak_points_ignores_recovered_attempts() -> None:
    plan = LearningPlan(
        plan_id="plan-1",
        user_id="user-1",
        title="自定义学习",
        duration_days=3,
        daily_minutes=60,
    )
    node = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="核心概念",
        status="done",
        mastery=90,
        estimated_minutes=60,
    )
    plan.nodes = [node]
    attempts = [
        LearningNodeAttempt(
            node_id="node-1",
            score=90,
            diagnosis={"weak_points": []},
            created_at=datetime(2026, 1, 2, 12, 0, 0),
        ),
        LearningNodeAttempt(
            node_id="node-1",
            score=45,
            diagnosis={"weak_points": ["核心概念掌握不稳"]},
            created_at=datetime(2026, 1, 1, 12, 0, 0),
        ),
    ]

    assert _collect_weak_points(plan, attempts) == []


def test_pending_review_node_excludes_completed_review_children() -> None:
    done_review = LearningPlanNode(
        node_id="review-1",
        plan_id="plan-1",
        order_index=1,
        title="复习：核心概念",
        node_type="review",
        status="done",
        estimated_minutes=30,
    )
    pending_parent = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="核心概念",
        status="review",
        estimated_minutes=60,
    )

    assert _is_pending_review_node(done_review) is False
    assert _is_pending_review_node(pending_parent) is True


def test_next_action_hint_prioritizes_review_child() -> None:
    plan = LearningPlan(
        plan_id="plan-1",
        user_id="user-1",
        title="自定义学习",
        duration_days=3,
        daily_minutes=60,
        status="active",
    )
    parent = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="核心概念",
        status="review",
        mastery=45,
        estimated_minutes=60,
        explanation="已有详解",
        quiz_questions=[{"id": "q1"}],
    )
    review_child = LearningPlanNode(
        node_id="review-1",
        plan_id="plan-1",
        parent_node_id="node-1",
        order_index=1,
        title="复习：核心概念",
        node_type="review",
        status="open",
        estimated_minutes=30,
    )
    plan.nodes = [parent, review_child]

    hint = _next_action_hint(plan, parent)

    assert hint["action"] == "goto_review_child"
    assert hint["node_id"] == "review-1"


def test_finalize_node_assessment_marks_review_and_inserts_child() -> None:
    plan = LearningPlan(
        plan_id="plan-1",
        user_id="user-1",
        title="自定义学习",
        duration_days=3,
        daily_minutes=60,
    )
    node = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="核心概念",
        status="learning",
        mastery=40,
        estimated_minutes=60,
    )
    next_node = LearningPlanNode(
        node_id="node-2",
        plan_id="plan-1",
        order_index=1,
        title="案例练习",
        status="locked",
        mastery=0,
        estimated_minutes=60,
    )
    plan.nodes = [node, next_node]

    adjustment, unlocked = _finalize_node_assessment(
        plan,
        node,
        45,
        {"weak_points": ["核心概念掌握不稳"]},
    )

    assert adjustment is not None
    assert unlocked == []
    assert node.status == "review"
    assert any(item.node_type == "review" for item in plan.nodes)


def test_next_action_hint_for_review_child_node() -> None:
    plan = LearningPlan(
        plan_id="plan-1",
        user_id="user-1",
        title="自定义学习",
        duration_days=3,
        daily_minutes=60,
        status="active",
    )
    review_child = LearningPlanNode(
        node_id="review-1",
        plan_id="plan-1",
        parent_node_id="node-1",
        order_index=1,
        title="复习：核心概念",
        node_type="review",
        status="open",
        estimated_minutes=30,
    )
    plan.nodes = [review_child]

    hint = _next_action_hint(plan, review_child)

    assert hint["action"] == "start_node"
    assert hint["label"] == "开始复习"


def test_style_snapshot_maps_to_visible_task_hints() -> None:
    snapshot = {
        "enabled": True,
        "result": {
            "dimensions": [
                {"dimension_id": "processing", "preferred_label": "活跃型"},
                {"dimension_id": "input", "preferred_label": "视觉型"},
                {"dimension_id": "understanding", "preferred_label": "综合型"},
            ]
        },
    }

    hints = _style_task_hints(snapshot)

    assert any("即时练习" in hint for hint in hints)
    assert any("结构图" in hint or "流程图" in hint for hint in hints)
    assert any("整体框架" in hint for hint in hints)


def test_style_summary_exposes_agent_strategy_for_frontend() -> None:
    summary = _style_summary(
        {
            "enabled": True,
            "result": {
                "dimensions": [
                    {"dimension_id": "processing", "preferred_label": "活跃型"},
                    {"dimension_id": "input", "preferred_label": "视觉型"},
                ]
            },
        }
    )

    assert summary["enabled"] is True
    assert summary["dimensions"]["processing"] == "活跃型"
    assert "活跃型" in summary["labels"]
    assert any("即时练习" in hint for hint in summary["task_hints"])
    assert "结构图" in summary["explanation_hint"] or "流程图" in summary["explanation_hint"]


def test_node_explanation_is_deterministic_and_uses_style_snapshot() -> None:
    plan = LearningPlan(
        plan_id="plan-1",
        user_id="user-1",
        title="自定义学习",
        duration_days=3,
        daily_minutes=60,
        style_snapshot={
            "enabled": True,
            "result": {"dimensions": [{"dimension_id": "input", "preferred_label": "视觉型"}]},
        },
    )
    node = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="PN 结与耗尽区",
        subtitle="理解灵敏体积",
        objective="解释反偏 PN 结形成灵敏体积的机制",
        status="learning",
        resource="半导体探测器讲义",
        tasks=["画出耗尽区变化示意"],
        checks=["能说明偏置电压对耗尽层厚度的影响"],
        estimated_minutes=60,
    )

    explanation = _build_node_explanation(plan, node)

    assert "## PN 结与耗尽区" in explanation
    assert "半导体探测器讲义" in explanation
    assert "结构图" in explanation or "流程图" in explanation
    assert "能说明偏置电压对耗尽层厚度的影响" in explanation


def test_preamp_goal_generates_usable_domain_nodes_and_quiz() -> None:
    nodes = _keyword_nodes("前置放大器原理", 7)

    titles = [node["title"] for node in nodes]
    assert "学习目标拆解" not in titles
    assert "电荷灵敏前置放大器原理" in titles
    assert "噪声来源与等效噪声电荷" in titles

    quiz_node = LearningPlanNode(title="电荷灵敏前置放大器原理", subtitle="", objective="")
    questions = _build_multi_agent_quiz(quiz_node)
    assert "ΔV≈Q/Cf" in questions[0]["options"][0]["text"]
    assert len(questions) == 5
    assert {item["type"] for item in questions} >= {"choice", "judge", "short"}
    assert {item["source_agent"] for item in questions} >= {
        "concept_examiner",
        "scenario_examiner",
        "mechanism_examiner",
        "misconception_diagnostician",
        "transfer_coach",
    }


def test_repair_plan_content_replaces_placeholder_nodes() -> None:
    plan = LearningPlan(
        plan_id="plan-1",
        user_id="user-1",
        title="前置放大器原理",
        goal="前置放大器原理",
        duration_days=7,
        daily_minutes=60,
        metadata_={"generator": "rules-v1"},
    )
    node = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="学习目标拆解",
        subtitle="把目标拆成可验证的学习任务",
        objective="把目标拆成可验证的学习任务",
        task="整理学习目标拆解学习笔记",
        check="能解释学习目标拆解的关键概念",
        status="learning",
        quiz_questions=[{"id": "q1", "type": "choice", "question": "学习时，最需要先确认的是哪一项？"}],
    )
    plan.nodes = [node]

    changed = _repair_plan_content(plan)

    assert changed is True
    assert node.title == "前放在测量链路中的作用"
    assert "前置放大器" in (node.explanation or "")
    assert node.quiz_questions is None


def test_draft_plan_recalculation_preserves_draft_status() -> None:
    plan = LearningPlan(
        plan_id="plan-1",
        user_id="user-1",
        title="自定义学习草稿",
        status="draft",
        duration_days=7,
        daily_minutes=60,
    )
    first_node = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="确认学习目标",
        status="locked",
        mastery=0,
    )
    second_node = LearningPlanNode(
        node_id="node-2",
        plan_id="plan-1",
        order_index=1,
        title="开始节点学习",
        status="locked",
        mastery=0,
    )
    plan.nodes = [first_node, second_node]

    _recalculate_plan(plan)

    assert plan.status == "draft"
    assert plan.progress == 0
    assert plan.current_node_id == "node-1"


def test_next_action_hint_guides_learning_flow() -> None:
    plan = LearningPlan(
        plan_id="plan-1",
        user_id="user-1",
        title="自定义学习",
        status="active",
        duration_days=7,
        daily_minutes=60,
        kb_ids=["kb-1"],
    )
    node = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="电荷灵敏前置放大器原理",
        status="learning",
        mastery=0,
    )
    plan.nodes = [node]

    assert _next_action_hint(plan, node)["action"] == "start_node"

    node.explanation = "节点详解"
    assert _next_action_hint(plan, node)["action"] == "enrich_content"

    node.explore_cache = {"detail_content": "知识库详解", "map": {"children": []}, "grounded": True}
    assert _next_action_hint(plan, node)["action"] == "generate_quiz"

    node.quiz_questions = [{"id": "q1", "question": "题目"}]
    assert _next_action_hint(plan, node)["action"] == "await_quiz"


def test_module_effects_explain_each_learning_module() -> None:
    plan = LearningPlan(
        plan_id="plan-1",
        user_id="user-1",
        title="前置放大器原理",
        goal="前置放大器原理",
        status="active",
        duration_days=7,
        daily_minutes=60,
        progress=30,
        kb_ids=["kb-1"],
        style_snapshot={
            "enabled": True,
            "result": {"dimensions": [{"dimension_id": "input", "preferred_label": "视觉型"}]},
        },
    )
    node = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="电荷灵敏前置放大器原理",
        status="learning",
        mastery=20,
        explanation="节点详解",
        quiz_questions=[{"id": "q1", "question": "题目"}],
        explore_cache={"resources": [{"title": "前放资料"}]},
        study_signals={"prewarmed": True},
    )
    plan.nodes = [node]

    effects = _module_effects(
        plan,
        review={"weak_points": ["反馈电容"], "latest_attempts": [{"score": 60}], "review_nodes": []},
    )

    effect_ids = {item["id"] for item in effects}
    assert effect_ids >= {"custom_path", "explore", "learning_style", "practice", "response_speed"}
    assert next(item for item in effects if item["id"] == "custom_path")["metric"] == "30% 进度"
    assert next(item for item in effects if item["id"] == "explore")["status"] == "active"
    assert next(item for item in effects if item["id"] == "practice")["status"] == "risk"
    assert "视觉型" in next(item for item in effects if item["id"] == "learning_style")["metric"]


def test_prewarm_node_content_generates_explanation_for_unlocked_node() -> None:
    plan = LearningPlan(
        plan_id="plan-1",
        user_id="user-1",
        title="前置放大器原理",
        goal="前置放大器原理",
        status="active",
        duration_days=7,
        daily_minutes=60,
    )
    node = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="电荷灵敏前置放大器原理",
        subtitle="理解 Q/Cf 增益关系",
        status="open",
        mastery=0,
        resource="前放讲义",
        task="画出电荷灵敏前放链路",
        check="能解释反馈电容对输出幅度的影响",
    )

    _prewarm_node_content(plan, node)

    assert node.explanation
    assert node.study_signals["prewarmed"] is True
    assert node.study_signals["last_agent"] == "content_explainer"


def test_custom_learning_agent_team_generates_traceable_blueprint() -> None:
    team = CustomLearningAgentTeam()
    blueprint = team.generate_plan_blueprint(
        goal="7 天掌握前置放大器原理",
        baseline="beginner",
        depth="analysis",
        duration_days=7,
        daily_minutes=60,
        kb_ids=["kb-1"],
        style_snapshot={
            "enabled": True,
            "result": {"dimensions": [{"dimension_id": "input", "preferred_label": "视觉型"}]},
        },
    )

    assert blueprint.nodes
    assert blueprint.nodes[0].title == "前放在测量链路中的作用"
    assert "结构图" in "；".join(blueprint.style_task_hints)
    assert {item["agent"] for item in blueprint.agent_trace} >= {
        "curriculum_planner",
        "baseline_assessor",
        "learning_style_coach",
        "resource_curator",
        "practice_designer",
        "weekly_scheduler",
        "path_monitor",
        "explore_bridge",
    }
    assert all("decision" in item for item in blueprint.agent_trace)
    assert any(item.get("confidence") is not None for item in blueprint.agent_trace)
    assert blueprint.projects
    assert blueprint.weekly_plan


def test_normalize_kb_ids_deduplicates_and_trims() -> None:
    assert _normalize_kb_ids([" kb-1 ", "kb-1", "", "kb-2", "kb-2"]) == ["kb-1", "kb-2"]
    assert _normalize_kb_ids(None) == []


def test_node_learning_path_collects_plan_context() -> None:
    plan = LearningPlan(plan_id="plan-1", user_id="user-1", title="探测器原理", goal="掌握探测器基础")
    node = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="辐射相互作用",
        subtitle="电离、激发与光子三效应",
        status="learning",
    )

    assert _node_learning_path(plan, node) == [
        "探测器原理",
        "掌握探测器基础",
        "电离、激发与光子三效应",
        "辐射相互作用",
    ]


def test_merge_node_explanation_prefers_kb_detail() -> None:
    plan = LearningPlan(plan_id="plan-1", user_id="user-1", title="探测器原理", goal="掌握探测器基础")
    node = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="辐射相互作用",
        status="learning",
        explanation="## 模板导引",
    )

    merged = _merge_node_explanation(plan, node, "## 知识库详解\n\n基于资料展开。")

    assert merged.startswith("## 知识库详解")
    assert "模板导引" not in merged


def test_next_action_hint_requests_kb_detail_when_missing() -> None:
    plan = LearningPlan(plan_id="plan-1", user_id="user-1", title="探测器原理", kb_ids=["kb-1"], status="active")
    node = LearningPlanNode(
        node_id="node-1",
        plan_id="plan-1",
        order_index=0,
        title="辐射相互作用",
        status="learning",
        explanation="## 模板导引",
        explore_cache={"map": {"children": []}},
    )

    hint = _next_action_hint(plan, node)

    assert hint["action"] == "enrich_content"
    assert "知识库" in hint["label"]
