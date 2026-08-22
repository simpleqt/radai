"""声明-来源台账、图谱与 Web/子代理证据契约（拆自 test_evidence_matrix.py）。"""

from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
    CoordinationUnitResult,
    ResearchTask,
)
from backend.application.agent_workflows.coordination_handoff import (
    CoordinationHandoffService,
)
from backend.application.agent_workflows.evidence_quality import (
    build_claim_source_graph,
    canonical_source_identity,
)
from backend.application.agent_workflows.evidence_verifier import (
    CitationVerifier,
    analyze_answer_support,
)
from backend.application.agent_workflows.report_convergence_builder import (
    ReportConvergenceBuilder,
)
from backend.application.agent_workflows.research_evidence_ledger import (
    build_coverage_ledger,
    format_compact_evidence_ledger,
)
from backend.core.agents.context.ledger import make_evidence_record
from backend.core.agents.context.subagent_adapter import build_subagent_result
from backend.domain.agents.request import AgentRequest


def test_compact_evidence_ledger_deduplicates_sources_and_excludes_raw_summary():
    source = "https://www.example.com/report?utm_source=test#section"
    branch = CoordinationUnitResult(
        role="evidence_researcher",
        answer="raw branch answer must not reach writer",
        task_key="t1",
        task_title="性能",
        handoff={
            "summary": "uncited summary must not reach writer",
            "evidence_refs": [source],
            "claims": [
                {
                    "claim": "高纯锗探测器具有较高能量分辨率",
                    "supporting_refs": [source, "https://example.com/report"],
                    "confidence": 0.9,
                },
                {
                    "claim": "没有来源的结论",
                    "supporting_refs": [],
                    "confidence": 0.8,
                },
            ],
        },
    )

    rendered = format_compact_evidence_ledger([branch])

    assert "高纯锗探测器具有较高能量分辨率" in rendered
    assert "没有来源的结论" not in rendered
    assert "uncited summary" not in rendered
    assert rendered.count('"source_id"') == 1
    assert canonical_source_identity(source) == "https://example.com/report"
    assert (
        canonical_source_identity("https://example.com/report?id=17&utm_source=x")
        == "https://example.com/report?id=17"
    )


def test_coverage_ledger_tracks_degraded_dimensions_and_source_novelty():
    tasks = [
        ResearchTask("t1", "性能", "q1"),
        ResearchTask("t2", "局限", "q2"),
    ]
    source = "https://example.com/report"
    results = [
        CoordinationUnitResult(
            role="evidence_researcher",
            answer="supported",
            task_key="t1",
            handoff={
                "claims": [
                    {
                        "claim": "supported",
                        "supporting_refs": [source],
                        "confidence": 0.9,
                    }
                ]
            },
        ),
        CoordinationUnitResult(
            role="risk_reviewer",
            answer="failed",
            task_key="t2",
            error="timeout",
        ),
    ]
    report = CitationVerifier().verify_handoffs(results)

    ledger = build_coverage_ledger(tasks, results, report)
    repeated = build_coverage_ledger(
        tasks,
        results,
        report,
        previous_source_ids=frozenset({source}),
    )

    assert ledger.coverage_score == 0.5
    assert ledger.covered_dimensions == ("性能",)
    assert ledger.unresolved_dimensions == ("局限",)
    assert ledger.novel_source_ratio == 1.0
    assert repeated.novel_source_ratio == 0.0


def test_citation_report_exposes_retrieval_to_claim_extraction_yield():
    branch = CoordinationUnitResult(
        role="evidence_researcher",
        answer="supported",
        captured_evidence_refs=tuple(
            f"https://example.com/source-{index}" for index in range(4)
        ),
        handoff={
            "evidence_refs": ["https://example.com/source-0"],
            "claims": [
                {
                    "claim": "一个逐项绑定来源的结论",
                    "supporting_refs": ["https://example.com/source-0"],
                    "confidence": 0.9,
                }
            ],
        },
    )

    report = CitationVerifier().verify_handoffs([branch])

    assert report.captured_evidence_refs == 4
    assert report.independent_sources == 1
    assert report.to_dict()["extraction_yield"] == 0.25


