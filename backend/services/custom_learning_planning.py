"""自定义学习规划域:蓝图数据类、风格教练、课程规划、基线评估与周排程。(拆自 services/custom_learning_agents.py)"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


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

    def _curated_nodes(self, text: str) -> list[dict[str, str]] | None:
        """命中人工整理的领域模板时返回模板节点，否则返回 None。"""
        if "前置放大" in text or "前放" in text or "放大器原理" in text:
            return _preamp_nodes()
        if "半导体" in text or "探测器" in text:
            return _detector_nodes(text)
        if "数字" in text or "信号" in text:
            return _digital_signal_nodes()
        if "衰变" in text or "活度" in text or "半衰期" in text:
            return _decay_nodes(text)
        return None

    def uses_generic_fallback(self, goal: str) -> bool:
        """目标未命中任何人工模板、只能用通用五段骨架时为 True。"""
        return self._curated_nodes(goal.strip() or "自定义学习") is None

    def _keyword_nodes(self, goal: str, duration_days: int) -> list[dict[str, str]]:
        text = goal.strip() or "自定义学习"
        curated = self._curated_nodes(text)
        return curated if curated is not None else _generic_nodes(text)


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
