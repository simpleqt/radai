"""Evidence-matrix rendering and deterministic answer verification."""

from __future__ import annotations

from backend.application.agent_workflows.evidence_verifier_models import (  # noqa: F401  (兼容 re-export)
    AnswerVerificationDiagnostics,
    CitationVerificationReport,
    EvidenceCarrier,
    _AnswerSupportAnalysis,
    _BRACKET_REF_RE,
    _MARKDOWN_LINK_RE,
    _URL_RE,
    _source_key,
    claim_fingerprint,
    format_evidence_matrix,
    normalize_confidence,
)
from backend.application.agent_workflows.evidence_verifier_claims import (  # noqa: F401  (兼容 re-export)
    _CLAIM_OVERLAP_THRESHOLD,
    _MAX_INLINE_DOWNGRADES,
    _SEMANTIC_REVIEW_OVERLAP_THRESHOLD,
    _WALL_RATIO_THRESHOLD,
    _analyze_answer_support,
    _attach_document_warning,
    _collect_authorized_claims,
    _factual_numbers,
    _negations,
    _numbers,
    analyze_answer_support,
    answer_claim_candidates,
    verify_answer,
)
from backend.application.agent_workflows.evidence_verifier_citations import (  # noqa: F401  (兼容 re-export)
    CitationVerifier,
    _normalize_ref,
    _numbered_citation_aliases,
    _refs,
    _replace_unsupported_refs,
)
