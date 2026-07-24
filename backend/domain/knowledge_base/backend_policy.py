"""Knowledge-base backend policy for legacy and self-ingestion flows.

严格隔离原则（新旧两条路互不影响）：
- KB 显式标记 legacy_lightrag → 永远走 legacy，绝不走 self。
- KB 显式标记 self_ingestion → 永远走 self；若全局开关/白名单未满足，
  返回 BLOCKED_BACKEND（阻塞），不回退 legacy，防止新 KB 污染旧索引。
- KB 字段为 NULL/缺失 → 用全局 default_backend（默认 legacy_lightrag）。
"""

from __future__ import annotations

from typing import Any


LEGACY_BACKEND = "legacy_lightrag"
SELF_BACKEND = "self_ingestion"
HYBRID_RETRIEVAL_BACKEND = "hybrid"
# 当 KB 显式要求 self 但全局开关/白名单未满足时返回，调用方应阻塞任务而非走 legacy。
BLOCKED_BACKEND = "blocked"

VALID_INGESTION_BACKENDS = {LEGACY_BACKEND, SELF_BACKEND}
VALID_RETRIEVAL_BACKENDS = {LEGACY_BACKEND, SELF_BACKEND, HYBRID_RETRIEVAL_BACKEND}


def _get_value(source: Any, name: str, default: Any = None) -> Any:
    if source is None:
        return default
    if isinstance(source, dict):
        return source.get(name, default)
    return getattr(source, name, default)


def _csv_set(value: Any) -> set[str]:
    if value is None:
        return set()
    if isinstance(value, (list, tuple, set)):
        return {str(item).strip() for item in value if str(item).strip()}
    return {item.strip() for item in str(value).split(",") if item.strip()}


def _is_allowlisted(settings: Any, kb: Any, field_name: str) -> bool:
    allowlist = _csv_set(_get_value(settings, field_name, ""))
    if not allowlist and field_name.endswith("_allowlist"):
        kb_field_name = field_name.replace("_allowlist", "_kb_allowlist")
        allowlist = _csv_set(_get_value(settings, kb_field_name, ""))
    if not allowlist:
        return True
    kb_id = str(_get_value(kb, "kb_id", "") or "")
    kb_code = str(_get_value(kb, "kb_code", "") or "")
    return kb_id in allowlist or kb_code in allowlist


def _normalize_backend(candidate: Any, valid: set[str], fallback: str) -> str:
    value = str(candidate or "").strip()
    return value if value in valid else fallback


def _kb_explicit_backend(kb: Any, pending_file: Any, default_backend: str) -> str:
    """解析 KB/pending_file 显式声明的 backend（NULL → default）。"""
    kb_backend = _normalize_backend(
        _get_value(kb, "ingestion_backend", default_backend),
        VALID_INGESTION_BACKENDS,
        default_backend,
    )
    pending_raw = _get_value(pending_file, "ingestion_backend", None)
    pending_backend = (
        _normalize_backend(pending_raw, VALID_INGESTION_BACKENDS, "")
        if pending_raw not in (None, "")
        else ""
    )

    if kb_backend == LEGACY_BACKEND or pending_backend == LEGACY_BACKEND:
        return LEGACY_BACKEND
    if kb_backend == SELF_BACKEND and pending_backend in ("", SELF_BACKEND):
        return SELF_BACKEND
    return LEGACY_BACKEND


def validate_backend_isolation(
    *,
    ingestion_backend: str,
    retrieval_backend: str,
    self_ingestion_enabled: bool,
    self_retriever_enabled: bool,
) -> None:
    """Validate backend combinations that could break legacy/self isolation."""
    if ingestion_backend not in VALID_INGESTION_BACKENDS:
        raise ValueError(f"无效的入库后端: {ingestion_backend}")
    if retrieval_backend not in VALID_RETRIEVAL_BACKENDS:
        raise ValueError(f"无效的查询后端: {retrieval_backend}")

    if ingestion_backend == SELF_BACKEND and not self_ingestion_enabled:
        raise ValueError("选择 self_ingestion 入库时，必须同时启用 self_ingestion_enabled")
    if retrieval_backend in {SELF_BACKEND, HYBRID_RETRIEVAL_BACKEND} and not self_retriever_enabled:
        raise ValueError("选择 self_ingestion/hybrid 查询时，必须同时启用 self_retriever_enabled")
    if ingestion_backend == SELF_BACKEND and retrieval_backend == LEGACY_BACKEND:
        raise ValueError("self_ingestion 入库不能搭配 legacy_lightrag 查询，避免新写入文档不可查")
    if ingestion_backend == LEGACY_BACKEND and retrieval_backend == SELF_BACKEND:
        raise ValueError("legacy_lightrag 入库不能直接搭配 self_ingestion 查询；旧库灰度请使用 hybrid")


