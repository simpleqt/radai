"""Policy-driven tool assembly helpers."""

from __future__ import annotations

from dataclasses import dataclass, field

from langchain_core.tools import BaseTool

from backend.application.tools.provider_loader import ToolProviderLoader
from backend.application.tools.registry import ToolRegistry
from backend.application.tools.runtime_state import tool_runtime_state
from backend.config.loggers import get_logger
from backend.domain.tools.runtime_context import ToolRuntimeContext
from backend.domain.tools.spec import ToolSpec

logger = get_logger(__name__)


_DEFAULT_TOOL_REGISTRY: ToolRegistry | None = None
_DEFAULT_PROVIDER_LOADER: ToolProviderLoader | None = None


def _default_tool_registry() -> ToolRegistry:
    """Reuse immutable default declarations without caching request-scoped tools."""
    global _DEFAULT_TOOL_REGISTRY
    if _DEFAULT_TOOL_REGISTRY is None:
        _DEFAULT_TOOL_REGISTRY = ToolRegistry()
    return _DEFAULT_TOOL_REGISTRY


def _default_provider_loader() -> ToolProviderLoader:
    global _DEFAULT_PROVIDER_LOADER
    if _DEFAULT_PROVIDER_LOADER is None:
        _DEFAULT_PROVIDER_LOADER = ToolProviderLoader()
    return _DEFAULT_PROVIDER_LOADER


def shared_default_tool_registry() -> ToolRegistry:
    """Expose the process-wide immutable default registry to other layers.

    注册表的 YAML 每进程只解析一次;按请求构造的消费方(如定义校验器)
    共享同一份只读实例,避免每请求重复解析 registry/policies。
    """

    return _default_tool_registry()


def tool_spec_metadata(spec: ToolSpec) -> dict[str, object]:
    """Translate the registry contract into runtime metadata."""

    metadata: dict[str, object] = {
        "tool_spec_name": spec.name,
        "risk_level": spec.risk_level,
        "policy_source": spec.policy_source,
        "side_effect": spec.side_effect,
        "idempotent": spec.idempotent,
        "parallel_safe": spec.parallel_safe,
        "max_attempts": spec.max_attempts,
        "retryable_errors": sorted(spec.retryable_errors),
        "cache_enabled": spec.cache_enabled,
        "required_skills": sorted(spec.required_skills),
        "required_skill_mode": spec.required_skill_mode,
    }
    optional = {
        "timeout_seconds": spec.timeout_seconds,
        "max_concurrency": spec.max_concurrency,
        "max_output_chars": spec.max_output_chars,
        "cache_ttl_seconds": spec.cache_ttl_seconds,
    }
    metadata.update(
        {key: value for key, value in optional.items() if value is not None}
    )
    if spec.instructions:
        metadata["instructions"] = spec.instructions
    return metadata


@dataclass(frozen=True)
class ToolAssemblyContext:
    domain: str
    workflow: str
    policy_name: str
    selected_skills: set[str] = field(default_factory=set)


@dataclass(frozen=True)
class ToolPolicyAudit:
    policy_name: str
    domain: str
    workflow: str
    expected: set[str]
    present: set[str]
    missing: set[str]
    unexpected: set[str]


@dataclass(frozen=True)
class ToolHealth:
    name: str
    status: str
    reason: str = ""
    detail: str = ""


