"""Agent-style orchestration for custom learning plans.

The first implementation is deterministic so tests and product behavior stay
stable. The class boundaries are intentionally agent-shaped: each role owns one
decision surface and can later be backed by LLM/tool calls without changing the
router response schema.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4


@dataclass(frozen=True)
class LearningNodeBlueprint:
    """A planned learning node before it is persisted."""

    title: str
    subtitle: str
    objective: str
    resource: str
    task: str
    check: str
    node_type: str = "concept"
    difficulty: str = "analysis"
    estimated_minutes: int = 60
    agent_notes: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LearningPlanBlueprint:
    """A complete plan blueprint produced by the custom-learning agent team."""

    title: str
    summary: str
    nodes: list[LearningNodeBlueprint]
    agent_trace: list[dict[str, Any]]
    objectives: list[dict[str, Any]]
    projects: list[dict[str, Any]]
    style_task_hints: list[str]
    weekly_plan: list[str] = field(default_factory=list)


class LearningStyleCoachAgent:
    """Turns a style snapshot into concrete learning-task hints."""

    name = "learning_style_coach"

    def dimensions(self, style_snapshot: dict[str, Any] | None) -> dict[str, str]:
        if not style_snapshot or not style_snapshot.get("enabled"):
            return {}
        result = style_snapshot.get("result") or {}
        dimensions = result.get("dimensions") or []
        prefs: dict[str, str] = {}
        for item in dimensions:
            if not isinstance(item, dict):
                continue
            dimension_id = str(item.get("dimension_id") or "")
            label = str(item.get("preferred_label") or "")
            if dimension_id and label:
                prefs[dimension_id] = label
        return prefs

    def task_hints(self, style_snapshot: dict[str, Any] | None) -> list[str]:
        prefs = self.dimensions(style_snapshot)
        hints: list[str] = []
        if prefs.get("processing") == "活跃型":
            hints.append("完成一个即时练习，并把思路讲给同伴或自己听")
        elif prefs.get("processing") == "沉思型":
            hints.append("先写出推理过程，再对照验收标准自检")
        if prefs.get("perception") == "感知型":
            hints.append("补充一个具体案例、参数或实验场景")
        elif prefs.get("perception") == "直觉型":
            hints.append("写出本节点与上一节点的原理联系和迁移问题")
        if prefs.get("input") == "视觉型":
            hints.append("画一张结构图、流程图或对比表")
        elif prefs.get("input") == "言语型":
            hints.append("用自己的话写一段定义、类比和关键句")
        if prefs.get("understanding") == "序列型":
            hints.append("按 1-2-3 步列出学习过程")
        elif prefs.get("understanding") == "综合型":
            hints.append("先写整体框架，再把当前节点放入框架中")
        return hints

    def explanation_hint(self, style_snapshot: dict[str, Any] | None) -> str:
        hints = self.task_hints(style_snapshot)
        if not hints:
            return "按目标、原理、例子和检查点组织学习。"
        return "；".join(hints[:4])


class CurriculumPlannerAgent:
    """Breaks a user's goal into a teachable sequence."""

    name = "curriculum_planner"

    def plan_nodes(self, goal: str, duration_days: int, *, depth: str = "analysis", daily_minutes: int = 90) -> list[LearningNodeBlueprint]:
        raw_nodes = self._keyword_nodes(goal, duration_days)
        max_nodes = max(3, min(10, duration_days))
        nodes: list[LearningNodeBlueprint] = []
        for index, item in enumerate(raw_nodes[:max_nodes]):
            nodes.append(
                LearningNodeBlueprint(
                    title=item["title"],
                    subtitle=item.get("subtitle", ""),
                    objective=item.get("objective") or item.get("subtitle", ""),
                    resource=item.get("resource", ""),
                    task=item.get("task", ""),
                    check=item.get("check", ""),
                    difficulty=depth or "analysis",
                    estimated_minutes=max(30, int(daily_minutes or 90)),
                    agent_notes={
                        "agent": self.name,
                        "order_reason": "按前置概念、核心方法、案例练习和综合输出排序",
                        "target_day": min(index + 1, max(1, duration_days)),
                    },
                )
            )
        return nodes

    def objectives(self, nodes: list[LearningNodeBlueprint]) -> list[dict[str, Any]]:
        return [
            {
                "title": node.title,
                "description": node.objective or node.subtitle,
                "rationale": "该节点是完成目标路径中的必要能力单元。",
                "checkpoints": [node.check] if node.check else [],
            }
            for node in nodes[:5]
        ]

    def _keyword_nodes(self, goal: str, duration_days: int) -> list[dict[str, str]]:
        text = goal.strip() or "自定义学习"
        if "前置放大" in text or "前放" in text or "放大器原理" in text:
            return _preamp_nodes()
        if "半导体" in text or "探测器" in text:
            return _detector_nodes(text)
        if "数字" in text or "信号" in text:
            return _digital_signal_nodes()
        if "衰变" in text or "活度" in text or "半衰期" in text:
            return _decay_nodes(text)
        return _generic_nodes(text)


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


