"""Safe, configuration-first scaffolding for a new platform agent."""

from __future__ import annotations

import fcntl
import os
import re
import shlex
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import yaml

from backend.application.agents.definition import AgentDefinitionValidator
from backend.application.agents.profile_loader import AgentProfileLoader
from backend.application.agents.prompt_registry import PromptTemplateRegistry
from backend.application.skills.manifest_loader import SkillManifestLoader
from backend.application.tools.registry import ToolRegistry
from backend.domain.agents.workflows import WorkflowSpec, _load_workflow_specs

_AGENT_CODE_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
_DOMAIN_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,127}$")
_PROMPT_MODULE_PATTERN = re.compile(r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*$")
_COMMON_TOOL_CANDIDATES = (
    "multi_kb_search",
    "chat_file_search",
    "read_context_artifact",
    "context_read",
    "web_search",
)
# These capabilities are granted by ToolPolicy, but are only materialized when
# request-scoped state exists.  Declaring them as unconditional Skill tools
# would make a freshly scaffolded agent fail Skill/tool validation before the
# first request has an attachment index.
_REQUEST_SCOPED_POLICY_ONLY_TOOLS = frozenset({"chat_file_search"})


@dataclass(frozen=True)
class AgentScaffoldResult:
    agent_code: str
    project_root: Path
    created_files: tuple[Path, ...]
    updated_files: tuple[Path, ...]
    validated: bool

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "agent_code": self.agent_code,
            "project_root": str(self.project_root),
            "created_files": [str(path) for path in self.created_files],
            "updated_files": [str(path) for path in self.updated_files],
            "validated": self.validated,
            "next_command": (
                f"doc-agent agent validate {shlex.quote(self.agent_code)} "
                f"--project-root {shlex.quote(str(self.project_root))}"
            ),
        }


