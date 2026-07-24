"""Resolve and validate a complete, runnable agent definition.

An :class:`AgentProfile` is the declarative entry point, while a
``ResolvedAgentDefinition`` is produced only after every referenced platform
registry has been checked. Runtime and admin callers can therefore fail before
a workflow starts instead of discovering a missing provider halfway through.
"""

from __future__ import annotations

import importlib
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.tools import BaseTool

from backend.application.agents.declarative_provider import (
    DeclarativeProviderLoader,
)
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.agents.prompt_registry import (
    PromptProfileSpec,
    PromptProvider,
    PromptTemplateRegistry,
)
from backend.application.domain_context import DomainContextResolver
from backend.application.policies import ProfileRuntimePolicy
from backend.application.policies.knowledge_base_access import (
    KnowledgeBaseAccessPolicy,
)
from backend.application.skills.manifest_loader import SkillManifestLoader
from backend.application.tools.provider_loader import ToolProviderLoader
from backend.application.tools.registry import ToolRegistry
from backend.domain.agents.profile import AgentProfile
from backend.domain.agents.workflows import WORKFLOW_SPECS, WorkflowSpec
from backend.domain.skills.manifest import SkillManifest
from backend.domain.tools.policy import ToolPolicy
from backend.domain.tools.spec import ToolSpec

Provider = Callable[..., Any]


@dataclass(frozen=True)
class AgentDefinitionIssue:
    """One actionable configuration problem in a resolved definition."""

    component: str
    code: str
    message: str

    def to_json_dict(self) -> dict[str, str]:
        return {
            "component": self.component,
            "code": self.code,
            "message": self.message,
        }


@dataclass(frozen=True)
class AgentRuntimeRegistryScope:
    """Process-local registry snapshot used while composing an executor.

    The names-only :class:`AgentRuntimeBindings` remains the checkpoint-safe
    runtime contract.  Registry objects stay at the application composition
    root so validation and executor construction cannot read different trees.
    """

    prompt_registry: PromptTemplateRegistry
    tool_registry: ToolRegistry
    skill_loader: SkillManifestLoader
    skills_base_path: Path | None = None


@dataclass(frozen=True)
class ResolvedAgentDefinition:
    """All profile references resolved to their platform declarations."""

    profile: AgentProfile
    prompt_spec: PromptProfileSpec
    prompt_provider: PromptProvider
    tool_policy: ToolPolicy
    tools: tuple[ToolSpec, ...]
    skills: tuple[SkillManifest, ...]
    workflows: tuple[WorkflowSpec, ...]
    workflow_providers: tuple[tuple[str, Provider], ...]
    domain_context_provider: Provider | None = None
    runtime_scope: AgentRuntimeRegistryScope | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "agent_code": self.profile.code,
            "name": self.profile.name,
            "domain": self.profile.domain,
            "default_workflow": self.profile.default_workflow,
            "allowed_workflows": [spec.name for spec in self.workflows],
            "workflow_strategies": {
                spec.name: spec.execution_strategy for spec in self.workflows
            },
            "prompt_profile": self.prompt_spec.name,
            "prompt_provider": self.prompt_spec.provider,
            "tool_policy": self.tool_policy.name,
            "tools": [spec.name for spec in self.tools],
            "skills": [manifest.name for manifest in self.skills],
            "guardrail_policy": self.profile.guardrail_policy,
            "domain_context_provider": self.profile.domain_context_provider,
        }


@dataclass(frozen=True)
class AgentDefinitionValidationReport:
    agent_code: str
    issues: tuple[AgentDefinitionIssue, ...]
    definition: ResolvedAgentDefinition | None = None

    @property
    def valid(self) -> bool:
        return not self.issues and self.definition is not None

    def to_json_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "agent_code": self.agent_code,
            "valid": self.valid,
            "issues": [issue.to_json_dict() for issue in self.issues],
        }
        if self.definition is not None:
            payload["definition"] = self.definition.to_json_dict()
        return payload


class AgentDefinitionValidationError(ValueError):
    def __init__(self, report: AgentDefinitionValidationReport):
        self.report = report
        summary = "; ".join(
            f"{issue.component}.{issue.code}: {issue.message}"
            for issue in report.issues
        )
        super().__init__(
            f"Agent definition '{report.agent_code}' is invalid"
            + (f": {summary}" if summary else "")
        )


