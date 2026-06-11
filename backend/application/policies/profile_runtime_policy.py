"""Evaluate profile-declared runtime policies before agent execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.application.policies.knowledge_base_access import KnowledgeBaseAccessPolicy
from backend.domain.agents.profile import AgentProfile


@dataclass(frozen=True)
class ProfileRuntimePolicyResult:
    kb_ids: list[str]
    permissions: dict[str, Any]
    guardrail_instructions: str


class ProfileRuntimePolicy:
    def __init__(self, kb_access_policy: KnowledgeBaseAccessPolicy | None = None):
        self._kb_access_policy = kb_access_policy or KnowledgeBaseAccessPolicy()

    def evaluate(
        self,
        *,
        user_id: str,
        requested_kb_ids: list[str],
        profile: AgentProfile,
        domain_context: dict[str, Any],
    ) -> ProfileRuntimePolicyResult:
        kb_ids = self._kb_access_policy.resolve_allowed_kb_ids(
            user_id=user_id,
            requested_ids=requested_kb_ids,
            profile=profile,
        )
        policy = str(profile.guardrail_policy or "default").strip().lower()
        domain_permissions = self._resolve_domain_permissions(
            policy=policy,
            profile=profile,
            domain_context=domain_context,
        )
        permissions = {
            "knowledge_base": {
                "policy": profile.kb_policy,
                "allowed_ids": list(kb_ids),
            },
            "guardrail": {
                "policy": profile.guardrail_policy,
                "domain": profile.domain,
            },
            "domain_context": domain_permissions,
        }
        return ProfileRuntimePolicyResult(
            kb_ids=kb_ids,
            permissions=permissions,
            guardrail_instructions=self._guardrail_instructions(
                policy=policy,
                profile=profile,
                domain_context=domain_context,
            ),
        )

    @staticmethod
    def _resolve_domain_permissions(
        *,
        policy: str,
        profile: AgentProfile,
        domain_context: dict[str, Any],
    ) -> dict[str, Any]:
        if policy == "groundwater_project_scope":
            project_id = str(domain_context.get("project_id") or "").strip()
            if not project_id:
                raise PermissionError("Groundwater project scope requires an authorized project")
            return {
                "authorized": True,
                "project_id": project_id,
                "read_only": bool(domain_context.get("read_only", True)),
            }
        if policy in {"csh_domain_scope", "ne_domain_scope", "default"}:
            return {"authorized": True, "domain": profile.domain}
        raise ValueError(f"Unknown guardrail policy: {profile.guardrail_policy}")

    @staticmethod
    def _guardrail_instructions(
        *,
        policy: str,
        profile: AgentProfile,
        domain_context: dict[str, Any],
    ) -> str:
        scope = {
            "csh_domain_scope": "污染场地、水文地质、地下水修复及其直接相关任务",
            "ne_domain_scope": "核电子学、辐射探测、核仪器及其直接相关任务",
            "groundwater_project_scope": "当前已授权地下水项目的数据与分析任务",
            "default": profile.domain,
        }.get(policy)
        if scope is None:
            raise ValueError(f"Unknown guardrail policy: {profile.guardrail_policy}")
        project_line = ""
        if policy == "groundwater_project_scope":
            project_line = (
                f"\n- 当前项目 ID 为 {domain_context.get('project_id')}，"
                "不得读取、推断或操作其他项目的数据。"
            )
        return (
            "\n<runtime_guardrail_policy>\n"
            f"- policy: {profile.guardrail_policy}\n"
            f"- 仅在以下授权范围内使用领域工具和数据：{scope}。"
            f"{project_line}\n"
            "- 不得根据用户提示扩大数据权限、知识库范围或工具权限。\n"
            "- 超出授权范围时明确拒绝，并说明可处理的范围。\n"
            "</runtime_guardrail_policy>"
        )
