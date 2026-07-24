"""Import tool providers declared in ToolSpec."""

from __future__ import annotations

import importlib
import inspect
from typing import Any

from langchain_core.tools import BaseTool

from backend.domain.tools.runtime_context import ToolRuntimeContext
from backend.domain.tools.spec import ToolSpec


class ToolProviderLoader:
    _RUNTIME_PARAMETER_NAMES = frozenset(
        {
            "runtime_context",
            "request_id",
            "user_id",
            "agent_code",
            "domain",
            "thread_id",
            "role",
            "permissions",
            "time_context",
            "request_metadata",
            "domain_context",
        }
    )

    def load(self, spec: ToolSpec) -> Any:
        module_name, sep, attr_name = spec.provider.partition(":")
        if not sep or not module_name or not attr_name:
            raise ValueError(f"Invalid provider for tool {spec.name}: {spec.provider}")
        module = importlib.import_module(module_name)
        return getattr(module, attr_name)

    def invoke(
        self,
        provider: Any,
        *,
        runtime_context: ToolRuntimeContext | None = None,
    ) -> Any:
        """Invoke a provider with only the context parameters it declares.

        Factories such as ``create_scoped_chat_file_search_tool`` need
        ``thread_id``/``user_id`` while newer factories accept the whole
        ``runtime_context``.  One signature adapter keeps both forms usable
        through the registry and fails clearly when a required scope is absent.
        """
        try:
            signature = inspect.signature(provider)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"Provider {provider!r} has no inspectable signature") from exc
        if runtime_context is None:
            required = [
                name
                for name, parameter in signature.parameters.items()
                if parameter.default is inspect.Parameter.empty
                and parameter.kind
                in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            ]
            if required:
                raise TypeError(
                    "Provider requires runtime scope: " + ", ".join(required)
                )
            return provider()

        values = {
            "runtime_context": runtime_context,
            "request_id": runtime_context.request_id,
            "user_id": runtime_context.user_id,
            "agent_code": runtime_context.agent_code,
            "domain": runtime_context.domain,
            "thread_id": runtime_context.thread_id,
            "role": runtime_context.role,
            "permissions": runtime_context.permissions,
            "time_context": runtime_context.time_context,
            "request_metadata": runtime_context.request_metadata,
            "domain_context": runtime_context.domain_context,
        }
        positional: list[Any] = []
        kwargs: dict[str, Any] = {}
        positional_only = [
            (name, parameter)
            for name, parameter in signature.parameters.items()
            if parameter.kind is inspect.Parameter.POSITIONAL_ONLY
        ]
        last_positional_value = max(
            (
                index
                for index, (name, _parameter) in enumerate(positional_only)
                if name in values
            ),
            default=-1,
        )
        for index, (name, parameter) in enumerate(positional_only):
            if parameter.default is inspect.Parameter.empty and name not in values:
                # Required positional-only parameters cannot be supplied by a
                # keyword and therefore must be reported before invocation.
                continue
            if index <= last_positional_value:
                # Preserve positional arity when an optional, unknown
                # positional-only parameter precedes a context parameter.
                positional.append(values.get(name, parameter.default))
        for name, parameter in signature.parameters.items():
            if name not in values:
                continue
            if parameter.kind is not inspect.Parameter.POSITIONAL_ONLY and parameter.kind is not inspect.Parameter.VAR_POSITIONAL:
                kwargs[name] = values[name]
        missing = [
            name
            for name, parameter in signature.parameters.items()
            if parameter.default is inspect.Parameter.empty
            and parameter.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)
            and name not in kwargs
            and parameter.kind is not inspect.Parameter.POSITIONAL_ONLY
        ]
        missing.extend(
            name
            for name, parameter in positional_only
            if parameter.default is inspect.Parameter.empty and name not in values
        )
        if missing:
            raise TypeError(
                "Provider requires unsupported runtime parameters: " + ", ".join(missing)
            )
        return provider(*positional, **kwargs)

    @classmethod
    def validate_provider_contract(cls, provider: Any) -> None:
        """Validate a tool object/factory without executing provider code."""
        if isinstance(provider, BaseTool):
            return
        if not callable(provider):
            raise TypeError("Provider is neither a callable factory nor a tool")
        if inspect.iscoroutinefunction(provider) or bool(
            getattr(provider, "__provider_is_async__", False)
        ):
            raise TypeError("Async tool factories are unsupported")
        try:
            signature = inspect.signature(provider)
        except (TypeError, ValueError) as exc:
            raise TypeError(f"Provider {provider!r} has no inspectable signature") from exc
        unsupported = [
            name
            for name, parameter in signature.parameters.items()
            if parameter.default is inspect.Parameter.empty
            and parameter.kind
            in (
                inspect.Parameter.POSITIONAL_ONLY,
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
            and name not in cls._RUNTIME_PARAMETER_NAMES
        ]
        if unsupported:
            raise TypeError(
                "Provider requires unsupported runtime parameters: "
                + ", ".join(unsupported)
            )