def test_evidence_only_report_eliminates_unsupported_writer_content():
    source = "https://example.edu/neutron-detector?id=17&utm_source=search"
    tasks = [
        ResearchTask("materials", "新型探测材料", "核验材料性能"),
        ResearchTask("ai", "AI信号处理", "核验AI进展"),
    ]
    branches = [
        CoordinationUnitResult(
            role="evidence_researcher",
            answer="",
            task_key="materials",
            task_title="新型探测材料",
            handoff={
                "claims": [
                    {
                        "claim": "实验结果显示该材料的热中子探测效率达到 31.3%",
                        "supporting_refs": [source],
                        "confidence": 0.9,
                    }
                ]
            },
        ),
        CoordinationUnitResult(
            role="evidence_researcher",
            answer="模型扩写出的无来源AI结论",
            task_key="ai",
            task_title="AI信号处理",
            handoff={"claims": []},
        ),
    ]
    request = AgentRequest(
        question="分析中子探测器最新技术",
        agent_code="ne",
        user_id="u",
    )

    report = ReportConvergenceBuilder._build_evidence_only_report(
        request=request,
        tasks=tasks,
        branch_results=branches,
    )
    diagnostics = analyze_answer_support(report, branches)
    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(report, branches)

    assert report.startswith("# 分析中子探测器最新技术")
    assert "## 摘要" in report
    assert "## 1 研究范围与方法" in report
    assert "## 2 研究结果" in report
    assert "## 3 讨论与证据缺口" in report
    assert "## 4 结论" in report
    assert "## 参考文献" in report
    assert "31.3%" in report
    assert "31.3%[1]" in report
    assert source in report
    assert report.count(source) == 1
    assert f"[1] {source}" in report
    assert "模型扩写出的无来源AI结论" not in report
    assert "AI信号处理**：未检索到可用于该维度的逐项证据条目" in report
    assert diagnostics.unsupported_claims == 0
    assert diagnostics.unsupported_references == 0
    assert verified == report
    assert "待核验" not in report


def test_claim_source_graph_measures_corroboration_and_authority() -> None:
    claim = "高纯锗探测器具有较高能量分辨率"
    results = [
        CoordinationUnitResult(
            role="evidence_researcher",
            answer=claim,
            task_key="kb",
            handoff={
                "claims": [
                    {
                        "claim": claim,
                        "supporting_refs": ["[HPGe手册#chunk-1]"],
                        "confidence": 0.9,
                    }
                ]
            },
        ),
        CoordinationUnitResult(
            role="risk_reviewer",
            answer=claim,
            task_key="official",
            handoff={
                "claims": [
                    {
                        "claim": claim,
                        "supporting_refs": ["https://physics.example.edu/hpge"],
                        "confidence": 0.8,
                    }
                ]
            },
        ),
    ]

    quality = build_claim_source_graph(results)
    citation = CitationVerifier().verify_handoffs(results)

    assert quality.claim_count == 1
    assert quality.source_count == 2
    assert quality.multi_source_claims == 1
    assert quality.corroboration_rate == 1.0
    assert quality.high_authority_sources == 2
    assert quality.semantic_review_candidates == 0
    assert citation.corroboration_rate == 1.0
    assert citation.high_authority_source_ratio == 1.0


