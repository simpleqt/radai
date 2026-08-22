"""Test runtime configuration for Python 3.14.

The standard asyncio loop in the deployed 3.14 runtime stalls on callbacks from
worker threads used by aiosqlite and LangGraph model middleware. Production
Uvicorn already selects uvloop; tests must exercise the same supported loop.
"""

from __future__ import annotations

import os
import sys

import pytest

# The source tree intentionally does not contain ``backend/.env``.  Keep unit
# test collection self-contained with inert values so a clean checkout never
# needs deployment secrets merely to import application modules.
_TEST_ENV_DEFAULTS = {
    "LLM_PROVIDER": "openai",
    "TEMPERATURE": "0",
    "STREAMING": "false",
    "MAX_TOKENS": "1024",
    "MINERU_API_URL": "http://127.0.0.1:1",
    "MINERU_BACKEND": "pipeline",
    "MINERU_LANG": "ch",
    "MINERU_METHOD": "auto",
    "MINERU_FORMULA_ENABLE": "false",
    "MINERU_TABLE_ENABLE": "false",
    "MYSQL_HOST": "127.0.0.1",
    "MYSQL_PORT": "3306",
    "MYSQL_USER": "pytest",
    "MYSQL_PASSWORD": "pytest-not-a-secret",
    "MYSQL_DATABASE": "pytest",
    "MYSQL_POOL_SIZE": "1",
    "MYSQL_MAX_OVERFLOW": "0",
    "STORAGE_BASE_PATH": "/tmp/doc-agent-pytest/storage",
    "DOCUMENTS_PATH": "/tmp/doc-agent-pytest/documents",
    "RESULTS_PATH": "/tmp/doc-agent-pytest/results",
    "CACHE_PATH": "/tmp/doc-agent-pytest/cache",
    "MAX_FILE_SIZE": "1048576",
    "FILE_EXPIRE_DAYS": "1",
    "CACHE_EXPIRE_HOURS": "1",
    "DISK_USAGE_WARNING_THRESHOLD": "0.9",
    "SAVE_RESULTS_LOCALLY": "false",
    "MINIO_ENDPOINT": "127.0.0.1:9000",
    "MINIO_BUCKET_NAME": "pytest",
    "MINIO_ACCESS_KEY": "pytest",
    "MINIO_SECRET_KEY": "pytest-not-a-secret",
    "MINIO_SECURE": "false",
    "CHUNK_SIZE": "1048576",
    "UPLOAD_TEMP_DIR": "/tmp/doc-agent-pytest/uploads",
    "UPLOAD_EXPIRE_HOURS": "1",
    "SERVER_HOST": "127.0.0.1",
    "SERVER_PORT": "8082",
    "SERVER_RELOAD": "false",
    "AMAP_KEY": "",
    "LOG_LEVEL": "WARNING",
    "LOG_DIR": "/tmp/doc-agent-pytest/logs",
    "LOG_MAX_SIZE": "1048576",
    "LOG_BACKUP_COUNT": "1",
    "LOG_FORMAT": "%(message)s",
    "LOG_ASYNC": "false",
    "LOG_COMPRESSION": "false",
    "LOG_FILE": "pytest.log",
    "KAFKA_BOOTSTRAP_SERVERS": "127.0.0.1:9092",
    "KAFKA_TOPICS_PREFIX": "pytest",
    "KAFKA_TASK_TOPIC": "tasks",
    "KAFKA_STATUS_TOPIC": "status",
    "KAFKA_RESULT_TOPIC": "results",
    "KAFKA_EVENT_TOPIC": "events",
    "KAFKA_RETRY_TOPIC": "retry",
    "KAFKA_DEAD_LETTER_TOPIC": "dead-letter",
    "KAFKA_CONSUMER_GROUP_PREFIX": "pytest",
    "KAFKA_TASK_GROUP_ID": "tasks",
    "KAFKA_STATUS_GROUP_ID": "status",
    "KAFKA_RESULT_GROUP_ID": "results",
    "KAFKA_EVENT_GROUP_ID": "events",
    "KAFKA_RETRY_GROUP_ID": "retry",
    "KAFKA_DEAD_LETTER_GROUP_ID": "dead-letter",
    "KAFKA_NUM_PARTITIONS": "1",
    "KAFKA_REPLICATION_FACTOR": "1",
    "KAFKA_MAX_POLL_RECORDS": "1",
    "KAFKA_SESSION_TIMEOUT_MS": "10000",
    "KAFKA_HEARTBEAT_INTERVAL_MS": "3000",
    "KAFKA_AUTO_OFFSET_RESET": "earliest",
    "KAFKA_ENABLE_AUTO_COMMIT": "false",
    "KAFKA_PRODUCER_ACKS": "all",
    "KAFKA_PRODUCER_RETRIES": "0",
    "KAFKA_PRODUCER_ENABLE_IDEMPOTENCE": "false",
    # Unit tests exercise the local concurrency bulkhead unless a test opts in
    # to Redis explicitly.  A clean checkout must never contact deployment
    # infrastructure merely because a model call is mocked.
    "SELF_INGESTION_DISTRIBUTED_CONCURRENCY": "false",
}
for _name, _value in _TEST_ENV_DEFAULTS.items():
    os.environ.setdefault(_name, _value)

# Unit tests must never inherit developer or deployment infrastructure backends.
# Backend settings may already have loaded backend/.env before test modules are
# collected, so overwrite the inherited values. Integration/persistence tests
# opt in again explicitly with monkeypatch.
os.environ["AGENT_RESOURCE_LEASE_BACKEND"] = "memory"
os.environ["AGENT_RUNTIME_STORE_BACKEND"] = "disabled"
os.environ["AGENT_TRACE_BACKEND"] = "jsonl"
os.environ["LOG_KAFKA_ENABLED"] = "false"


@pytest.fixture(autouse=True)
def _isolated_process_shared_state():
    """Reset intentionally process-shared runtime state between tests.

    Token calibration is shared per model name and the skill directory scan is
    shared per root path; without a reset, tests would leak state into each
    other and results would depend on execution order.
    """

    from backend.application.conversations.thread_guard import (
        reset_conversation_thread_guard,
    )
    from backend.core.agents.context.ledger import reset_shared_token_calibration
    from backend.core.skills.scoped_skills_provider import invalidate_all_skill_caches

    reset_shared_token_calibration()
    invalidate_all_skill_caches()
    reset_conversation_thread_guard()
    yield


@pytest.fixture(scope="session")
def event_loop_policy():
    if sys.platform == "win32":
        import asyncio

        return asyncio.DefaultEventLoopPolicy()

    import uvloop

    return uvloop.EventLoopPolicy()
