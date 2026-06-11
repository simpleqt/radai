import asyncio
import concurrent.futures
import contextlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

from typing import Awaitable

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.application.mcp import create_mcp_sse_app
from backend.application.blocking import (
    initialize_blocking_executor,
    shutdown_blocking_executor,
)
from backend.config.filters import clear_user_context, set_user_context
from backend.config.loggers import get_logger
from backend.config.logging import setup_logging
from backend.config.settings import settings
from backend.db.connection import get_async_session_factory
from backend.db.models.knowledge_base import KnowledgeBase
from backend.rag.knowledge_base.manager import get_kb_manager
from backend.rag.service import get_rag_service
from backend.rag.sync import get_rag_index_sync_manager, get_rag_index_sync_scheduler
from backend.services.startup_task_processor import get_startup_task_processor
from sqlalchemy import select

logger = get_logger(__name__)

STARTUP_INIT_TIMEOUT_SECONDS = float(os.getenv("STARTUP_INIT_TIMEOUT_SECONDS", "12"))
STARTUP_TASK_PROCESSOR_ENABLED = os.getenv("STARTUP_TASK_PROCESSOR_ENABLED", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STARTUP_RAG_PREWARM_ENABLED = os.getenv("STARTUP_RAG_PREWARM_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STARTUP_KEYWORD_GRAPH_PREWARM_ENABLED = os.getenv(
    "STARTUP_KEYWORD_GRAPH_PREWARM_ENABLED", "true"
).lower() in {
    "1",
    "true",
    "yes",
    "on",
}
STARTUP_KEYWORD_GRAPH_PREWARM_TIMEOUT_SECONDS = float(
    os.getenv("STARTUP_KEYWORD_GRAPH_PREWARM_TIMEOUT_SECONDS", "180")
)
TLS_CERT_SANDBOX_ENABLED = os.getenv("TLS_CERT_SANDBOX_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
ASYNCIO_THREAD_POOL_MAX_WORKERS = int(
    os.getenv("ASYNCIO_THREAD_POOL_MAX_WORKERS", "64") or "64"
)

def setup_app_dirs():
    """设置应用目录"""
    base_dir = os.path.abspath(os.path.dirname(os.path.dirname(__file__)))
    data_dir = os.path.join(base_dir, "data")
    os.makedirs(data_dir, exist_ok=True)


def _configure_tls_cert_loading() -> None:
    """Configure deterministic TLS cert loading for rust reqwest clients.

    Some third-party clients (via reqwest/rustls) scan system cert directories on Linux,
    which may include unreadable files such as `/etc/ssl/certs/caddy-root.pem` and produce
    noisy permission warnings. We pin cert loading to a known readable CA bundle and force
    SSL_CERT_DIR to an empty app-owned directory to avoid scanning system cert directories.
    """
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
        # Keep Python requests stack aligned as well.
        os.environ.setdefault("REQUESTS_CA_BUNDLE", chosen_cert_file)
        os.environ.setdefault("CURL_CA_BUNDLE", chosen_cert_file)
    else:
        logger.warning("[TLS] 未找到可读 CA 证书文件，将仅设置空 SSL_CERT_DIR")

    # Force reqwest/rustls native cert probing to skip unreadable system cert directories.
    os.environ["SSL_CERT_DIR"] = str(empty_cert_dir)

    logger.info(
        "[TLS] 已启用受控证书加载: SSL_CERT_FILE=%s, SSL_CERT_DIR=%s",
        os.environ.get("SSL_CERT_FILE", "<unset>"),
        os.environ.get("SSL_CERT_DIR", "<unset>"),
    )


def _configure_asyncio_default_executor() -> concurrent.futures.ThreadPoolExecutor | None:
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
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    setup_app_dirs()
    _configure_tls_cert_loading()
    setup_logging()
    logger.info("文档处理服务启动")
    logger.info(f"服务配置: host={settings.server_host}, port={settings.server_port}")
    logger.info(f"LLM Provider: {settings.llm_provider}")
    default_executor = _configure_asyncio_default_executor()
    initialize_blocking_executor()

    startup_background_tasks: list[asyncio.Task] = []

    # 订阅 Redis 模型配置变更
    from backend.core.models.model_cache import subscribe_model_changes
    startup_background_tasks.append(asyncio.create_task(subscribe_model_changes()))

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
            logger.warning(
                "[%s] 初始化超过 %.1fs，切换为后台继续执行",
                name,
                timeout_seconds,
            )

    async def _init_rag() -> None:
        try:
            await get_rag_service().initialize()
            logger.info("RAGAnything 初始化完成")
        except Exception as e:
            logger.exception(f"RAGAnything 初始化失败: {e}")

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
                except Exception as e:
                    logger.warning(f"[RAG预热] 预热失败: kb_id={kb_id}, error={e}")

            elapsed = asyncio.get_running_loop().time() - started_at
            logger.info("[RAG预热] 完成: 预热知识库=%d, 耗时=%.2fs", len(active_kb_ids), elapsed)
        except Exception as e:
            logger.warning(f"[RAG预热] 执行失败: {e}")

    async def _init_keyword_index() -> None:
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
                    async def _init_single_kb(kb_code: str, kb_id: str):
                        try:
                            index = await kb_manager._keyword_index_manager.get_index(kb_code, kb_id)
                            logger.info(f"[关键词索引] 预加载完成: {kb_code}, 实体数={len(index.entity_names)}")
                        except Exception as e:
                            logger.warning(f"[关键词索引] 预加载失败: {kb_code}, {e}")
                    
                    init_tasks.append(_init_single_kb(kb.kb_code, kb.kb_id))
                
                await asyncio.gather(*init_tasks, return_exceptions=True)
                logger.info(f"[关键词索引] 所有知识库预加载完成, 共 {len(kbs)} 个")
        except Exception as e:
            logger.warning(f"[关键词索引] 预加载失败: {e}")

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

                        node_count = extractor._graph.number_of_nodes() if extractor._graph is not None else 0
                        edge_count = extractor._graph.number_of_edges() if extractor._graph is not None else 0
                        logger.info(
                            "[关键词图缓存预热] 完成: kb=%s, 节点=%d, 边=%d",
                            kb.kb_code,
                            node_count,
                            edge_count,
                        )
                    except Exception as e:
                        logger.warning(
                            "[关键词图缓存预热] 失败: kb=%s, error=%s",
                            kb.kb_code,
                            e,
                        )

                elapsed = asyncio.get_running_loop().time() - started_at
                logger.info("[关键词图缓存预热] 全部完成: 知识库=%d, 耗时=%.2fs", len(kbs), elapsed)
        except Exception as e:
            logger.warning(f"[关键词图缓存预热] 执行失败: {e}")

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
                    logger.info(f"[索引同步调度器] 已启动, 知识库: {kb_codes}")
                else:
                    logger.info("[索引同步调度器] 无活跃知识库")
        except Exception as e:
            logger.warning(f"[索引同步调度器] 启动失败: {e}")

    async def _cleanup_index_sync_scheduler() -> None:
        try:
            scheduler = get_rag_index_sync_scheduler()
            await scheduler.stop()
            logger.info("[索引同步调度器] 已停止")
        except Exception as e:
            logger.warning(f"[索引同步调度器] 停止失败: {e}")

    async def _init_startup_task_processor() -> None:
        try:
            processor = get_startup_task_processor()
            await processor.initialize()
            processor.start_background_processor()
            logger.info("[启动任务处理器] 已在主事件循环启动")
        except Exception as e:
            logger.warning(f"[启动任务处理器] 启动失败: {e}")

    async def _cleanup_startup_task_processor() -> None:
        try:
            processor = get_startup_task_processor()
            await processor.stop_background_processor()
            logger.info("[启动任务处理器] 已停止")
        except Exception as e:
            logger.warning(f"[启动任务处理器] 停止失败: {e}")

    async def _init_checkpointer() -> None:
        try:
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
            import aiosqlite
            base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            db_path = os.path.join(base_dir, "data", "checkpoints.db")
            os.makedirs(os.path.dirname(db_path), exist_ok=True)
            
            conn = await aiosqlite.connect(db_path)
            saver = AsyncSqliteSaver(conn)
            await saver.setup()
            app.state.checkpointer = saver
            app.state.checkpointer_conn = conn
            logger.info(f"Checkpointer (SQLite) 初始化完成: {db_path}")
        except Exception as e:
            logger.exception(f"Checkpointer 初始化失败: {e}")

    async def _cleanup_checkpointer() -> None:
        try:
            if hasattr(app.state, "checkpointer_conn"):
                await app.state.checkpointer_conn.close()
                logger.info("Checkpointer 数据库连接已关闭")
        except Exception as e:
            logger.warning(f"Checkpointer 关闭失败: {e}")

    async def _init_experiment_datasets() -> None:
        """初始化实验数据集表并预注册默认数据集"""
        try:
            from backend.db.init_datasets import init_dataset_table, register_default_datasets
            await init_dataset_table()
            await register_default_datasets()
            logger.info("[实验数据集] 初始化完成")
        except Exception as e:
            logger.warning("[实验数据集] 初始化失败: %s", e)

    await asyncio.gather(
        _wait_with_timeout("RAG", _init_rag()),
        _wait_with_timeout("RAG实例预热", _prewarm_rag_instances()),
        _wait_with_timeout("关键词索引", _init_keyword_index()),
        _wait_with_timeout(
            "关键词图缓存预热",
            _prewarm_keyword_graph_cache(),
            timeout_seconds=STARTUP_KEYWORD_GRAPH_PREWARM_TIMEOUT_SECONDS,
        ),
        _wait_with_timeout("索引同步调度器", _init_index_sync_scheduler()),
        _wait_with_timeout("Checkpointer", _init_checkpointer()),
        _wait_with_timeout("实验数据集", _init_experiment_datasets()),
    )

    if STARTUP_TASK_PROCESSOR_ENABLED:
        await _init_startup_task_processor()
    else:
        logger.info("[启动任务处理器] 已禁用（STARTUP_TASK_PROCESSOR_ENABLED=false）")

    yield

    for task in startup_background_tasks:
        if not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    if STARTUP_TASK_PROCESSOR_ENABLED:
        await _cleanup_startup_task_processor()
    await _cleanup_index_sync_scheduler()
    await _cleanup_checkpointer()
    shutdown_blocking_executor()
    if default_executor is not None:
        default_executor.shutdown(wait=False, cancel_futures=True)

    logger.info("文档处理服务关闭")


app = FastAPI(
    title="文档处理智能体服务",
    description="提供文档解析、抽取、增强和入库的智能体服务",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        o.strip() for o in os.getenv("CORS_ALLOWED_ORIGINS", "").split(",") if o.strip()
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def user_context_middleware(request: Request, call_next):
    """
    用户上下文注入中间件

    支持三种认证方式：
    1. Authorization: Bearer <session_token>  → Redis 直接取 user_id/username（无 DB 查询）
    2. X-API-Key: <api_key>                  → 数据库查询 API Key 绑定的用户
    3. Authorization: Bearer <api_key>        → 兼容旧客户端（DB 查询）
    未认证请求静默跳过，不影响正常流程。
    """
    import json
    import secrets as _secrets

    request_id = _secrets.token_hex(8)
    set_user_context(request_id=request_id)
    request.state.user_id = None

    try:
        auth_header = request.headers.get("Authorization", "")
        x_api_key = request.headers.get("X-API-Key", "").strip()
        bearer = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
        # 兼容 ?token= 查询参数（WebSocket / SSE 场景）
        if not bearer:
            bearer = request.query_params.get("token", "").strip()

        resolved_user_id: str | None = None
        resolved_username: str | None = None

        if bearer:
            # 优先尝试 session token（Redis 快速路径，无 DB 查询）
            try:
                from backend.db.redis import get_redis
                redis_client = get_redis()
                try:
                    raw = await redis_client.get(f"auth:token:{bearer}")
                    if raw:
                        info = json.loads(raw)
                        resolved_user_id = info.get("user_id")
                        resolved_username = info.get("username") or info.get("display_name")
                finally:
                    await redis_client.aclose()
            except Exception:
                pass

            # Redis 未命中则尝试 API Key（DB 查询）
            if not resolved_user_id:
                try:
                    from backend.api.dependencies.auth import _resolve_api_key_auth
                    auth = await _resolve_api_key_auth(bearer)
                    if auth and auth.user_id:
                        resolved_user_id = auth.user_id
                except Exception:
                    pass

        elif x_api_key:
            try:
                from backend.api.dependencies.auth import _resolve_api_key_auth
                auth = await _resolve_api_key_auth(x_api_key)
                if auth and auth.user_id:
                    resolved_user_id = auth.user_id
            except Exception:
                pass

        if resolved_user_id:
            set_user_context(user_id=resolved_user_id, username=resolved_username)
            request.state.user_id = resolved_user_id

        response = await call_next(request)
    finally:
        clear_user_context()

    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理"""
    # 流式响应在连接中断或已发送 response.start 后，若再尝试构造 JSONResponse
    # 会触发 "Expected ASGI message 'http.response.body'"。此时应直接上抛，
    # 避免二次发送 response.start。
    if isinstance(exc, RuntimeError) and "Expected ASGI message 'http.response.body'" in str(exc):
        logger.warning("检测到已进入 response.body 阶段的协议错误，跳过全局 JSON 异常响应: %s", exc)
        raise exc

    logger.exception(f"未捕获的异常: {exc}")
    return JSONResponse(
        status_code=500, content={"detail": f"内部服务器错误: {str(exc)}"}
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """HTTP异常处理"""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy", "service": "document-processing-agent"}


@app.get("/config")
async def get_config():
    """获取当前配置"""
    return {
        "llm_provider": settings.llm_provider,
        "llm_model": (
            settings.deepseek_model
            if settings.llm_provider == "deepseek"
            else settings.zhipu_model
        ),
        "vlm_model": settings.vlm_model or "glm-4.6v",
    }


@app.get("/")
async def home():
    return RedirectResponse(url="/ui/")


mcp_sse_app = create_mcp_sse_app()


def create_app() -> FastAPI:
    """应用工厂函数"""
    from backend.api.routers.auth import router as auth_router
    from backend.api.routers.auth_sms import router as auth_sms_router
    from backend.api.routers.conversations import router as conversations_router
    from backend.api.routers.directories import router as directories_router
    from backend.api.routers.document_ingestion import (
        router as document_ingestion_router,
    )
    from backend.api.routers.document_processing import (
        router as document_processing_router,
    )
    from backend.api.routers.extraction import router as extraction_router
    from backend.api.routers.knowledge_base import router as kb_router
    from backend.api.routers.knowledge_base_documents import router as kb_docs_router
    from backend.api.routers.keywords import router as keywords_router
    from backend.api.routers.mcp import router as mcp_router
    from backend.api.routers.rag import router as rag_router
    from backend.api.routers.uploads import router as uploads_router
    from backend.api.routers.artifacts import router as artifacts_router
    from backend.api.routers.files import router as files_router
    from backend.api.routers.chat_files import router as chat_files_router
    from backend.api.routers.models import router as models_router
    from backend.api.routers.my_files import router as my_files_router
    from backend.api.routers.asr import router as asr_router
    from backend.api.routers.agents import router as agents_router
    from backend.api.routers.datasets import router as datasets_router
    from backend.api.routers.chart_images import router as chart_images_router
    from backend.api.routers.admin_conversations import router as admin_conv_router
    from backend.api.routers.groundwater_projects import (
        router as groundwater_projects_router,
    )

    app.include_router(kb_docs_router, prefix="/api/v1", tags=["知识库文档"])
    app.include_router(auth_router, prefix="/api/v1", tags=["认证"])
    app.include_router(auth_sms_router, prefix="", tags=["短信认证"])
    app.include_router(conversations_router, prefix="/api/v2", tags=["对话管理"])
    app.include_router(
        document_processing_router, prefix="/api/v1/documents", tags=["文档处理"]
    )
    app.include_router(
        extraction_router, prefix="/api/v1/extraction", tags=["内容抽取"]
    )
    app.include_router(kb_router, prefix="/api/v1", tags=["知识库管理"])
    app.include_router(directories_router, prefix="/api/v1", tags=["知识库目录"])
    app.include_router(keywords_router, prefix="/api/v1", tags=["关键词管理"])
    app.include_router(rag_router, prefix="/api/v1/rag", tags=["RAG服务"])
    app.include_router(
        document_ingestion_router, prefix="/api/v1/ingestion", tags=["文档入库"]
    )
    app.include_router(mcp_router, prefix="/api/v1/mcp", tags=["MCP服务"])
    app.include_router(uploads_router, prefix="/api/v1", tags=["文件上传"])
    app.include_router(artifacts_router, prefix="/api/v2/threads", tags=["线程工件"])
    app.include_router(files_router, prefix="/api/v2", tags=["文件中心"])
    app.include_router(chat_files_router, prefix="/api/v2", tags=["对话文件"])
    app.include_router(models_router, prefix="/api/v1/models", tags=["模型管理"])
    app.include_router(asr_router, prefix="/api/v1", tags=["语音转写"])
    app.include_router(my_files_router)
    app.include_router(agents_router, prefix="/api/v1/agents", tags=["智能体管理"])
    app.include_router(datasets_router, prefix="/api/v1", tags=["实验数据集"])
    app.include_router(chart_images_router, prefix="/api/v1", tags=["可视化图表"])
    app.include_router(admin_conv_router, prefix="/api/v1", tags=["管理员-会话管理"])
    app.include_router(groundwater_projects_router, prefix="/api/v2")

    # 注册 v2 API 路由 (全功能智能体)
    try:
        from backend.api.routers.agent_runtime_v2 import api_router as v2_router

        app.include_router(v2_router)
        logger.info("[路由] v2 API 路由已注册: /api/v2/agent-runtime, /api/v2/guest, /api/v2/explore")
    except Exception as e:
        logger.warning(f"[路由] v2 API 路由注册失败 (智能体模块可能未配置): {e}")

    frontend_dir = Path(__file__).resolve().parents[2] / "frontend"
    if frontend_dir.exists() and frontend_dir.is_dir():
        app.mount("/ui", StaticFiles(directory=str(frontend_dir), html=True), name="ui")

    html_ppt_assets_dir = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "skills"
        / "public"
        / "html-ppt"
        / "assets"
    )
    if html_ppt_assets_dir.exists() and html_ppt_assets_dir.is_dir():
        app.mount(
            "/static/html-ppt-assets",
            StaticFiles(directory=str(html_ppt_assets_dir)),
            name="html-ppt-assets",
        )

    # 挂载本地图表文件目录（MinIO 不可用时的降级方案）
    charts_dir = Path(__file__).resolve().parents[1] / "data" / "charts"
    charts_dir.mkdir(parents=True, exist_ok=True)
    app.mount("/data/charts", StaticFiles(directory=str(charts_dir)), name="charts")

    app.mount("/mcp/sse", mcp_sse_app)

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    bind_host = settings.server_host
    if bind_host in {"127.0.0.1", "localhost"}:
        bind_host = "0.0.0.0"

    kwargs = {
        "host": bind_host,
        "port": settings.server_port,
        "reload": settings.server_reload,
    }

    if settings.server_reload:
        kwargs["reload_dirs"] = ["backend"]
        kwargs["reload_excludes"] = [
            "backend/logs",
            "backend/data",
            "*.log",
            "*.md",
            "*.xlsx",
            "backend/logs/*",
        ]
    else:
        kwargs["workers"] = int(os.environ.get("SERVER_WORKERS", "4"))

    uvicorn.run(
        "backend.api.http_server:app",
        **kwargs
    )