def resolve_ingestion_backend(
    settings: Any,
    kb: Any = None,
    pending_file: Any = None,
) -> str:
    """返回入库后端：legacy_lightrag | self_ingestion | blocked。"""
    default_backend = _normalize_backend(
        _get_value(settings, "default_ingestion_backend", LEGACY_BACKEND),
        VALID_INGESTION_BACKENDS,
        LEGACY_BACKEND,
    )
    candidate = _kb_explicit_backend(kb, pending_file, default_backend)

    if candidate != SELF_BACKEND:
        return LEGACY_BACKEND

    if not bool(_get_value(settings, "self_ingestion_enabled", False)):
        return BLOCKED_BACKEND
    if not bool(_get_value(kb, "self_ingestion_enabled", False)):
        return BLOCKED_BACKEND
    if not _is_allowlisted(settings, kb, "self_ingestion_allowlist"):
        return BLOCKED_BACKEND
    return SELF_BACKEND


def should_use_self_ingestion(
    settings: Any,
    kb: Any = None,
    pending_file: Any = None,
) -> bool:
    return resolve_ingestion_backend(settings, kb, pending_file) == SELF_BACKEND


def is_ingestion_blocked(
    settings: Any,
    kb: Any = None,
    pending_file: Any = None,
) -> bool:
    """KB 显式要求 self 但全局开关/白名单未满足，应阻塞而非走 legacy。"""
    return resolve_ingestion_backend(settings, kb, pending_file) == BLOCKED_BACKEND


def declared_ingestion_backend(
    settings: Any,
    kb: Any = None,
    pending_file: Any = None,
) -> str:
    """返回 KB/pending 显式声明的入库后端（legacy_lightrag | self_ingestion）。"""
    default_backend = _normalize_backend(
        _get_value(settings, "default_ingestion_backend", LEGACY_BACKEND),
        VALID_INGESTION_BACKENDS,
        LEGACY_BACKEND,
    )
    return _kb_explicit_backend(kb, pending_file, default_backend)


def declared_retrieval_backend(settings: Any, kb: Any = None) -> str:
    """返回 KB 显式声明的检索后端（不含 blocked）。"""
    default_backend = _normalize_backend(
        _get_value(settings, "default_retrieval_backend", LEGACY_BACKEND),
        VALID_RETRIEVAL_BACKENDS,
        LEGACY_BACKEND,
    )
    return _normalize_backend(
        _get_value(kb, "retrieval_backend", default_backend),
        VALID_RETRIEVAL_BACKENDS,
        default_backend,
    )


def resolve_retrieval_backend(settings: Any, kb: Any = None) -> str:
    """返回检索后端：legacy_lightrag | self_ingestion | hybrid | blocked。"""
    default_backend = _normalize_backend(
        _get_value(settings, "default_retrieval_backend", LEGACY_BACKEND),
        VALID_RETRIEVAL_BACKENDS,
        LEGACY_BACKEND,
    )
    candidate = _normalize_backend(
        _get_value(kb, "retrieval_backend", default_backend),
        VALID_RETRIEVAL_BACKENDS,
        default_backend,
    )

    if candidate == LEGACY_BACKEND:
        return LEGACY_BACKEND

    if not bool(_get_value(settings, "self_retriever_enabled", False)):
        return BLOCKED_BACKEND
    if not bool(_get_value(kb, "self_retriever_enabled", False)):
        return BLOCKED_BACKEND
    if not _is_allowlisted(settings, kb, "self_retriever_allowlist"):
        return BLOCKED_BACKEND
    return candidate
