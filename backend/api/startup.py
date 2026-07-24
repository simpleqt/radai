"""Startup orchestration shared by API entrypoints."""

from __future__ import annotations

import asyncio
import concurrent.futures
import os
from collections.abc import Awaitable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from sqlalchemy import select

from backend.application.blocking import (
    initialize_blocking_executor,
    shutdown_blocking_executor,
)
from backend.application.checkpointing.checkpointer import (
    cleanup_app_checkpointer,
    init_app_checkpointer,
)
from backend.config.dependency_versions import (
    RuntimeDependencyError,
    validate_runtime_dependencies,
)
from backend.config.loggers import get_logger
from backend.config.logging import setup_logging
from backend.config.settings import settings
from backend.db.connection import close_db, close_db_async, get_async_session_factory
from backend.db.models.knowledge_base import KnowledgeBase
from backend.db.redis import close_redis_pool
from backend.rag.knowledge_base.manager import get_kb_manager
from backend.rag.service import get_rag_service
from backend.rag.sync import get_rag_index_sync_manager, get_rag_index_sync_scheduler
from backend.services.startup_task_processor import get_startup_task_processor

logger = get_logger(__name__)

ALLOW_QUERY_TOKEN_AUTH = os.getenv("ALLOW_QUERY_TOKEN_AUTH", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STARTUP_INIT_TIMEOUT_SECONDS = float(os.getenv("STARTUP_INIT_TIMEOUT_SECONDS", "12"))
STARTUP_TASK_PROCESSOR_ENABLED = os.getenv("STARTUP_TASK_PROCESSOR_ENABLED", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STARTUP_RAG_PREWARM_ENABLED = os.getenv("STARTUP_RAG_PREWARM_ENABLED", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STARTUP_KEYWORD_INDEX_PREWARM_ENABLED = os.getenv(
    "STARTUP_KEYWORD_INDEX_PREWARM_ENABLED", "false"
).lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STARTUP_KEYWORD_GRAPH_PREWARM_ENABLED = os.getenv(
    "STARTUP_KEYWORD_GRAPH_PREWARM_ENABLED", "false"
).lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STARTUP_KEYWORD_GRAPH_PREWARM_TIMEOUT_SECONDS = float(
    os.getenv("STARTUP_KEYWORD_GRAPH_PREWARM_TIMEOUT_SECONDS", "180")
)
TASK_DISPATCH_OUTBOX_ENABLED = os.getenv(
    "TASK_DISPATCH_OUTBOX_ENABLED", "false"
).lower() in {
    "1",
    "true",
    "yes",
    "on",
}
TLS_CERT_SANDBOX_ENABLED = os.getenv("TLS_CERT_SANDBOX_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ASYNCIO_THREAD_POOL_MAX_WORKERS = int(
    os.getenv("ASYNCIO_THREAD_POOL_MAX_WORKERS", "64") or "64"
)
API_SERVICE_ROLE = os.getenv("API_SERVICE_ROLE", "all").strip().lower() or "all"
_API_SERVICE_ROLES = {"all", "agent", "knowledge"}


def service_role() -> str:
    if API_SERVICE_ROLE not in _API_SERVICE_ROLES:
        raise RuntimeError(
            "API_SERVICE_ROLE must be one of: all, agent, knowledge "
            f"(got {API_SERVICE_ROLE!r})"
        )
    return API_SERVICE_ROLE


def setup_app_dirs() -> None:
    """设置应用目录。"""
    base_dir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    data_dir = os.path.join(base_dir, "data")
    os.makedirs(data_dir, exist_ok=True)


def configure_tls_cert_loading() -> None:
    """Configure deterministic TLS cert loading for rust reqwest clients."""
    if not TLS_CERT_SANDBOX_ENABLED:
        logger.info("[TLS] 证书受控加载已禁用（TLS_CERT_SANDBOX_ENABLED=false）")
        return

    backend_dir = Path(__file__).resolve().parents[1]
    empty_cert_dir = backend_dir / "data" / "tls-empty-cert-dir"
    empty_cert_dir.mkdir(parents=True, exist_ok=True)

    cert_candidates = [
        os.getenv("TLS_CERT_FILE", "").strip(),
        "/etc/ssl/certs/ca-certificates.crt",
        "/etc/pki/tls/certs/ca-bundle.crt",
    ]

    chosen_cert_file = ""
    for candidate in cert_candidates:
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.R_OK):
            chosen_cert_file = candidate
            break

    if chosen_cert_file:
        os.environ["SSL_CERT_FILE"] = chosen_cert_file
        os.environ.setdefault("REQUESTS_CA_BUNDLE", chosen_cert_file)
        os.environ.setdefault("CURL_CA_BUNDLE", chosen_cert_file)
    else:
        logger.warning("[TLS] 未找到可读 CA 证书文件，将仅设置空 SSL_CERT_DIR")

    os.environ["SSL_CERT_DIR"] = str(empty_cert_dir)
    logger.info(
        "[TLS] 已启用受控证书加载: SSL_CERT_FILE=%s, SSL_CERT_DIR=%s",
        os.environ.get("SSL_CERT_FILE", "<unset>"),
        os.environ.get("SSL_CERT_DIR", "<unset>"),
    )


def configure_asyncio_default_executor() -> concurrent.futures.ThreadPoolExecutor | None:
    if ASYNCIO_THREAD_POOL_MAX_WORKERS <= 0:
        logger.info("[线程池] 使用 Python 默认线程池大小")
        return None

    loop = asyncio.get_running_loop()
    executor = concurrent.futures.ThreadPoolExecutor(
        max_workers=ASYNCIO_THREAD_POOL_MAX_WORKERS,
        thread_name_prefix="api-worker",
    )
    loop.set_default_executor(executor)
    logger.info("[线程池] 已设置 asyncio 默认线程池大小: %d", ASYNCIO_THREAD_POOL_MAX_WORKERS)
    return executor


@asynccontextmanager
async def main_lifespan(app: FastAPI):
    """应用生命周期管理。"""
    role = service_role()
    runs_agent_runtime = role in {"all", "agent"}
    runs_knowledge_runtime = role in {"all", "knowledge"}
    setup_app_dirs()
    configure_tls_cert_loading()
    setup_logging()
    try:
        dependency_report = validate_runtime_dependencies()
    except RuntimeDependencyError:
        logger.exception("[依赖校验] 运行时依赖版本不符合项目基线，拒绝启动")
        raise
    logger.info("文档处理服务启动: role=%s", role)
    logger.info("服务配置: host=%s, port=%s", settings.server_host, settings.server_port)
    logger.info("LLM Provider: %s", settings.llm_provider)
    default_executor = configure_asyncio_default_executor()
    initialize_blocking_executor()
    app.state.startup_components = {
        "dependencies": {
            "status": "ready",
            "error": "",
            "versions": dependency_report.as_dict(),
        },
        "database": {"status": "starting", "error": ""},
    }
    if runs_knowledge_runtime:
        app.state.startup_components["rag"] = {"status": "starting", "error": ""}
    if runs_agent_runtime:
        app.state.startup_components["checkpointer"] = {"status": "starting", "error": ""}

    startup_background_tasks: list[asyncio.Task] = []

    if runs_agent_runtime:
        from backend.core.models.model_cache import subscribe_model_changes
        from backend.core.skills.skill_cache import subscribe_skill_changes

        startup_background_tasks.append(asyncio.create_task(subscribe_model_changes()))
        startup_background_tasks.append(asyncio.create_task(subscribe_skill_changes()))

    async def _wait_with_timeout(
        name: str,
        coro: Awaitable[None],
        timeout_seconds: float = STARTUP_INIT_TIMEOUT_SECONDS,
    ) -> None:
        task = asyncio.create_task(coro)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            startup_background_tasks.append(task)
            logger.warning("[%s] 初始化超过 %.1fs，切换为后台继续执行", name, timeout_seconds)

    async def _init_rag() -> None:
        try:
            await get_rag_service().initialize()
            app.state.startup_components["rag"] = {"status": "ready", "error": ""}
            logger.info("RAGAnything 初始化完成")
        except Exception as exc:
            app.state.startup_components["rag"] = {
                "status": "error",
                "error": type(exc).__name__,
            }
            logger.exception("RAGAnything 初始化失败: %s", exc)

    async def _init_database_readiness() -> None:
        try:
            factory = get_async_session_factory()
            async with factory() as session:
                await session.execute(select(1))
            app.state.startup_components["database"] = {"status": "ready", "error": ""}
            logger.info("数据库连接检查完成")
        except Exception as exc:
            app.state.startup_components["database"] = {
                "status": "error",
                "error": type(exc).__name__,
            }
            logger.warning("数据库连接检查失败: %s", exc)

    async def _prewarm_rag_instances() -> None:
        if not STARTUP_RAG_PREWARM_ENABLED:
            logger.info("[RAG预热] 已禁用（STARTUP_RAG_PREWARM_ENABLED=false）")
            return

        try:
            rag_service = get_rag_service()
            await rag_service.initialize()
            factory = get_async_session_factory()
            async with factory() as session:
                result = await session.execute(
                    select(KnowledgeBase.kb_id).where(KnowledgeBase.status == "active")
                )
                active_kb_ids = [row[0] for row in result.all() if row and row[0]]

            if not active_kb_ids:
                logger.info("[RAG预热] 无活跃知识库")
                return

            started_at = asyncio.get_running_loop().time()
            for kb_id in active_kb_ids:
                try:
                    await rag_service._get_rag_instance(kb_id)
                except Exception as exc:
                    logger.warning("[RAG预热] 预热失败: kb_id=%s, error=%s", kb_id, exc)

            elapsed = asyncio.get_running_loop().time() - started_at
            logger.info("[RAG预热] 完成: 预热知识库=%d, 耗时=%.2fs", len(active_kb_ids), elapsed)
        except Exception as exc:
            logger.warning("[RAG预热] 执行失败: %s", exc)

    async def _init_keyword_index() -> None:
        if not STARTUP_KEYWORD_INDEX_PREWARM_ENABLED:
            logger.info("[关键词索引] 预加载已禁用（STARTUP_KEYWORD_INDEX_PREWARM_ENABLED=false）")
            return

        try:
            factory = get_async_session_factory()
            async with factory() as session:
                result = await session.execute(
                    select(KnowledgeBase).where(KnowledgeBase.status == "active")
                )
                kbs = result.scalars().all()

                if not kbs:
                    logger.info("[关键词索引] 无活跃知识库")
                    return

                kb_manager = get_kb_manager()
                init_tasks = []

                for kb in kbs:

                    async def _init_single_kb(kb_code: str, kb_id: str) -> None:
                        try:
                            index = await kb_manager._keyword_index_manager.get_index(kb_code, kb_id)
                            logger.info(
                                "[关键词索引] 预加载完成: %s, 实体数=%d",
                                kb_code,
                                len(index.entity_names),
                            )
                        except Exception as exc:
                            logger.warning("[关键词索引] 预加载失败: %s, %s", kb_code, exc)

                    init_tasks.append(_init_single_kb(kb.kb_code, kb.kb_id))

                await asyncio.gather(*init_tasks, return_exceptions=True)
                logger.info("[关键词索引] 所有知识库预加载完成, 共 %d 个", len(kbs))
        except Exception as exc:
            logger.warning("[关键词索引] 预加载失败: %s", exc)

    async def _prewarm_keyword_graph_cache() -> None:
        if not STARTUP_KEYWORD_GRAPH_PREWARM_ENABLED:
            logger.info("[关键词图缓存预热] 已禁用（STARTUP_KEYWORD_GRAPH_PREWARM_ENABLED=false）")
            return

        try:
            factory = get_async_session_factory()
            async with factory() as session:
                result = await session.execute(
                    select(KnowledgeBase).where(KnowledgeBase.status == "active")
                )
                kbs = result.scalars().all()

                if not kbs:
                    logger.info("[关键词图缓存预热] 无活跃知识库")
                    return

                kb_manager = get_kb_manager()
                started_at = asyncio.get_running_loop().time()

                for kb in kbs:
                    try:
                        extractor = await kb_manager.get_keyword_extractor(session, kb.kb_id)
                        await extractor._build_full_graph()
                        node_count = (
                            extractor._graph.number_of_nodes()
                            if extractor._graph is not None
                            else 0
                        )
                        edge_count = (
                            extractor._graph.number_of_edges()
                            if extractor._graph is not None
                            else 0
                        )
                        logger.info(
                            "[关键词图缓存预热] 完成: kb=%s, 节点=%d, 边=%d",
                            kb.kb_code,
                            node_count,
                            edge_count,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[关键词图缓存预热] 失败: kb=%s, error=%s",
                            kb.kb_code,
                            exc,
                        )

                elapsed = asyncio.get_running_loop().time() - started_at
                logger.info("[关键词图缓存预热] 全部完成: 知识库=%d, 耗时=%.2fs", len(kbs), elapsed)
        except Exception as exc:
            logger.warning("[关键词图缓存预热] 执行失败: %s", exc)

    async def _init_index_sync_scheduler() -> None:
        try:
            scheduler = get_rag_index_sync_scheduler()
            await get_rag_index_sync_manager().initialize()

            factory = get_async_session_factory()
            async with factory() as session:
                result = await session.execute(
                    select(KnowledgeBase).where(KnowledgeBase.status == "active")
                )
                kbs = result.scalars().all()

                if kbs:
                    kb_codes = [kb.kb_code for kb in kbs]
                    kb_ids = {kb.kb_code: kb.kb_id for kb in kbs}
                    await scheduler.start(kb_codes, kb_ids)
                    logger.info("[索引同步调度器] 已启动, 知识库: %s", kb_codes)
                else:
                    logger.info("[索引同步调度器] 无活跃知识库")
        except Exception as exc:
            logger.warning("[索引同步调度器] 启动失败: %s", exc)

    async def _cleanup_index_sync_scheduler() -> None:
        try:
            scheduler = get_rag_index_sync_scheduler()
            await scheduler.stop()
            logger.info("[索引同步调度器] 已停止")
        except Exception as exc:
            logger.warning("[索引同步调度器] 停止失败: %s", exc)

    async def _init_startup_task_processor() -> None:
        try:
            processor = get_startup_task_processor()
            await processor.initialize()
            processor.start_background_processor()
            logger.info("[启动任务处理器] 已在主事件循环启动")
        except Exception as exc:
            logger.warning("[启动任务处理器] 启动失败: %s", exc)

    async def _cleanup_startup_task_processor() -> None:
        try:
            processor = get_startup_task_processor()
            await processor.stop_background_processor()
            logger.info("[启动任务处理器] 已停止")
        except Exception as exc:
            logger.warning("[启动任务处理器] 停止失败: %s", exc)

    async def _init_checkpointer() -> None:
        try:
            db_path = await init_app_checkpointer(app, "checkpoints.db")
            app.state.startup_components["checkpointer"] = {"status": "ready", "error": ""}
            logger.info(
                "Checkpointer (%s) 初始化完成: %s",
                getattr(app.state, "checkpointer_backend", "unknown"),
                db_path,
            )
        except Exception as exc:
            app.state.startup_components["checkpointer"] = {
                "status": "error",
                "error": type(exc).__name__,
            }
            logger.exception("Checkpointer 初始化失败: %s", exc)

    async def _cleanup_checkpointer() -> None:
        try:
            if await cleanup_app_checkpointer(app):
                logger.info("Checkpointer 数据库连接已关闭")
        except Exception as exc:
            logger.warning("Checkpointer 关闭失败: %s", exc)

    async def _init_experiment_datasets() -> None:
        try:
            from backend.db.init_datasets import (
                init_dataset_table,
                register_default_datasets,
            )

            await init_dataset_table()
            await register_default_datasets()
            logger.info("[实验数据集] 初始化完成")
        except Exception as exc:
            logger.warning("[实验数据集] 初始化失败: %s", exc)

    async def _init_admin_tables() -> None:
        try:
            from backend.db.init_admin import init_admin_tables

            await init_admin_tables()
            logger.info("[管理员后台] 表初始化完成")
        except Exception as exc:
            logger.warning("[管理员后台] 表初始化失败: %s", exc)

    startup_initializers = [
        _wait_with_timeout("数据库", _init_database_readiness()),
    ]
    if runs_knowledge_runtime:
        startup_initializers.extend(
            [
                _wait_with_timeout("RAG", _init_rag()),
                _wait_with_timeout("RAG实例预热", _prewarm_rag_instances()),
                _wait_with_timeout("关键词索引", _init_keyword_index()),
                _wait_with_timeout(
                    "关键词图缓存预热",
                    _prewarm_keyword_graph_cache(),
                    timeout_seconds=STARTUP_KEYWORD_GRAPH_PREWARM_TIMEOUT_SECONDS,
                ),
                _wait_with_timeout("索引同步调度器", _init_index_sync_scheduler()),
            ]
        )
    if runs_agent_runtime:
        startup_initializers.extend(
            [
                _wait_with_timeout("Checkpointer", _init_checkpointer()),
                _wait_with_timeout("实验数据集", _init_experiment_datasets()),
                _wait_with_timeout("管理员后台表", _init_admin_tables()),
            ]
        )
    try:
        await asyncio.gather(*startup_initializers)

        if TASK_DISPATCH_OUTBOX_ENABLED:
            from backend.services.task_dispatch_outbox import task_dispatch_outbox

            await task_dispatch_outbox.start()
            logger.info("[任务投递 outbox] 后台对账已启动")
        else:
            logger.warning("[任务投递 outbox] 已禁用；管理员任务重试将无法自动补投")

        if runs_knowledge_runtime and STARTUP_TASK_PROCESSOR_ENABLED:
            await _init_startup_task_processor()
        else:
            logger.info("[启动任务处理器] 未在当前服务角色启动")
    except BaseException:
        logger.exception("应用启动失败，开始回收已初始化资源")
        for task in startup_background_tasks:
            if not task.done():
                task.cancel()
        if startup_background_tasks:
            await asyncio.gather(*startup_background_tasks, return_exceptions=True)
        if TASK_DISPATCH_OUTBOX_ENABLED:
            from backend.services.task_dispatch_outbox import task_dispatch_outbox

            try:
                await task_dispatch_outbox.stop()
            except Exception as exc:
                logger.warning("启动失败后关闭任务投递 outbox 失败: %s", exc)
        if runs_knowledge_runtime and STARTUP_TASK_PROCESSOR_ENABLED:
            await _cleanup_startup_task_processor()
        if runs_knowledge_runtime:
            await _cleanup_index_sync_scheduler()
            for label, cleanup_factory in (
                ("索引同步管理器", lambda: get_rag_index_sync_manager().shutdown()),
                ("知识库管理器", lambda: get_kb_manager().shutdown()),
                ("RAG 服务", lambda: get_rag_service().close()),
            ):
                try:
                    await cleanup_factory()
                except Exception as exc:
                    logger.warning("启动失败后关闭 %s 失败: %s", label, exc)
        if runs_agent_runtime:
            await _cleanup_checkpointer()
        from backend.kafka.client import kafka_client

        for label, cleanup_factory in (
            ("Kafka 客户端", kafka_client.stop),
            ("数据库连接", close_db_async),
            ("Redis 连接池", close_redis_pool),
        ):
            try:
                await cleanup_factory()
            except Exception as exc:
                logger.warning("启动失败后关闭 %s 失败: %s", label, exc)
        try:
            await asyncio.to_thread(close_db)
        except Exception as exc:
            logger.warning("启动失败后关闭同步数据库连接失败: %s", exc)
        try:
            shutdown_blocking_executor()
        except Exception as exc:
            logger.warning("启动失败后关闭阻塞执行器失败: %s", exc)
        if default_executor is not None:
            try:
                default_executor.shutdown(wait=False, cancel_futures=True)
            except Exception as exc:
                logger.warning("启动失败后关闭默认执行器失败: %s", exc)
        raise

    try:
        yield
    finally:
        async def _safe_cleanup(label: str, awaitable: Awaitable[Any]) -> None:
            try:
                await awaitable
            except Exception as exc:
                logger.warning("关闭 %s 失败: %s", label, exc)

        if runs_agent_runtime:
            from backend.application.agent_tasks import agent_task_registry

            await _safe_cleanup("Agent 任务注册器", agent_task_registry.shutdown())
        if TASK_DISPATCH_OUTBOX_ENABLED:
            from backend.services.task_dispatch_outbox import task_dispatch_outbox

            await _safe_cleanup("任务投递 outbox", task_dispatch_outbox.stop())
        for task in startup_background_tasks:
            if not task.done():
                task.cancel()
        if startup_background_tasks:
            await asyncio.gather(*startup_background_tasks, return_exceptions=True)

        if runs_knowledge_runtime and STARTUP_TASK_PROCESSOR_ENABLED:
            await _safe_cleanup("启动任务处理器", _cleanup_startup_task_processor())
        if runs_knowledge_runtime:
            await _safe_cleanup("索引同步调度器", _cleanup_index_sync_scheduler())
            await _safe_cleanup("索引同步管理器", get_rag_index_sync_manager().shutdown())
            await _safe_cleanup("知识库管理器", get_kb_manager().shutdown())
            await _safe_cleanup("RAG 服务", get_rag_service().close())
        if runs_agent_runtime:
            await _safe_cleanup("Checkpointer", _cleanup_checkpointer())
            from backend.core.models.base_llm import close_shared_http_clients

            await _safe_cleanup("LLM HTTP 客户端", close_shared_http_clients())
        from backend.kafka.client import kafka_client

        await _safe_cleanup("Kafka 客户端", kafka_client.stop())
        await _safe_cleanup("数据库连接", close_db_async())
        try:
            await asyncio.to_thread(close_db)
        except Exception as exc:
            logger.warning("关闭同步数据库连接失败: %s", exc)
        await _safe_cleanup("Redis 连接池", close_redis_pool())
        try:
            shutdown_blocking_executor()
        except Exception as exc:
            logger.warning("关闭阻塞执行器失败: %s", exc)
        if default_executor is not None:
            try:
                default_executor.shutdown(wait=False, cancel_futures=True)
            except Exception as exc:
                logger.warning("关闭默认执行器失败: %s", exc)

        logger.info("文档处理服务关闭")


# Backward-compatible aliases for existing entrypoints.
_configure_tls_cert_loading = configure_tls_cert_loading
_configure_asyncio_default_executor = configure_asyncio_default_executor
