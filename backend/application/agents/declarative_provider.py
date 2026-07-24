"""Side-effect-free provider contract loading for definition tooling.

Runtime composition imports provider modules and keeps the resolved callables.
CLI commands such as ``agent validate`` and ``agent init`` only need to prove
that a declared Python symbol exists and has the platform signature.  Importing
the whole runtime graph for that check would initialize databases, logging, and
global settings.  This module reads Python source through import specs instead
and returns a callable that imports the real provider only if it is later used.
"""

from __future__ import annotations

import ast
import importlib
import importlib.machinery
import inspect
import sys
from dataclasses import dataclass
from types import ModuleType
from typing import Any


@dataclass(frozen=True)
class _StaticCallable:
    signature: inspect.Signature
    is_async: bool = False
    is_tool_object: bool = False


class DeferredProvider:
    """A validated provider reference that has no import-time side effects."""

    def __init__(
        self,
        provider_path: str,
        *,
        signature: inspect.Signature,
        is_async: bool,
    ) -> None:
        self.provider_path = provider_path
        self.__signature__ = signature
        self.__provider_is_async__ = is_async
        self.__name__ = provider_path.partition(":")[2]

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        module_name, _separator, attribute_name = self.provider_path.partition(":")
        provider = getattr(importlib.import_module(module_name), attribute_name)
        if not callable(provider):
            raise TypeError(f"Provider is not callable: {self.provider_path}")
        return provider(*args, **kwargs)

    def __repr__(self) -> str:
        return f"DeferredProvider({self.provider_path!r})"


