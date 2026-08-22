"""证据矩阵校验器与引用降级契约（拆自 test_evidence_matrix.py）。"""

from backend.application.agent_workflows.coordinated_agent_workflow import (
    CoordinatedAgentWorkflow,
    CoordinationUnitResult,
)
from backend.application.agent_workflows.evidence_verifier import (
    CitationVerifier,
    analyze_answer_support,
)


def test_paper_style_numbered_citation_resolves_to_authorized_source():
    source = "https://example.edu/neutron-detector?id=17&utm_source=search"
    claim = "实验结果显示该材料的热中子探测效率达到 31.3%"
    branch = CoordinationUnitResult(
        role="evidence_researcher",
        answer="",
        handoff={
            "claims": [
                {
                    "claim": claim,
                    "supporting_refs": [source],
                    "confidence": 0.9,
                }
            ]
        },
    )
    answer = f"# 研究报告\n\n## 研究结果\n\n{claim}[1]。\n\n## 参考文献\n\n[1] {source}"

    diagnostics = analyze_answer_support(answer, [branch])
    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(answer, [branch])

    assert diagnostics.candidate_claims == 1
    assert diagnostics.supported_claims == 1
    assert diagnostics.unsupported_claims == 0
    assert diagnostics.unsupported_references == 0
    assert verified == answer


def test_paper_style_numbered_citation_rejects_invented_bibliography_source():
    source = "https://example.edu/authorized"
    invented = "https://invented.example/report"
    claim = "研究结果显示该探测器具备稳定响应"
    branch = CoordinationUnitResult(
        role="evidence_researcher",
        answer="",
        handoff={
            "claims": [
                {
                    "claim": claim,
                    "supporting_refs": [source],
                    "confidence": 0.9,
                }
            ]
        },
    )
    answer = (
        f"# 研究报告\n\n## 研究结果\n\n{claim}[1]。\n\n## 参考文献\n\n[1] {invented}"
    )

    diagnostics = analyze_answer_support(answer, [branch])

    assert diagnostics.unsupported_claims == 1
    assert diagnostics.reference_conflicts == 1
    assert diagnostics.unsupported_references == 1


def test_evidence_matrix_verifier_flags_unsupported_refs():
    branch = CoordinationUnitResult(
        role="researcher_1",
        answer="",
        handoff={
            "summary": "标准 A 有证据",
            "evidence_refs": ["[doc-a]"],
            "claims": [
                {
                    "claim": "标准 A 支持该结论",
                    "supporting_refs": ["[doc-a]"],
                    "confidence": 0.8,
                    "conflicts": [],
                    "missing": [],
                }
            ],
        },
    )

    matrix = CoordinatedAgentWorkflow._format_evidence_matrix([branch])
    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(
        "最终结论引用 [doc-b]",
        [branch],
    )

    assert "<evidence_matrix>" in matrix
    assert "标准 A 支持该结论" in matrix
    assert "已移除 1 个" in verified
    assert "[doc-b]" not in verified


def test_evidence_matrix_verifier_flags_unsupported_claims_with_supported_ref():
    branch = CoordinationUnitResult(
        role="researcher_1",
        answer="",
        handoff={
            "summary": "标准 A 有证据",
            "evidence_refs": ["[doc-a]"],
            "claims": [
                {
                    "claim": "标准 A 支持该结论",
                    "supporting_refs": ["[doc-a]"],
                    "confidence": 0.8,
                    "conflicts": [],
                    "missing": [],
                }
            ],
        },
    )

    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(
        "地下水限值为 999 mg/L [doc-a]",
        [branch],
    )

    assert "证据核验提示" in verified
    assert "关键结论" in verified
    assert "999 mg/L" in verified
    assert "待核验：" in verified


def test_evidence_matrix_verifier_preserves_markdown_tables():
    branch = CoordinationUnitResult(
        role="researcher_1",
        answer="",
        handoff={
            "evidence_refs": ["[project-db]"],
            "claims": [
                {
                    "claim": "GW25 地下水埋深为 6.28 m",
                    "supporting_refs": ["[project-db]"],
                    "confidence": 0.9,
                }
            ],
        },
    )
    table = "| 点位 | 地下水埋深(m) |\n" "|---|---:|\n" "| GW25 | 6.28 |"

    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(table, [branch])

    assert verified.startswith(table)
    assert "待核验：|" not in verified


