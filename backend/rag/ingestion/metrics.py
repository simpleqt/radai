"""Prometheus metrics for self-ingestion pipeline and retrieval (§25.2)."""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Iterator

from backend.config.settings import settings

try:
    from prometheus_client import Counter, Histogram

    _PROMETHEUS = True
except ImportError:  # pragma: no cover
    Counter = Histogram = None  # type: ignore
    _PROMETHEUS = False


def prometheus_enabled() -> bool:
    return _PROMETHEUS and bool(getattr(settings, "self_ingestion_metrics_enabled", True))


if _PROMETHEUS:
    SELF_INGESTION_TOTAL = Counter(
        "self_ingestion_total",
        "Self-ingestion jobs by terminal status",
        ["kb_code", "status"],
    )
    SELF_INGESTION_ERRORS = Counter(
        "self_ingestion_errors_total",
        "Self-ingestion errors by stage",
        ["kb_code", "stage", "error_type"],
    )
    SELF_INGESTION_DURATION = Histogram(
        "self_ingestion_duration_seconds",
        "Self-ingestion stage duration",
        ["kb_code", "stage"],
        buckets=(0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600),
    )
    SELF_RETRIEVAL_DURATION = Histogram(
        "self_retrieval_duration_seconds",
        "Self-ingestion retrieval latency",
        ["kb_code", "path"],
        buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1, 2, 5),
    )
    SELF_INGESTION_RETRY = Counter(
        "self_ingestion_retry_total",
        "Self-ingestion repair/retry attempts",
        ["kb_code", "reason"],
    )
else:  # pragma: no cover
    SELF_INGESTION_TOTAL = SELF_INGESTION_ERRORS = SELF_INGESTION_RETRY = None
    SELF_INGESTION_DURATION = SELF_RETRIEVAL_DURATION = None


@contextmanager
def observe_stage(kb_code: str, stage: str) -> Iterator[None]:
    if not prometheus_enabled() or SELF_INGESTION_DURATION is None:
        yield
        return
    start = time.perf_counter()
    try:
        yield
    finally:
        SELF_INGESTION_DURATION.labels(kb_code=kb_code or "unknown", stage=stage).observe(
            time.perf_counter() - start
        )


def record_job_status(kb_code: str, status: str) -> None:
    if prometheus_enabled() and SELF_INGESTION_TOTAL is not None:
        SELF_INGESTION_TOTAL.labels(kb_code=kb_code or "unknown", status=status).inc()


def record_error(kb_code: str, stage: str, error_type: str = "exception") -> None:
    if prometheus_enabled() and SELF_INGESTION_ERRORS is not None:
        SELF_INGESTION_ERRORS.labels(
            kb_code=kb_code or "unknown",
            stage=stage,
            error_type=error_type[:64],
        ).inc()


def record_retry(kb_code: str, reason: str) -> None:
    if prometheus_enabled() and SELF_INGESTION_RETRY is not None:
        SELF_INGESTION_RETRY.labels(kb_code=kb_code or "unknown", reason=reason[:64]).inc()


def observe_retrieval(kb_code: str, path: str, seconds: float) -> None:
    if prometheus_enabled() and SELF_RETRIEVAL_DURATION is not None:
        SELF_RETRIEVAL_DURATION.labels(kb_code=kb_code or "unknown", path=path).observe(seconds)
