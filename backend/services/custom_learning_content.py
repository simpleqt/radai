"""自定义学习内容域:资源策展、练习设计、知识点提取与内容讲解。(拆自 services/custom_learning_agents.py)"""

from __future__ import annotations

from typing import Any
from backend.services.custom_learning_planning import (
    LearningNodeBlueprint,
    LearningStyleCoachAgent,
)


class ResourceCuratorAgent:
    """Adds resource intent and output artifacts for every node."""

    name = "resource_curator"

    def projects(self, nodes: list[LearningNodeBlueprint]) -> list[dict[str, str]]:
        projects: list[dict[str, str]] = []
        for node in nodes[:4]:
            projects.append(
                {
                    "title": node.task or f"完成“{node.title}”节点任务",
                    "description": f"围绕“{node.title}”形成可检查的学习产出。",
                    "output": self._output_for(node),
                }
            )
        return projects

    def enhance(self, node: LearningNodeBlueprint, *, kb_ids: list[str]) -> LearningNodeBlueprint:
        source = "关联知识库资料" if kb_ids else "课程讲义、教材章节和案例资料"
        resource = node.resource or f"围绕“{node.title}”检索{source}"
        notes = {**node.agent_notes, "resource_agent": self.name, "resource_scope": source}
        return LearningNodeBlueprint(**{**node.__dict__, "resource": resource, "agent_notes": notes})

    def _output_for(self, node: LearningNodeBlueprint) -> str:
        text = f"{node.title} {node.task}"
        if "图" in text or "结构" in text or "链路" in text:
            return "结构图或链路图"
        if "计算" in text or "估算" in text:
            return "计算记录表"
        if "对比" in text:
            return "对比卡片"
        return "一页学习小结"


class PracticeDesignerAgent:
    """Shapes tasks and checks so every node can be evaluated."""

    name = "practice_designer"

    def enhance(self, node: LearningNodeBlueprint, *, style_hints: list[str]) -> LearningNodeBlueprint:
        base_task = node.task or f"整理“{node.title}”学习笔记"
        task_parts = [base_task, "完成节点练习"]
        task_parts.extend(style_hints[:1])
        check = node.check or f"能解释“{node.title}”的关键概念、适用条件和常见误区"
        notes = {**node.agent_notes, "practice_agent": self.name, "assessment": "quiz_and_task_check"}
        return LearningNodeBlueprint(
            **{
                **node.__dict__,
                "task": "；".join(dict.fromkeys(task_parts)),
                "check": check,
                "agent_notes": notes,
            }
        )


