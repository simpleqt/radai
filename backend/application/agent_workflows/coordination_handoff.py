"""Evidence-bound handoff preparation for coordinated workflows."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from backend.application.agent_workflows.coordination_types import (
    CoordinationUnitResult,
)
from backend.application.agent_workflows.evidence_verifier import normalize_confidence
from backend.application.agent_workflows.evidence_quality import (
    canonical_source_identity,
)
from backend.core.agents.context.ledger import make_evidence_record
from backend.core.agents.context.subagent_adapter import normalize_subagent_result
from backend.domain.agents.events import AgentEvent


class CoordinationHandoffService:
    """Enforce the retrieval-to-synthesis trust boundary in one component."""

    _MARKDOWN_URL_RE = re.compile(r"\[[^\]]+\]\((https?://[^\s]+)\)")
    _RAW_URL_RE = re.compile(r"https?://[^\s<>\[\]\"'，。；、]+")

    @classmethod
    def _clean_ref(cls, value: Any) -> str:
        return str(value or "").strip().rstrip(".,;:，。；：）")

    @classmethod
    def _urls_in_ref(cls, value: Any) -> list[str]:
        text = str(value or "").strip()
        urls = [match.group(1) for match in cls._MARKDOWN_URL_RE.finditer(text)]
        urls.extend(match.group(0) for match in cls._RAW_URL_RE.finditer(text))
        return list(
            dict.fromkeys(cls._clean_ref(url).rstrip(")") for url in urls if url)
        )

    @staticmethod
    def _url_locator_key(value: str) -> str:
        """Return a scheme/tracking-insensitive key for one exact URL document."""

        try:
            canonical = canonical_source_identity(value)
            parts = urlsplit(canonical)
            port = parts.port
        except ValueError:
            return ""
        host = (parts.hostname or "").casefold().removeprefix("www.")
        if not host:
            return ""
        netloc = f"{host}:{port}" if port else host
        path = re.sub(r"/{2,}", "/", parts.path or "/").rstrip("/") or "/"
        query = f"?{parts.query}" if parts.query else ""
        return f"url-location:{netloc}{path}{query}"

    @classmethod
    def _ref_aliases(cls, value: Any) -> list[str]:
        cleaned = cls._clean_ref(value)
        aliases = [cleaned] if cleaned else []
        for url in cls._urls_in_ref(cleaned):
            aliases.extend(
                (
                    url,
                    url.rstrip("/"),
                    canonical_source_identity(url),
                    cls._url_locator_key(url),
                )
            )
        if cleaned.startswith(("http://", "https://")):
            aliases.extend(
                (
                    cleaned.rstrip("/"),
                    canonical_source_identity(cleaned),
                    cls._url_locator_key(cleaned),
                )
            )
        return list(dict.fromkeys(alias for alias in aliases if alias))

    @staticmethod
    def evidence_record_ref(record: dict[str, Any]) -> str:
        raw_ref = str(record.get("raw_ref") or "").strip()
        if raw_ref:
            return raw_ref.rstrip(").,;:，。；：）")
        title = str(
            record.get("doc_title")
            or record.get("title")
            or record.get("source_id")
            or ""
        ).strip()
        chunk_id = str(record.get("chunk_id") or "").strip()
        if title and chunk_id:
            return f"[{title}#{chunk_id}]"
        return f"[{title}]" if title else ""

    @classmethod
    def captured_evidence_ref_map(
        cls,
        evidence_refs: list[str],
        evidence_records: list[dict[str, Any]],
    ) -> dict[str, str]:
        """Return the exact source identifiers a model is allowed to cite."""

        allowed: dict[str, str] = {}
        candidates = [str(ref) for ref in evidence_refs]
        candidates.extend(
            cls.evidence_record_ref(record)
            for record in evidence_records
            if isinstance(record, dict)
        )
        for candidate in candidates:
            normalized = cls._clean_ref(candidate)
            if not normalized:
                continue
            urls = cls._urls_in_ref(normalized)
            canonical = urls[0] if urls else normalized
            for alias in cls._ref_aliases(normalized):
                allowed.setdefault(alias, canonical)
        return allowed

    @staticmethod
    def resolve_captured_ref(
        raw_ref: str,
        allowed_refs: dict[str, str],
    ) -> str | None:
        """Map a model citation onto a captured source identifier."""

        key = CoordinationHandoffService._clean_ref(raw_ref)
        if not key:
            return None
        for alias in CoordinationHandoffService._ref_aliases(key):
            if alias in allowed_refs:
                return allowed_refs[alias]
        if key.startswith(
            ("http://", "https://")
        ) or CoordinationHandoffService._urls_in_ref(key):
            return None
        if key.startswith("[") and key.endswith("]") and "#" not in key:
            title = key[1:-1].strip()
            if not title:
                return None
            prefix = f"[{title}#"
            for allowed_key, canonical in allowed_refs.items():
                if allowed_key.startswith(prefix) or allowed_key == f"[{title}]":
                    return canonical
        return None

    @staticmethod
    def parse_structured_payload(payload: Any) -> dict[str, Any] | None:
        if isinstance(payload, dict):
            parsed = payload
        elif isinstance(payload, str):
            text = payload.strip()
            if text.startswith("```"):
                lines = text.splitlines()
                if lines and lines[0].lstrip().startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                text = "\n".join(lines).strip()
            if not text.startswith("{"):
                start = text.find("{")
                end = text.rfind("}")
                if start < 0 or end <= start:
                    return None
                text = text[start : end + 1]
            try:
                parsed = json.loads(text)
            except (TypeError, json.JSONDecodeError):
                return None
        else:
            return None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("claims"), list):
            return None
        return parsed

    @classmethod
    def sanitize_structured_handoff(
        cls,
        payload: Any,
        *,
        role: str,
        objective: str,
        answer_fallback: str,
        task_id: str,
        evidence_refs: list[str],
        evidence_records: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Normalize structured convergence output against captured sources."""

        parsed = cls.parse_structured_payload(payload)
        if parsed is None:
            return None
        normalized = normalize_subagent_result(
            parsed,
            role=role,
            objective=objective,
            answer_fallback=answer_fallback,
            task_id=task_id,
            evidence_refs=evidence_refs,
            evidence_records=evidence_records,
        )
        allowed_refs = cls.captured_evidence_ref_map(
            evidence_refs,
            evidence_records,
        )
        allowed_source_ids = {
            str(value).strip()
            for record in evidence_records
            if isinstance(record, dict)
            for value in (record.get("id"), record.get("source_id"))
            if str(value or "").strip()
        }
        source_id_refs: dict[str, str] = {}
        for record in evidence_records:
            if not isinstance(record, dict):
                continue
            record_ref = cls.resolve_captured_ref(
                cls.evidence_record_ref(record), allowed_refs
            )
            if not record_ref:
                continue
            for source_id in (record.get("id"), record.get("source_id")):
                source_key = str(source_id or "").strip()
                if source_key:
                    source_id_refs.setdefault(source_key, record_ref)
        safe_claims: list[dict[str, Any]] = []
        used_refs: list[str] = []
        dropped_claims = 0
        for raw_claim in normalized.get("claims") or []:
            claim_text = str(raw_claim.get("claim") or "").strip()
            verified_refs: list[str] = []
            candidate_refs = list(raw_claim.get("supporting_refs") or [])
            candidate_refs.extend(cls._urls_in_ref(claim_text))
            for source_id in raw_claim.get("source_ids") or []:
                source_ref = source_id_refs.get(str(source_id or "").strip())
                if source_ref:
                    candidate_refs.append(source_ref)
            for raw_ref in candidate_refs:
                key = cls._clean_ref(raw_ref)
                canonical = cls.resolve_captured_ref(key, allowed_refs)
                if canonical and canonical not in verified_refs:
                    verified_refs.append(canonical)
            if not claim_text or not verified_refs:
                dropped_claims += 1
                continue
            used_refs.extend(verified_refs)
            safe_claims.append(
                {
                    "claim": claim_text,
                    "supporting_refs": verified_refs,
                    "confidence": normalize_confidence(
                        raw_claim.get("confidence") or normalized.get("confidence")
                    ),
                    "conflicts": [
                        str(item)
                        for item in raw_claim.get("conflicts") or []
                        if str(item).strip()
                    ][:10],
                    "missing": [
                        str(item)
                        for item in raw_claim.get("missing") or []
                        if str(item).strip()
                    ][:10],
                    "source_ids": [
                        str(source_id).strip()
                        for source_id in raw_claim.get("source_ids") or []
                        if str(source_id).strip() in allowed_source_ids
                    ][:10],
                }
            )

        unique_refs = list(dict.fromkeys(used_refs))[:20]
        missing = [
            str(item) for item in normalized.get("missing") or [] if str(item).strip()
        ][:9]
        if dropped_claims:
            missing.append(
                f"{dropped_claims} 条候选结论未绑定到实际捕获来源，已从事实交接中排除。"
            )
        summary = (
            "\n".join(str(claim["claim"]) for claim in safe_claims)
            if safe_claims
            else "本分支未形成逐项绑定到实际捕获来源的结论。"
        )
        return normalize_subagent_result(
            {
                **normalized,
                "summary": summary,
                "claims": safe_claims,
                "evidence_refs": unique_refs,
                "missing": missing,
                "raw_artifact_refs": [
                    ref for ref in unique_refs if ref.startswith("artifact://")
                ],
            },
            role=role,
            objective=objective,
            answer_fallback=summary,
            task_id=task_id,
            evidence_refs=unique_refs,
            evidence_records=evidence_records,
        )

    @classmethod
    def select_convergence_evidence_records(
        cls,
        evidence_records: list[dict[str, Any]],
        *,
        max_records: int = 24,
        max_chars: int = 24000,
    ) -> list[dict[str, Any]]:
        """Select informative, source-diverse records instead of the first N."""

        ranked: list[tuple[float, int, dict[str, Any], str]] = []
        seen: set[tuple[str, str]] = set()
        tool_weight = {
            "multi_kb_search": 1.5,
            "academic_search": 1.35,
            "chat_file_search": 1.25,
            "web_search": 1.0,
        }
        for index, record in enumerate(evidence_records):
            if not isinstance(record, dict):
                continue
            content = str(record.get("content") or record.get("claim") or "").strip()
            ref = cls.evidence_record_ref(record)
            if not content or not ref:
                continue
            source_locator = str(
                record.get("raw_ref")
                or record.get("source_id")
                or record.get("doc_title")
                or ref
            ).strip()
            source_key = (
                canonical_source_identity(source_locator)
                or source_locator.casefold()
                or ref.casefold()
            )
            content_key = re.sub(r"\s+", " ", content).casefold()[:240]
            dedupe_key = (source_key, content_key)
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            try:
                score = float(record.get("score") or 0.0)
            except (TypeError, ValueError):
                score = 0.0
            try:
                reliability = float(record.get("reliability") or 0.0)
            except (TypeError, ValueError):
                reliability = 0.0
            rank = (
                tool_weight.get(str(record.get("source_tool") or ""), 0.8)
                + min(max(score, 0.0), 1.0)
                + min(max(reliability, 0.0), 1.0)
                + min(len(content), 2400) / 2400
            )
            ranked.append((rank, index, record, source_key))
        ranked.sort(key=lambda item: (-item[0], item[1]))

        # First cover as many independent sources as possible, then admit a
        # second chunk per source. This prevents early results from consuming
        # the whole convergence context.
        ordered: list[tuple[float, int, dict[str, Any], str]] = []
        for per_source_limit in (1, 2):
            counts: dict[str, int] = {}
            for item in ordered:
                counts[item[3]] = counts.get(item[3], 0) + 1
            for item in ranked:
                if item in ordered or counts.get(item[3], 0) >= per_source_limit:
                    continue
                ordered.append(item)
                counts[item[3]] = counts.get(item[3], 0) + 1
                if len(ordered) >= max_records:
                    break
            if len(ordered) >= max_records:
                break

        compact_records: list[dict[str, Any]] = []
        for _, _, record, _ in ordered:
            candidate = {
                "id": str(record.get("id") or "")[:120],
                "source_id": str(record.get("source_id") or "")[:300],
                "title": str(record.get("doc_title") or record.get("title") or "")[
                    :300
                ],
                "source": cls.evidence_record_ref(record)[:500],
                "content": str(record.get("content") or record.get("claim") or "")[
                    :1400
                ],
            }
            candidate_json = json.dumps(
                [*compact_records, candidate], ensure_ascii=False, default=str
            )
            if len(candidate_json) > max_chars:
                break
            compact_records.append(candidate)
        return compact_records

    @classmethod
    def build_tool_free_convergence_prompt(
        cls,
        *,
        objective: str,
        partial_answer: str,
        evidence_refs: list[str],
        evidence_records: list[dict[str, Any]],
    ) -> str:
        """Build a bounded evidence-only handoff for the convergence model turn."""

        compact_records = cls.select_convergence_evidence_records(evidence_records)
        evidence_json = json.dumps(
            compact_records,
            ensure_ascii=False,
            default=str,
            indent=2,
        )
        refs = "\n".join(f"- {ref}" for ref in evidence_refs[:30])
        return (
            "研究执行阶段已结束。你现在是该分支的无工具结论整理器：禁止调用任何工具，"
            "只能依据下列已捕获证据和阶段性文本生成结构化交接。"
            "只输出一个 JSON 对象，不要 Markdown 代码围栏。JSON 必须包含 summary、claims、"
            "confidence、conflicts、missing、risks、suggested_next_actions；claims 中每项必须包含 "
            "claim、supporting_refs、confidence、conflicts、missing、source_ids。"
            "每个 claim 只表达一个可独立核验的事实；supporting_refs 必须逐条复制自‘已捕获来源’"
            "或证据记录中的 source，不得新造、改写或把全部来源统一挂到每条结论。"
            "找不到直接来源的内容不要写成事实 claim，应移入 missing。"
            "summary 只能概括 claims 已覆盖的事实，不得加入 claims 中没有的数值或判断。"
            "逐一检查每条证据记录：只要记录含有与研究目标直接相关的实质事实，就至少提取一条 claim；"
            "不要把多个来源、多个性能指标或不同技术路线压缩成一条宽泛结论。claims 最多 12 条。"
            "保留真实来源 URL；不要把搜索意图当作事实。证据不足或冲突必须明确列为缺口。\n\n"
            f"研究目标：\n{objective[:8000]}\n\n"
            f"阶段性文本：\n{partial_answer[:6000] or '尚未形成阶段性文本。'}\n\n"
            f"证据记录：\n{evidence_json or '[]'}\n\n"
            f"已捕获来源：\n{refs or '无'}"
        )

    @staticmethod
    def collect_dynamic_web_evidence(
        process_events: list[dict[str, Any]],
        *,
        evidence_refs: list[str] | None = None,
        evidence_records: list[dict[str, Any]] | None = None,
    ) -> tuple[list[str], list[dict[str, Any]]]:
        """Promote successful tool results from internal events into typed evidence."""

        effective_refs = list(evidence_refs or [])
        effective_records = list(evidence_records or [])
        known_record_ids = {
            str(record.get("id") or "").strip()
            for record in effective_records
            if isinstance(record, dict) and str(record.get("id") or "").strip()
        }
        for process_event in process_events:
            if process_event.get("kind") != "tool_result" or str(
                process_event.get("level") or ""
            ).lower() in {"warning", "error"}:
                continue
            internal_records = process_event.get("_coordination_evidence_records")
            if isinstance(internal_records, list):
                for record in internal_records:
                    if not isinstance(record, dict):
                        continue
                    record_id = str(record.get("id") or "").strip()
                    if record_id and record_id not in known_record_ids:
                        effective_records.append(record)
                        known_record_ids.add(record_id)
            if str(process_event.get("tool") or "").strip() != "web_search":
                continue
            metadata = process_event.get("metadata")
            if not isinstance(metadata, dict):
                continue
            engine = str(metadata.get("engine") or "firecrawl").strip()
            captured_urls: set[str] = set()
            web_results = metadata.get("web_results")
            if isinstance(web_results, list):
                for result_index, web_result in enumerate(web_results, start=1):
                    if not isinstance(web_result, dict):
                        continue
                    url = str(web_result.get("url") or "").strip()
                    if not url.startswith(("http://", "https://")):
                        continue
                    captured_urls.add(url)
                    effective_refs.append(url)
                    title = str(web_result.get("title") or "").strip()
                    domain = str(web_result.get("domain") or "").strip()
                    excerpt = str(web_result.get("content_excerpt") or "").strip()
                    evidence_content = "\n".join(
                        part for part in (title, excerpt) if part
                    )
                    if not evidence_content:
                        continue
                    record = make_evidence_record(
                        source_tool="web_search",
                        source_id=url,
                        chunk_id=f"result-{result_index}",
                        doc_title=title or domain or url,
                        content=evidence_content,
                        category="retrieval",
                        raw_ref=url,
                        tags=[tag for tag in ("web", engine) if tag],
                    )
                    record_id = str(record.get("id") or "").strip()
                    if record_id and record_id not in known_record_ids:
                        effective_records.append(record)
                        known_record_ids.add(record_id)
            for raw_url in metadata.get("urls") or []:
                url = str(raw_url or "").strip()
                if url.startswith(("http://", "https://")) and url not in captured_urls:
                    effective_refs.append(url)
        return list(dict.fromkeys(effective_refs)), effective_records

    @staticmethod
    def task_scoped_unit_process_event(
        event: AgentEvent,
        *,
        task_id: str | None,
        role: str,
    ) -> dict[str, Any] | None:
        """Capture an isolated unit's tool trace, with task scope when present."""

        if event.type != "workflow.step":
            return None
        raw_process_event = event.payload.get("event")
        if not isinstance(raw_process_event, dict):
            return None
        kind = str(raw_process_event.get("kind") or "").strip()
        if kind not in {"tool_start", "tool_end", "tool_result"}:
            return None
        metadata = dict(raw_process_event.get("metadata") or {})
        internal_records = metadata.pop("_coordination_evidence_records", None)
        scope = "task" if task_id is not None else "unit"
        runtime_type = (
            f"{scope}_tool_start" if kind == "tool_start" else f"{scope}_tool_end"
        )
        scope_metadata = {"task_id": task_id} if task_id is not None else {}
        scoped_event = {
            **raw_process_event,
            "metadata": {
                **metadata,
                **scope_metadata,
                "subagent_type": role,
                "coordination_role": role,
                "runtime_type": runtime_type,
                "unit_event_kind": kind,
            },
        }
        if isinstance(internal_records, list) and internal_records:
            scoped_event["_coordination_evidence_records"] = internal_records
        return scoped_event

    @staticmethod
    def writer_safe_branch_results(
        branch_results: list[CoordinationUnitResult],
    ) -> list[CoordinationUnitResult]:
        """Project branch handoffs down to source-bound facts for synthesis."""

        safe_results: list[CoordinationUnitResult] = []
        for result in branch_results:
            handoff = result.handoff or {}
            raw_claims = list(handoff.get("claims") or [])
            cited_claims: list[dict[str, Any]] = []
            used_refs: list[str] = []
            for raw_claim in raw_claims:
                if not isinstance(raw_claim, dict):
                    continue
                claim_text = str(raw_claim.get("claim") or "").strip()
                claim_refs = list(
                    dict.fromkeys(
                        str(ref).strip()
                        for ref in raw_claim.get("supporting_refs") or []
                        if str(ref).strip()
                    )
                )
                if not claim_text or not claim_refs:
                    continue
                used_refs.extend(claim_refs)
                cited_claims.append(
                    {
                        "claim": claim_text,
                        "supporting_refs": claim_refs,
                        "confidence": normalize_confidence(
                            raw_claim.get("confidence") or handoff.get("confidence")
                        ),
                        "conflicts": [],
                        "missing": [],
                        "source_ids": [
                            str(source_id)
                            for source_id in raw_claim.get("source_ids") or []
                            if str(source_id).strip()
                        ][:10],
                    }
                )

            cited_count = len(cited_claims)
            omitted_count = max(0, len(raw_claims) - cited_count)
            if result.error:
                summary = "该研究分支未形成可用于最终写作的有效结论。"
            else:
                summary = f"该研究分支提供 {cited_count} 条逐项绑定来源的结论。"
            missing = []
            if omitted_count:
                missing.append(
                    f"另有 {omitted_count} 条内容未逐项绑定来源，已从事实材料中排除。"
                )
            if result.error:
                missing.append("该研究分支执行异常，原始内容仅保留在诊断记录中。")

            unique_refs = list(dict.fromkeys(used_refs))[:20]
            safe_handoff = normalize_subagent_result(
                {
                    "summary": summary,
                    "claims": cited_claims,
                    "evidence_refs": unique_refs,
                    "confidence": normalize_confidence(handoff.get("confidence")),
                    "conflicts": [],
                    "missing": missing,
                    "risks": [],
                    "suggested_next_actions": [],
                    "raw_artifact_refs": [
                        ref for ref in unique_refs if ref.startswith("artifact://")
                    ],
                },
                role=result.role,
                objective=str(
                    handoff.get("description") or result.task_title or result.role
                ),
                answer_fallback=summary,
                task_id=str(handoff.get("task_id") or result.task_key or result.role),
                evidence_refs=unique_refs,
            )
            safe_results.append(replace(result, answer=summary, handoff=safe_handoff))
        return safe_results
