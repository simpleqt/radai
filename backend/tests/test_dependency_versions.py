from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.config.dependency_versions import (
    EXPECTED_RUNTIME_DEPENDENCIES,
    RuntimeDependencyError,
    RuntimeDependencyReport,
    validate_runtime_dependencies,
)


def test_runtime_dependency_contract_accepts_project_baseline():
    report = validate_runtime_dependencies(
        installed_versions=EXPECTED_RUNTIME_DEPENDENCIES,
        python_version=(3, 14),
    )

    assert report.python_version == "3.14"
    assert report.packages["duckdb"] == "1.5.3"
    assert report.packages["langgraph"] == "1.2.8"


def test_runtime_dependency_contract_rejects_old_langgraph_and_missing_duckdb():
    versions = dict(EXPECTED_RUNTIME_DEPENDENCIES)
    versions["langgraph"] = "1.0.5"
    versions["duckdb"] = "<missing>"

    with pytest.raises(RuntimeDependencyError) as exc_info:
        validate_runtime_dependencies(
            installed_versions=versions,
            python_version=(3, 14),
        )

    message = str(exc_info.value)
    assert "langgraph expected 1.2.8, found 1.0.5" in message
    assert "duckdb expected 1.5.3, found <missing>" in message


def test_runtime_dependency_contract_rejects_unsupported_python():
    with pytest.raises(RuntimeDependencyError, match="Python expected"):
        validate_runtime_dependencies(
            installed_versions=EXPECTED_RUNTIME_DEPENDENCIES,
            python_version=(3, 11),
        )


@pytest.mark.asyncio
async def test_main_api_lifespan_rejects_dependency_mismatch_before_initializers(
    monkeypatch,
):
    from backend.api import startup

    report = RuntimeDependencyReport(python_version="3.14.2", packages={})
    expected_error = RuntimeDependencyError(("duckdb expected 1.5.3",), report)

    monkeypatch.setattr(startup, "service_role", lambda: "agent")
    monkeypatch.setattr(startup, "setup_app_dirs", lambda: None)
    monkeypatch.setattr(startup, "configure_tls_cert_loading", lambda: None)
    monkeypatch.setattr(startup, "setup_logging", lambda: None)

    def reject_startup():
        raise expected_error

    monkeypatch.setattr(startup, "validate_runtime_dependencies", reject_startup)

    with pytest.raises(RuntimeDependencyError, match="duckdb expected"):
        async with startup.main_lifespan(SimpleNamespace(state=SimpleNamespace())):
            raise AssertionError("startup should not reach the application context")
