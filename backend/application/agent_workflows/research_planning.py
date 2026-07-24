"""Planning and specialist-coverage rules for coordinated research."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from typing import Any

from backend.application.agent_workflows.coordination_types import ResearchTask
from backend.application.agent_workflows.research_specialists import (
    normalize_research_specialist,
    planner_research_specialists,
)
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext


@dataclass(frozen=True)
class ResearchReflection:
    """Planner verdict after inspecting the evidence gathered so far."""

    complete: bool
    reason: str
    tasks: tuple[ResearchTask, ...] = ()
    coverage_score: float = 0.0
    unresolved_dimensions: tuple[str, ...] = ()
    source_diversity_score: float = 0.0
    contradiction_count: int = 0
    expected_information_gain: float = 0.0
    stop_reason: str = ""


def _coerce_int(value: Any, *, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except Exception:
        parsed = default
    return max(low, min(high, parsed))


def _coerce_float(
    value: Any,
    *,
    default: float,
    low: float = 0.0,
    high: float = 1.0,
) -> float:
    try:
        parsed = float(value)
    except Exception:
        parsed = default
    return max(low, min(high, parsed))


def _coerce_str_tuple(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw_values = [value]
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raw_values = [value]
    return tuple(str(item).strip() for item in raw_values if str(item).strip())


def _default_task_budget(agent_type: str) -> int:
    """Keep source verification and adversarial review on the primary tier."""

    return 2 if agent_type in {"evidence_researcher", "risk_reviewer"} else 1


def _planner_specialist_contract(
    context: AgentRuntimeContext,
) -> tuple[str, str]:
    """Build the planner role catalog from request-scoped manifest specs."""

    specialists = planner_research_specialists(context.agent_specs)
    lines = ["agent_type 必须严格从以下当前可执行能力角色中选择；不得输出目录外角色："]
    for specialist in specialists:
        alias_note = (
            f"；兼容别名：{', '.join(specialist.aliases)}" if specialist.aliases else ""
        )
        lines.append(
            f"- {specialist.key}（{specialist.title}）："
            f"{specialist.planning_description}{alias_note}"
        )
    lines.append("输出时优先使用上述 canonical agent_type；别名仅用于兼容输入。")
    return specialists[0].key, "\n".join(lines) + "\n"


def build_planner_prompt(
    request: AgentRequest,
    context: AgentRuntimeContext,
    *,
    max_tasks: int = 5,
) -> str:
    max_tasks = max(2, min(32, int(max_tasks)))
    example_agent_type, specialist_contract = _planner_specialist_contract(context)
    source_requirements = []
    if context.kb_ids:
        source_requirements.append(f"必须检索用户选中的 {len(context.kb_ids)} 个知识库")
    if context.use_search:
        source_requirements.append("必须进行联网检索以补充和核验时效性信息")
    if not source_requirements:
        source_requirements.append("当前没有外部检索源，只能规划基于模型知识的分析")
    route = dict(context.request_metadata.get("_workflow_route") or {})
    planner_identity = (
        "深度研究范围与任务规划智能体"
        if context.workflow_name == "deep_research"
        else "主控 Plan-Solve 的研究规划智能体"
    )
    return (
        f"你是{planner_identity}。先明确研究范围，再把任务拆成 2-{max_tasks} 个边界清晰、可按依赖并行、合并后能完整回答"
        "原问题的研究子任务。优先让无依赖的不同专业角色并行，只有确实消费前置结果的任务才设置 dependencies。"
        "先从用户问题中识别需要覆盖的不同视角、比较维度和证据类型，再据此拆分任务；"
        "各任务不得只是重复改写同一问题，合并后应覆盖关键机制、可核验事实、量化证据、时效性、反例与适用边界中"
        "与原问题真正相关的部分，不相关的维度不要机械添加。"
        "只输出 JSON，不要 Markdown。research_brief 是后续监督和写作必须遵守的研究契约；"
        "dimensions 只列回答本问题不可缺少的维度，exclusions 明确不应扩展的范围。格式："
        '{"research_brief":{"objective":"...","dimensions":["..."],'
        '"exclusions":["..."],"source_policy":["..."],"success_criteria":["..."]},'
        '"summary":"...","tasks":[{"key":"t1","title":"...","question":"...",'
        '"priority":80,"expected_tools":["multi_kb_search"],"risk":"medium",'
        f'"dependencies":[],"budget":1,"agent_type":"{example_agent_type}"}}]}}。'
        "每个 question 是交给研究子智能体的完整任务说明，由子智能体根据检索结果自主决定和改写搜索词；"
        "priority 为 1-100，"
        "dependencies 填依赖任务 key。budget=1 仅用于低风险、少量背景整理；"
        "需要联网多源核验、时效信息、标准/法规、关键性能数据或反证审查时必须设为 2-5，"
        "高风险或强核验任务的 budget 不得低于 2。\n"
        f"{specialist_contract}"
        f"路由判断：{json.dumps(route, ensure_ascii=False)}。\n"
        f"检索约束：{'；'.join(source_requirements)}。\n\n原始任务：\n{request.question}"
    )


def build_research_reflection_prompt(
    request: AgentRequest,
    context: AgentRuntimeContext,
    *,
    plan: str,
    evidence_matrix: str,
    citation_report: dict[str, Any],
    research_brief: dict[str, Any] | None = None,
    coverage_ledger: dict[str, Any] | None = None,
    max_tasks: int = 2,
) -> str:
    """Ask the planner to decide whether one bounded follow-up wave is needed."""

    max_tasks = max(1, min(5, int(max_tasks)))
    example_agent_type, specialist_contract = _planner_specialist_contract(context)
    source_requirements = []
    if context.kb_ids:
        source_requirements.append("已选择知识库，关键结论应优先有知识库证据")
    if context.use_search:
        source_requirements.append("已开启联网，时效性或外部事实应有相关网页来源")
    if not source_requirements:
        source_requirements.append("没有外部检索源，不要假装能够补充外部证据")
    return (
        "你是深度研究监督智能体。现在不是重做计划，也不是撰写最终答案，而是依据研究契约和覆盖账本审查当前证据，"
        "决定是否启动下一批高价值研究。"
        "逐项检查：主要问题维度是否覆盖；关键结论是否有可追溯来源；来源是否真正相关且相互独立；"
        "是否存在未解决的冲突、过时信息、定量证据缺口或适用边界缺口。"
        "证据矩阵中 status=degraded 的分支只可用于识别缺口，不得作为结论已被证明的依据；"
        "引用预校验中的 failed_branches、citation_coverage、independent_sources、corroboration_rate、"
        "high_authority_source_ratio、conflicted_claims、semantic_review_candidates 和 low_confidence_claims 必须纳入判断。"
        "只有缺口会实质影响最终答案且预计能获得新信息时才安排补研；不要为了凑数量重复已有任务。"
        "coverage_score、source_diversity_score、expected_information_gain 均输出 0-1；"
        "unresolved_dimensions 只列仍会影响回答的维度；contradiction_count 输出尚未解决的冲突数。"
        f"最多提出 {max_tasks} 个互不重叠、可独立执行的补研任务。每个 question 必须是完整研究任务说明，"
        "由执行该任务的子智能体自主决定搜索词，并根据搜索结果继续改写搜索词。"
        "只输出 JSON，不要 Markdown。格式："
        '{"complete":false,"reason":"...","coverage_score":0.7,'
        '"unresolved_dimensions":["..."],"source_diversity_score":0.6,'
        '"contradiction_count":1,"expected_information_gain":0.8,"stop_reason":"",'
        '"tasks":[{"key":"gap1","title":"...",'
        '"question":"...","priority":90,"expected_tools":["web_search"],"risk":"medium",'
        f'"dependencies":[],"budget":2,"agent_type":"{example_agent_type}"}}]}}。'
        "若证据已经足够，必须输出 "
        '{"complete":true,"reason":"...","coverage_score":1.0,'
        '"unresolved_dimensions":[],"source_diversity_score":0.8,'
        '"contradiction_count":0,"expected_information_gain":0.0,'
        '"stop_reason":"证据充分","tasks":[]}。\n'
        f"{specialist_contract}"
        f"来源约束：{'；'.join(source_requirements)}。\n\n"
        f"原始任务：\n{request.question}\n\n"
        f"研究契约：\n{json.dumps(research_brief or {}, ensure_ascii=False)}\n\n"
        f"首轮研究计划：\n{plan}\n\n"
        f"确定性覆盖账本：\n{json.dumps(coverage_ledger or {}, ensure_ascii=False)}\n\n"
        f"引用预校验：\n{json.dumps(citation_report, ensure_ascii=False)}\n\n"
        f"当前证据矩阵：\n{evidence_matrix}"
    )


def _decode_json_payload(text: str) -> Any | None:
    text = str(text or "").strip()
    if not text:
        return None
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)```", text, flags=re.IGNORECASE)
    candidate = fenced.group(1).strip() if fenced else text
    start = min(
        [pos for pos in (candidate.find("{"), candidate.find("[")) if pos >= 0],
        default=-1,
    )
    if start > 0:
        candidate = candidate[start:]
    try:
        parsed, _ = json.JSONDecoder().raw_decode(candidate)
    except Exception:
        return None
    return parsed


def parse_research_tasks(
    planner_answer: str,
    original_question: str,
    context: AgentRuntimeContext | None = None,
    *,
    max_tasks: int = 5,
) -> list[ResearchTask]:
    del original_question
    parsed = _decode_json_payload(planner_answer)
    if parsed is None:
        return []
    raw_tasks = parsed.get("tasks") if isinstance(parsed, dict) else parsed
    if not isinstance(raw_tasks, list):
        return []
    tasks: list[ResearchTask] = []
    used_keys: set[str] = set()
    task_limit = max(1, min(32, int(max_tasks)))
    for index, item in enumerate(raw_tasks[:task_limit], start=1):
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or item.get("query") or "").strip()
        if not question:
            continue
        requested_key = str(item.get("key") or f"t{index}").strip() or f"t{index}"
        key = requested_key
        suffix = 2
        while key in used_keys:
            key = f"{requested_key}_{suffix}"
            suffix += 1
        used_keys.add(key)
        agent_type = normalize_research_specialist(
            str(item.get("agent_type") or item.get("role") or ""),
            agent_specs=(context.agent_specs if context is not None else None),
            default_index=index,
        )
        expected_tools = _coerce_str_tuple(
            item.get("expected_tools") or item.get("tools")
        )
        expected_tools = _available_expected_tools(
            expected_tools,
            context=context,
            agent_type=agent_type,
        )
        tasks.append(
            ResearchTask(
                key=key,
                title=str(item.get("title") or f"研究子任务 {index}").strip(),
                question=question,
                priority=_coerce_int(
                    item.get("priority"),
                    default=max(10, 90 - index * 10),
                    low=1,
                    high=100,
                ),
                expected_tools=expected_tools,
                risk=str(item.get("risk") or "medium").strip() or "medium",
                dependencies=_coerce_str_tuple(
                    item.get("dependencies") or item.get("dependency")
                ),
                budget=_coerce_int(
                    item.get("budget"),
                    default=_default_task_budget(agent_type),
                    low=1,
                    high=5,
                ),
                agent_type=agent_type,
            )
        )
    known_keys = {task.key for task in tasks}
    solution_dependencies = tuple(
        task.key for task in tasks if task.agent_type != "solution_architect"
    )
    normalized_tasks: list[ResearchTask] = []
    for task in tasks:
        explicit_dependencies = tuple(
            dependency
            for dependency in dict.fromkeys(task.dependencies)
            if dependency in known_keys and dependency != task.key
        )
        # Respect a planner's explicit dependency subset. Expanding every
        # solution task to every evidence lane makes one unrelated failed lane
        # block the whole solution stage and needlessly serializes research.
        # Fall back to all evidence lanes only when no valid dependency was
        # supplied at all.
        dependencies = explicit_dependencies
        if task.agent_type == "solution_architect" and not dependencies:
            dependencies = solution_dependencies
        normalized_tasks.append(replace(task, dependencies=dependencies))
    return normalized_tasks


def parse_research_reflection(
    reflection_answer: str,
    original_question: str,
    context: AgentRuntimeContext | None = None,
    *,
    max_tasks: int = 2,
) -> ResearchReflection | None:
    """Parse a fail-closed evidence review and its optional follow-up tasks."""

    parsed = _decode_json_payload(reflection_answer)
    if not isinstance(parsed, dict) or not isinstance(parsed.get("complete"), bool):
        return None
    complete = bool(parsed["complete"])
    reason = str(parsed.get("reason") or "").strip()
    metrics = {
        "coverage_score": _coerce_float(
            parsed.get("coverage_score"), default=1.0 if complete else 0.0
        ),
        "unresolved_dimensions": _coerce_str_tuple(parsed.get("unresolved_dimensions")),
        "source_diversity_score": _coerce_float(
            parsed.get("source_diversity_score"), default=0.0
        ),
        "contradiction_count": _coerce_int(
            parsed.get("contradiction_count"), default=0, low=0, high=100
        ),
        "expected_information_gain": _coerce_float(
            parsed.get("expected_information_gain"), default=0.0
        ),
        "stop_reason": str(parsed.get("stop_reason") or "").strip(),
    }
    if complete:
        return ResearchReflection(complete=True, reason=reason, **metrics)
    tasks = parse_research_tasks(
        json.dumps({"tasks": parsed.get("tasks")}, ensure_ascii=False),
        original_question,
        context,
        max_tasks=max_tasks,
    )
    return ResearchReflection(
        complete=False,
        reason=reason,
        tasks=tuple(tasks),
        **metrics,
    )


def fallback_research_tasks(
    workflow_name: str,
    question: str,
    context: AgentRuntimeContext | None = None,
) -> list[ResearchTask]:
    specialists = planner_research_specialists(
        context.agent_specs if context is not None else None
    )

    def role_for(index: int) -> str:
        return specialists[index % len(specialists)].key

    kb_tools = ("multi_kb_search",) if context is not None and context.kb_ids else ()
    web_tools = ("web_search",) if context is not None and context.use_search else ()
    if workflow_name == "plan_solve":
        return [
            ResearchTask(
                "t1",
                "通用问题分析",
                question,
                priority=90,
                expected_tools=kb_tools,
                agent_type=role_for(0),
            )
        ]
    return [
        ResearchTask(
            "t1",
            "核心事实与技术依据",
            f"围绕原始问题“{question}”，研究回答所需的核心事实、概念边界与技术依据，并保留可核验来源。",
            priority=90,
            expected_tools=kb_tools,
            agent_type=role_for(0),
        ),
        ResearchTask(
            "t2",
            "现状、案例与最新进展",
            f"围绕原始问题“{question}”，核验当前状态、代表性案例和时效性进展；自主确定并迭代检索词。",
            priority=70,
            expected_tools=tuple(dict.fromkeys((*kb_tools, *web_tools))),
            budget=2,
            agent_type=role_for(1),
        ),
        ResearchTask(
            "t3",
            "局限、风险与争议",
            f"围绕原始问题“{question}”，审查反例、局限、争议和适用边界，指出仍需验证的证据缺口。",
            priority=50,
            expected_tools=web_tools,
            budget=2,
            agent_type=role_for(2),
        ),
    ]


def _available_expected_tools(
    expected_tools: tuple[str, ...],
    *,
    context: AgentRuntimeContext | None,
    agent_type: str,
) -> tuple[str, ...]:
    """Remove impossible planner contracts while preserving valid tool intent."""

    if context is None:
        return tuple(dict.fromkeys(expected_tools))
    selected_skills = set(context.selected_skills or [])
    role_spec = context.agent_specs.get(agent_type)
    role_allowed_tools = (
        set(role_spec.allowed_tools)
        if role_spec is not None and role_spec.allowed_tools is not None
        else None
    )
    normalized: list[str] = []
    for tool_name in expected_tools:
        if role_allowed_tools is not None and tool_name not in role_allowed_tools:
            continue
        if tool_name == "multi_kb_search" and not context.kb_ids:
            continue
        if tool_name in {"web_search", "academic_search"} and not context.use_search:
            continue
        if tool_name == "academic_search" and not (
            {"academic-search", "paper-review"} & selected_skills
        ):
            continue
        if tool_name.startswith("groundwater_") or tool_name in {
            "inspect_project_tables",
            "query_project_tables",
            "summarize_project_table",
        }:
            if context.agent_profile.domain != "groundwater-project-analysis":
                continue
        if tool_name in {
            "list_experiment_datasets",
            "inspect_excel_data",
            "query_excel_data",
            "summarize_excel_data",
            "visualize_excel_data",
        } and not (
            {"general-table-analysis", "excel-experiment-analysis"} & selected_skills
        ):
            continue
        normalized.append(tool_name)
    return tuple(dict.fromkeys(normalized))
