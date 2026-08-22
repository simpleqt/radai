"""访客专用精简 API 服务

为 guest 前端提供登录 + 问答 + 会话能力，不包含知识库管理、文档入库、MCP 等管理端路由。
默认监听 8083 端口，checkpoint 使用 guest_checkpoints.db（与 8082 的 checkpoints.db 隔离）。
"""

import asyncio
import hashlib
import os
from contextlib import asynccontextmanager

os.environ.setdefault("GUEST_API_SERVICE", "1")

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import select

from backend.api.rate_limit import (
    env_enabled,
    positive_int_env,
    request_rate_limiter,
)
from backend.api.startup import (
    STARTUP_INIT_TIMEOUT_SECONDS,
    STARTUP_KEYWORD_GRAPH_PREWARM_TIMEOUT_SECONDS,
    STARTUP_RAG_PREWARM_ENABLED,
    _configure_asyncio_default_executor,
    _configure_tls_cert_loading,
    setup_app_dirs,
)
from backend.application.checkpointing.checkpointer import (
    cleanup_app_sqlite_checkpointer,
    init_app_sqlite_checkpointer,
    init_sqlite_checkpointer,
)
from backend.config.dependency_versions import (
    RuntimeDependencyError,
    validate_runtime_dependencies,
)
from backend.config.filters import clear_user_context, set_user_context
from backend.config.loggers import get_logger
from backend.config.logging import setup_guest_logging
from backend.config.runtime_security import (
    cors_allow_origins,
    fastapi_documentation_kwargs,
    install_fastapi_documentation_guard,
)
from backend.config.settings import settings
from backend.core.utils.client_ip import resolve_client_ip
from backend.db.connection import get_async_session_factory
from backend.db.models.knowledge_base import KnowledgeBase
from backend.db.redis import close_redis_pool

logger = get_logger(__name__)

GUEST_SERVER_PORT = int(os.getenv("GUEST_SERVER_PORT", "8083"))
GUEST_CHECKPOINT_DB = os.getenv("GUEST_CHECKPOINT_DB", "guest_checkpoints.db")
GUEST_SERVER_WORKERS = int(os.getenv("GUEST_SERVER_WORKERS", "1"))
GUEST_RAG_STARTUP_ENABLED = os.getenv(
    "GUEST_RAG_STARTUP_ENABLED", "true"
).lower() in {"1", "true", "yes", "on"}