class PathMonitorAgent:
    """Defines path-level monitoring and adjustment policy."""

    name = "path_monitor"

    def trace(self, nodes: list[LearningNodeBlueprint], *, duration_days: int) -> dict[str, Any]:
        return {
            "agent": self.name,
            "decision": "monitor_mastery_and_adjust_path",
            "reason": "练习得分低于阈值时需要即时补强，避免带着薄弱点进入后续节点",
            "policy": "score_below_70_insert_review_node",
            "node_count": len(nodes),
            "duration_days": duration_days,
            "confidence": 0.84,
            "impact": "低分后自动插入复习节点，并把当前节点标记为需复习",
        }


class BaselineAssessorAgent:
    """Tunes pacing, node count and difficulty from the learner baseline."""

    name = "baseline_assessor"

    _PROFILES: dict[str, dict[str, Any]] = {
        "beginner": {"minute_multiplier": 1.2, "node_cap_offset": 0, "intro_emphasis": True},
        "unknown": {"minute_multiplier": 1.0, "node_cap_offset": 0, "intro_emphasis": False},
        "intermediate": {"minute_multiplier": 1.0, "node_cap_offset": 0, "intro_emphasis": False},
        "advanced": {"minute_multiplier": 0.9, "node_cap_offset": -1, "intro_emphasis": False},
        "expert": {"minute_multiplier": 0.8, "node_cap_offset": -2, "intro_emphasis": False},
    }

    def profile(self, baseline: str) -> dict[str, Any]:
        return dict(self._PROFILES.get(baseline, self._PROFILES["unknown"]))

    def adjust_nodes(
        self,
        nodes: list[LearningNodeBlueprint],
        *,
        baseline: str,
        depth: str,
    ) -> list[LearningNodeBlueprint]:
        prof = self.profile(baseline)
        adjusted: list[LearningNodeBlueprint] = []
        for index, node in enumerate(nodes):
            minutes = max(20, int(node.estimated_minutes * prof["minute_multiplier"]))
            notes = {**node.agent_notes, "baseline": baseline}
            if prof["intro_emphasis"] and index == 0:
                notes["baseline_note"] = "入门级加强前置概念与术语铺垫"
            adjusted.append(
                LearningNodeBlueprint(
                    **{
                        **node.__dict__,
                        "difficulty": depth or node.difficulty,
                        "estimated_minutes": minutes,
                        "agent_notes": notes,
                    }
                )
            )
        cap_offset = int(prof["node_cap_offset"])
        if cap_offset < 0 and len(adjusted) > 4:
            adjusted = adjusted[: max(3, len(adjusted) + cap_offset)]
        return adjusted

    def trace(self, *, baseline: str, before: int, after: int) -> dict[str, Any]:
        return {
            "agent": self.name,
            "decision": "baseline_pacing",
            "reason": "根据学习者基础压缩或保留节点密度，控制每日负荷",
            "baseline": baseline,
            "nodes_before": before,
            "nodes_after": after,
            "confidence": 0.78,
            "impact": f"节点数量从 {before} 调整为 {after}",
        }


