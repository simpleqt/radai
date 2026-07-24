import asyncio
import hashlib
import os
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import ClientDisconnect

from backend.api.dependencies.auth import ApiKeyAuth, require_admin
from backend.api.startup import (
    ALLOW_QUERY_TOKEN_AUTH,
    main_lifespan as lifespan,
    service_role,
)
from backend.application.auth_service import api_key_scope_allows_path, auth_service
from backend.application.auth_security import consume_sliding_window_quota
from backend.application.exceptions import ApplicationError
from backend.config.filters import clear_user_context, set_user_context
from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.db.redis import get_redis

logger = get_logger(__name__)


def _positive_int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.getenv(name, str(default)) or default))
    except (TypeError, ValueError):
        return default


@dataclass
class _RateBucket:
    tokens: float
    updated_at: float


class _RequestRateLimiter:
    """Redis-backed HTTP admission control with an outage-only local fallback."""

    def __init__(self) -> None:
        self._buckets: OrderedDict[str, _RateBucket] = OrderedDict()
        self._lock = asyncio.Lock()

    async def allow(self, key: str, limit_per_minute: int) -> tuple[bool, int]:
        limit = max(1, limit_per_minute)
        if settings.redis_host:
            client = get_redis()
            quota_key = "http:rate:" + hashlib.sha256(
                key.encode("utf-8")
            ).hexdigest()
            try:
                return await consume_sliding_window_quota(
                    client,
                    quota_key,
                    window_seconds=60,
                    max_requests=limit,
                )
            except Exception as exc:
                logger.warning(
                    "Redis HTTP 限流不可用，回退到进程内限流: %s",
                    type(exc).__name__,
                )
            finally:
                await client.aclose()

        now = time.monotonic()
        refill_per_second = limit / 60.0
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _RateBucket(tokens=float(limit), updated_at=now)
                self._buckets[key] = bucket
            else:
                bucket.tokens = min(
                    float(limit),
                    bucket.tokens + (now - bucket.updated_at) * refill_per_second,
                )
                bucket.updated_at = now
                self._buckets.move_to_end(key)
            allowed = bucket.tokens >= 1.0
            if allowed:
                bucket.tokens -= 1.0
            retry_after = 0 if allowed else max(1, int((1.0 - bucket.tokens) / refill_per_second) + 1)
            while len(self._buckets) > 10000:
                self._buckets.popitem(last=False)
            return allowed, retry_after


_request_rate_limiter = _RequestRateLimiter()


class SPAStaticFiles(StaticFiles):
    """Serve built assets and fall back to index.html for client-side routes."""

    async def get_response(self, path: str, scope):
        try:
            response = await super().get_response(path, scope)
        except StarletteHTTPException as exc:
            if exc.status_code != 404:
                raise
            response = None

        should_fallback = (
            response is None
            and scope.get("method") in {"GET", "HEAD"}
            and not path.startswith(("api/", "mcp/"))
            and not Path(path).suffix
        )
        if should_fallback:
            return await super().get_response("index.html", scope)
        if response is None:
            raise StarletteHTTPException(status_code=404)
        return response