def test_claim_source_graph_detects_numeric_conflicts() -> None:
    results = [
        CoordinationUnitResult(
            role="data_analyst",
            answer="a",
            task_key="a",
            handoff={
                "claims": [
                    {
                        "claim": "该探测器的能量分辨率为 1.0%",
                        "supporting_refs": ["https://a.example/report"],
                        "confidence": 0.9,
                    }
                ]
            },
        ),
        CoordinationUnitResult(
            role="evidence_researcher",
            answer="b",
            task_key="b",
            handoff={
                "claims": [
                    {
                        "claim": "该探测器的能量分辨率为 2.0%",
                        "supporting_refs": ["https://b.example/report"],
                        "confidence": 0.9,
                    }
                ]
            },
        ),
    ]

    quality = build_claim_source_graph(results)
    rendered = format_compact_evidence_ledger(results)

    assert quality.claim_count == 2
    assert quality.conflicted_claims == 2
    assert quality.conflicts[0].reason == "numeric_mismatch"
    assert quality.semantic_review_candidates == 2
    assert '"detected_conflicts"' in rendered
    assert '"reason":"numeric_mismatch"' in rendered


def test_answer_support_diagnostics_separates_numeric_conflict() -> None:
    source = "https://example.com/source"
    branch = CoordinationUnitResult(
        role="data_analyst",
        answer="evidence",
        handoff={
            "claims": [
                {
                    "claim": "该探测器的能量分辨率为 1.0%",
                    "supporting_refs": [source],
                    "confidence": 0.9,
                }
            ]
        },
    )

    diagnostics = analyze_answer_support(
        f"研究结果表明，该探测器的能量分辨率为 2.0%。[来源]({source})",
        [branch],
    )

    assert diagnostics.candidate_claims == 1
    assert diagnostics.unsupported_claims == 1
    assert diagnostics.numeric_conflicts == 1
    assert diagnostics.ambiguous_claims == 0


def test_local_capture_artifact_uses_document_chunk_as_canonical_reference():
    record = {
        "source_id": "kb-source-1",
        "doc_title": "污染水文地质学原理",
        "chunk_id": "fragment-5",
        "raw_ref": "artifact://context/thread/tool-raw.txt",
    }

    canonical = CoordinationHandoffService.evidence_record_ref(record)
    allowed = CoordinationHandoffService.captured_evidence_ref_map([], [record])

    assert canonical == "[污染水文地质学原理#fragment-5]"
    assert (
        CoordinationHandoffService.resolve_captured_ref("kb-source-1", allowed)
        == canonical
    )


def test_subagent_claims_map_to_matching_raw_chunk_only():
    result = build_subagent_result(
        role="researcher",
        objective="比较两个标准",
        answer="结论：标准 A 的限值为 17 mg/L。\n结论：标准 B 的限值为 25 mg/L。",
        evidence_records=[
            make_evidence_record(
                source_tool="multi_kb_search",
                source_id="kb-a",
                chunk_id="a1",
                doc_title="doc-a",
                content="标准 A 的限值为 17 mg/L。",
                category="retrieval",
            ),
            make_evidence_record(
                source_tool="multi_kb_search",
                source_id="kb-b",
                chunk_id="b1",
                doc_title="doc-b",
                content="标准 B 的限值为 25 mg/L。",
                category="retrieval",
            ),
        ],
    )

    assert result["claims"][0]["supporting_refs"] == ["[doc-a#a1]"]
    assert result["claims"][1]["supporting_refs"] == ["[doc-b#b1]"]


def test_dynamic_web_results_become_claim_level_evidence():
    url = "https://example.com/msnd-study"
    refs, records = CoordinatedAgentWorkflow._collect_dynamic_web_evidence(
        [
            {
                "kind": "tool_result",
                "tool": "web_search",
                "level": "info",
                "metadata": {
                    "engine": "firecrawl",
                    "urls": [url],
                    "web_results": [
                        {
                            "title": "Microstructured semiconductor neutron detector",
                            "url": url,
                            "domain": "example.com",
                            "content_excerpt": (
                                "The optimized microstructured semiconductor neutron "
                                "detector reaches 31.3% thermal neutron efficiency."
                            ),
                        }
                    ],
                },
            }
        ]
    )
    handoff = build_subagent_result(
        role="evidence_researcher",
        objective="核验 MSND 性能",
        answer=(
            "结论：The optimized microstructured semiconductor neutron detector "
            "reaches 31.3% thermal neutron efficiency."
        ),
        evidence_refs=refs,
        evidence_records=records,
    )

    assert refs == [url]
    assert records[0]["raw_ref"] == url
    assert handoff["claims"][0]["supporting_refs"] == [url]
    branch = CoordinationUnitResult(
        role="evidence_researcher",
        answer=handoff["summary"],
        handoff=handoff,
    )
    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(
        (
            "结论：The optimized microstructured semiconductor neutron detector "
            f"reaches 31.3% thermal neutron efficiency. {url}"
        ),
        [branch],
    )

    assert CitationVerifier().verify_handoffs([branch]).claims_with_refs == 1
    assert "待核验" not in verified