class WeeklySchedulerAgent:
    """Distributes learning nodes across calendar weeks."""

    name = "weekly_scheduler"

    def build(self, nodes: list[LearningNodeBlueprint], duration_days: int) -> list[str]:
        if not nodes:
            return []
        weeks = max(1, (duration_days + 6) // 7)
        nodes_per_week = max(1, (len(nodes) + weeks - 1) // weeks)
        weekly: list[str] = []
        for week in range(weeks):
            start = week * nodes_per_week
            chunk = nodes[start : start + nodes_per_week]
            if not chunk:
                break
            titles = "、".join(item.title for item in chunk[:4])
            weekly.append(f"第 {week + 1} 周：{titles}")
        return weekly

    def trace(self, *, duration_days: int, week_count: int) -> dict[str, Any]:
        return {
            "agent": self.name,
            "decision": "weekly_distribution",
            "reason": "把节点按学习周期分组，便于阶段复盘",
            "duration_days": duration_days,
            "weeks": week_count,
            "confidence": 0.86,
            "impact": f"形成 {week_count} 个周程段落",
        }


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


class QuizAuthorAgent:
    """Authors node-level practice questions with multi-role coverage."""

    name = "quiz_author"

    def build(
        self,
        *,
        title: str,
        subtitle: str = "",
        objective: str = "",
        check: str = "",
        external_questions: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        profile = self._profile(title=title, subtitle=subtitle, objective=objective, check=check)
        candidates = [
            self._concept_question(profile),
            self._application_question(profile),
        ]
        candidates.extend(self._adapt_external_questions(external_questions or []))
        candidates.extend(
            [
                self._mechanism_question(profile),
                self._misconception_question(profile),
                self._transfer_question(profile),
            ]
        )
        max_count = 6 if external_questions else 5
        return self._quality_gate(candidates, max_count=max_count)

    def _profile(self, *, title: str, subtitle: str, objective: str, check: str) -> dict[str, Any]:
        joined = f"{title} {subtitle} {objective}"
        check_text = check or f"能说明“{title}”的关键概念、适用条件和常见误区。"
        profile = {
            "title": title,
            "check": check_text,
            "concept_question": f"学习“{title}”时，最需要先确认的是哪一项？",
            "concept_options": [
                {"key": "A", "text": "核心概念、适用条件和检查点"},
                {"key": "B", "text": "跳过基础，直接记忆结论"},
                {"key": "C", "text": "只看标题，不做练习"},
            ],
            "concept_answer": "A",
            "concept_analysis": "自定义学习节点必须围绕目标、条件和检查点完成闭环。",
            "mechanism_question": f"判断：只要记住“{title}”的定义，就等于掌握了本节点。",
            "mechanism_answer": "错误",
            "mechanism_analysis": "还需要能说明适用条件、关键步骤、判断依据和迁移案例。",
            "application_question": f"遇到一个和“{title}”相关的新案例时，第一步更应该做什么？",
            "application_options": [
                {"key": "A", "text": "识别输入、输出、约束条件和验收标准"},
                {"key": "B", "text": "直接套用结论，不检查条件"},
                {"key": "C", "text": "只复制资料中的一句定义"},
            ],
            "application_answer": "A",
            "application_analysis": "迁移应用前必须先确认问题边界和可验证标准。",
            "misconception_question": f"关于“{title}”，哪一种学习方式最容易造成后续节点卡住？",
            "misconception_options": [
                {"key": "A", "text": "只背术语，不解释条件、步骤和风险"},
                {"key": "B", "text": "用例子检验概念"},
                {"key": "C", "text": "对照验收标准复盘错因"},
            ],
            "misconception_answer": "A",
            "misconception_analysis": "只背术语无法支撑练习、诊断和后续迁移。",
            "transfer_question": f"请用一个具体案例说明“{title}”的关键判断依据。",
            "transfer_reference": check_text,
        }
        if "电荷灵敏" in joined:
            profile.update(
                {
                    "concept_question": "电荷灵敏前置放大器中，输入电荷 Q 与反馈电容 Cf 决定的理想输出阶跃近似为哪一项？",
                    "concept_options": [
                        {"key": "A", "text": "ΔV≈Q/Cf"},
                        {"key": "B", "text": "ΔV≈Q·Cf"},
                        {"key": "C", "text": "ΔV≈Rf/Cf"},
                    ],
                    "concept_analysis": "电荷被反馈电容积分，输出电压阶跃近似等于输入电荷除以反馈电容。",
                    "mechanism_question": "判断：电荷灵敏前放输入端近似虚地，有助于减小输入节点电压摆动并稳定电荷收集。",
                    "mechanism_answer": "正确",
                    "mechanism_analysis": "虚地输入能降低探测器电容变化对读出增益的影响。",
                    "application_question": "某探测器输出电荷不变，若反馈电容 Cf 增大，理想输出阶跃会怎样变化？",
                    "application_options": [
                        {"key": "A", "text": "变小"},
                        {"key": "B", "text": "变大"},
                        {"key": "C", "text": "与 Cf 无关"},
                    ],
                    "application_analysis": "ΔV≈Q/Cf，Q 不变时 Cf 增大，输出阶跃变小。",
                    "misconception_question": "调试电荷灵敏前放输出过小时，哪种判断最可能是误区？",
                    "misconception_options": [
                        {"key": "A", "text": "只怀疑 ADC，不检查反馈电容和输入电荷估计"},
                        {"key": "B", "text": "核对 Q/Cf 估算"},
                        {"key": "C", "text": "检查输入端和反馈网络"},
                    ],
                    "misconception_analysis": "输出幅度首先应回到电荷积分关系、反馈参数和输入信号估计上排查。",
                    "transfer_question": "给定输入电荷和反馈电容时，你会如何估算电荷灵敏前放输出幅度，并判断结果是否合理？",
                    "transfer_reference": "先用 ΔV≈Q/Cf 估算输出阶跃，再结合输入动态范围、反馈复位、后级增益和噪声裕量判断。",
                }
            )
        elif "反馈" in joined or "复位" in joined:
            profile.update(
                {
                    "concept_question": "Rf-Cf 连续反馈中，输出尾脉冲的主要时间常数由什么决定？",
                    "concept_options": [
                        {"key": "A", "text": "τ=Rf·Cf"},
                        {"key": "B", "text": "τ=Rf/Cf"},
                        {"key": "C", "text": "τ=Cf/Rf"},
                    ],
                    "concept_analysis": "反馈电阻释放反馈电容电荷，形成由 Rf 和 Cf 乘积决定的指数衰减。",
                    "mechanism_question": "判断：反馈时间常数过长会增加高计数率下的脉冲堆积风险。",
                    "mechanism_answer": "正确",
                    "mechanism_analysis": "尾脉冲变长会使基线恢复变慢，脉冲更容易重叠。",
                    "application_question": "如果输出长尾导致基线恢复慢，优先应检查哪组因素？",
                    "application_options": [
                        {"key": "A", "text": "Rf·Cf、成形时间和极零相消"},
                        {"key": "B", "text": "显示器亮度"},
                        {"key": "C", "text": "文件命名方式"},
                    ],
                    "application_analysis": "长尾和基线问题通常来自反馈时间常数、成形匹配和计数率条件。",
                    "transfer_reference": "应说明反馈释放路径、τ=Rf·Cf、基线恢复、堆积风险和复位方式取舍。",
                }
            )
        elif "噪声" in joined or "等效噪声" in joined or "ENC" in joined:
            profile.update(
                {
                    "concept_question": "输入电容增大时，前置放大器哪类噪声影响通常更容易变差？",
                    "concept_options": [
                        {"key": "A", "text": "串联噪声"},
                        {"key": "B", "text": "量化噪声"},
                        {"key": "C", "text": "宇宙线本底"},
                    ],
                    "concept_analysis": "串联噪声与输入级电压噪声和输入电容密切相关，输入电容越大通常 ENC 越差。",
                    "mechanism_question": "判断：ENC 是把输出噪声折算到输入端的等效电荷量。",
                    "mechanism_answer": "正确",
                    "mechanism_analysis": "ENC 可直接用于比较前放和探测器组合对能量分辨率的影响。",
                    "application_question": "漏电流变大且成形时间较长时，哪类噪声更需要警惕？",
                    "application_options": [
                        {"key": "A", "text": "并联噪声"},
                        {"key": "B", "text": "采样混叠"},
                        {"key": "C", "text": "文件压缩噪声"},
                    ],
                    "application_analysis": "并联噪声与漏电流和反馈电阻热噪声密切相关，成形时间较长时更明显。",
                    "transfer_reference": "应关联输入电容、漏电流、成形时间、串联/并联噪声和 ENC 对分辨率的影响。",
                }
            )
        elif "接口" in joined or "ADC" in joined or "成形" in joined:
            profile.update(
                {
                    "concept_question": "前置放大器输出后接成形放大器的主要目的是什么？",
                    "concept_options": [
                        {"key": "A", "text": "改善信噪比、控制脉冲宽度并便于后级采集"},
                        {"key": "B", "text": "完全消除探测器物理涨落"},
                        {"key": "C", "text": "让反馈电容不再需要"},
                    ],
                    "concept_analysis": "成形用于滤波、改善信噪比、限制脉冲宽度，并匹配甄别或 ADC。",
                    "mechanism_question": "判断：ADC 量程选择需要同时考虑前放输出幅度、后级增益和动态范围。",
                    "mechanism_answer": "正确",
                    "mechanism_analysis": "量程过小会饱和，过大又会浪费有效分辨率。",
                    "application_question": "若最大输入电荷增大，后级接口设计最需要重新核对哪一项？",
                    "application_options": [
                        {"key": "A", "text": "前放输出幅度、成形增益和 ADC 量程"},
                        {"key": "B", "text": "章节标题长度"},
                        {"key": "C", "text": "是否省略极零相消概念"},
                    ],
                    "application_analysis": "输入电荷变化会改变输出幅度和动态范围需求。",
                    "transfer_reference": "应说明最大输入电荷、反馈电容、前放输出、后级增益、ADC 量程和噪声裕量。",
                }
            )
        elif "参数" in joined or "诊断" in joined:
            profile.update(
                {
                    "concept_question": "如果前放输出经常饱和，优先排查哪类原因更合理？",
                    "concept_options": [
                        {"key": "A", "text": "反馈电容过小、输入电荷过大或复位异常"},
                        {"key": "B", "text": "教材页码太少"},
                        {"key": "C", "text": "ADC 位数一定过高"},
                    ],
                    "concept_analysis": "输出饱和通常来自增益过高、动态范围不足或反馈/复位链路异常。",
                    "mechanism_question": "判断：波形振荡和噪声过大只需要调低 ADC 采样率即可解决。",
                    "mechanism_answer": "错误",
                    "mechanism_analysis": "还需要检查输入电容、接地屏蔽、反馈布局和放大器稳定性。",
                    "application_question": "看到输出长尾和堆积时，你会优先检查哪些参数？",
                    "application_options": [
                        {"key": "A", "text": "反馈时间常数、成形时间、计数率和极零相消"},
                        {"key": "B", "text": "登录页配色"},
                        {"key": "C", "text": "报告页边距"},
                    ],
                    "application_analysis": "长尾和堆积与反馈恢复、成形匹配和事件间隔直接相关。",
                    "transfer_reference": "应能从输出过小、饱和、长尾、振荡或噪声过大反推可能原因。",
                }
            )
        return profile

    def _concept_question(self, profile: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "choice",
            "question": profile["concept_question"],
            "options": profile["concept_options"],
            "answer": profile.get("concept_answer", "A"),
            "analysis": profile["concept_analysis"],
            "source_agent": "concept_examiner",
            "skill": "core_concept",
            "difficulty": "基础",
        }

    def _mechanism_question(self, profile: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "judge",
            "question": profile["mechanism_question"],
            "options": [{"key": "正确", "text": "正确"}, {"key": "错误", "text": "错误"}],
            "answer": profile["mechanism_answer"],
            "analysis": profile["mechanism_analysis"],
            "source_agent": "mechanism_examiner",
            "skill": "principle_judgement",
            "difficulty": "理解",
        }

    def _application_question(self, profile: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "choice",
            "question": profile["application_question"],
            "options": profile["application_options"],
            "answer": profile.get("application_answer", "A"),
            "analysis": profile["application_analysis"],
            "source_agent": "scenario_examiner",
            "skill": "application",
            "difficulty": "应用",
        }

    def _misconception_question(self, profile: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "choice",
            "question": profile["misconception_question"],
            "options": profile["misconception_options"],
            "answer": profile.get("misconception_answer", "A"),
            "analysis": profile["misconception_analysis"],
            "source_agent": "misconception_diagnostician",
            "skill": "error_diagnosis",
            "difficulty": "诊断",
        }

    def _transfer_question(self, profile: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "short",
            "question": profile["transfer_question"],
            "reference": profile["transfer_reference"],
            "source_agent": "transfer_coach",
            "skill": "transfer_explanation",
            "difficulty": "迁移",
        }

    def _adapt_external_questions(self, questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        adapted: list[dict[str, Any]] = []
        for item in questions[:4]:
            if not isinstance(item, dict):
                continue
            stem = str(item.get("question") or item.get("stem") or "").strip()
            if not stem:
                continue
            qtype = str(item.get("type") or "short")
            question = {**item, "question": stem, "source_agent": "knowledge_base_question_agent"}
            question.setdefault("skill", "knowledge_recall")
            question.setdefault("difficulty", "资料")
            if qtype == "judge":
                question["options"] = [{"key": "正确", "text": "正确"}, {"key": "错误", "text": "错误"}]
                question.setdefault("answer", "正确")
            elif qtype == "choice" and not question.get("options"):
                continue
            elif qtype not in {"choice", "judge"}:
                question["type"] = "short"
                question.setdefault("reference", item.get("reference") or item.get("analysis") or "结合资料说明关键依据。")
            adapted.append(question)
        return adapted

    def _quality_gate(self, questions: list[dict[str, Any]], *, max_count: int) -> list[dict[str, Any]]:
        accepted: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in questions:
            stem = str(item.get("question") or "").strip()
            if not stem:
                continue
            key = stem.replace(" ", "")
            if key in seen:
                continue
            qtype = str(item.get("type") or "short")
            normalized = {**item, "question": stem, "type": qtype}
            if qtype in {"choice", "judge"}:
                options = normalized.get("options") or []
                if len(options) < 2 or not normalized.get("answer"):
                    continue
                normalized.setdefault("analysis", "对照节点详解和验收标准判断。")
            else:
                normalized["type"] = "short"
                normalized.setdefault("reference", "能结合概念、条件、步骤和案例说明判断依据。")
            accepted.append(normalized)
            seen.add(key)
            if len(accepted) >= max_count:
                break

        for index, item in enumerate(accepted, start=1):
            item["id"] = f"q{index}"
            item.setdefault("source_agent", self.name)
        return accepted


class MasteryDiagnosticianAgent:
    """Scores quiz attempts and surfaces weak points."""

    name = "mastery_diagnostician"

    _STOPWORDS = {
        "一个",
        "一种",
        "需要",
        "说明",
        "解释",
        "判断",
        "完成",
        "关键",
        "概念",
        "条件",
        "步骤",
        "案例",
        "依据",
        "节点",
        "学习",
        "资料",
        "标准",
        "能够",
        "可以",
        "以及",
        "进行",
        "相关",
    }

    def grade(self, questions: list[dict[str, Any]], answers: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        if not questions:
            return 0, {"weak_points": ["尚未生成练习"], "suggestions": ["请先生成节点练习"]}
        total = len(questions)
        score_sum = 0
        weak_points: list[str] = []

        for question in questions:
            qid = str(question.get("id", ""))
            answer = answers.get(qid)
            qtype = question.get("type")
            if qtype in {"choice", "judge"}:
                if str(answer or "").strip().upper() == str(question.get("answer", "")).upper():
                    score_sum += 100
                else:
                    weak_points.append(str(question.get("question") or question.get("stem") or qid))
            elif qtype == "multi":
                expected = set(str(question.get("answer", "")).upper())
                actual = set(str(answer or "").upper())
                if expected and expected == actual:
                    score_sum += 100
                elif expected and expected.intersection(actual):
                    score_sum += 60
                    weak_points.append(str(question.get("question") or question.get("stem") or qid))
                else:
                    weak_points.append(str(question.get("question") or question.get("stem") or qid))
            else:
                text = str(answer or "").strip()
                short_score, missing = self._score_short_answer(question, text)
                score_sum += short_score
                if missing:
                    weak_points.append(str(question.get("question") or question.get("stem") or qid))

        score = round(score_sum / total)
        suggestions = ["回到节点详解，对照验收标准补充说明"] if weak_points else ["可以进入下一个节点"]
        return score, {"weak_points": weak_points, "suggestions": suggestions}

    def _score_short_answer(self, question: dict[str, Any], answer: str) -> tuple[int, list[str]]:
        if not answer:
            return 0, ["未作答"]

        reference_text = " ".join(
            str(question.get(key) or "")
            for key in ("reference", "reference_answer", "analysis", "answer")
        )
        scoring_points = question.get("scoring_points") or []
        if isinstance(scoring_points, list):
            reference_text = f"{reference_text} {' '.join(str(item) for item in scoring_points)}"

        expected_terms = self._keywords(reference_text)
        answer_terms = set(self._keywords(answer))
        if not expected_terms:
            completeness_markers = ("概念", "条件", "误区", "依据", "例子", "步骤", "关系", "应用", "风险", "结论")
            marker_count = sum(1 for marker in completeness_markers if marker in answer)
            if len(answer) >= 12 and marker_count >= 2:
                return 90, []
            if len(answer) >= 80:
                return 85, []
            if len(answer) >= 24:
                return 70, []
            return 45, ["答案过短"]

        covered = [term for term in expected_terms if term in answer_terms or term in answer]
        coverage = len(covered) / max(len(expected_terms), 1)
        length_bonus = 10 if len(answer) >= 80 else 5 if len(answer) >= 32 else 0
        score = round(min(95, coverage * 90 + length_bonus))
        if score >= 70:
            return score, []
        missing = [term for term in expected_terms if term not in covered][:5]
        return max(score, 30 if answer else 0), missing or ["参考要点覆盖不足"]

    def _keywords(self, text: str) -> list[str]:
        raw = re.findall(r"[A-Za-z][A-Za-z0-9_+\-/.]{1,}|[\u4e00-\u9fff]{2,}", text or "")
        terms: list[str] = []
        seen: set[str] = set()
        for item in raw:
            value = item.strip()
            if not value or value in self._STOPWORDS:
                continue
            if len(value) > 14 and re.fullmatch(r"[\u4e00-\u9fff]+", value):
                chunks = [value[i : i + 4] for i in range(0, len(value), 4)]
            else:
                chunks = [value]
            for chunk in chunks:
                if len(chunk) < 2 or chunk in self._STOPWORDS or chunk in seen:
                    continue
                seen.add(chunk)
                terms.append(chunk)
                if len(terms) >= 12:
                    return terms
        return terms


class PathAdjusterAgent:
    """Inserts review nodes and remediation tasks when mastery is insufficient."""

    name = "path_adjuster"
    mastery_threshold = 70

    def build_low_score_adjustment(self, plan: Any, node: Any, *, score: int, diagnosis: dict[str, Any] | None = None) -> Any | None:
        if score >= self.mastery_threshold:
            return None

        from backend.db.models import LearningPlanAdjustment, LearningPlanNode

        now = datetime.now()
        nodes = sorted(list(plan.nodes or []), key=lambda item: item.order_index)
        before_snapshot = {
            "node": self._node_snapshot(node),
            "plan": {
                "progress": int(plan.progress or 0),
                "mastery": int(plan.mastery or 0),
                "total_nodes": len(nodes),
            },
            "score": score,
            "diagnosis": diagnosis or {},
        }
        weak_points = [
            str(item) for item in (diagnosis or {}).get("weak_points", []) if str(item).strip()
        ][:3]
        review_task = f"复盘“{node.title}”薄弱点并补充 3 条解释"
        if weak_points:
            review_task = f"复盘“{node.title}”：{weak_points[0]}"

        existing_review = next(
            (
                item
                for item in nodes
                if item.parent_node_id == node.node_id
                and item.node_type == "review"
                and item.status in {"open", "learning", "review"}
            ),
            None,
        )

        created_node_id: str | None = None
        accepted = False
        node.status = "review"
        node.study_signals = {
            **(node.study_signals or {}),
            "needs_review": True,
            "latest_low_score": score,
            "weak_points": weak_points,
            "adjusted_by": self.name,
        }
        node.tasks = list(dict.fromkeys([*(node.tasks or []), review_task]))
        node.checks = list(dict.fromkeys([*(node.checks or []), node.check or f"能重新解释“{node.title}”"]))
        node.updated_at = now

        strategy = "mark_current_node_review"
        if existing_review:
            created_node_id = existing_review.node_id
            existing_review.status = "open"
            existing_review.updated_at = now
            accepted = True
            strategy = "reopen_review_node"
        elif len(nodes) < 10:
            insert_index = node.order_index + 1
            for item in nodes:
                if item.order_index >= insert_index:
                    item.order_index += 1
                    item.updated_at = now
            created_node_id = str(uuid4())
            plan.nodes.append(
                LearningPlanNode(
                    node_id=created_node_id,
                    parent_node_id=node.node_id,
                    order_index=insert_index,
                    title=f"复习：{node.title}",
                    subtitle="根据练习结果自动补强",
                    objective=f"修正“{node.title}”的薄弱点",
                    day=node.day,
                    node_type="review",
                    difficulty=node.difficulty,
                    estimated_minutes=max(20, min(node.estimated_minutes or 60, 45)),
                    status="open",
                    mastery=0,
                    resource=node.resource,
                    task=review_task,
                    check=node.check,
                    resources=node.resources or [],
                    tasks=[review_task, "重新完成节点练习"],
                    checks=node.checks or [node.check or f"能重新解释“{node.title}”"],
                    prerequisites=[node.node_id],
                    study_signals={
                        "adaptive": True,
                        "trigger": "low_quiz_score",
                        "source_node_id": node.node_id,
                        "score": score,
                        "weak_points": weak_points,
                        "adjusted_by": self.name,
                    },
                )
            )
            accepted = True
            strategy = "insert_review_node"
        else:
            accepted = True
            strategy = "mark_current_node_review_only"

        after_snapshot = {
            "node": self._node_snapshot(node),
            "created_node_id": created_node_id,
            "strategy": strategy,
            "agent": self.name,
        }
        reason = (
            "节点练习未达到 70 分，路径调整智能体自动加入复习闭环。"
            if strategy in {"insert_review_node", "reopen_review_node"}
            else "节点练习未达到 70 分；计划节点较多，当前仅标记需复习，未插入新节点。"
        )
        return LearningPlanAdjustment(
            plan_id=plan.plan_id,
            trigger_type="low_score",
            before_snapshot=before_snapshot,
            after_snapshot=after_snapshot,
            reason=reason,
            accepted=accepted,
        )

    def _node_snapshot(self, node: Any) -> dict[str, Any]:
        return {
            "node_id": node.node_id,
            "title": node.title,
            "status": node.status,
            "mastery": int(node.mastery or 0),
            "tasks": node.tasks or [],
            "checks": node.checks or [],
        }


class ExploreBridgeAgent:
    """Bridges custom-learning nodes to explore-style knowledge expansion."""

    name = "explore_bridge"

    def topic_for(self, *, goal: str, node_title: str) -> str:
        title = (node_title or "").strip()
        if title:
            return title
        return (goal or "").strip() or "自定义学习"

    def trace(self) -> dict[str, Any]:
        return {
            "agent": self.name,
            "decision": "explore_on_demand",
            "reason": "资料检索较重，放到节点学习过程中按需触发",
            "policy": "node_explore_endpoint",
            "confidence": 0.8,
            "impact": "节点学习时可从绑定知识库召回材料",
        }


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
    ) -> LearningPlanBlueprint:
        style_hints = self.style_coach.task_hints(style_snapshot)
        raw_nodes = self.planner.plan_nodes(goal, duration_days, depth=depth, daily_minutes=daily_minutes)
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
                "reason": "把自然语言目标拆成可验证的学习节点",
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


def _preamp_nodes() -> list[dict[str, str]]:
    return [
        {"title": "前放在测量链路中的作用", "subtitle": "把探测器微弱电荷/电流信号转换为后级可处理的电压脉冲", "objective": "说明前置放大器为什么必须靠近探测器，以及它在探测器、成形放大器和采集系统之间承担的阻抗匹配与信号转换作用。", "resource": "探测器等效电路、前置放大器分类、电荷灵敏前放框图", "task": "画出“探测器-前置放大器-成形放大器-ADC”的信号链，并标注每一级输入输出量。", "check": "能区分电荷灵敏、电压灵敏和电流灵敏前置放大器的适用场景。"},
        {"title": "电荷灵敏前置放大器原理", "subtitle": "用反馈电容把探测器电荷 Q 转换为电压阶跃 ΔV≈Q/Cf", "objective": "掌握电荷灵敏前放的虚地输入、反馈电容积分和输出阶跃形成过程。", "resource": "电荷灵敏前放等效电路、反馈电容 Cf、运算放大器虚短虚断模型", "task": "根据给定输入电荷和反馈电容估算输出电压幅度，并解释为什么输入端近似为虚地。", "check": "能写出 ΔV≈Q/Cf，并说明反馈电容、输入电容和探测器电容对输出的影响。"},
        {"title": "反馈网络与复位方式", "subtitle": "理解反馈电阻、连续复位、脉冲复位和输出衰减时间常数", "objective": "说明反馈网络如何释放反馈电容上的电荷，并判断复位方式对计数率、基线和死时间的影响。", "resource": "Rf-Cf 反馈网络、指数衰减波形、连续复位与脉冲复位案例", "task": "比较 Rf-Cf 连续反馈和开关复位的优缺点，画出典型输出波形。", "check": "能解释 τ=Rf·Cf 对脉冲尾部、堆积和基线恢复的影响。"},
        {"title": "噪声来源与等效噪声电荷", "subtitle": "分析串联噪声、并联噪声、1/f 噪声及输入电容的影响", "objective": "建立前放噪声预算意识，理解为什么探测器电容、漏电流和成形时间会影响能量分辨率。", "resource": "等效噪声电荷 ENC、串联/并联/1/f 噪声、成形时间优化曲线", "task": "列出影响 ENC 的主要因素，并说明改变成形时间时串联噪声和并联噪声的变化趋势。", "check": "能判断输入电容变大、漏电流变大、成形时间过短或过长分别会带来什么噪声问题。"},
        {"title": "与成形放大器和 ADC 的接口", "subtitle": "把前放输出阶跃/指数衰减波形送入成形、甄别和数字采集链路", "objective": "理解前放输出不是最终测量结果，必须经过成形、滤波、甄别或 ADC 才能获得能量和时间信息。", "resource": "CR-RC 成形、极零相消、动态范围、ADC 输入范围", "task": "根据前放输出幅度和衰减时间，给出后级成形时间、增益和 ADC 量程的初步选择。", "check": "能说明极零相消、脉冲堆积、动态范围饱和和基线漂移的风险。"},
        {"title": "参数估算与故障诊断", "subtitle": "用输入电荷、反馈电容、噪声和波形异常完成一次前放方案判断", "objective": "把前置放大器原理转化为可执行的选型、估算和调试流程。", "resource": "典型探测器输出电荷、Cf/Rf 选型范围、示波器波形诊断案例", "task": "完成一页前置放大器参数估算表：输入电荷、Cf、输出幅度、衰减常数、主要噪声风险。", "check": "能根据输出过小、饱和、长尾、振荡或噪声过大判断可能原因。"},
    ]


def _detector_nodes(goal: str) -> list[dict[str, str]]:
    return [
        {"title": "辐射相互作用", "subtitle": "电离、激发与光子三效应", "objective": "解释不同射线在介质中的能量沉积方式", "resource": "辐射探测与测量基础章节", "task": "整理三类射线与介质作用表", "check": "能判断常见射线适合的探测介质"},
        {"title": "电荷收集", "subtitle": "漂移、扩散与收集效率", "objective": "说明载流子如何形成可读出信号", "resource": "电荷收集过程讲义", "task": "画出载流子收集时序图", "check": "能解释脉冲上升沿来源"},
        {"title": "脉冲形成", "subtitle": "从电流脉冲到电压脉冲", "objective": "把探测器输出映射到前端电子学输入", "resource": "半导体探测器信号形成演示", "task": "完成等效电流脉冲草图", "check": "能说明成形时间与计数率取舍"},
        {"title": "探测器类型对比", "subtitle": "气体、闪烁体、半导体", "objective": f"为“{goal}”选择合适探测方案", "resource": "典型探测器对比讲义", "task": "完成三类探测器对比卡片", "check": "能选择合适的能谱测量方案"},
        {"title": "系统指标与综合设计", "subtitle": "噪声、增益、动态范围与方案输出", "objective": "完成探测链路指标说明", "resource": "前端电子学指标速查", "task": "提交一套探测系统链路方案", "check": "能完成链路图与指标说明"},
    ]


def _digital_signal_nodes() -> list[dict[str, str]]:
    return [
        {"title": "采样与量化", "subtitle": "采样率、混叠、ADC 位数与动态范围", "objective": "判断采样率和量化精度是否满足核电子学脉冲采集", "resource": "数字信号处理入门、ADC 指标讲义", "task": "为给定脉冲宽度选择采样率和 ADC 位数", "check": "能解释奈奎斯特频率、混叠和量化步长"},
        {"title": "离散系统与卷积", "subtitle": "差分方程、冲激响应和系统输出", "objective": "用离散系统语言描述数字处理链路", "resource": "离散系统讲义、卷积可视化练习", "task": "写出简单滤波器差分方程并计算输出", "check": "能连接冲激响应、卷积和系统稳定性"},
        {"title": "DFT/FFT 频域分析", "subtitle": "频率分辨率、窗函数和频谱泄漏", "objective": "用频域视角观察脉冲信号和噪声", "resource": "FFT 可视化演示", "task": "输出样例脉冲波形的频谱并解释主频成分", "check": "能识别频谱泄漏和窗函数影响"},
        {"title": "数字滤波与成形", "subtitle": "平滑、梯形成形和参数选择", "objective": "比较不同数字滤波方法对噪声和能量分辨率的影响", "resource": "脉冲波形采样数据", "task": "比较滤波前后噪声和脉冲幅度", "check": "能给出滤波参数建议"},
        {"title": "触发与谱图输出", "subtitle": "阈值、时间戳、事件筛选和谱图统计", "objective": "串联数字采集到谱图输出的完整处理流程", "resource": "数字触发逻辑讲义", "task": "设计一个简单触发判据并输出处理流程", "check": "能说明误触发、漏触发和统计结果影响"},
    ]


def _decay_nodes(goal: str) -> list[dict[str, str]]:
    return [
        {"title": "指数衰变模型", "subtitle": "半衰期、衰变常数与平均寿命", "objective": f"为“{goal}”建立可计算的指数模型", "resource": "放射性衰变基础章节", "task": "推导并整理半衰期、衰变常数和平均寿命关系", "check": "能完成半衰期与衰变常数换算"},
        {"title": "活度与计数率", "subtitle": "源强、效率、本底与几何因子", "objective": "把实验计数转换为活度或源强估计", "resource": "活度计算例题与效率修正讲义", "task": "完成一组活度、本底扣除和效率修正计算", "check": "能说明净计数率和活度估算流程"},
        {"title": "计数统计与不确定度", "subtitle": "泊松涨落、相对误差和测量时间", "objective": "用统计不确定度指导测量时间规划", "resource": "计数统计与误差传播讲义", "task": "按目标相对误差反推测量时间", "check": "能比较不同计数条件下的信噪比"},
        {"title": "半衰期拟合", "subtitle": "半对数图、斜率和残差", "objective": "根据计数序列估计半衰期并报告误差", "resource": "短寿命核素计数序列", "task": "拟合一条衰变曲线并解释残差", "check": "能写出拟合结论和主要误差来源"},
    ]


def _generic_nodes(text: str) -> list[dict[str, str]]:
    return [
        {"title": f"{text}任务边界", "subtitle": "明确学习对象、前置知识、输出要求和评价标准", "objective": f"把“{text}”拆成可验证的学习任务", "resource": f"{text}基础资料、课程讲义和案例", "task": "写出本主题的概念清单和问题清单", "check": "能说明学习对象、适用场景和完成标准"},
        {"title": f"{text}核心概念", "subtitle": "建立术语、结构、公式或流程之间的关系", "objective": f"掌握“{text}”的关键概念网络", "resource": "教材章节、课堂笔记、知识库召回资料", "task": "整理概念-关系-例子的三列表", "check": "能不用资料解释 3 个核心概念及其关系"},
        {"title": f"{text}关键方法", "subtitle": "掌握解决典型问题所需的方法和步骤", "objective": f"能用“{text}”相关方法处理典型问题", "resource": "例题、实验步骤或工程案例", "task": "完成一个方法流程图和一组例题", "check": "能说明每一步的输入、输出和判断依据"},
        {"title": f"{text}案例练习", "subtitle": "通过例题或数据验证理解", "objective": "把概念迁移到具体案例", "resource": "案例数据、习题或实验记录", "task": "完成一个案例分析并标出易错点", "check": "能独立复现案例的关键步骤"},
        {"title": f"{text}综合输出", "subtitle": "形成总结、方案或报告", "objective": f"完成“{text}”的阶段性学习产出", "resource": "复盘清单、报告模板", "task": "提交一页总结或方案报告", "check": "能回答目标、方法、证据和风险四类问题"},
    ]
