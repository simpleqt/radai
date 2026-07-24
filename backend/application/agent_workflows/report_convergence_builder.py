"""Evidence-bound report construction and final convergence."""

from __future__ import annotations

import json
from dataclasses import replace

from backend.application.agent_executors.stream_utils import make_process_event
from backend.application.agent_workflows import coordinated_agent_workflow as _legacy
from backend.application.agent_workflows.coordination_component import (
    CoordinationComponent,
)
from backend.application.agent_workflows.coordination_types import (
    CoordinationUnitResult,
    ResearchTask,
)
from backend.application.agent_workflows.evidence_verifier import (
    CitationVerifier,
    analyze_answer_support,
    answer_claim_candidates,
    claim_fingerprint,
    format_evidence_matrix,
    verify_answer,
)
from backend.application.agent_workflows.research_evidence_ledger import (
    build_default_research_brief,
    format_compact_evidence_ledger,
    parse_research_brief,
)
from backend.core.agents.context.subagent_adapter import format_handoff_bundle
from backend.domain.agents.request import AgentRequest
from backend.domain.agents.runtime_context import AgentRuntimeContext

WorkflowEventSink = _legacy.WorkflowEventSink


class ReportConvergenceBuilder(CoordinationComponent):
    async def _build_research_report(
        self,
        *,
        request: AgentRequest,
        context: AgentRuntimeContext,
        planner: CoordinationUnitResult,
        tasks: list[ResearchTask],
        branch_results: list[CoordinationUnitResult],
        event_sink: WorkflowEventSink | None,
        research_budget_exhausted: bool,
    ) -> CoordinationUnitResult:
        # Give synthesis only facts that have a claim-level binding to an
        # actually captured source.  ``structured_only`` still serializes the
        # summary field, so passing raw handoffs here used to let uncited prose
        # bypass the evidence matrix and get copied into the final report.
        writer_branch_results = self._writer_safe_branch_results(branch_results)
        research_brief = parse_research_brief(
            planner.answer,
            fallback=build_default_research_brief(request, context),
        )
        if self.name == "deep_research":
            evidence_context = (
                "研究契约（限定报告范围、必要维度和成功条件）：\n"
                f"{json.dumps(research_brief.to_dict(), ensure_ascii=False)}\n\n"
                "去重证据账本（writer 只能依据其中的 claims/supporting_refs/"
                "confidence/conflicts/missing 写作）：\n"
                f"{format_compact_evidence_ledger(writer_branch_results, max_chars=28000)}"
            )
        else:
            branch_handoffs = format_handoff_bundle(
                [result.handoff for result in writer_branch_results if result.handoff],
                max_chars=24000,
                structured_only=True,
            )
            evidence_matrix = self._host._format_evidence_matrix(
                writer_branch_results,
                max_chars=24000,
            )
            evidence_context = (
                "证据矩阵（reviewer 必须优先依据 claims/supporting_refs/"
                "confidence/conflicts/missing 写作）：\n"
                f"{evidence_matrix}\n\n"
                "子智能体交接结果（仅包含结论、证据引用、风险和待办，不含工具执行轨迹）：\n"
                f"{branch_handoffs}"
            )
        citation_report = CitationVerifier().verify_handoffs(branch_results)
        citation_diagnostics = {
            key: value
            for key, value in citation_report.to_dict().items()
            if key != "warnings"
        }
        outline_instruction = (
            "先根据研究契约形成精简章节提纲，再按章节使用证据账本写作；"
            "没有可用结论的章节不要用模型常识补齐。"
            if self.name == "deep_research"
            else ""
        )
        conflict_instruction = (
            "detected_conflicts 中仍未解决的结论不得择一写成确定事实；"
            "应核对条件后并列说明差异和不确定性。"
            if self.name == "deep_research"
            else ""
        )
        writer_role = "report_writer" if self.name == "deep_research" else "reviewer"
        await self._emit_process_event(
            event_sink,
            make_process_event(
                "status",
                "正在汇总研究结论",
                "答案输出智能体正在核对各分支证据、处理冲突并编写最终报告。",
                level="active",
                metadata={
                    "runtime_type": "writer_started",
                    "partial_handoff": False,
                    "completed_branches": len(branch_results),
                    "planned_branches": len(tasks),
                    "citation_verification": citation_report.to_dict(),
                    "research_phase_timed_out": research_budget_exhausted,
                    "writer_timeout_seconds": (
                        self._runtime_budget.deep_research_writer_timeout_seconds
                        if self.name == "deep_research"
                        else self._role_timeout_seconds(writer_role, context)
                    ),
                },
            ),
        )
        writer = await self._run_unit(
            role=writer_role,
            question=(
                "你是答案输出智能体。基于规划和已完成的子智能体结果，解决冲突、标明不确定性，"
                "输出结构完整的最终 Markdown 报告。不得声称使用了未出现在证据中的来源；"
                "证据矩阵中 status=degraded 的分支不得支撑最终事实，只能用于说明失败、限制或待补证项；"
                "如果部分规划任务尚未完成，必须明确哪些结论仍不确定；"
                "采用论文式结构写作，至少包含摘要、关键词、编号章节、讨论、结论和参考文献；"
                "正文引用统一使用连续编号标记 [1]、[2]，不要在正文展开长 URL。文末以‘## 参考文献’集中列出"
                "‘[编号] supporting_ref 原文’，保证每个编号都能逐项映射到 claims/supporting_refs 中的真实来源；"
                "不得生成没有对应定义的编号或改写 supporting_ref。每个事实句只能来自一条带 supporting_refs 的 claim，"
                "不得从摘要、规划或引用统计中补充领域事实。不要逐条编造或复述‘待核验’事实；"
                "未绑定来源的内容只在一个简短的‘证据缺口’小节按主题和数量合并说明。"
                "引用预校验数字是流程诊断，不是领域结论，不得写入正文。"
                f"{conflict_instruction}"
                f"{outline_instruction}\n\n"
                f"原始任务：\n{request.question}\n\n"
                "引用预校验摘要（仅用于判断证据覆盖，不得作为报告事实复述）：\n"
                f"{citation_diagnostics}\n\n"
                f"{evidence_context}"
            ),
            request=request,
            context=context,
            tool_access=False,
            event_sink=event_sink,
            progress_title="答案输出智能体仍在汇总",
            timeout_override_seconds=(
                float(self._runtime_budget.deep_research_writer_timeout_seconds)
                if self.name == "deep_research"
                else None
            ),
        )
        initial_answer_verification = analyze_answer_support(
            writer.answer,
            writer_branch_results,
        )
        repair_attempted = False
        evidence_only_fallback_used = False
        writer_fallback_used = not writer.answer
        repair_answer_verification: dict[str, object] | None = None
        repair_failure_reasons: list[str] = []
        needs_evidence_repair = bool(
            self.name == "deep_research"
            and (
                not writer.answer
                or writer.error
                or initial_answer_verification.unsupported_claims
                or initial_answer_verification.unsupported_references
                or initial_answer_verification.numeric_conflicts
                or initial_answer_verification.negation_conflicts
                or initial_answer_verification.reference_conflicts
                or (
                    citation_report.claims_with_refs > 0
                    and initial_answer_verification.supported_claims == 0
                )
            )
        )
        if needs_evidence_repair:
            repair_attempted = True
            await self._emit_process_event(
                event_sink,
                make_process_event(
                    "status",
                    "正在修复未通过核验的报告",
                    "初稿包含未被证据账本授权的结论或引用，正在进行一次无工具、证据限定重写。",
                    level="warning",
                    metadata={
                        "runtime_type": "writer_evidence_repair_started",
                        "answer_verification": initial_answer_verification.to_dict(),
                    },
                ),
            )
            repair = await self._run_unit(
                role=writer_role,
                question=(
                    "上一版报告未通过证据核验。现在进行最终证据限定重写，禁止调用工具。"
                    "只允许使用下方去重证据账本中逐项列出的 claim；每个事实条目尽量原样复制一条 claim，"
                    "并将该 claim 的 supporting_refs 映射为正文连续编号 [1]、[2]；正文不要展开长 URL，"
                    "文末必须用‘## 参考文献’和‘[编号] supporting_ref 原文’给出完整映射。禁止补充背景常识、跨对象外推、合并不同 claim "
                    "产生新结论、改写 URL 或生成新来源。没有 claim 的维度只能在‘证据缺口’中简短说明，"
                    "不得写成事实段落。不要复述流程统计，不要输出‘待核验’事实。"
                    "输出完整 Markdown 报告，不要解释修复过程。\n\n"
                    f"原始任务：\n{request.question}\n\n"
                    f"研究契约：\n{json.dumps(research_brief.to_dict(), ensure_ascii=False)}\n\n"
                    "唯一允许使用的证据账本：\n"
                    f"{format_compact_evidence_ledger(writer_branch_results, max_chars=28000)}"
                ),
                request=request,
                context=context,
                tool_access=False,
                event_sink=event_sink,
                progress_title="答案输出智能体正在修复证据边界",
                task_id="report-writer:evidence-repair",
                allowed_tools=set(),
                timeout_override_seconds=min(
                    120.0,
                    max(
                        15.0,
                        float(self._runtime_budget.deep_research_writer_timeout_seconds)
                        * 0.5,
                    ),
                ),
            )
            repair_verification = analyze_answer_support(
                repair.answer,
                writer_branch_results,
            )
            repair_answer_verification = repair_verification.to_dict()
            repair_passed = bool(
                repair.answer
                and not repair.error
                and (
                    citation_report.claims_with_refs == 0
                    or repair_verification.supported_claims > 0
                )
                and repair_verification.unsupported_claims == 0
                and repair_verification.unsupported_references == 0
                and repair_verification.numeric_conflicts == 0
                and repair_verification.negation_conflicts == 0
                and repair_verification.reference_conflicts == 0
            )
            if repair_passed:
                writer = replace(
                    repair,
                    process_events=(*writer.process_events, *repair.process_events),
                )
            else:
                if not repair.answer:
                    repair_failure_reasons.append("repair_empty")
                if repair.error:
                    repair_failure_reasons.append("repair_error")
                for field_name in (
                    "unsupported_claims",
                    "unsupported_references",
                    "numeric_conflicts",
                    "negation_conflicts",
                    "reference_conflicts",
                ):
                    if getattr(repair_verification, field_name):
                        repair_failure_reasons.append(f"repair_{field_name}")
                if (
                    citation_report.claims_with_refs > 0
                    and repair_verification.supported_claims == 0
                ):
                    repair_failure_reasons.append("repair_no_supported_claims")
                evidence_only_fallback_used = True
                writer_fallback_used = True
                writer = replace(
                    writer,
                    answer=self._build_evidence_only_report(
                        request=request,
                        tasks=tasks,
                        branch_results=writer_branch_results,
                    ),
                    error=None,
                    timed_out=False,
                    process_events=(*writer.process_events, *repair.process_events),
                )
        elif writer_fallback_used:
            writer = replace(
                writer,
                answer=self._host._build_fallback_report(
                    request=request,
                    planner=planner,
                    tasks=tasks,
                    # When the writer returns nothing, preserve the original
                    # branch summaries in the emergency report for operator
                    # visibility.  The verifier will place one document-level
                    # warning above this draft; normal synthesis still sees
                    # only the citation-safe projection.
                    branch_results=branch_results,
                ),
            )
        final_answer_verification = analyze_answer_support(
            writer.answer,
            writer_branch_results,
        )
        # A deterministic evidence-only fallback with zero factual candidates
        # already states that no source-bound conclusion is available. Adding
        # another document-level “待核验” banner would mislabel a safe gap
        # report as an unverified factual draft.
        if not (
            evidence_only_fallback_used
            and final_answer_verification.candidate_claims == 0
            and final_answer_verification.unsupported_references == 0
        ):
            writer = replace(
                writer,
                answer=self._host._verify_answer_against_matrix(
                    writer.answer,
                    writer_branch_results,
                ),
            )
        await self._emit_process_event(
            event_sink,
            make_process_event(
                "answer",
                (
                    "研究报告已按证据边界收敛"
                    if evidence_only_fallback_used
                    else "研究报告内容已完成"
                ),
                (
                    "初稿和证据限定重写均未通过逐项核验，已回退为仅包含来源绑定条目的论文式报告。"
                    if evidence_only_fallback_used
                    else "已完成多分支证据汇总，正在生成可下载报告。"
                ),
                level=(
                    "warning"
                    if writer.error
                    or evidence_only_fallback_used
                    or final_answer_verification.unsupported_claims
                    or final_answer_verification.unsupported_references
                    else "success"
                ),
                metadata={
                    "runtime_type": "writer_completed",
                    "fallback_used": writer_fallback_used,
                    "repair_attempted": repair_attempted,
                    "evidence_only_fallback_used": evidence_only_fallback_used,
                    "partial_handoff": False,
                    "initial_answer_verification": (
                        initial_answer_verification.to_dict()
                    ),
                    "repair_answer_verification": repair_answer_verification,
                    "fallback_reasons": repair_failure_reasons,
                    "answer_verification": final_answer_verification.to_dict(),
                    "evidence_quality": {
                        "corroboration_rate": citation_report.corroboration_rate,
                        "high_authority_source_ratio": (
                            citation_report.high_authority_source_ratio
                        ),
                        "conflicted_claims": citation_report.conflicted_claims,
                        "semantic_review_candidates": (
                            citation_report.semantic_review_candidates
                        ),
                    },
                },
            ),
        )
        return writer

    @staticmethod
    def _build_evidence_only_report(
        *,
        request: AgentRequest,
        tasks: list[ResearchTask],
        branch_results: list[CoordinationUnitResult],
    ) -> str:
        """Build a paper-style report containing only source-bound claims."""

        title = " ".join(str(request.question or "深度研究报告").split()).rstrip(
            "？?。"
        )
        for prefix in ("请帮我分析一下", "帮我分析一下", "请分析一下", "分析一下"):
            if title.startswith(prefix):
                title = title.removeprefix(prefix).strip("：:，, ")
                break
        title = title or "深度研究报告"
        sections = [
            f"# {title}",
            "",
            "## 摘要",
            "",
            "本文整理多分支检索形成的可核验证据。正文仅收录已逐项绑定实际来源的证据条目，"
            "未形成直接证据的主题集中列入讨论部分，不使用模型常识补写。",
            "",
            "**关键词：** 深度研究；证据核验；来源追踪",
            "",
            "## 1 研究范围与方法",
            "",
            "研究过程采用分主题检索、逐项证据绑定和汇总核验。每项事实均以连续编号标记指向文末来源，"
            "重复来源只保留一个编号。",
            "",
            "## 2 研究结果",
        ]
        result_by_key = {
            result.task_key: result for result in branch_results if result.task_key
        }
        seen_claims: set[str] = set()
        gap_titles: list[str] = []
        emitted_sections = 0
        reference_numbers: dict[str, int] = {}
        references: list[str] = []
        for task in tasks:
            result = result_by_key.get(task.key)
            handoff = result.handoff if result is not None and not result.error else {}
            claims: list[tuple[str, list[str]]] = []
            for raw_claim in (handoff or {}).get("claims") or []:
                claim = str(raw_claim.get("claim") or "").strip()
                refs = list(
                    dict.fromkeys(
                        str(ref).strip()
                        for ref in raw_claim.get("supporting_refs") or []
                        if str(ref).strip()
                    )
                )
                claim_key = " ".join(claim.casefold().split())
                if not claim or not refs or claim_key in seen_claims:
                    continue
                seen_claims.add(claim_key)
                claims.append((claim, refs))
            if not claims:
                gap_titles.append(task.title)
                continue
            emitted_sections += 1
            sections.extend(["", f"### 2.{emitted_sections} {task.title}"])
            for claim, refs in claims:
                citation_numbers: list[int] = []
                for ref in refs:
                    if ref not in reference_numbers:
                        reference_numbers[ref] = len(references) + 1
                        references.append(ref)
                    citation_numbers.append(reference_numbers[ref])
                citations = "".join(f"[{number}]" for number in citation_numbers)
                claim_text = claim.rstrip("。.!！？ ")
                sections.extend(["", f"{claim_text}{citations}。"])
        if not emitted_sections:
            sections.extend(
                [
                    "",
                    "### 2.1 当前证据状态",
                    "",
                    "当前没有可使用的逐项证据条目。",
                ]
            )
        sections.extend(["", "## 3 讨论与证据缺口"])
        if gap_titles:
            sections.extend(
                item
                for index, gap_title in enumerate(
                    dict.fromkeys(gap_titles),
                    start=1,
                )
                for item in (
                    "",
                    f"（{index}）**{gap_title}**：未检索到可用于该维度的逐项证据条目。",
                )
            )
        else:
            sections.extend(["", "当前规划主题均形成了至少一项逐项来源证据。"])
        sections.extend(
            [
                "",
                "## 4 结论",
                "",
                "本文仅收录已完成逐项来源绑定的证据条目；研究结果部分给出可核验内容，"
                "讨论部分保留当前证据边界，后续更新需在补充直接来源后纳入正文。",
            ]
        )
        if references:
            sections.extend(["", "## 参考文献"])
            for index, ref in enumerate(references, start=1):
                sections.extend(["", f"[{index}] {ref}"])
        return "\n".join(sections).strip()

    @staticmethod
    def _build_fallback_report(
        *,
        request: AgentRequest,
        planner: CoordinationUnitResult,
        tasks: list[ResearchTask],
        branch_results: list[CoordinationUnitResult],
    ) -> str:
        sections = [
            f"# 深度研究报告：{request.question.strip()}",
            "",
            "## 研究说明",
            "最终写作智能体未返回完整内容，以下报告由规划结果和各研究分支的有效证据自动汇编。",
        ]
        if planner.answer:
            sections.extend(["", "## 研究计划", planner.answer])
        keyed_results = {
            result.task_key: result for result in branch_results if result.task_key
        }
        unkeyed_results = iter(
            result for result in branch_results if not result.task_key
        )
        for task in tasks:
            result = keyed_results.get(task.key)
            if result is None:
                result = next(unkeyed_results, None)
            if result is None:
                body = "该研究分支尚未完成或未返回有效内容，相关结论需保持不确定表述。"
            else:
                body = (
                    result.answer
                    or f"该研究分支未返回有效内容。{result.error or ''}".strip()
                )
            sections.extend(["", f"## {task.title}", body])
        sections.extend(
            [
                "",
                "## 结论与证据缺口",
                "请结合上述分支结论交叉核验引用；未被检索证据支持的内容不应视为确定事实。",
            ]
        )
        return "\n".join(sections).strip()

    @staticmethod
    def _format_evidence_matrix(
        branch_results: list[CoordinationUnitResult],
        *,
        max_chars: int = 24000,
    ) -> str:
        return format_evidence_matrix(branch_results, max_chars=max_chars)

    @staticmethod
    def _claim_fingerprint(text: str) -> set[str]:
        return claim_fingerprint(text)

    @staticmethod
    def _answer_claim_candidates(answer: str) -> list[str]:
        return answer_claim_candidates(answer)

    @staticmethod
    def _verify_answer_against_matrix(
        answer: str,
        branch_results: list[CoordinationUnitResult],
    ) -> str:
        return verify_answer(answer, branch_results)