GUEST_API_ROUTES = """
需要:
  POST /api/v1/auth/login
  POST /api/v2/guest/query
  POST /api/v2/agent-runtime/query
  GET  /api/v2/agent-runtime/profiles/{agent_code}/info
  GET  /api/v2/conversations
  GET  /api/v2/conversations/{thread_id}/messages
不需要:
  知识库管理、文档入库、MCP、Worker、管理端
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    """访客服务最小化启动：RAG + Checkpointer。"""
    setup_app_dirs()
    _configure_tls_cert_loading()
    setup_guest_logging()
    try:
        dependency_report = validate_runtime_dependencies()
    except RuntimeDependencyError:
        logger.exception("[依赖校验] 访客 API 运行时依赖版本不符合项目基线，拒绝启动")
        raise
    # 版本清单只进服务端日志：/health 是匿名端点，不向公网暴露依赖版本组合。
    logger.info("访客 API 运行时依赖版本: %s", dependency_report.as_dict())
    logger.info(
        "访客问答服务启动: host=%s, port=%s, checkpoint=%s",
        settings.server_host,
        GUEST_SERVER_PORT,
        GUEST_CHECKPOINT_DB,
    )
    default_executor = _configure_asyncio_default_executor()
    app.state.startup_components = {
        "dependencies": {
            "status": "ready",
            "error": "",
        },
        "knowledge_service": {"status": "starting", "error": ""},
        "checkpointer": {"status": "starting", "error": ""},
    }
    startup_background_tasks: list[asyncio.Task] = []

    async def _wait_with_timeout(name: str, coro, timeout_seconds: float = STARTUP_INIT_TIMEOUT_SECONDS) -> None:
        task = asyncio.create_task(coro)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            startup_background_tasks.append(task)
            logger.warning("[%s] 初始化超过 %.1fs，切换为后台继续执行", name, timeout_seconds)

    async def _init_rag() -> None:
        """知识库运行时已迁至 omnigraph;此处仅探测知识服务可达性。"""
        import httpx

        from backend.api.routers.knowledge_proxy import knowledge_service_base_url

        base_url = knowledge_service_base_url()
        try:
            async with httpx.AsyncClient(trust_env=False, timeout=3.0) as client:
                response = await client.get(f"{base_url}/live")
            if response.status_code == 200:
                app.state.startup_components["knowledge_service"] = {
                    "status": "ready",
                    "error": "",
                }
                logger.info("[Guest] omnigraph 知识服务可达: %s", base_url)
            else:
                raise RuntimeError(f"status={response.status_code}")
        except Exception as exc:
            app.state.startup_components["knowledge_service"] = {
                "status": "error",
                "error": type(exc).__name__,
            }
            logger.warning("[Guest] omnigraph 知识服务不可达: %s error=%s", base_url, exc)

    async def _load_guest_knowledge_bases() -> list[KnowledgeBase]:
        from backend.db.crud.agent_crud import get_runtime_agent_kb_ids

        factory = get_async_session_factory()
        async with factory() as session:
            guest_kb_ids = await get_runtime_agent_kb_ids(session, "ne")
            if not guest_kb_ids:
                return []
            result = await session.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.kb_id.in_(guest_kb_ids),
                    KnowledgeBase.status == "active",
                )
            )
            return list(result.scalars().all())


    async def _prewarm_retrieval_stack() -> None:
        """访客知识库列表来自共享 MySQL,无需预热检索器。"""
        logger.info("[Guest] 检索栈预热已随知识库运行时迁移至 omnigraph")

    async def _init_checkpointer() -> None:
        try:
            checkpoint_data_dir = (
                os.getenv("GUEST_CHECKPOINT_DATA_DIR")
                or os.getenv("AGENT_CHECKPOINT_DATA_DIR")
                or None
            )
            db_path = await init_app_sqlite_checkpointer(
                app,
                GUEST_CHECKPOINT_DB,
                data_dir=checkpoint_data_dir,
            )
            app.state.startup_components["checkpointer"] = {
                "status": "ready",
                "error": "",
            }
            logger.info("[Guest] Checkpointer 初始化完成: %s", db_path)
        except Exception as exc:
            app.state.startup_components["checkpointer"] = {
                "status": "error",
                "error": type(exc).__name__,
            }
            logger.exception("[Guest] Checkpointer 初始化失败: %s", exc)

    async def _init_main_checkpointer_fallback() -> None:
        """主站 checkpoint 只读回退，供登录用户读取主站产生的历史消息。"""
        main_db = os.getenv("MAIN_CHECKPOINT_DB", "checkpoints.db")
        try:
            checkpoint_data_dir = os.getenv("AGENT_CHECKPOINT_DATA_DIR") or None
            saver, conn, db_path = await init_sqlite_checkpointer(
                main_db,
                data_dir=checkpoint_data_dir,
            )
            app.state.main_checkpointer = saver
            app.state.main_checkpointer_conn = conn
            logger.info("[Guest] 主站 Checkpointer 回退已加载: %s", db_path)
        except Exception as exc:
            app.state.main_checkpointer = None
            app.state.main_checkpointer_conn = None
            logger.warning("[Guest] 主站 Checkpointer 回退加载失败: %s", exc)

    async def _cleanup_checkpointer() -> None:
        try:
            if await cleanup_app_sqlite_checkpointer(app):
                logger.info("[Guest] Checkpointer 连接已关闭")
        except Exception as exc:
            logger.warning("[Guest] Checkpointer 关闭失败: %s", exc)
        main_conn = getattr(app.state, "main_checkpointer_conn", None)
        if main_conn is not None:
            try:
                await main_conn.close()
                logger.info("[Guest] 主站 Checkpointer 回退连接已关闭")
            except Exception as exc:
                logger.warning("[Guest] 主站 Checkpointer 回退关闭失败: %s", exc)
            app.state.main_checkpointer_conn = None
            app.state.main_checkpointer = None

    startup_jobs = [
        _wait_with_timeout("Checkpointer", _init_checkpointer()),
        _wait_with_timeout("主站Checkpointer回退", _init_main_checkpointer_fallback()),
    ]
    if GUEST_RAG_STARTUP_ENABLED:
        startup_jobs.extend(
            [
                _wait_with_timeout("知识服务探测", _init_rag()),
            ]
        )
    else:
        app.state.startup_components["knowledge_service"] = {
            "status": "degraded",
            "error": "startup_disabled",
        }
        logger.warning(
            "[Guest] 已跳过知识服务探测；认证和会话接口继续提供服务"
        )

    await asyncio.gather(*startup_jobs)

    try:
        yield
    finally:
        for task in startup_background_tasks:
            if not task.done():
                task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.warning(
                    "[Guest] 启动后台任务关闭失败: task=%s",
                    task.get_name(),
                    exc_info=True,
                )
        await _cleanup_checkpointer()
        try:
            await close_redis_pool()
        except Exception as exc:
            logger.warning("[Guest] Redis 连接池关闭失败: %s", exc)
        finally:
            if default_executor is not None:
                default_executor.shutdown(wait=False, cancel_futures=True)

        logger.info("访客问答服务关闭")


app = FastAPI(
    title="访客前端专用 API",
    description=GUEST_API_ROUTES.strip(),
    version="1.0.0",
    lifespan=lifespan,
    **fastapi_documentation_kwargs(),
)
install_fastapi_documentation_guard(app)

app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_allow_origins(
        [
            "http://localhost:8085",
            "http://127.0.0.1:8085",
            "http://localhost:8084",
            "http://127.0.0.1:8084",
            "https://fpitekai.com",
            "http://202.115.138.57:8084",
            "http://162.14.80.195:8084",
        ],
        env_var="GUEST_CORS_ALLOW_ORIGINS",
    ),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Request-ID", "Accept", "X-Device-ID"],
)


_RATE_LIMIT_EXEMPT_PATHS = {"/health", "/api/v1/health"}


def _guest_rate_limit_identities(request: Request) -> list[tuple[str, int]]:
    """Rate-limit buckets a guest request must pass, strongest bound first.

    A guest caller has no account, so the device header is the only stable
    per-caller key. It is client-controlled, though: rotating ``X-Device-ID``
    used to mint a fresh bucket per request, effectively bypassing the limit.
    Every request therefore also consumes a per-IP bucket (higher default, so
    legitimate NAT sharing is not throttled), capping rotation abuse at the
    IP-tier rate instead of unbounded.
    """

    buckets: list[tuple[str, int]] = []
    device_id = request.headers.get("X-Device-ID", "").strip()
    if device_id:
        device_limit = positive_int_env("GUEST_RATE_LIMIT_PER_MINUTE", 60)
        buckets.append(
            (
                "guest-device:"
                + hashlib.sha256(device_id.encode("utf-8")).hexdigest(),
                device_limit,
            )
        )
    ip_limit = positive_int_env("GUEST_RATE_LIMIT_PER_IP_PER_MINUTE", 600)
    buckets.append((f"guest-ip:{resolve_client_ip(request)}", ip_limit))
    return buckets


@app.middleware("http")
async def user_context_middleware(request: Request, call_next):
    """为每个请求生成 request_id 并注入日志上下文（访客服务无登录态）。

    同时执行 HTTP 准入限流：访客服务此前完全没有限流，未认证调用方可以
    无限触发 /api/v2/guest/query 的完整检索与模型推理（以及本进程挂载的
    agent-runtime），直接打满算力与外部模型配额。设备桶与 IP 桶双计：设备
    头可伪造轮换，单靠它等效无限流。
    """
    import secrets as _secrets

    set_user_context(request_id=_secrets.token_hex(8), user_id="guest")
    try:
        if request.url.path not in _RATE_LIMIT_EXEMPT_PATHS:
            for identity_key, identity_limit in _guest_rate_limit_identities(request):
                allowed, retry_after = await request_rate_limiter.allow(
                    identity_key,
                    identity_limit,
                )
                if not allowed:
                    return JSONResponse(
                        status_code=429,
                        content={
                            "detail": "请求过于频繁，请稍后重试",
                            "code": "rate_limited",
                        },
                        headers={"Retry-After": str(retry_after)},
                    )
        response = await call_next(request)
    finally:
        clear_user_context()
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, RuntimeError) and "Expected ASGI message 'http.response.body'" in str(exc):
        raise exc
    logger.exception("未捕获的异常: %s", exc)
    return JSONResponse(status_code=500, content={"detail": "内部服务器错误"})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/health")
async def health_check():
    components = getattr(app.state, "startup_components", {})
    ready = bool(components) and all(
        component.get("status") in {"ready", "degraded"}
        for component in components.values()
    )
    degraded = any(
        component.get("status") == "degraded" for component in components.values()
    )
    payload = {
        "status": "degraded" if ready and degraded else "healthy" if ready else "not_ready",
        "service": "guest-qa-api",
        "port": GUEST_SERVER_PORT,
        # The checkpoint database path is internal deployment detail; an
        # unauthenticated probe only needs the readiness verdict.
        "checkpoint_ready": bool(getattr(app.state, "checkpoint_db", None)),
        "components": components,
    }
    if ready:
        return payload
    return JSONResponse(status_code=503, content=payload)


def create_app() -> FastAPI:
    from backend.api.routers.agent_runtime import router as agent_runtime_router
    from backend.api.routers.auth import router as auth_router
    from backend.api.routers.conversations import shared_router as conversations_router
    from backend.api.routers.files import router as files_router
    from backend.api.routers.guest_agent import router as guest_router
    from backend.api.routers.guest_conversations import (
        router as guest_conversations_router,
    )
    from backend.api.routers.guest_sms_auth import router as guest_sms_auth_router

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(guest_sms_auth_router, prefix="/api/v1")
    app.include_router(guest_conversations_router, prefix="/api/v2")
    app.include_router(conversations_router, prefix="/api/v2")
    app.include_router(files_router, prefix="/api/v2")
    app.include_router(guest_router, prefix="/api/v2")
    app.include_router(agent_runtime_router, prefix="/api/v2")
    logger.info("[Guest] 路由已注册: auth, guest-sms, conversations, guest, agent-runtime, files")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    bind_host = os.getenv("GUEST_SERVER_HOST", settings.server_host)

    from backend.core.utils.client_ip import forwarded_allow_ips

    uvicorn.run(
        "backend.api.guest_http_server:app",
        host=bind_host,
        port=GUEST_SERVER_PORT,
        workers=GUEST_SERVER_WORKERS,
        proxy_headers=True,
        forwarded_allow_ips=forwarded_allow_ips(),
    )
