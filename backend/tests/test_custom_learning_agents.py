from backend.services.custom_learning_agents import (
    ContentExplainerAgent,
    CustomLearningAgentTeam,
    KnowledgePointAgent,
    LearningStyleCoachAgent,
    MasteryDiagnosticianAgent,
    PathAdjusterAgent,
    QuizAuthorAgent,
)


def test_baseline_assessor_compresses_nodes_for_expert() -> None:
    team = CustomLearningAgentTeam()
    raw = team.planner.plan_nodes("前置放大器原理", 14, depth="analysis", daily_minutes=60)
    adjusted = team.baseline.adjust_nodes(raw, baseline="expert", depth="analysis")
    assert len(adjusted) <= len(raw)


def test_weekly_scheduler_builds_week_labels() -> None:
    team = CustomLearningAgentTeam()
    nodes = team.planner.plan_nodes("数字信号处理", 14, depth="analysis", daily_minutes=60)
    weekly = team.scheduler.build(nodes, 14)
    assert weekly
    assert weekly[0].startswith("第 1 周：")


def test_content_explainer_and_quiz_author_share_domain_knowledge() -> None:
    coach = LearningStyleCoachAgent()
    knowledge = KnowledgePointAgent()
    explainer = ContentExplainerAgent(coach, knowledge)
    quiz = QuizAuthorAgent()

    explanation = explainer.build(
        goal="前置放大器原理",
        plan_title="前置放大器原理",
        title="电荷灵敏前置放大器原理",
        subtitle="反馈电容积分",
        objective="掌握电荷灵敏前放输出关系",
        resource="讲义",
        task="估算输出电压",
        check="能写出 ΔV≈Q/Cf",
    )
    questions = quiz.build(title="电荷灵敏前置放大器原理", objective="掌握电荷灵敏前放输出关系")

    assert "ΔV≈Q/Cf" in explanation
    assert "ΔV≈Q/Cf" in questions[0]["options"][0]["text"]
    assert len(questions) == 5
    assert {item["type"] for item in questions} >= {"choice", "judge", "short"}
    assert {item["skill"] for item in questions} >= {
        "core_concept",
        "application",
        "principle_judgement",
        "error_diagnosis",
        "transfer_explanation",
    }
    assert {item["source_agent"] for item in questions} >= {
        "concept_examiner",
        "scenario_examiner",
        "mechanism_examiner",
        "misconception_diagnostician",
        "transfer_coach",
    }


def test_quiz_author_merges_knowledge_base_questions_with_quality_gate() -> None:
    quiz = QuizAuthorAgent()
    questions = quiz.build(
        title="电荷灵敏前置放大器原理",
        objective="掌握电荷灵敏前放输出关系",
        external_questions=[
            {
                "type": "short",
                "stem": "资料中如何描述反馈电容 Cf 对输出幅度的影响？",
                "reference": "Cf 越大，同样输入电荷对应的输出阶跃越小。",
            }
        ],
    )

    assert len(questions) == 6
    assert questions[0]["skill"] == "core_concept"
    assert any(item["source_agent"] == "knowledge_base_question_agent" for item in questions)
    assert len({item["id"] for item in questions}) == len(questions)


def test_explore_bridge_uses_node_title_without_redundant_goal_suffix() -> None:
    from backend.services.custom_learning_agents import ExploreBridgeAgent

    bridge = ExploreBridgeAgent()
    assert bridge.topic_for(goal="前置放大器原理", node_title="电荷灵敏前置放大器原理") == "电荷灵敏前置放大器原理"
    assert bridge.topic_for(goal="前置放大器原理", node_title="") == "前置放大器原理"


def test_mastery_diagnostician_and_path_adjuster_work_together() -> None:
    from backend.db.models import LearningPlan, LearningPlanNode

    diagnostician = MasteryDiagnosticianAgent()
    adjuster = PathAdjusterAgent()
    questions = [
        {"id": "q1", "type": "choice", "question": "测试", "answer": "A"},
        {"id": "q2", "type": "short", "question": "说明"},
    ]
    score, diagnosis = diagnostician.grade(questions, {"q1": "B", "q2": ""})
    assert score < 70
    assert diagnosis["weak_points"]

    plan = LearningPlan(plan_id="p1", user_id="u1", title="测试", duration_days=3, daily_minutes=60)
    node = LearningPlanNode(
        node_id="n1",
        plan_id="p1",
        order_index=0,
        title="核心概念",
        status="learning",
        mastery=score,
    )
    plan.nodes = [node]
    adjustment = adjuster.build_low_score_adjustment(plan, node, score=score, diagnosis=diagnosis)
    assert adjustment is not None
    assert adjustment.after_snapshot["agent"] == "path_adjuster"