app = FastAPI(
    title="文档处理智能体服务",
    description="提供文档解析、抽取、增强和入库的智能体服务",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        # Additional CORS origins should be configured via environment variable
        # CORS_ALLOWED_ORIGINS (comma-separated)
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
    默认不接受 URL query token，避免认证凭据进入访问日志；如需迁移兼容可显式设置
    ALLOW_QUERY_TOKEN_AUTH=1。
    未认证请求静默跳过，不影响正常流程。
    """
    import secrets as _secrets

    supplied_request_id = request.headers.get("X-Request-ID", "").strip()
    # Keep correlation IDs useful across browser/API logs while rejecting
    # control characters and unbounded values supplied by a client.
    request_id = (
        supplied_request_id
        if supplied_request_id
        and len(supplied_request_id) <= 128
        and all(char.isalnum() or char in "._:-" for char in supplied_request_id)
        else _secrets.token_hex(8)
    )
    set_user_context(request_id=request_id)
    request.state.request_id = request_id
    request.state.user_id = None
    request.state.auth = None

    try:
        auth_header = request.headers.get("Authorization", "")
        x_api_key = request.headers.get("X-API-Key", "").strip()
        bearer = auth_header[7:].strip() if auth_header.startswith("Bearer ") else ""
        if not bearer and ALLOW_QUERY_TOKEN_AUTH:
            bearer = request.query_params.get("token", "").strip()

        try:
            auth = await auth_service.resolve_credentials(
                x_api_key=x_api_key,
                bearer=bearer,
            )
        except ApplicationError as exc:
            # Credential infrastructure failures must not silently turn an
            # authenticated request into an anonymous one.
            logger.warning(
                "请求认证服务不可用: code=%s path=%s",
                exc.error_code,
                request.url.path,
            )
            return JSONResponse(
                status_code=exc.status_code,
                content={"detail": exc.public_detail, "code": exc.error_code},
            )
        except Exception as exc:
            logger.exception("请求身份解析发生未分类错误: %s", type(exc).__name__)
            raise

        if (
            auth
            and auth.source == "api_key"
            and not api_key_scope_allows_path(auth.access_scope, request.url.path)
        ):
            response = JSONResponse(
                status_code=403,
                content={
                    "detail": "此 API Key 仅允许访问 MCP 服务",
                    "code": "api_key_scope_forbidden",
                },
            )
            response.headers["X-Request-ID"] = request_id
            return response

        if auth and auth.user_id:
            request.state.auth = auth
            set_user_context(user_id=auth.user_id, username=auth.username)
            request.state.user_id = auth.user_id

        if request.url.path not in {"/health", "/api/v1/health"}:
            if auth and auth.source == "api_key":
                limit = auth.rate_limit_per_minute or _positive_int_env(
                    "HTTP_AUTHENTICATED_RATE_LIMIT_PER_MINUTE", 600
                )
                identity = "api:" + hashlib.sha256(
                    auth.api_key.encode("utf-8")
                ).hexdigest()
            elif auth and auth.user_id:
                limit = _positive_int_env("HTTP_AUTHENTICATED_RATE_LIMIT_PER_MINUTE", 600)
                identity = f"user:{auth.user_id}"
            else:
                limit = _positive_int_env("HTTP_ANONYMOUS_RATE_LIMIT_PER_MINUTE", 120)
                client_ip = request.client.host if request.client else "unknown"
                if os.getenv("HTTP_TRUST_FORWARDED_FOR", "").strip().lower() in {
                    "1",
                    "true",
                    "yes",
                    "on",
                }:
                    forwarded = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
                    client_ip = forwarded or client_ip
                identity = f"ip:{client_ip}"
            allowed, retry_after = await _request_rate_limiter.allow(identity, limit)
            if not allowed:
                response = JSONResponse(
                    status_code=429,
                    content={"detail": "请求过于频繁，请稍后重试", "code": "rate_limited"},
                    headers={"Retry-After": str(retry_after), "X-Request-ID": request_id},
                )
                return response

        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
    finally:
        clear_user_context()

    return response


@app.exception_handler(ClientDisconnect)
async def client_disconnect_handler(request: Request, exc: ClientDisconnect):
    """客户端主动断开（取消请求/切页）时不记为未捕获 500。"""
    logger.info(
        "客户端断开连接: path=%s method=%s",
        request.url.path,
        request.method,
    )
    return Response(status_code=499)


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    """全局异常处理"""
    # 流式响应在连接中断或已发送 response.start 后，若再尝试构造 JSONResponse
    # 会触发 "Expected ASGI message 'http.response.body'"。此时应直接上抛，
    # 避免二次发送 response.start。
    if isinstance(exc, ClientDisconnect):
        return await client_disconnect_handler(request, exc)

    if isinstance(
        exc, RuntimeError
    ) and "Expected ASGI message 'http.response.body'" in str(exc):
        logger.warning(
            "检测到已进入 response.body 阶段的协议错误，跳过全局 JSON 异常响应: %s", exc
        )
        raise exc

    logger.exception(
        "未捕获的异常: path=%s method=%s",
        request.url.path,
        request.method,
    )
    return JSONResponse(status_code=500, content={"detail": "内部服务器错误"})


@app.exception_handler(ApplicationError)
async def application_exception_handler(request: Request, exc: ApplicationError):
    """Render stable application errors without leaking infrastructure details."""
    logger.warning(
        "应用服务错误: code=%s path=%s method=%s",
        exc.error_code,
        request.url.path,
        request.method,
    )
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.public_detail, "code": exc.error_code},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    """HTTP异常处理"""
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
        headers=exc.headers,
    )


@app.get("/health")
@app.get("/ready", include_in_schema=False)
async def health_check():
    """Readiness check for dependencies required by the agent runtime.

    Startup state is useful for expensive components, but it is only a
    snapshot.  Re-check MySQL on every readiness request so an API process is
    removed from service after a database outage as well as during startup.
    """
    components = dict(getattr(app.state, "startup_components", {}) or {})

    db_check_enabled = os.environ.get("HEALTH_DB_CHECK_ENABLED", "true").strip().lower()
    if db_check_enabled not in {"0", "false", "no", "off"}:
        try:
            timeout_seconds = max(
                float(os.environ.get("HEALTH_DB_TIMEOUT_SECONDS", "2")), 0.1
            )
        except ValueError:
            timeout_seconds = 2.0

        try:
            from sqlalchemy import text

            from backend.db.connection import (
                _cancellation_safe_context,
                get_async_engine,
            )

            async def check_database_connection() -> None:
                async with _cancellation_safe_context(
                    get_async_engine().connect()
                ) as connection:
                    await connection.execute(text("SELECT 1"))

            await asyncio.wait_for(check_database_connection(), timeout=timeout_seconds)
            components["database"] = {"status": "ready", "error": ""}
        except Exception as exc:
            logger.warning("数据库就绪检查失败: %s", type(exc).__name__)
            components["database"] = {
                "status": "error",
                "error": type(exc).__name__,
            }

    if service_role() in {"all", "knowledge"}:
        try:
            from backend.rag.service import get_rag_service

            if get_rag_service().is_ready:
                components["rag"] = {"status": "ready", "error": ""}
            else:
                components["rag"] = {"status": "error", "error": "not_ready"}
        except Exception as exc:
            logger.warning("RAG 就绪检查失败: %s", type(exc).__name__)
            components["rag"] = {
                "status": "error",
                "error": type(exc).__name__,
            }

    ready = bool(components) and all(
        item.get("status") == "ready" for item in components.values()
    )
    payload = {
        "status": "healthy" if ready else "degraded",
        "service": "document-processing-agent",
        "components": components,
    }
    return JSONResponse(status_code=200 if ready else 503, content=payload)


@app.get("/live")
async def liveness_check():
    """Process liveness endpoint; dependency readiness is exposed by /health."""
    return {"status": "alive", "service": "document-processing-agent"}


@app.get("/config")
async def get_config(_admin: ApiKeyAuth = Depends(require_admin)):
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
    if service_role() == "knowledge":
        return {
            "service": "document-processing-agent",
            "role": "knowledge",
            "health": "/health",
        }
    return RedirectResponse(url="/app")


@app.get("/ui", include_in_schema=False)
@app.get("/ui/", include_in_schema=False)
async def legacy_ui_redirect():
    return RedirectResponse(url="/app", status_code=308)


def create_app() -> FastAPI:
    """应用工厂函数"""
    role = service_role()
    runs_agent_runtime = role in {"all", "agent"}
    runs_knowledge_runtime = role in {"all", "knowledge"}

    from backend.api.routers.admin import router as admin_router
    from backend.api.routers.auth import router as auth_router
    from backend.api.routers.auth_sms import router as auth_sms_router

    app.include_router(auth_router, prefix="/api/v1", tags=["认证"])
    app.include_router(auth_sms_router, prefix="", tags=["短信认证"])
    app.include_router(admin_router, prefix="/api/v1", tags=["管理员后台"])

    if role == "agent":
        from backend.api.routers.knowledge_proxy import (
            router as knowledge_proxy_router,
        )

        app.include_router(
            knowledge_proxy_router,
            prefix="/api/v1",
            tags=["知识库服务代理"],
        )

    if runs_knowledge_runtime:
        from backend.api.dependencies.auth import get_db_session
        from backend.api.routers.directories import router as directories_router
        from backend.api.routers.document_ingestion import (
            router as document_ingestion_router,
        )
        from backend.api.routers.document_processing import (
            router as document_processing_router,
        )
        from backend.api.routers.keywords import router as keywords_router
        from backend.api.routers.knowledge_base import router as kb_router
        from backend.api.routers.knowledge_base_documents import (
            router as kb_docs_router,
        )
        from backend.api.routers.rag import router as rag_router
        from backend.api.routers.self_ingestion_admin import (
            router as self_ingestion_admin_router,
        )
        from backend.api.routers.uploads import router as uploads_router

        app.include_router(kb_docs_router, prefix="/api/v1", tags=["知识库文档"])
        app.include_router(
            document_processing_router,
            prefix="/api/v1/documents",
            tags=["文档处理"],
        )
        app.include_router(kb_router, prefix="/api/v1", tags=["知识库管理"])
        app.include_router(directories_router, prefix="/api/v1", tags=["知识库目录"])
        app.include_router(keywords_router, prefix="/api/v1", tags=["关键词管理"])
        app.include_router(rag_router, prefix="/api/v1/rag", tags=["RAG服务"])
        app.include_router(
            document_ingestion_router,
            prefix="/api/v1/ingestion",
            tags=["文档入库"],
        )
        app.include_router(uploads_router, prefix="/api/v1", tags=["文件上传"])
        app.include_router(
            self_ingestion_admin_router,
            prefix="/api/v1",
            tags=["自研入库管理"],
        )

        @app.get("/api/v1/health/self-ingestion")
        async def health_self_ingestion_alias(
            session=Depends(get_db_session),
            admin: ApiKeyAuth = Depends(require_admin),
        ):
            from backend.api.routers.self_ingestion_admin import self_ingestion_health

            return await self_ingestion_health(session, admin)

    if runs_agent_runtime:
        from backend.api.routers.admin_conversations import (
            router as admin_conv_router,
        )
        from backend.api.routers.agent_skills import router as agent_skills_router
        from backend.api.routers.agents import router as agents_router
        from backend.api.routers.artifacts import router as artifacts_router
        from backend.api.routers.asr import router as asr_router
        from backend.api.routers.chart_images import router as chart_images_router
        from backend.api.routers.chat_files import router as chat_files_router
        from backend.api.routers.conversations import (
            router as conversations_router,
            shared_router as shared_conversations_router,
        )
        from backend.api.routers.custom_learning import router as custom_learning_router
        from backend.api.routers.datasets import router as datasets_router
        from backend.api.routers.files import router as files_router
        from backend.api.routers.groundwater_projects import (
            router as groundwater_projects_router,
        )
        from backend.api.routers.learning_style import router as learning_style_router
        from backend.api.routers.mcp import router as mcp_router
        from backend.api.routers.models import router as models_router
        from backend.api.routers.my_files import router as my_files_router
        from backend.api.routers.prompts import router as prompts_router
        from backend.api.routers.skills import router as skills_router
        from backend.application.mcp import create_mcp_sse_app

        app.include_router(conversations_router, prefix="/api/v2", tags=["对话管理"])
        app.include_router(
            shared_conversations_router,
            prefix="/api/v2",
            tags=["对话管理"],
        )
        app.include_router(mcp_router, prefix="/api/v1/mcp", tags=["MCP服务"])
        app.include_router(
            artifacts_router, prefix="/api/v2/threads", tags=["线程工件"]
        )
        app.include_router(files_router, prefix="/api/v2", tags=["文件中心"])
        app.include_router(chat_files_router, prefix="/api/v2", tags=["对话文件"])
        app.include_router(models_router, prefix="/api/v1/models", tags=["模型管理"])
        app.include_router(prompts_router, prefix="/api/v1", tags=["提示词管理"])
        app.include_router(asr_router, prefix="/api/v1", tags=["语音转写"])
        app.include_router(my_files_router)
        app.include_router(agents_router, prefix="/api/v1/agents", tags=["智能体管理"])
        app.include_router(
            agent_skills_router,
            prefix="/api/v1/agents",
            tags=["智能体 Skill 绑定"],
        )
        app.include_router(skills_router, prefix="/api/v1")
        app.include_router(datasets_router, prefix="/api/v1", tags=["实验数据集"])
        app.include_router(chart_images_router, prefix="/api/v1", tags=["可视化图表"])
        app.include_router(
            admin_conv_router, prefix="/api/v1", tags=["管理员-会话管理"]
        )
        app.include_router(groundwater_projects_router, prefix="/api/v2")
        app.include_router(learning_style_router, prefix="/api/v1")
        app.include_router(custom_learning_router, prefix="/api/v1")
        # The v2 runtime is required in production agent processes.  Reload
        # mode may tolerate a temporarily incomplete module while editing.
        try:
            from backend.api.routers.agent_runtime_v2 import api_router as v2_router

            app.include_router(v2_router)
            logger.info(
                "[路由] v2 API 路由已注册: "
                "/api/v2/agent-runtime, /api/v2/guest, /api/v2/explore"
            )
        except Exception:
            logger.exception("[路由] v2 API 路由注册失败")
            if not settings.server_reload:
                raise

        app.mount("/mcp/sse", create_mcp_sse_app())

    @app.get("/metrics")
    async def prometheus_metrics(_admin: ApiKeyAuth = Depends(require_admin)):
        from fastapi.responses import Response

        try:
            from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
        except ImportError:
            return Response(
                content="prometheus_client not installed\n", media_type="text/plain"
            )
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    html_ppt_assets_dir = (
        Path(__file__).resolve().parents[2]
        / "backend"
        / "skills"
        / "public"
        / "html-ppt"
        / "assets"
    )
    if (
        runs_agent_runtime
        and html_ppt_assets_dir.exists()
        and html_ppt_assets_dir.is_dir()
    ):
        app.mount(
            "/static/html-ppt-assets",
            StaticFiles(directory=str(html_ppt_assets_dir)),
            name="html-ppt-assets",
        )

    # Keep this mount last so API and static skill routes always take precedence.
    frontend_dist_dir = Path(__file__).resolve().parents[2] / "frontend" / "dist"
    if (
        runs_agent_runtime
        and frontend_dist_dir.exists()
        and frontend_dist_dir.is_dir()
    ):
        app.mount(
            "/",
            SPAStaticFiles(directory=str(frontend_dist_dir), html=True),
            name="frontend",
        )

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
        requested_workers = int(os.environ.get("SERVER_WORKERS", "1"))
        if requested_workers != 1:
            raise RuntimeError(
                "SERVER_WORKERS must be 1 while reconnectable agent tasks use "
                "the in-process task registry"
            )
        kwargs["workers"] = 1

    uvicorn.run("backend.api.http_server:app", **kwargs)
