"""Default coordination strategies for extensible agent workflows."""

from __future__ import annotations

from backend.application.coordination.base import CoordinationPlan, CoordinationStage
from backend.config.log_privacy import text_fingerprint
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext


class DefaultCoordinationStrategy:
    def __init__(self, workflow_name: str):
        self.workflow_name = workflow_name

    def build_plan(
        self,
        request: AgentRequest,
        context: AgentRuntimeContext,
    ) -> CoordinationPlan:
        workflow_name = self.workflow_name
        plan_templates = {
            "deep_research": (
                "先规划研究问题，再由多个隔离子智能体并行检索，最后生成并归档报告。",
                [
                    CoordinationStage("plan", "研究规划", "拆解证据问题和检索任务。"),
                    CoordinationStage("dispatch", "并行研究", "子智能体检索知识库与网络证据。"),
                    CoordinationStage("write", "报告输出", "汇总证据、校验冲突并生成下载报告。"),
                ],
            ),
            "plan_solve": (
                "先生成结构化计划，再由隔离子智能体并行求解并统一审校。",
                [
                    CoordinationStage("plan", "任务规划", "拆解目标、约束和证据需求。"),
                    CoordinationStage("solve", "并行求解", "隔离子智能体按子任务检索和分析。"),
                    CoordinationStage("review", "结果校验", "汇总分支结论并检查输出完整性。"),
                ],
            ),
            "multi_agent_parallel": (
                "将任务拆成可并行子任务，由多个子执行单元并发推进后合并结果。",
                [
                    CoordinationStage("plan", "任务拆解", "识别可并行处理的子问题。"),
                    CoordinationStage("dispatch", "并行分派", "为子问题分配独立执行单元。"),
                    CoordinationStage("merge", "结果合并", "汇总各子任务结论并形成最终答案。"),
                ],
            ),
            "multi_agent_alternating": (
                "由多个子执行单元轮转推进，每轮共享最新中间结果后继续求解。",
                [
                    CoordinationStage("plan", "轮次规划", "定义交替协作顺序与目标。"),
                    CoordinationStage("alternate", "交替执行", "各执行单元按轮次接力处理任务。"),
                    CoordinationStage("merge", "收敛总结", "整合轮次结果并输出最终结论。"),
                ],
            ),
        }
        summary, stages = plan_templates.get(
            workflow_name,
            (
                "按协调策略推进复杂任务处理。",
                [CoordinationStage("execute", "执行", "执行协调流程。")],
            ),
        )
        return CoordinationPlan(
            workflow=workflow_name,
            summary=summary,
            stages=stages,
            metadata={
                "agent_code": context.agent_profile.code,
                "question_fingerprint": text_fingerprint(request.question),
            },
        )
