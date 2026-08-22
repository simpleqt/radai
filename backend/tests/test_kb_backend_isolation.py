from types import SimpleNamespace

import pytest

from backend.domain.knowledge_base.backend_policy import (
    BLOCKED_BACKEND,
    HYBRID_RETRIEVAL_BACKEND,
    LEGACY_BACKEND,
    SELF_BACKEND,
    declared_ingestion_backend,
    resolve_ingestion_backend,
    validate_backend_isolation,
)


def _settings(**overrides):
    values = {
        "default_ingestion_backend": LEGACY_BACKEND,
        "self_ingestion_enabled": True,
        "self_ingestion_allowlist": "",
        "self_ingestion_kb_allowlist": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _kb(**overrides):
    values = {
        "kb_id": "kb-1",
        "kb_code": "legacy_kb",
        "ingestion_backend": LEGACY_BACKEND,
        "self_ingestion_enabled": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_legacy_kb_wins_over_pending_self_ingestion():
    kb = _kb(ingestion_backend=LEGACY_BACKEND)
    pending = SimpleNamespace(ingestion_backend=SELF_BACKEND)

    assert declared_ingestion_backend(_settings(), kb=kb, pending_file=pending) == LEGACY_BACKEND
    assert resolve_ingestion_backend(_settings(), kb=kb, pending_file=pending) == LEGACY_BACKEND


def test_pending_legacy_locks_task_out_of_self_ingestion():
    kb = _kb(
        ingestion_backend=SELF_BACKEND,
        self_ingestion_enabled=True,
    )
    pending = SimpleNamespace(ingestion_backend=LEGACY_BACKEND)

    assert resolve_ingestion_backend(_settings(), kb=kb, pending_file=pending) == LEGACY_BACKEND


def test_self_ingestion_is_blocked_instead_of_falling_back_to_legacy_when_disabled():
    kb = _kb(
        ingestion_backend=SELF_BACKEND,
        self_ingestion_enabled=True,
    )

    assert (
        resolve_ingestion_backend(
            _settings(self_ingestion_enabled=False),
            kb=kb,
        )
        == BLOCKED_BACKEND
    )


@pytest.mark.parametrize(
    "selection",
    [
        {
            "ingestion_backend": SELF_BACKEND,
            "retrieval_backend": LEGACY_BACKEND,
            "self_ingestion_enabled": True,
            "self_retriever_enabled": False,
        },
        {
            "ingestion_backend": LEGACY_BACKEND,
            "retrieval_backend": SELF_BACKEND,
            "self_ingestion_enabled": False,
            "self_retriever_enabled": True,
        },
        {
            "ingestion_backend": SELF_BACKEND,
            "retrieval_backend": SELF_BACKEND,
            "self_ingestion_enabled": False,
            "self_retriever_enabled": True,
        },
        {
            "ingestion_backend": LEGACY_BACKEND,
            "retrieval_backend": HYBRID_RETRIEVAL_BACKEND,
            "self_ingestion_enabled": False,
            "self_retriever_enabled": False,
        },
    ],
)
def test_backend_isolation_rejects_unsafe_combinations(selection):
    with pytest.raises(ValueError):
        validate_backend_isolation(**selection)


def test_backend_isolation_allows_legacy_and_explicit_shadow_hybrid():
    validate_backend_isolation(
        ingestion_backend=LEGACY_BACKEND,
        retrieval_backend=LEGACY_BACKEND,
        self_ingestion_enabled=False,
        self_retriever_enabled=False,
    )
    validate_backend_isolation(
        ingestion_backend=LEGACY_BACKEND,
        retrieval_backend=HYBRID_RETRIEVAL_BACKEND,
        self_ingestion_enabled=False,
        self_retriever_enabled=True,
    )
    validate_backend_isolation(
        ingestion_backend=SELF_BACKEND,
        retrieval_backend=SELF_BACKEND,
        self_ingestion_enabled=True,
        self_retriever_enabled=True,
    )