class PolicyToolAssembler:
    """Resolve and materialize tools from ToolPolicy/ToolSpec contracts."""

    def __init__(
        self,
        *,
        registry: ToolRegistry | None = None,
        provider_loader: ToolProviderLoader | None = None,
    ):
        self._registry = (
            registry if registry is not None else _default_tool_registry()
        )
        self._provider_loader = (
            provider_loader
            if provider_loader is not None
            else _default_provider_loader()
        )
        self._health: dict[str, ToolHealth] = {}

    def resolve_specs(self, context: ToolAssemblyContext) -> list[ToolSpec]:
        return self._registry.resolve_policy_tools(
            policy_name=context.policy_name,
            domain=context.domain,
            workflow=context.workflow,
            selected_skills=context.selected_skills,
        )

    def get_spec(self, tool_name: str) -> ToolSpec:
        return self._registry.get(tool_name)

    def materialize_tool_name(
        self,
        tool_name: str,
        *,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> list[BaseTool]:
        return self.materialize_spec(
            self._registry.get(tool_name),
            runtime_context=runtime_context,
        )

    def materialize_spec(
        self,
        spec: ToolSpec,
        *,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> list[BaseTool]:
        provider = self._provider_loader.load(spec)
        if callable(provider) and not isinstance(provider, BaseTool):
            raw_tools = self._provider_loader.invoke(
                provider,
                runtime_context=runtime_context,
            )
        else:
            raw_tools = provider
        if isinstance(raw_tools, BaseTool):
            self._health[spec.name] = ToolHealth(name=spec.name, status="ready")
            tool_runtime_state.record_health(spec.name, status="ready")
            return [self._attach_spec_metadata(raw_tools, spec)]
        if isinstance(raw_tools, list):
            invalid = [item for item in raw_tools if not isinstance(item, BaseTool)]
            if invalid:
                raise TypeError(
                    f"Provider for {spec.name} returned non-tool values: "
                    f"{[type(item).__name__ for item in invalid]}"
                )
            self._health[spec.name] = ToolHealth(name=spec.name, status="ready")
            tool_runtime_state.record_health(spec.name, status="ready")
            return [
                self._attach_spec_metadata(tool_item, spec) for tool_item in raw_tools
            ]
        raise TypeError(
            f"Provider for {spec.name} must return BaseTool or list[BaseTool], "
            f"got {type(raw_tools).__name__}"
        )

    @staticmethod
    def _attach_spec_metadata(tool_item: BaseTool, spec: ToolSpec) -> BaseTool:
        metadata = dict(getattr(tool_item, "metadata", None) or {})
        for key, value in tool_spec_metadata(spec).items():
            metadata.setdefault(key, value)
        try:
            tool_item.metadata = metadata
        except Exception:
            logger.warning(
                "[ToolAssembly] 工具元数据写入失败: tool=%s",
                spec.name,
                exc_info=True,
            )
        return tool_item

    def materialize_policy_tools(
        self,
        context: ToolAssemblyContext,
        *,
        skip_tool_names: set[str] | None = None,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> dict[str, BaseTool]:
        skip_tool_names = skip_tool_names or set()
        tools_by_name: dict[str, BaseTool] = {}
        for spec in self.resolve_specs(context):
            if spec.name in skip_tool_names:
                continue
            try:
                materialized = self.materialize_spec(
                    spec,
                    runtime_context=runtime_context,
                )
            except Exception as exc:
                reason = _health_reason(exc)
                self._health[spec.name] = ToolHealth(
                    name=spec.name,
                    status="unavailable",
                    reason=reason,
                    detail=f"{type(exc).__name__}: {exc}"[:500],
                )
                tool_runtime_state.record_health(
                    spec.name,
                    status="unavailable",
                    reason=reason,
                    detail=type(exc).__name__,
                )
                logger.warning(
                    "[ToolAssembly] 工具不可用，已从本次请求移除: tool=%s reason=%s error=%s",
                    spec.name,
                    reason,
                    exc,
                )
                continue
            for tool_item in materialized:
                if tool_item.name == spec.name:
                    tools_by_name[tool_item.name] = tool_item
        return tools_by_name

    def health_report(self) -> list[ToolHealth]:
        return [self._health[name] for name in sorted(self._health)]

    def audit(
        self,
        context: ToolAssemblyContext,
        *,
        actual_tool_names: set[str],
    ) -> ToolPolicyAudit:
        expected = {spec.name for spec in self.resolve_specs(context)}
        return ToolPolicyAudit(
            policy_name=context.policy_name,
            domain=context.domain,
            workflow=context.workflow,
            expected=expected,
            present=expected & actual_tool_names,
            missing=expected - actual_tool_names,
            unexpected=actual_tool_names - expected,
        )


def _health_reason(error: BaseException) -> str:
    if isinstance(error, ModuleNotFoundError):
        return "missing_dependency"
    if isinstance(error, ImportError):
        return "import_error"
    if isinstance(error, (TypeError, ValueError)):
        return "invalid_provider"
    return "initialization_failed"