class KnowledgePointAgent:
    """Produces domain knowledge bullets for node explanations."""

    name = "knowledge_curator"

    def points(self, *, goal: str, title: str, subtitle: str = "") -> list[str]:
        joined = f"{goal} {title} {subtitle}"
        if "电荷灵敏" in joined:
            return [
                "探测器输出的电荷 Q 被反馈电容 Cf 积分，理想输出阶跃幅度近似为 ΔV≈Q/Cf。",
                "输入端近似虚地，可减小探测器电容变化对电荷收集读出的影响。",
                "反馈回路必须提供电荷释放路径，否则输出会逐次堆积直到饱和。",
            ]
        if "反馈" in joined or "复位" in joined:
            return [
                "Rf-Cf 连续反馈会形成指数衰减尾部，时间常数 τ=Rf·Cf。",
                "反馈时间常数过大会增加堆积风险，过小会降低低频增益并影响幅度保持。",
                "脉冲复位可降低并联噪声，但会引入死时间和复位瞬态。",
            ]
        if "噪声" in joined or "等效噪声" in joined or "ENC" in joined:
            return [
                "串联噪声通常随输入电容增大而变差，和放大器输入级电压噪声密切相关。",
                "并联噪声主要来自探测器漏电流和反馈电阻热噪声，成形时间过长时更明显。",
                "等效噪声电荷 ENC 是把输出噪声折算到输入端的电荷量，可直接关联能量分辨率。",
            ]
        if "接口" in joined or "ADC" in joined or "成形" in joined:
            return [
                "前放输出通常还不能直接用于能谱分析，需要成形放大器改善信噪比和脉冲宽度。",
                "极零相消用于抵消前放长尾，降低堆积和基线漂移。",
                "后级增益和 ADC 量程需要按最大输入电荷、前放输出幅度和动态范围共同确定。",
            ]
        if "参数" in joined or "诊断" in joined:
            return [
                "输出过小优先检查反馈电容是否过大、输入电荷估计是否偏小或增益链路是否不足。",
                "输出饱和通常与反馈电容过小、输入电荷过大、复位异常或动态范围不足有关。",
                "振荡和噪声过大需要检查输入电容、接地屏蔽、反馈布局和放大器稳定性。",
            ]
        if "前放" in joined or "前置放大" in joined:
            return [
                "前置放大器位于探测器后端，核心任务是尽早把微弱探测器信号转换为低阻抗、可传输的电压信号。",
                "电荷灵敏前放适合多数辐射探测器的能量测量，电压/电流灵敏结构适合不同阻抗和速度需求。",
                "前放设计必须同时考虑信号幅度、输入电容、噪声、复位、计数率和后级接口。",
            ]
        return [
            f"本节点围绕“{title}”建立概念、适用条件和可验证输出。",
            f"学习时要把“{title}”放回“{goal}”这个目标中，而不是只记孤立定义。",
            "完成节点后需要能说明关键概念、操作步骤、常见误区和一个迁移案例。",
        ]


class ContentExplainerAgent:
    """Assembles structured node explanations from plan and style context."""

    name = "content_explainer"

    def __init__(self, style_coach: LearningStyleCoachAgent, knowledge: KnowledgePointAgent) -> None:
        self._style_coach = style_coach
        self._knowledge = knowledge

    def build(
        self,
        *,
        goal: str,
        plan_title: str,
        title: str,
        subtitle: str = "",
        objective: str = "",
        resource: str = "",
        tasks: list[str] | None = None,
        task: str = "",
        checks: list[str] | None = None,
        check: str = "",
        style_snapshot: dict[str, Any] | None = None,
    ) -> str:
        checks_text = "；".join(checks or ([check] if check else [])) or f"能解释“{title}”"
        tasks_text = "；".join(tasks or ([task] if task else [])) or "完成节点练习并整理要点"
        resource_text = resource or "结合知识库资料和课堂笔记"
        style_hint = self._style_coach.explanation_hint(style_snapshot)
        knowledge_points = self._knowledge.points(goal=goal or plan_title, title=title, subtitle=subtitle)
        point_lines = [f"- {item}" for item in knowledge_points]
        return "\n".join(
            [
                f"## {title}",
                "",
                f"**学习目标**：{objective or subtitle or f'掌握“{title}”的核心概念、适用条件和常见误区'}",
                "",
                f"**推荐资源**：{resource_text}",
                "",
                "**必须掌握**：",
                *point_lines,
                "",
                f"**学习建议**：{style_hint}",
                "",
                "**学习步骤**：",
                f"1. 先明确“{title}”在“{goal or plan_title}”中的位置和要解决的问题。",
                "2. 对照上面的必须掌握点整理关键概念、公式或流程，不确定处优先用探索资源展开。",
                f"3. 完成任务：{tasks_text}。",
                f"4. 用验收标准自检：{checks_text}。",
                "",
                "**常见误区**：",
                "- 只背术语，不说明输入、输出、适用条件和限制。",
                "- 只看单个模块，不把它放回完整信号链或问题链路中。",
                "- 做完题后不复盘错误原因，导致后续节点重复卡住。",
                "",
                "**复盘问题**：",
                f"- 我能否不用资料解释“{title}”的核心逻辑？",
                "- 哪个条件、步骤或概念最容易混淆？",
                "- 如果换一个案例，我能否迁移这个方法？",
            ]
        )
