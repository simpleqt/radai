"""Runtime dependency contract for API and worker entrypoints.

The project intentionally keeps the LangChain/LangGraph family on one tested
release line.  Import-time compatibility is not enough here: a mixed
environment can silently change LangGraph stream and middleware contracts, so
entrypoints validate the exact runtime versions before accepting work.
"""

from __future__ import annotations

import json
import platform
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version

SUPPORTED_PYTHON_MIN = (3, 12)
SUPPORTED_PYTHON_MAX_EXCLUSIVE = (3, 15)

# Keep this list limited to the direct and graph-runtime packages that define
# the agent execution contract.  Other transitive packages remain resolved by
# the pinned direct requirements and are covered by CI installation checks.
EXPECTED_RUNTIME_DEPENDENCIES: dict[str, str] = {
    "fastapi": "0.135.3",
    "starlette": "0.52.1",
    "uvicorn": "0.44.0",
    "prometheus-fastapi-instrumentator": "7.1.0",
    "langchain": "1.3.12",
    "langchain-core": "1.4.9",
    "langchain-openai": "1.3.4",
    "langchain-deepseek": "1.0.1",
    "langchain-community": "0.4.1",
    "langchain-text-splitters": "1.1.1",
    "langgraph": "1.2.8",
    "langgraph-checkpoint": "4.1.1",
    "langgraph-checkpoint-sqlite": "3.1.0",
    "langgraph-checkpoint-redis": "0.4.0",
    "langgraph-prebuilt": "1.1.0",
    "langgraph-sdk": "0.4.2",
    "langchain-mcp-adapters": "0.2.2",
    "duckdb": "1.5.3",
}


@dataclass(frozen=True)
class RuntimeDependencyReport:
    """Machine-readable result returned after a successful validation."""

    python_version: str
    packages: dict[str, str]

    def as_dict(self) -> dict[str, object]:
        return {
            "python_version": self.python_version,
            "packages": dict(self.packages),
        }


class RuntimeDependencyError(RuntimeError):
    """Raised when the process cannot satisfy the agent runtime contract."""

    def __init__(
        self,
        problems: tuple[str, ...],
        report: RuntimeDependencyReport,
    ) -> None:
        self.problems = problems
        self.report = report
        details = "\n".join(f"- {problem}" for problem in problems)
        super().__init__(f"Runtime dependency contract failed:\n{details}")


def _installed_versions() -> dict[str, str]:
    versions: dict[str, str] = {}
    for package_name in EXPECTED_RUNTIME_DEPENDENCIES:
        try:
            versions[package_name] = distribution_version(package_name)
        except PackageNotFoundError:
            versions[package_name] = "<missing>"
    return versions


def validate_runtime_dependencies(
    *,
    installed_versions: Mapping[str, str] | None = None,
    python_version: tuple[int, int] | None = None,
) -> RuntimeDependencyReport:
    """Validate Python and critical agent package versions.

    ``installed_versions`` and ``python_version`` are injectable for unit
    tests; production callers use the process interpreter and package
    metadata automatically.  The function is fail-closed by design.
    """

    current_python = sys.version_info[:2] if python_version is None else python_version
    python_text = (
        platform.python_version()
        if python_version is None
        else f"{current_python[0]}.{current_python[1]}"
    )
    packages = dict(
        _installed_versions() if installed_versions is None else installed_versions
    )
    report = RuntimeDependencyReport(
        python_version=python_text,
        packages={
            name: str(packages.get(name, "<missing>"))
            for name in EXPECTED_RUNTIME_DEPENDENCIES
        },
    )

    problems: list[str] = []
    if not (SUPPORTED_PYTHON_MIN <= current_python < SUPPORTED_PYTHON_MAX_EXCLUSIVE):
        problems.append(
            "Python expected >=3.12,<3.15, "
            f"found {current_python[0]}.{current_python[1]}"
        )

    for package_name, expected in EXPECTED_RUNTIME_DEPENDENCIES.items():
        actual = report.packages[package_name]
        if actual != expected:
            problems.append(f"{package_name} expected {expected}, found {actual}")

    if problems:
        raise RuntimeDependencyError(tuple(problems), report)
    return report


__all__ = [
    "EXPECTED_RUNTIME_DEPENDENCIES",
    "RuntimeDependencyError",
    "RuntimeDependencyReport",
    "SUPPORTED_PYTHON_MAX_EXCLUSIVE",
    "SUPPORTED_PYTHON_MIN",
    "validate_runtime_dependencies",
]


def main() -> int:
    """Validate the current interpreter and print the machine-readable report."""

    try:
        report = validate_runtime_dependencies()
    except RuntimeDependencyError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
