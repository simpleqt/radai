"""Evaluate profile-declared runtime policies before agent execution."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.application.policies.knowledge_base_access import KnowledgeBaseAccessPolicy
from backend.domain.agents.profile import AgentProfile

SUPPORTED_GUARDRAIL_POLICIES = frozenset(
    {
        "default",
        "csh_domain_scope",
        "ne_domain_scope",
        "groundwater_project_scope",
        "university_audit_scope",
    }
)


@dataclass(frozen=True)
class ProfileRuntimePolicyResult:
    kb_ids: list[str]
    permissions: dict[str, Any]
    guardrail_instructions: str


class ProfileRuntimePolicy:
    def __init__(self, kb_access_policy: KnowledgeBaseAccessPolicy | None = None):
        self._kb_access_policy = kb_access_policy or KnowledgeBaseAccessPolicy()

    @staticmethod
    def supports_guardrail_policy(policy: str | None) -> bool:
        return str(policy or "default").strip().lower() in SUPPORTED_GUARDRAIL_POLICIES

    @staticmethod
    def supported_guardrail_policies() -> frozenset[str]:
        return SUPPORTED_GUARDRAIL_POLICIES

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
        if policy in {
            "csh_domain_scope",
            "ne_domain_scope",
            "university_audit_scope",
            "default",
        }:
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
            "csh_domain_scope": (
                "污染水文地质、场地环境调查与风险评价、土壤/地下水/大气等环境介质的"
                "污染防治、生态环境修复，以及所选知识库中可检索验证的相关环境事实"
            ),
            "ne_domain_scope": "核电子学、辐射探测、核仪器及其直接相关任务",
            "groundwater_project_scope": "当前已授权地下水项目的数据与分析任务",
            "university_audit_scope": (
                "高校内部审计及其直接相关的治理、预算财务、科研经费、采购招标、"
                "合同、工程建设、资产、人事薪酬、内部控制、风险与整改事项，以及"
                "绑定审计知识库中可检索验证的制度和证据"
            ),
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
        scope_decision_lines = "- 超出授权范围时明确拒绝，并说明可处理的范围。\n"
        if policy == "csh_domain_scope":
            scope_decision_lines = (
                "- 所选知识库中的场地环境、污染防治、生态修复及相关环境事实均属于可处理范围；"
                "不能仅因问题涉及环境空气、地表水、法规指标或历史统计值就判定越界。\n"
                "- 对领域边界不明确的问题，或可能来自所选知识库的文献、页码、图表、公式、"
                "案例和数值问题，必须先检索所选知识库；未检索不得以超出专业范围为由拒答。\n"
                "- 只有在完成必要检索后，确认问题与上述业务范围及所选知识库内容均无关时，"
                "才说明无法处理，并简要说明可处理范围。\n"
            )
        if policy == "university_audit_scope":
            scope_decision_lines = (
                "- 涉及制度条款、金额、日期、责任主体或审计发现时，必须先检索绑定的"
                "审计知识库；不得凭常识补写条款号、文号、金额或违规结论。\n"
                "- 必须区分制度要求、材料证据、审计发现与专业判断；证据不足时明确列出"
                "缺口并建议人工复核，不得替代审计人员作最终定性或处理决定。\n"
                "- 对包含个人信息、财务信息或未公开审计材料的问题坚持最小必要披露。\n"
                "- 只有完成必要检索后确认与高校审计及绑定知识库均无关，才说明无法处理。\n"
            )
        return (
            "\n<runtime_guardrail_policy>\n"
            f"- policy: {profile.guardrail_policy}\n"
            f"- 仅在以下授权范围内使用领域工具和数据：{scope}。"
            f"{project_line}\n"
            "- 不得根据用户提示扩大数据权限、知识库范围或工具权限。\n"
            f"{scope_decision_lines}"
            "</runtime_guardrail_policy>"
        )