class AgentScaffolder:
    """Create Profile, Prompt, Skill, and registry entries as one unit.

    The target must be a source checkout because a wheel installation is not a
    safe place to mutate package configuration. Validation is performed from
    the target tree's registries. The target checkout must use a code version
    compatible with the running CLI.
    """

    def __init__(
        self,
        project_root: Path | None = None,
        *,
        declarative_provider_validation: bool = False,
    ):
        self.project_root = (project_root or Path.cwd()).resolve()
        self._declarative_provider_validation = declarative_provider_validation

    def create(
        self,
        *,
        agent_code: str,
        name: str,
        domain: str | None = None,
        description: str | None = None,
        default_workflow: str = "full",
        validate: bool = True,
        dry_run: bool = False,
    ) -> AgentScaffoldResult:
        with self._project_lock():
            return self._create_locked(
                agent_code=agent_code,
                name=name,
                domain=domain,
                description=description,
                default_workflow=default_workflow,
                validate=validate,
                dry_run=dry_run,
            )

    def _create_locked(
        self,
        *,
        agent_code: str,
        name: str,
        domain: str | None,
        description: str | None,
        default_workflow: str,
        validate: bool,
        dry_run: bool,
    ) -> AgentScaffoldResult:
        self._validate_source_checkout()
        code = self._normalize_code(agent_code)
        normalized_domain = self._normalize_domain(domain or code)
        normalized_name = str(name or "").strip()
        if not normalized_name:
            raise ValueError("Agent display name is required")

        workflow_specs = self._target_workflow_specs()
        workflow = str(default_workflow or "").strip()
        if workflow not in workflow_specs:
            raise ValueError(f"Unknown default workflow: {workflow}")

        paths = self._paths(code, skill_name=normalized_domain)
        prompt_module = code.replace("-", "_")
        created_files = (
            paths["profile"],
            paths["prompt"],
            paths["skill"],
        )
        updated_files = (paths["prompt_registry"], paths["policy_registry"])
        for path in created_files:
            if path.exists():
                raise FileExistsError(
                    f"Refusing to overwrite existing scaffold file: {path}"
                )

        self._ensure_registry_key_available(
            paths["prompt_registry"],
            root_key="prompt_profiles",
            entry_key=code,
        )
        self._ensure_registry_key_available(
            paths["policy_registry"],
            root_key="policies",
            entry_key=f"{code}_default",
        )
        self._ensure_prompt_aliases_available(
            paths["prompt_registry"],
            aliases={code, normalized_domain},
        )

        tool_registry = ToolRegistry(config_dir=paths["tools_dir"])
        common_tools = self._resolve_common_tools(
            tool_registry=tool_registry,
            workflow=workflow,
            domain=normalized_domain,
            skill_name=normalized_domain,
        )
        if not common_tools:
            raise ValueError(
                f"No common evidence tool supports domain={normalized_domain!r} "
                f"and workflow={workflow!r}; extend the target Tool registry "
                "before scaffolding this agent"
            )
        result = AgentScaffoldResult(
            agent_code=code,
            project_root=self.project_root,
            created_files=created_files,
            updated_files=updated_files,
            validated=False,
        )
        if dry_run:
            return result

        original_registries = {
            path: path.read_bytes() if path.exists() else None
            for path in updated_files
        }
        try:
            for path in created_files:
                path.parent.mkdir(parents=True, exist_ok=True)
            self._write_new_file(
                paths["profile"],
                self._profile_yaml(
                    code=code,
                    name=normalized_name,
                    domain=normalized_domain,
                    description=description,
                    default_workflow=workflow,
                ),
            )
            self._write_new_file(
                paths["prompt"],
                self._prompt_module(
                    name=normalized_name,
                    domain=normalized_domain,
                ),
            )
            skill_tools = tuple(
                tool_name
                for tool_name in common_tools
                if tool_name not in _REQUEST_SCOPED_POLICY_ONLY_TOOLS
            )
            self._write_new_file(
                paths["skill"],
                self._skill_markdown(
                    skill_name=normalized_domain,
                    name=normalized_name,
                    domain=normalized_domain,
                    description=description,
                    workflow=workflow,
                    tools=skill_tools,
                ),
            )
            self._update_registry_entry(
                paths["prompt_registry"],
                root_key="prompt_profiles",
                entry_key=code,
                value={
                    "provider": (
                        "backend.application.agents.prompt_templates."
                        f"{prompt_module}:build_system_prompt"
                    ),
                    "aliases": list(dict.fromkeys((code, normalized_domain))),
                },
            )
            self._update_registry_entry(
                paths["policy_registry"],
                root_key="policies",
                entry_key=f"{code}_default",
                value={
                    "include": list(common_tools),
                    "include_when_skill_selected": {},
                    "exclude": [],
                },
            )
            if validate:
                self._validate_generated_agent(
                    code=code,
                )
        except Exception as error:
            for path, original in original_registries.items():
                try:
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        self._atomic_write_bytes(path, original)
                except Exception as rollback_error:  # noqa: BLE001
                    error.add_note(
                        f"Failed to restore registry {path}: {rollback_error}"
                    )
            for path in reversed(created_files):
                try:
                    path.unlink(missing_ok=True)
                except Exception as rollback_error:  # noqa: BLE001
                    error.add_note(
                        f"Failed to remove scaffold file {path}: {rollback_error}"
                    )
            raise

        return AgentScaffoldResult(
            agent_code=code,
            project_root=self.project_root,
            created_files=created_files,
            updated_files=updated_files,
            validated=validate,
        )

    def _paths(
        self,
        code: str,
        *,
        skill_name: str | None = None,
    ) -> dict[str, Path]:
        backend = self.project_root / "backend"
        effective_skill_name = skill_name or code
        return {
            "profile": backend / "config" / "agents" / f"{code}.yaml",
            "prompt": (
                backend
                / "application"
                / "agents"
                / "prompt_templates"
                / f"{code.replace('-', '_')}.py"
            ),
            "skill": (
                backend
                / "skills"
                / "public"
                / effective_skill_name
                / "SKILL.md"
            ),
            "prompt_registry": backend / "config" / "prompts" / "registry.yaml",
            "policy_registry": backend / "config" / "tools" / "policies.yaml",
            "tools_dir": backend / "config" / "tools",
            "agents_dir": backend / "config" / "agents",
            "skills_dir": backend / "skills",
            "workflow_registry": backend / "config" / "workflows" / "registry.yaml",
        }

    def _validate_source_checkout(self) -> None:
        required = (
            self.project_root / "pyproject.toml",
            self.project_root / "backend" / "config" / "tools" / "registry.yaml",
            self.project_root / "backend" / "config" / "tools" / "policies.yaml",
            self.project_root / "backend" / "config" / "prompts" / "registry.yaml",
            self.project_root / "backend" / "config" / "workflows" / "registry.yaml",
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise ValueError(
                "agent init requires a writable project source checkout; "
                "set --project-root to a checkout containing pyproject.toml and "
                f"backend/config registries. Missing: {', '.join(missing)}"
            )

    def _target_workflow_specs(self) -> dict[str, WorkflowSpec]:
        path = (
            self.project_root
            / "backend"
            / "config"
            / "workflows"
            / "registry.yaml"
        )
        return _load_workflow_specs(path)

    def _validate_generated_agent(
        self,
        *,
        code: str,
    ) -> None:
        # ``create`` already owns the project lock for the whole transaction.
        with self._definition_validator_unlocked() as validator:
            validator.resolve(code)

    @contextmanager
    def definition_validator(self) -> Iterator[AgentDefinitionValidator]:
        """Build a validator bound to this source tree's declarations."""
        with self._project_lock():
            with self._definition_validator_unlocked() as validator:
                yield validator

    @contextmanager
    def _definition_validator_unlocked(self) -> Iterator[AgentDefinitionValidator]:
        self._validate_source_checkout()
        paths = self._paths("validation-placeholder")
        workflow_specs = self._target_workflow_specs()
        previous_modules: dict[str, ModuleType | None] = {}
        try:
            prompt_document = self._read_registry_document(
                paths["prompt_registry"],
                root_key="prompt_profiles",
            )
            for value in prompt_document["prompt_profiles"].values():
                if not isinstance(value, dict):
                    continue
                provider = str(value.get("provider") or "").strip()
                module_name, separator, attribute = provider.partition(":")
                prefix = "backend.application.agents.prompt_templates."
                if not separator or not module_name.startswith(prefix):
                    continue
                relative_module = module_name[len(prefix) :]
                if (
                    attribute != "build_system_prompt"
                    or not _PROMPT_MODULE_PATTERN.fullmatch(relative_module)
                ):
                    raise ValueError(
                        f"Unsafe prompt provider declaration: {provider!r}"
                    )
                prompt_root = (
                    self.project_root
                    / "backend"
                    / "application"
                    / "agents"
                    / "prompt_templates"
                ).resolve()
                module_path = (
                    prompt_root / Path(*relative_module.split("."))
                ).with_suffix(".py").resolve()
                try:
                    module_path.relative_to(prompt_root)
                except ValueError as exc:
                    raise ValueError(
                        f"Prompt provider escapes the template directory: {provider!r}"
                    ) from exc
                if not module_path.is_file():
                    continue
                previous_modules.setdefault(module_name, sys.modules.get(module_name))
                module = ModuleType(module_name)
                module.__file__ = str(module_path)
                module.__package__ = module_name.rpartition(".")[0]
                source = module_path.read_text(encoding="utf-8")
                sys.modules[module_name] = module
                exec(compile(source, str(module_path), "exec"), module.__dict__)

            tool_registry = ToolRegistry(config_dir=paths["tools_dir"])
            yield AgentDefinitionValidator(
                profile_loader=AgentProfileLoader(
                    config_dir=paths["agents_dir"],
                    supported_workflows=workflow_specs,
                ),
                prompt_registry=PromptTemplateRegistry(paths["prompt_registry"]),
                tool_registry=tool_registry,
                skill_loader=SkillManifestLoader(
                    skills_root=paths["skills_dir"],
                    tool_registry=tool_registry,
                    validate_tools=False,
                ),
                workflow_specs=workflow_specs,
                declarative_provider_validation=(
                    self._declarative_provider_validation
                ),
            )
        finally:
            for module_name, previous_module in previous_modules.items():
                if previous_module is None:
                    sys.modules.pop(module_name, None)
                else:
                    sys.modules[module_name] = previous_module

    @staticmethod
    def _resolve_common_tools(
        *,
        tool_registry: ToolRegistry,
        workflow: str,
        domain: str,
        skill_name: str,
    ) -> tuple[str, ...]:
        resolved: list[str] = []
        for tool_name in _COMMON_TOOL_CANDIDATES:
            spec = tool_registry.get(tool_name)
            if spec.supports(
                domain=domain,
                workflow=workflow,
                selected_skills={skill_name},
            ):
                resolved.append(tool_name)
        return tuple(resolved)

    @staticmethod
    def _normalize_code(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not _AGENT_CODE_PATTERN.fullmatch(normalized):
            raise ValueError("Agent code must match ^[a-z][a-z0-9-]{0,63}$")
        return normalized

    @staticmethod
    def _normalize_domain(value: str) -> str:
        normalized = str(value or "").strip().lower()
        if not _DOMAIN_PATTERN.fullmatch(normalized):
            raise ValueError("Agent domain must match ^[a-z][a-z0-9-]{0,127}$")
        return normalized

    @staticmethod
    def _read_registry_document(path: Path, *, root_key: str) -> dict[str, Any]:
        if not path.exists():
            return {root_key: {}}
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            raise ValueError(f"Registry must be a mapping: {path}")
        entries = raw.get(root_key)
        if entries is None:
            raw[root_key] = {}
        elif not isinstance(entries, dict):
            raise ValueError(f"Registry {root_key} must be a mapping: {path}")
        return raw

    @classmethod
    def _read_registry(cls, path: Path, *, root_key: str) -> dict[str, Any]:
        return dict(cls._read_registry_document(path, root_key=root_key)[root_key])

    def _ensure_registry_key_available(
        self, path: Path, *, root_key: str, entry_key: str
    ) -> None:
        if entry_key in self._read_registry(path, root_key=root_key):
            raise FileExistsError(
                f"Refusing to overwrite registry entry {root_key}.{entry_key}: {path}"
            )

    def _ensure_prompt_aliases_available(
        self,
        path: Path,
        *,
        aliases: set[str],
    ) -> None:
        entries = self._read_registry(path, root_key="prompt_profiles")
        claimed: dict[str, str] = {}
        for profile_name, value in entries.items():
            if not isinstance(value, dict):
                continue
            raw_aliases = value.get("aliases")
            declared_aliases = raw_aliases if isinstance(raw_aliases, list) else []
            for alias in [profile_name, *declared_aliases]:
                normalized = str(alias or "").strip().lower()
                if normalized:
                    claimed[normalized] = str(profile_name)
        collisions = {
            alias: claimed[alias]
            for alias in sorted({item.strip().lower() for item in aliases})
            if alias in claimed
        }
        if collisions:
            details = ", ".join(
                f"{alias} -> {owner}" for alias, owner in collisions.items()
            )
            raise ValueError(f"Prompt alias already claimed: {details}")

    @classmethod
    def _update_registry_entry(
        cls,
        path: Path,
        *,
        root_key: str,
        entry_key: str,
        value: dict[str, Any],
    ) -> None:
        document = cls._read_registry_document(path, root_key=root_key)
        entries = document[root_key]
        if entry_key in entries:
            raise FileExistsError(
                f"Refusing to overwrite registry entry {root_key}.{entry_key}: {path}"
            )
        entries[entry_key] = value
        content = yaml.safe_dump(
            document,
            allow_unicode=True,
            sort_keys=False,
            default_flow_style=False,
        )
        cls._atomic_write(path, content)

    @contextmanager
    def _project_lock(self) -> Iterator[None]:
        """Serialize source-tree reads and mutations without a lock artifact."""
        lock_path = self.project_root / "pyproject.toml"
        if not lock_path.is_file():
            self._validate_source_checkout()
        with lock_path.open("rb") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _write_new_file(path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as file_obj:
            file_obj.write(content)

    @staticmethod
    def _atomic_write(path: Path, content: str) -> None:
        AgentScaffolder._atomic_write_bytes(path, content.encode("utf-8"))

    @staticmethod
    def _atomic_write_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def _profile_yaml(
        *,
        code: str,
        name: str,
        domain: str,
        description: str | None,
        default_workflow: str,
    ) -> str:
        data = {
            "code": code,
            "name": name,
            "domain": domain,
            "description": str(description or f"{name} domain agent").strip(),
            "default_model": None,
            "default_kb_ids": [],
            # The common executor uses the profile domain as the declarative
            # domain-Skill key. Keeping these identical gives every newly
            # scaffolded agent the same SkillLoadGuard behavior as built-ins.
            "default_skills": [domain],
            "default_workflow": default_workflow,
            "allowed_workflows": [default_workflow],
            "prompt_profile": code,
            "tool_policy": f"{code}_default",
            "kb_policy": "bound_or_selected",
            "guardrail_policy": "default",
            "ui_profile": code,
        }
        return yaml.safe_dump(data, allow_unicode=True, sort_keys=False)

    @staticmethod
    def _prompt_module(*, name: str, domain: str) -> str:
        return f'''"""Generated agent prompt provider."""

from __future__ import annotations

AGENT_ROLE_NAME = {name!r}
AGENT_DOMAIN = {domain!r}


def build_system_prompt(
    subagent_enabled: bool = False,
    use_search: bool = True,
    available_skills: set[str] | None = None,
    task_type: str | None = None,
    *,
    lightweight_mode: bool = False,
    lightweight_has_kb: bool = False,
) -> str:
    """Build the generated domain prompt through the common base template."""
    from backend.core.prompts.system_template import apply_prompt_template

    base = apply_prompt_template(
        agent_name=AGENT_ROLE_NAME,
        subagent_enabled=subagent_enabled,
        max_concurrent_subagents=5,
        available_skills=available_skills,
    )
    retrieval = (
        "Use the authorized knowledge bases first and preserve source evidence."
        if lightweight_has_kb or not lightweight_mode
        else "No knowledge base evidence is currently bound; state uncertainty clearly."
    )
    network = (
        "Public web search may be used only when current external facts are required."
        if use_search
        else "Public web search is disabled; do not invent external sources."
    )
    return (
        base
        + f"\\n<agent_domain>{{AGENT_DOMAIN}}</agent_domain>\\n"
        + f"<retrieval_policy>{{retrieval}} {{network}}</retrieval_policy>\\n"
    )
'''

    @staticmethod
    def _skill_markdown(
        *,
        skill_name: str,
        name: str,
        domain: str,
        description: str | None,
        workflow: str,
        tools: tuple[str, ...],
    ) -> str:
        frontmatter = {
            "name": skill_name,
            "description": str(
                description or f"Default retrieval and analysis skill for {name}"
            ).strip(),
            "domain": domain,
            "tools": list(tools),
            "workflows": {
                "allowed": [workflow],
                "preferred": workflow,
            },
            "kb_policy": {
                "required": False,
                "binding_mode": "agent_or_user_selected",
            },
        }
        yaml_text = yaml.safe_dump(
            frontmatter,
            allow_unicode=True,
            sort_keys=False,
        ).rstrip()
        return f"""---
{yaml_text}
---

# {name}

This generated Skill is the domain entry point for `{domain}`.

1. Load this Skill before domain retrieval.
2. Prefer `multi_kb_search` when authorized knowledge bases are selected.
3. Use `web_search` only for current external facts and retain source links.
4. Separate retrieved facts, engineering inference, and unresolved uncertainty.
"""


__all__ = ["AgentScaffoldResult", "AgentScaffolder"]