def test_citation_verifier_reports_missing_supporting_refs():
    branch = CoordinationUnitResult(
        role="researcher_1",
        answer="",
        handoff={
            "claims": [
                {"claim": "标准 A 有效", "supporting_refs": ["kb:1"]},
                {"claim": "标准 B 有效", "supporting_refs": []},
            ],
            "evidence_refs": ["kb:1"],
        },
    )

    report = CitationVerifier().verify_handoffs([branch])

    assert report.total_claims == 2
    assert report.claims_with_refs == 1
    assert not report.strict_pass
    assert report.warnings


def test_branch_level_reference_does_not_authorize_an_unlinked_claim():
    url = "https://example.com/source"
    claim = "该材料的中子探测效率显著提高"
    branch = CoordinationUnitResult(
        role="evidence_researcher",
        answer=claim,
        handoff={
            "evidence_refs": [url],
            "claims": [
                {
                    "claim": claim,
                    "supporting_refs": [],
                    "confidence": 0.8,
                }
            ],
        },
    )

    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(
        f"{claim}。来源：{url}",
        [branch],
    )

    assert "待核验：当前证据矩阵未提供充分支持；" in verified


def test_evidence_matrix_tolerates_malformed_model_confidence() -> None:
    branch = CoordinationUnitResult(
        role="researcher_1",
        answer="",
        handoff={
            "claims": [
                {
                    "claim": "标准 A 有效",
                    "supporting_refs": ["kb:1"],
                    "confidence": "high",
                }
            ],
            "evidence_refs": ["kb:1"],
        },
    )

    report = CitationVerifier().verify_handoffs([branch])
    matrix = CoordinatedAgentWorkflow._format_evidence_matrix([branch])

    assert report.total_claims == 1
    assert report.low_confidence_claims == 0
    assert "confidence: 0.50" in matrix


def test_failed_branch_cannot_authorize_final_claim_or_citation():
    branch = CoordinationUnitResult(
        role="evidence_researcher",
        answer="partial result",
        error="execution timed out after 180s",
        handoff={
            "evidence_refs": ["https://example.com/source"],
            "claims": [
                {
                    "claim": "超时前形成的未完成结论",
                    "supporting_refs": ["https://example.com/source"],
                    "confidence": 0.8,
                }
            ],
        },
    )

    report = CitationVerifier().verify_handoffs([branch])
    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(
        "结论：超时前形成的未完成结论 https://example.com/source",
        [branch],
    )

    assert report.failed_branches == 1
    assert report.successful_branches == 0
    assert report.independent_sources == 0
    assert report.total_claims == 0
    assert report.strict_pass is False
    assert "https://example.com/source" not in verified
    assert "当前研究分支没有形成可核验的证据矩阵" in verified


def test_verifier_removes_invalid_bibliography_entries_without_marker_wall():
    supported = "https://example.com/supported"
    unsupported = "https://example.com/invented"
    branch = CoordinationUnitResult(
        role="researcher_1",
        answer="",
        handoff={
            "evidence_refs": [supported],
            "claims": [
                {
                    "claim": "研究表明标准 A 的限值为 17 mg/L",
                    "supporting_refs": [supported],
                    "confidence": 0.9,
                }
            ],
        },
    )
    answer = "\n".join(
        (
            "研究表明标准 A 的限值为 17 mg/L。[1][2]",
            "",
            "## 参考文献",
            f"[1] {supported}",
            f"[2] {unsupported}",
        )
    )

    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(answer, [branch])

    assert f"[1] {supported}" in verified
    assert unsupported not in verified
    assert "[2]" not in verified
    assert "未核验来源" not in verified
    assert "已移除 1 个" in verified


def test_evidence_matrix_rejects_reference_from_a_different_claim():
    branch = CoordinationUnitResult(
        role="researcher_1",
        answer="",
        handoff={
            "summary": "两项标准",
            "evidence_refs": ["[doc-a]", "[doc-b]"],
            "claims": [
                {
                    "claim": "标准 A 的限值为 17 mg/L",
                    "supporting_refs": ["[doc-a]"],
                    "confidence": 0.9,
                    "conflicts": [],
                    "missing": [],
                },
                {
                    "claim": "标准 B 的限值为 25 mg/L",
                    "supporting_refs": ["[doc-b]"],
                    "confidence": 0.9,
                    "conflicts": [],
                    "missing": [],
                },
            ],
        },
    )

    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(
        "结论：标准 A 的限值为 17 mg/L [doc-b]。",
        [branch],
    )

    assert "待核验：" in verified