def test_supported_markdown_web_link_is_not_misparsed_or_downgraded():
    url = "https://news.tongji.edu.cn/info/1003/94932.htm"
    claim = "同济大学团队在高熵氟化物超快无机闪烁晶体领域取得突破性成果"
    branch = CoordinationUnitResult(
        role="evidence_researcher",
        answer=claim,
        handoff={
            "evidence_refs": [url],
            "claims": [
                {
                    "claim": claim,
                    "supporting_refs": [url],
                    "confidence": 0.9,
                }
            ],
        },
    )
    answer = f"{claim}。[同济大学新闻]({url})"

    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(
        answer,
        [branch],
    )

    assert verified == answer
    assert "待核验" not in verified
    assert f"]({url})" in verified


def test_supported_web_link_tracking_variant_is_not_removed():
    captured_url = "https://www.example.org/paper?id=17&utm_source=search"
    writer_url = "https://example.org/paper?id=17#results"
    claim = "研究结果显示该探测器的热中子效率得到提升"
    branch = CoordinationUnitResult(
        role="evidence_researcher",
        answer=claim,
        handoff={
            "evidence_refs": [captured_url],
            "claims": [
                {
                    "claim": claim,
                    "supporting_refs": [captured_url],
                    "confidence": 0.9,
                }
            ],
        },
    )
    answer = f"{claim}。[研究页面]({writer_url})"

    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(answer, [branch])

    assert verified == answer


def test_supported_source_only_line_is_not_treated_as_a_claim():
    url = "https://news.sciencenet.cn/htmlnews/2026/6/566983.shtm"
    claim = "双体系闪烁体通过复合结构设计拓宽了辐射无损检测应用路径"
    branch = CoordinationUnitResult(
        role="evidence_researcher",
        answer=claim,
        handoff={
            "evidence_refs": [url],
            "claims": [
                {
                    "claim": claim,
                    "supporting_refs": [url],
                    "confidence": 0.85,
                }
            ],
        },
    )
    answer = f"{claim}。\n\n来源：[{url}]({url})"

    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(
        answer,
        [branch],
    )

    assert verified == answer
    assert "当前证据矩阵未提供充分支持" not in verified


def test_unsupported_claim_warning_does_not_become_part_of_url():
    url = "https://example.com/verified-source"
    branch = CoordinationUnitResult(
        role="evidence_researcher",
        answer="已核验的中子效率为 31.3%",
        handoff={
            "evidence_refs": [url],
            "claims": [
                {
                    "claim": "已核验的中子效率为 31.3%",
                    "supporting_refs": [url],
                    "confidence": 0.9,
                }
            ],
        },
    )
    unsupported = f"未经支持的材料结论声称性能显著提升，来源：{url}"

    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(
        unsupported,
        [branch],
    )

    assert "待核验：当前证据矩阵未提供充分支持；" in verified
    assert url in verified
    assert f"{url}（" not in verified
    assert "%EF%BC%88" not in verified


def test_subagent_footnote_definition_preserves_claim_url():
    url = "https://example.com/source"
    handoff = build_subagent_result(
        role="evidence_researcher",
        objective="核验效率",
        answer=(
            "结论：该探测器的热中子效率达到 31.3%[^1]。\n\n" f"[^1]: 实验研究，{url}"
        ),
    )

    assert handoff["claims"][0]["supporting_refs"] == [url]