class DeclarativeProviderLoader:
    """Resolve callable contracts from source without executing provider code."""

    def __init__(self) -> None:
        self._trees: dict[str, ast.Module] = {}

    def load(self, provider_path: str, *, component: str) -> Any:
        module_name, separator, attribute_name = str(provider_path).partition(":")
        if not separator or not module_name or not attribute_name:
            raise ValueError(f"Invalid {component} provider: {provider_path}")

        loaded_module = sys.modules.get(module_name)
        if loaded_module is not None and self._module_has_runtime_symbol(
            loaded_module, attribute_name
        ):
            provider = getattr(loaded_module, attribute_name)
            if not callable(provider):
                # LangChain BaseTool objects are accepted by the tool loader;
                # importing its type here would defeat this module's purpose.
                if component != "tool":
                    raise ValueError(
                        f"{component.title()} provider is not callable: {provider_path}"
                    )
            return provider

        contract = self._resolve_contract(
            module_name,
            attribute_name,
            component=component,
            resolving=set(),
        )
        return DeferredProvider(
            provider_path,
            signature=contract.signature,
            is_async=contract.is_async,
        )

    @staticmethod
    def _module_has_runtime_symbol(module: ModuleType, attribute_name: str) -> bool:
        # Source-checkout scaffolding deliberately injects generated prompt
        # modules. Builtins are also safe and useful for negative contract tests.
        return hasattr(module, attribute_name)

    def _resolve_contract(
        self,
        module_name: str,
        attribute_name: str,
        *,
        component: str,
        resolving: set[tuple[str, str]],
    ) -> _StaticCallable:
        key = (module_name, attribute_name)
        if key in resolving:
            raise ValueError(
                f"Provider alias cycle while resolving {module_name}:{attribute_name}"
            )
        resolving.add(key)
        try:
            tree = self._module_tree(module_name)
            node = self._find_symbol(tree, attribute_name)
            if node is None:
                raise ValueError(
                    f"Provider symbol not found: {module_name}:{attribute_name}"
                )
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                decorated_tool = component == "tool" and any(
                    self._is_tool_decorator(item) for item in node.decorator_list
                )
                if decorated_tool:
                    # ``@tool`` replaces the function with a BaseTool object;
                    # its Python function parameters are model inputs, not
                    # factory parameters consumed by ToolProviderLoader.
                    return _StaticCallable(
                        signature=inspect.Signature(),
                        is_tool_object=True,
                    )
                return _StaticCallable(
                    signature=self._signature_from_arguments(node.args),
                    is_async=isinstance(node, ast.AsyncFunctionDef),
                )
            if isinstance(node, ast.Lambda):
                return _StaticCallable(
                    signature=self._signature_from_arguments(node.args)
                )
            if isinstance(node, ast.ClassDef):
                constructor = next(
                    (
                        item
                        for item in node.body
                        if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and item.name in {"__init__", "__new__"}
                    ),
                    None,
                )
                if constructor is None:
                    return _StaticCallable(inspect.Signature())
                signature = self._signature_from_arguments(constructor.args)
                parameters = list(signature.parameters.values())
                if parameters and parameters[0].name in {"self", "cls"}:
                    signature = signature.replace(parameters=parameters[1:])
                return _StaticCallable(
                    signature=signature,
                    is_async=isinstance(constructor, ast.AsyncFunctionDef),
                )
            if isinstance(node, ast.ImportFrom):
                imported_name, imported_module = self._imported_symbol(
                    module_name,
                    attribute_name,
                    node,
                )
                return self._resolve_contract(
                    imported_module,
                    imported_name,
                    component=component,
                    resolving=resolving,
                )
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value = node.value
                if isinstance(value, ast.Lambda):
                    return _StaticCallable(
                        signature=self._signature_from_arguments(value.args)
                    )
                if isinstance(value, ast.Name):
                    return self._resolve_contract(
                        module_name,
                        value.id,
                        component=component,
                        resolving=resolving,
                    )
                if component == "tool" and isinstance(value, ast.Call):
                    # Tool instances constructed at module scope have already
                    # declared a concrete symbol. Runtime validation still
                    # materializes them through the normal resolver.
                    return _StaticCallable(
                        signature=inspect.Signature(),
                        is_tool_object=True,
                    )
            raise ValueError(
                "Provider symbol cannot be proven callable without executing "
                f"module code: {module_name}:{attribute_name}"
            )
        finally:
            resolving.remove(key)

    def _module_tree(self, module_name: str) -> ast.Module:
        cached = self._trees.get(module_name)
        if cached is not None:
            return cached
        spec = self._find_spec_without_import(module_name)
        loader = spec.loader
        source = None
        if loader is not None:
            get_source = getattr(loader, "get_source", None)
            if callable(get_source):
                source = get_source(module_name)
        if source is None:
            raise ValueError(
                f"Provider module has no inspectable Python source: {module_name}"
            )
        try:
            tree = ast.parse(source, filename=str(spec.origin or module_name))
        except SyntaxError as exc:
            raise ValueError(f"Invalid provider module syntax: {module_name}: {exc}") from exc
        self._trees[module_name] = tree
        return tree

    @staticmethod
    def _find_spec_without_import(module_name: str):
        search_path: list[str] | None = None
        qualified_name = ""
        spec = None
        for index, part in enumerate(module_name.split(".")):
            qualified_name = f"{qualified_name}.{part}" if qualified_name else part
            spec = importlib.machinery.PathFinder.find_spec(
                qualified_name,
                search_path,
            )
            if spec is None and index == 0:
                spec = importlib.machinery.BuiltinImporter.find_spec(qualified_name)
            if spec is None:
                raise ValueError(f"Provider module not found: {module_name}")
            if index < len(module_name.split(".")) - 1:
                locations = spec.submodule_search_locations
                if locations is None:
                    raise ValueError(f"Provider module not found: {module_name}")
                search_path = list(locations)
        return spec

    @staticmethod
    def _find_symbol(tree: ast.Module, attribute_name: str) -> ast.AST | None:
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.name == attribute_name:
                    return node
            elif isinstance(node, ast.ImportFrom):
                if any(
                    (alias.asname or alias.name) == attribute_name
                    for alias in node.names
                ):
                    return node
            elif isinstance(node, ast.Assign):
                if any(
                    isinstance(target, ast.Name) and target.id == attribute_name
                    for target in node.targets
                ):
                    return node
            elif isinstance(node, ast.AnnAssign):
                if isinstance(node.target, ast.Name) and node.target.id == attribute_name:
                    return node
        return None

    @staticmethod
    def _imported_symbol(
        current_module: str,
        attribute_name: str,
        node: ast.ImportFrom,
    ) -> tuple[str, str]:
        alias = next(
            item
            for item in node.names
            if (item.asname or item.name) == attribute_name
        )
        if node.level:
            package = current_module.rpartition(".")[0]
            relative = "." * node.level + (node.module or "")
            imported_module = importlib.util.resolve_name(relative, package)
        else:
            imported_module = str(node.module or "")
        if not imported_module:
            raise ValueError(
                f"Cannot resolve provider import {current_module}:{attribute_name}"
            )
        return alias.name, imported_module

    @staticmethod
    def _is_tool_decorator(node: ast.expr) -> bool:
        if isinstance(node, ast.Call):
            node = node.func
        if isinstance(node, ast.Name):
            return node.id == "tool"
        return isinstance(node, ast.Attribute) and node.attr == "tool"

    @staticmethod
    def _signature_from_arguments(arguments: ast.arguments) -> inspect.Signature:
        parameters: list[inspect.Parameter] = []
        positional = [*arguments.posonlyargs, *arguments.args]
        required_count = len(positional) - len(arguments.defaults)
        positional_only_count = len(arguments.posonlyargs)
        for index, argument in enumerate(positional):
            parameters.append(
                inspect.Parameter(
                    argument.arg,
                    (
                        inspect.Parameter.POSITIONAL_ONLY
                        if index < positional_only_count
                        else inspect.Parameter.POSITIONAL_OR_KEYWORD
                    ),
                    default=(
                        inspect.Parameter.empty if index < required_count else None
                    ),
                )
            )
        if arguments.vararg is not None:
            parameters.append(
                inspect.Parameter(
                    arguments.vararg.arg,
                    inspect.Parameter.VAR_POSITIONAL,
                )
            )
        for argument, default in zip(
            arguments.kwonlyargs,
            arguments.kw_defaults,
            strict=True,
        ):
            parameters.append(
                inspect.Parameter(
                    argument.arg,
                    inspect.Parameter.KEYWORD_ONLY,
                    default=(inspect.Parameter.empty if default is None else None),
                )
            )
        if arguments.kwarg is not None:
            parameters.append(
                inspect.Parameter(
                    arguments.kwarg.arg,
                    inspect.Parameter.VAR_KEYWORD,
                )
            )
        return inspect.Signature(parameters)


__all__ = ["DeclarativeProviderLoader", "DeferredProvider"]