def test_evidence_matrix_verifies_qualitative_factual_statements():
    """Soft narrative without numbers/refs/markers is not sentence-downgraded."""

    branch = CoordinationUnitResult(
        role="researcher_1",
        answer="",
        handoff={
            "evidence_refs": ["[doc-a]"],
            "claims": [
                {
                    "claim": "标准 A 的限值为 17 mg/L",
                    "supporting_refs": ["[doc-a]"],
                    "confidence": 0.9,
                }
            ],
        },
    )

    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(
        "苯具有致癌性并会造成长期健康影响。",
        [branch],
    )

    # Background prose is left readable; only explicit claims are checked.
    assert verified == "苯具有致癌性并会造成长期健康影响。"
    assert "待核验：" not in verified


def test_unsupported_explicit_claim_without_matrix_match_is_downgraded():
    branch = CoordinationUnitResult(
        role="researcher_1",
        answer="",
        handoff={
            "evidence_refs": ["[doc-a]"],
            "claims": [
                {
                    "claim": "标准 A 的限值为 17 mg/L",
                    "supporting_refs": ["[doc-a]"],
                    "confidence": 0.9,
                }
            ],
        },
    )

    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(
        "研究表明苯的职业暴露限值应为 999 mg/L。",
        [branch],
    )

    assert "待核验：当前证据矩阵未提供充分支持；" in verified
    assert "999 mg/L" in verified


def test_verifier_collapses_pending_verification_wall():
    url = "https://example.com/source"
    branch = CoordinationUnitResult(
        role="evidence_researcher",
        answer="捕获证据支持该探测器用于热中子测量",
        handoff={
            "evidence_refs": [url],
            "claims": [
                {
                    "claim": "捕获证据支持该探测器用于热中子测量",
                    "supporting_refs": [url],
                    "confidence": 0.9,
                }
            ],
        },
    )
    answer = "\n".join(
        [
            "# 中子探测器进展",
            "",
            "研究表明路线 A 的热中子效率达到 11.1%。",
            "研究表明路线 B 的热中子效率达到 22.2%。",
            "研究表明路线 C 的热中子效率达到 33.3%。",
            "研究表明路线 D 的热中子效率达到 44.4%。",
            "研究表明路线 E 的热中子效率达到 55.5%。",
            f"捕获证据支持该探测器用于热中子测量。[来源]({url})",
        ]
    )

    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(answer, [branch])

    assert verified.count("待核验：当前证据矩阵未提供充分支持；研究表明") == 0
    assert "改为文档级提示" in verified or "待核验墙" in verified
    assert "捕获证据支持该探测器用于热中子测量" in verified
    assert verified.lstrip().startswith("# 中子探测器进展")
    assert verified.count("待核验：当前证据矩阵未提供充分支持；") == 1
    assert "> 证据核验提示：" not in verified


def test_verifier_accepts_one_sentence_synthesized_from_multiple_claims():
    first_url = "https://example.com/efficiency"
    second_url = "https://example.com/resolution"
    branch = CoordinationUnitResult(
        role="evidence_researcher",
        answer="",
        handoff={
            "evidence_refs": [first_url, second_url],
            "claims": [
                {
                    "claim": "该探测器的探测效率达到 31.3%",
                    "supporting_refs": [first_url],
                    "confidence": 0.9,
                },
                {
                    "claim": "该探测器的能量分辨率为 1.8%",
                    "supporting_refs": [second_url],
                    "confidence": 0.9,
                },
            ],
        },
    )
    answer = (
        "研究表明，该探测器的探测效率达到 31.3%，能量分辨率为 1.8%。"
        f"[效率来源]({first_url}) [分辨率来源]({second_url})"
    )

    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(answer, [branch])

    assert verified == answer
    assert "待核验" not in verified


def test_verifier_does_not_flag_year_only_numeric_mismatch():
    url = "https://www.tsinghua.edu.cn/info/1175/103685.htm"
    claim = "清华大学材料学院在 LiInP2Se6 单晶半导体中子探测晶体研发领域取得进展"
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
    answer = (
        f"研究表明，2025 年清华大学材料学院在 LiInP2Se6 单晶半导体中子探测晶体"
        f"研发领域取得进展。[来源]({url})"
    )

    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(answer, [branch])

    assert verified == answer
    assert "待核验" not in verified


def test_verifier_does_not_downgrade_references_recommendations_or_diagnostics():
    url = "https://example.com/source"
    claim = "捕获证据支持该探测器用于热中子测量"
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
    answer = (
        f"# 核心结论\n\n{claim}。[来源]({url})\n\n"
        "## 建议\n\n建议安排 3 轮独立复核。\n\n"
        "## 证据质量\n\n引用覆盖率为 80%，总结论数为 10。\n\n"
        f"## 参考文献\n\n- [来源]({url})"
    )

    verified = CoordinatedAgentWorkflow._verify_answer_against_matrix(
        answer,
        [branch],
    )

    assert verified == answer
    assert "待核验" not in verified