class AgentDefinitionValidator:
    """Cross-registry validator and ``ResolvedAgentDefinition`` factory."""

    def __init__(
        self,
        *,
        profile_loader: AgentProfileLoader | None = None,
        prompt_registry: PromptTemplateRegistry | None = None,
        tool_registry: ToolRegistry | None = None,
        skill_loader: SkillManifestLoader | None = None,
        workflow_specs: Mapping[str, WorkflowSpec] | None = None,
        runtime_policy: ProfileRuntimePolicy | None = None,
        declarative_provider_validation: bool = False,
    ):
        self._profile_loader = profile_loader or AgentProfileLoader()
        self._initialization_issues: list[AgentDefinitionIssue] = []
        if prompt_registry is not None:
            self._prompt_registry: PromptTemplateRegistry | None = prompt_registry
        else:
            try:
                self._prompt_registry = PromptTemplateRegistry()
            except Exception as exc:  # noqa: BLE001 - report registry diagnostics
                self._prompt_registry = None
                self._initialization_issues.append(
                    self._issue("prompt", "registry_load_failed", str(exc))
                )
        if tool_registry is not None:
            self._tool_registry: ToolRegistry | None = tool_registry
        else:
            try:
                self._tool_registry = ToolRegistry()
            except Exception as exc:  # noqa: BLE001 - report registry diagnostics
                self._tool_registry = None
                self._initialization_issues.append(
                    self._issue("tool_policy", "registry_load_failed", str(exc))
                )
        if skill_loader is not None:
            self._skill_loader: SkillManifestLoader | None = skill_loader
        elif self._tool_registry is not None:
            # The cross-registry validator owns tool diagnostics. Loading a
            # Skill must not throw before we can attribute an unknown tool to
            # that Skill in the final report.
            self._skill_loader = SkillManifestLoader(
                tool_registry=self._tool_registry,
                validate_tools=False,
            )
        else:
            self._skill_loader = None
        self._workflow_specs = dict(workflow_specs or WORKFLOW_SPECS)
        self._runtime_policy = runtime_policy or ProfileRuntimePolicy()
        self._tool_provider_loader = ToolProviderLoader()
        self._declarative_provider_loader = (
            DeclarativeProviderLoader()
            if declarative_provider_validation
            else None
        )
        self._resolved_cache: dict[str, ResolvedAgentDefinition] = {}

    def resolve(self, agent_code: str) -> ResolvedAgentDefinition:
        normalized_code = str(agent_code or "").strip().lower()
        cached = self._resolved_cache.get(normalized_code)
        if cached is not None:
            return cached
        report = self.validate(normalized_code)
        if not report.valid or report.definition is None:
            raise AgentDefinitionValidationError(report)
        self._resolved_cache[normalized_code] = report.definition
        return report.definition

    def clear_cache(self) -> None:
        """Discard resolved definitions after an intentional config reload."""
        self._resolved_cache.clear()

    def validate_all(self) -> list[AgentDefinitionValidationReport]:
        list_codes = getattr(self._profile_loader, "list_profile_codes", None)
        if callable(list_codes):
            codes = list_codes()
        else:
            codes = [profile.code for profile in self._profile_loader.list_profiles()]
        return [self.validate(code) for code in codes]

    def validate(self, agent_code: str) -> AgentDefinitionValidationReport:
        normalized_code = str(agent_code or "").strip().lower()
        issues: list[AgentDefinitionIssue] = list(self._initialization_issues)

        try:
            profile = self._profile_loader.load(normalized_code)
        except Exception as exc:  # noqa: BLE001 - aggregate configuration diagnostics
            issues.append(self._issue("profile", "load_failed", str(exc)))
            return AgentDefinitionValidationReport(
                agent_code=normalized_code,
                issues=tuple(issues),
            )

        prompt_spec: PromptProfileSpec | None = None
        prompt_provider: PromptProvider | None = None
        if self._prompt_registry is not None:
            try:
                prompt_spec = self._prompt_registry.get_spec(profile.prompt_profile)
                prompt_provider = self._resolve_provider_callable(
                    prompt_spec.provider,
                    component="prompt",
                )
                self._validate_provider_signature(
                    prompt_provider,
                    provider_path=prompt_spec.provider,
                    component="prompt",
                    kwargs={
                        "subagent_enabled": False,
                        "use_search": True,
                        "available_skills": set(),
                        "task_type": None,
                        "lightweight_mode": False,
                        "lightweight_has_kb": False,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                issues.append(self._issue("prompt", "resolution_failed", str(exc)))

        tool_policy: ToolPolicy | None = None
        all_policy_tools: dict[str, ToolSpec] = {}
        if self._tool_registry is not None:
            try:
                tool_policy = self._tool_registry.get_policy(profile.tool_policy)
            except Exception as exc:  # noqa: BLE001
                issues.append(self._issue("tool_policy", "unknown", str(exc)))
        if tool_policy is not None:
            referenced_names = (
                set(tool_policy.include)
                | set(tool_policy.exclude)
                | {
                    tool_name
                    for names in tool_policy.include_when_skill_selected.values()
                    for tool_name in names
                }
            )
            for tool_name in sorted(referenced_names):
                try:
                    if self._tool_registry is None:
                        raise ValueError("Tool registry is unavailable")
                    spec = self._tool_registry.get(tool_name)
                    provider = self._resolve_provider_callable(
                        spec.provider,
                        component="tool",
                    )
                    self._tool_provider_loader.validate_provider_contract(provider)
                    all_policy_tools[tool_name] = spec
                except Exception as exc:  # noqa: BLE001
                    issues.append(
                        self._issue(
                            "tool_policy",
                            "tool_resolution_failed",
                            f"{tool_name}: {exc}",
                        )
                    )

        workflow_specs: list[WorkflowSpec] = []
        workflow_providers: list[tuple[str, Provider]] = []
        for workflow_name in profile.allowed_workflows:
            spec = self._workflow_specs.get(workflow_name)
            if spec is None:
                issues.append(
                    self._issue(
                        "workflow",
                        "unknown",
                        f"Unknown workflow: {workflow_name}",
                    )
                )
                continue
            try:
                provider = self._resolve_provider_callable(
                    spec.provider,
                    component="workflow",
                )
                self._validate_provider_signature(
                    provider,
                    provider_path=spec.provider,
                    component="workflow",
                    kwargs={
                        "name": workflow_name,
                        "checkpointer": None,
                        "runnable_config": None,
                        "runtime_budget": None,
                        "agent_specs": {},
                        "context_policy": None,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                issues.append(
                    self._issue(
                        "workflow",
                        "provider_resolution_failed",
                        f"{workflow_name}: {exc}",
                    )
                )
                continue
            workflow_specs.append(spec)
            workflow_providers.append((workflow_name, provider))

        requested_skill_names = {
            str(name).strip() for name in profile.default_skills if str(name).strip()
        }
        selected_manifests: dict[str, SkillManifest] = {}
        visiting: list[str] = []

        def resolve_skill(skill_name: str, *, required_by: str | None = None) -> None:
            if skill_name in visiting:
                cycle = " -> ".join([*visiting, skill_name])
                issues.append(self._issue("skill", "dependency_cycle", cycle))
                return
            if skill_name in selected_manifests:
                return
            if self._skill_loader is None:
                issues.append(
                    self._issue(
                        "skill",
                        "registry_unavailable",
                        f"Cannot resolve {skill_name}: Skill registry is unavailable",
                    )
                )
                return
            try:
                manifest = self._skill_loader.load(skill_name)
            except Exception as exc:  # noqa: BLE001
                code = "dependency_missing" if required_by else "resolution_failed"
                prefix = f"{required_by} requires " if required_by else ""
                issues.append(
                    self._issue(
                        "skill",
                        code,
                        f"{prefix}{skill_name}: {exc}",
                    )
                )
                return
            selected_manifests[manifest.name] = manifest
            visiting.append(skill_name)
            for dependency in manifest.dependencies:
                resolve_skill(dependency, required_by=manifest.name)
            visiting.pop()

        for skill_name in sorted(requested_skill_names):
            resolve_skill(skill_name)

        selected_skill_names = set(selected_manifests)
        skill_manifests = [
            selected_manifests[name] for name in sorted(selected_manifests)
        ]
        for manifest in skill_manifests:
            self._validate_skill(
                manifest,
                profile=profile,
                selected_skill_names=selected_skill_names,
                tool_policy=tool_policy,
                issues=issues,
            )
            conflicts = set(manifest.conflicts) & selected_skill_names
            if conflicts:
                issues.append(
                    self._issue(
                        "skill",
                        "default_conflict",
                        f"{manifest.name} conflicts with "
                        f"{', '.join(sorted(conflicts))}",
                    )
                )

        effective_tools: list[ToolSpec] = []
        if tool_policy is not None:
            effective_names = tool_policy.allowed_tool_names(
                selected_skills=selected_skill_names
            )
            for tool_name in sorted(effective_names):
                spec = all_policy_tools.get(tool_name)
                if spec is None:
                    continue
                if not any(
                    spec.supports(
                        domain=profile.domain,
                        workflow=workflow_name,
                        selected_skills=selected_skill_names,
                    )
                    for workflow_name in profile.allowed_workflows
                ):
                    issues.append(
                        self._issue(
                            "tool_policy",
                            "tool_unreachable",
                            f"Tool {tool_name} supports none of agent "
                            f"{profile.code}'s workflows/domain",
                        )
                    )
                    continue
                effective_tools.append(spec)

        if not self._runtime_policy.supports_guardrail_policy(profile.guardrail_policy):
            supported = ", ".join(
                sorted(self._runtime_policy.supported_guardrail_policies())
            )
            issues.append(
                self._issue(
                    "guardrail",
                    "unknown",
                    f"Unknown guardrail policy: {profile.guardrail_policy}; "
                    f"supported: {supported}",
                )
            )

        if not KnowledgeBaseAccessPolicy.supports_policy(profile.kb_policy):
            supported = ", ".join(
                sorted(KnowledgeBaseAccessPolicy.supported_policies())
            )
            issues.append(
                self._issue(
                    "kb_policy",
                    "unknown",
                    f"Unknown knowledge-base policy: {profile.kb_policy}; "
                    f"supported: {supported}",
                )
            )

        domain_context_provider: Provider | None = None
        if profile.domain_context_provider:
            try:
                domain_context_provider = self._resolve_provider_callable(
                    profile.domain_context_provider,
                    component="domain context",
                )
                self._validate_provider_signature(
                    domain_context_provider,
                    provider_path=profile.domain_context_provider,
                    component="domain context",
                    kwargs={
                        "user_id": "agent-definition-validation",
                        "request_metadata": {},
                    },
                )
            except Exception as exc:  # noqa: BLE001
                issues.append(
                    self._issue(
                        "domain_context",
                        "provider_resolution_failed",
                        str(exc),
                    )
                )

        if (
            issues
            or prompt_spec is None
            or prompt_provider is None
            or tool_policy is None
            or self._skill_loader is None
        ):
            return AgentDefinitionValidationReport(
                agent_code=profile.code,
                issues=tuple(issues),
            )

        definition = ResolvedAgentDefinition(
            profile=profile,
            prompt_spec=prompt_spec,
            prompt_provider=prompt_provider,
            tool_policy=tool_policy,
            tools=tuple(effective_tools),
            skills=tuple(skill_manifests),
            workflows=tuple(workflow_specs),
            workflow_providers=tuple(workflow_providers),
            domain_context_provider=domain_context_provider,
            runtime_scope=AgentRuntimeRegistryScope(
                prompt_registry=self._prompt_registry,
                tool_registry=self._tool_registry,
                skill_loader=self._skill_loader,
                skills_base_path=getattr(
                    self._skill_loader,
                    "primary_skills_root",
                    None,
                ),
            ),
        )
        return AgentDefinitionValidationReport(
            agent_code=profile.code,
            issues=(),
            definition=definition,
        )

    def _validate_skill(
        self,
        manifest: SkillManifest,
        *,
        profile: AgentProfile,
        selected_skill_names: set[str],
        tool_policy: ToolPolicy | None,
        issues: list[AgentDefinitionIssue],
    ) -> None:
        unknown_workflows = set(manifest.allowed_workflows) - set(self._workflow_specs)
        if unknown_workflows:
            issues.append(
                self._issue(
                    "skill",
                    "unknown_workflow",
                    f"{manifest.name}: {', '.join(sorted(unknown_workflows))}",
                )
            )
        if not set(manifest.allowed_workflows) & set(profile.allowed_workflows):
            issues.append(
                self._issue(
                    "skill",
                    "workflow_incompatible",
                    f"{manifest.name} has no workflow allowed by {profile.code}",
                )
            )
        if manifest.preferred_workflow:
            if manifest.preferred_workflow not in manifest.allowed_workflows:
                issues.append(
                    self._issue(
                        "skill",
                        "invalid_preferred_workflow",
                        f"{manifest.name}: {manifest.preferred_workflow}",
                    )
                )
            elif manifest.preferred_workflow not in profile.allowed_workflows:
                issues.append(
                    self._issue(
                        "skill",
                        "preferred_workflow_not_allowed",
                        f"{manifest.name}: {manifest.preferred_workflow} is not "
                        f"allowed by {profile.code}",
                    )
                )

        policy_tools = (
            tool_policy.allowed_tool_names(selected_skills=selected_skill_names)
            if tool_policy is not None
            else set()
        )
        for tool_name in manifest.tools:
            if self._tool_registry is None:
                issues.append(
                    self._issue(
                        "skill",
                        "unknown_tool",
                        f"{manifest.name}/{tool_name}: Tool registry is unavailable",
                    )
                )
                continue
            try:
                self._tool_registry.get(tool_name)
            except Exception as exc:  # noqa: BLE001
                issues.append(
                    self._issue(
                        "skill",
                        "unknown_tool",
                        f"{manifest.name}/{tool_name}: {exc}",
                    )
                )
                continue
            if tool_policy is not None and tool_name not in policy_tools:
                issues.append(
                    self._issue(
                        "skill",
                        "tool_not_allowed",
                        f"{manifest.name} declares {tool_name}, but policy "
                        f"{tool_policy.name} does not allow it",
                    )
                )

    @staticmethod
    def _load_callable(provider_path: str, *, component: str) -> Provider:
        module_name, separator, attribute_name = str(provider_path).partition(":")
        if not separator or not module_name or not attribute_name:
            raise ValueError(f"Invalid {component} provider: {provider_path}")
        module = importlib.import_module(module_name)
        provider = getattr(module, attribute_name)
        if not callable(provider) and not (
            component == "tool" and isinstance(provider, BaseTool)
        ):
            raise ValueError(
                f"{component.title()} provider is not callable: {provider_path}"
            )
        return provider

    def _resolve_provider_callable(
        self,
        provider_path: str,
        *,
        component: str,
    ) -> Provider:
        if self._declarative_provider_loader is not None:
            return self._declarative_provider_loader.load(
                provider_path,
                component=component,
            )
        return self._load_callable(provider_path, component=component)

    @staticmethod
    def _validate_provider_signature(
        provider: Provider,
        *,
        provider_path: str,
        component: str,
        kwargs: Mapping[str, Any],
    ) -> None:
        """Prove a provider accepts the exact arguments used by runtime.

        Importability alone is insufficient: a callable such as
        ``builtins:len`` is resolvable but cannot satisfy the platform
        contract. ``bind`` performs the check without invoking user code.
        """
        if inspect.iscoroutinefunction(provider) or bool(
            getattr(provider, "__provider_is_async__", False)
        ):
            raise ValueError(
                f"{component.title()} provider must be synchronous: {provider_path}"
            )
        try:
            signature = inspect.signature(provider)
            signature.bind(**dict(kwargs))
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"{component.title()} provider has an incompatible signature: "
                f"{provider_path}: {exc}"
            ) from exc

    @staticmethod
    def _issue(component: str, code: str, message: str) -> AgentDefinitionIssue:
        return AgentDefinitionIssue(
            component=component,
            code=code,
            message=str(message),
        )


__all__ = [
    "AgentRuntimeRegistryScope",
    "AgentDefinitionIssue",
    "AgentDefinitionValidationError",
    "AgentDefinitionValidationReport",
    "AgentDefinitionValidator",
    "ResolvedAgentDefinition",
]
