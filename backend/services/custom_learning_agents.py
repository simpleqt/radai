"""Agent-style orchestration for custom learning plans.

The first implementation is deterministic so tests and product behavior stay
stable. The class boundaries are intentionally agent-shaped: each role owns one
decision surface and can later be backed by LLM/tool calls without changing the
router response schema.
"""

from __future__ import annotations

from typing import Any

# 领域模块 re-export:外部 import 与测试兼容(按拆分前的定义顺序)
from backend.services.custom_learning_planning import (  # noqa: F401
    BaselineAssessorAgent,
    CurriculumPlannerAgent,
    LearningNodeBlueprint,
    LearningPlanBlueprint,
    LearningStyleCoachAgent,
    WeeklySchedulerAgent,
    _decay_nodes,
    _detector_nodes,
    _digital_signal_nodes,
    _generic_nodes,
    _preamp_nodes,
)
from backend.services.custom_learning_content import (  # noqa: F401
    ContentExplainerAgent,
    KnowledgePointAgent,
    PracticeDesignerAgent,
    ResourceCuratorAgent,
)
from backend.services.custom_learning_quiz import (  # noqa: F401
    MasteryDiagnosticianAgent,
    QuizAuthorAgent,
)
from backend.services.custom_learning_path import (  # noqa: F401
    ExploreBridgeAgent,
    PathAdjusterAgent,
    PathMonitorAgent,
)


class CustomLearningAgentTeam:
    """Coordinates the custom-learning specialist agents."""

    def __init__(self) -> None:
        self.style_coach = LearningStyleCoachAgent()
        self.baseline = BaselineAssessorAgent()
        self.planner = CurriculumPlannerAgent()
        self.curator = ResourceCuratorAgent()
        self.practice = PracticeDesignerAgent()
        self.scheduler = WeeklySchedulerAgent()
        self.monitor = PathMonitorAgent()
        self.knowledge = KnowledgePointAgent()
        self.explainer = ContentExplainerAgent(self.style_coach, self.knowledge)
        self.quiz_author = QuizAuthorAgent()
        self.diagnostician = MasteryDiagnosticianAgent()
        self.path_adjuster = PathAdjusterAgent()
        self.explore_bridge = ExploreBridgeAgent()

    def generate_plan_blueprint(
        self,
        *,
        goal: str,
        baseline: str,
        depth: str,
        duration_days: int,
        daily_minutes: int,
        kb_ids: list[str],
        style_snapshot: dict[str, Any] | None,
        node_overrides: list[LearningNodeBlueprint] | None = None,
    ) -> LearningPlanBlueprint:
        """生成计划蓝图。

        ``node_overrides`` 允许上游（如 LLM 规划）注入候选节点；基线评估、
        资源/练习增强、周程与 trace 编排保持同一条路径，保证响应形状一致。
        """
        style_hints = self.style_coach.task_hints(style_snapshot)
        if node_overrides:
            raw_nodes = list(node_overrides)
            planner_source = "llm_blueprint"
            planner_reason = "采用 LLM 蓝图节点，其余智能体保持质量兜底"
        else:
            raw_nodes = self.planner.plan_nodes(goal, duration_days, depth=depth, daily_minutes=daily_minutes)
            planner_source = "keyword_rules"
            planner_reason = "把自然语言目标拆成可验证的学习节点"
        before_count = len(raw_nodes)
        paced_nodes = self.baseline.adjust_nodes(raw_nodes, baseline=baseline, depth=depth)
        enhanced_nodes: list[LearningNodeBlueprint] = []
        for node in paced_nodes:
            curated = self.curator.enhance(node, kb_ids=kb_ids)
            practiced = self.practice.enhance(curated, style_hints=style_hints)
            enhanced_nodes.append(practiced)

        weekly_plan = self.scheduler.build(enhanced_nodes, duration_days)
        agent_trace = [
            {
                "agent": self.planner.name,
                "decision": "goal_to_path",
                "reason": planner_reason,
                "source": planner_source,
                "baseline": baseline,
                "depth": depth,
                "confidence": 0.82,
                "impact": f"生成 {before_count} 个候选节点",
            },
            self.baseline.trace(baseline=baseline, before=before_count, after=len(enhanced_nodes)),
            {
                "agent": self.style_coach.name,
                "decision": "style_to_tasks",
                "reason": "把学习风格画像转换为任务、解释和复盘提示",
                "enabled": bool(style_snapshot and style_snapshot.get("enabled")),
                "hint_count": len(style_hints),
                "confidence": 0.76 if style_hints else 0.5,
                "impact": "；".join(style_hints[:3]) if style_hints else "使用默认学习策略",
            },
            {
                "agent": self.curator.name,
                "decision": "resource_scope",
                "reason": "根据绑定知识库和节点目标设置资源范围",
                "kb_count": len(kb_ids),
                "confidence": 0.74 if kb_ids else 0.58,
                "impact": "优先使用绑定知识库资料" if kb_ids else "使用通用课程资源提示",
            },
            {
                "agent": self.practice.name,
                "decision": "node_tasks_and_checks",
                "reason": "为每个节点配置任务和验收标准，支持后续诊断",
                "confidence": 0.81,
                "impact": "每个节点包含任务、检查点和练习准备",
            },
            self.scheduler.trace(duration_days=duration_days, week_count=len(weekly_plan)),
            self.monitor.trace(enhanced_nodes, duration_days=duration_days),
            self.explore_bridge.trace(),
        ]

        return LearningPlanBlueprint(
            title=goal[:80],
            summary=(
                f"{duration_days} 天完成“{goal}”的闭环学习，"
                "由规划、基线评估、资源、练习、周程与路径监控智能体协同生成。"
            ),
            nodes=enhanced_nodes,
            agent_trace=agent_trace,
            objectives=self.planner.objectives(enhanced_nodes),
            projects=self.curator.projects(enhanced_nodes),
            style_task_hints=style_hints,
            weekly_plan=weekly_plan,
        )

    def build_node_explanation(self, plan: Any, node: Any) -> str:
        return self.explainer.build(
            goal=str(plan.goal or ""),
            plan_title=str(plan.title or ""),
            title=str(node.title or ""),
            subtitle=str(node.subtitle or ""),
            objective=str(node.objective or ""),
            resource=str(node.resource or ""),
            tasks=list(node.tasks or []),
            task=str(node.task or ""),
            checks=list(node.checks or []),
            check=str(node.check or ""),
            style_snapshot=plan.style_snapshot,
        )

    def build_quiz_questions(
        self,
        node: Any,
        *,
        external_questions: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        return self.quiz_author.build(
            title=str(node.title or ""),
            subtitle=str(node.subtitle or ""),
            objective=str(node.objective or ""),
            check=str(node.check or ""),
            external_questions=external_questions,
        )

    def grade_quiz(self, questions: list[dict[str, Any]], answers: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        return self.diagnostician.grade(questions, answers)

    def build_low_score_adjustment(
        self,
        plan: Any,
        node: Any,
        *,
        score: int,
        diagnosis: dict[str, Any] | None = None,
    ) -> Any | None:
        return self.path_adjuster.build_low_score_adjustment(plan, node, score=score, diagnosis=diagnosis)
