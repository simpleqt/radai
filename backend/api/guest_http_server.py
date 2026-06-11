"""访客专用精简 API 服务

为 guest 前端提供登录 + 问答 + 会话能力，不包含知识库管理、文档入库、MCP 等管理端路由。
默认监听 8083 端口，checkpoint 使用 guest_checkpoints.db（与 8082 的 checkpoints.db 隔离）。
"""

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

os.environ.setdefault("GUEST_API_SERVICE", "1")

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api.http_server import (
    STARTUP_INIT_TIMEOUT_SECONDS,
    STARTUP_KEYWORD_GRAPH_PREWARM_TIMEOUT_SECONDS,
    STARTUP_RAG_PREWARM_ENABLED,
    _configure_asyncio_default_executor,
    _configure_tls_cert_loading,
    setup_app_dirs,
)
from backend.config.filters import clear_user_context, set_user_context
from backend.config.loggers import get_logger
from backend.config.logging import setup_guest_logging
from backend.config.settings import settings
from backend.db.connection import get_async_session_factory
from backend.db.models.knowledge_base import KnowledgeBase
from backend.rag.service import get_rag_service
from sqlalchemy import select

logger = get_logger(__name__)

GUEST_SERVER_PORT = int(os.getenv("GUEST_SERVER_PORT", "8083"))
GUEST_CHECKPOINT_DB = os.getenv("GUEST_CHECKPOINT_DB", "guest_checkpoints.db")
GUEST_SERVER_WORKERS = int(os.getenv("GUEST_SERVER_WORKERS", "1"))

# 访客前端实际使用的两个知识库（与 multi_kb_search_tool 默认一致）
from backend.application.tools.providers.multi_kb_search_tool import DEFAULT_KB_ID, PAPER_KB_ID

GUEST_KB_IDS = [DEFAULT_KB_ID, PAPER_KB_ID]

GUEST_API_ROUTES = """
需要:
  POST /api/v1/auth/login
  POST /api/v2/guest/query
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
    logger.info(
        "访客问答服务启动: host=%s, port=%s, checkpoint=%s",
        settings.server_host,
        GUEST_SERVER_PORT,
        GUEST_CHECKPOINT_DB,
    )
    default_executor = _configure_asyncio_default_executor()

    async def _wait_with_timeout(name: str, coro, timeout_seconds: float = STARTUP_INIT_TIMEOUT_SECONDS) -> None:
        task = asyncio.create_task(coro)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning("[%s] 初始化超过 %.1fs，切换为后台继续执行", name, timeout_seconds)

    async def _init_rag() -> None:
        try:
            await get_rag_service().initialize()
            logger.info("[Guest] RAG 初始化完成")
        except Exception as exc:
            logger.exception("[Guest] RAG 初始化失败: %s", exc)

    async def _load_guest_knowledge_bases() -> list[KnowledgeBase]:
        factory = get_async_session_factory()
        async with factory() as session:
            result = await session.execute(
                select(KnowledgeBase).where(
                    KnowledgeBase.kb_id.in_(GUEST_KB_IDS),
                    KnowledgeBase.status == "active",
                )
            )
            return list(result.scalars().all())

    async def _prewarm_rag_instances() -> None:
        if not STARTUP_RAG_PREWARM_ENABLED:
            return
        try:
            rag_service = get_rag_service()
            await rag_service.initialize()
            kbs = await _load_guest_knowledge_bases()
            for kb in kbs:
                try:
                    await rag_service._get_rag_instance(kb.kb_id)
                except Exception as exc:
                    logger.warning("[Guest] RAG 预热失败: kb=%s, error=%s", kb.kb_code, exc)
            logger.info("[Guest] RAG 预热完成: 知识库=%d (%s)", len(kbs), ", ".join(kb.kb_code for kb in kbs))
        except Exception as exc:
            logger.warning("[Guest] RAG 预热执行失败: %s", exc)

    async def _prewarm_retrieval_stack() -> None:
        """预热访客使用的两个知识库：关键词索引 + DirectRetriever。"""
        if not STARTUP_RAG_PREWARM_ENABLED:
            return
        try:
            from backend.api.routers.rag import _get_direct_retriever
            from backend.rag.knowledge_base.manager import get_kb_manager

            kbs = await _load_guest_knowledge_bases()
            if not kbs:
                logger.info("[Guest] 检索栈预热: 无可用知识库 (%s)", ", ".join(GUEST_KB_IDS))
                return

            kb_manager = get_kb_manager()
            started_at = asyncio.get_running_loop().time()

            async def _prewarm_single_kb(kb: KnowledgeBase) -> None:
                try:
                    index = await kb_manager._keyword_index_manager.get_index(kb.kb_code, kb.kb_id)
                    logger.info(
                        "[Guest] 关键词索引预热完成: kb=%s, 实体数=%d",
                        kb.kb_code,
                        len(index.entity_names),
                    )
                    retriever = await _get_direct_retriever(kb.kb_id, kb.kb_code)
                    if retriever is not None:
                        logger.info("[Guest] DirectRetriever 预热完成: kb=%s", kb.kb_code)
                    else:
                        logger.warning("[Guest] DirectRetriever 预热跳过: kb=%s", kb.kb_code)
                except Exception as exc:
                    logger.warning("[Guest] 检索栈预热失败: kb=%s, error=%s", kb.kb_code, exc)

            await asyncio.gather(*[_prewarm_single_kb(kb) for kb in kbs], return_exceptions=True)
            elapsed = asyncio.get_running_loop().time() - started_at
            logger.info("[Guest] 检索栈预热完成: 知识库=%d, 耗时=%.2fs", len(kbs), elapsed)
        except Exception as exc:
            logger.warning("[Guest] 检索栈预热执行失败: %s", exc)

    async def _init_checkpointer() -> None:
        try:
            import aiosqlite
            from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

            base_dir = Path(__file__).resolve().parents[1] / "data"
            base_dir.mkdir(parents=True, exist_ok=True)
            db_path = base_dir / GUEST_CHECKPOINT_DB

            conn = await aiosqlite.connect(str(db_path))
            saver = AsyncSqliteSaver(conn)
            await saver.setup()
            app.state.checkpointer = saver
            app.state.checkpointer_conn = conn
            app.state.checkpoint_db = str(db_path)
            logger.info("[Guest] Checkpointer 初始化完成: %s", db_path)
        except Exception as exc:
            logger.exception("[Guest] Checkpointer 初始化失败: %s", exc)

    async def _cleanup_checkpointer() -> None:
        try:
            if hasattr(app.state, "checkpointer_conn"):
                await app.state.checkpointer_conn.close()
                logger.info("[Guest] Checkpointer 连接已关闭")
        except Exception as exc:
            logger.warning("[Guest] Checkpointer 关闭失败: %s", exc)

    await asyncio.gather(
        _wait_with_timeout("RAG", _init_rag()),
        _wait_with_timeout("RAG实例预热", _prewarm_rag_instances()),
        _wait_with_timeout(
            "检索栈预热",
            _prewarm_retrieval_stack(),
            timeout_seconds=STARTUP_KEYWORD_GRAPH_PREWARM_TIMEOUT_SECONDS,
        ),
        _wait_with_timeout("Checkpointer", _init_checkpointer()),
    )

    yield

    await _cleanup_checkpointer()
    if default_executor is not None:
        default_executor.shutdown(wait=False, cancel_futures=True)

    logger.info("访客问答服务关闭")


app = FastAPI(
    title="访客前端专用 API",
    description=GUEST_API_ROUTES.strip(),
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
    """为每个请求生成 request_id 并注入日志上下文（访客服务无登录态）"""
    import secrets as _secrets

    set_user_context(request_id=_secrets.token_hex(8), user_id="guest")
    try:
        response = await call_next(request)
    finally:
        clear_user_context()
    return response


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    if isinstance(exc, RuntimeError) and "Expected ASGI message 'http.response.body'" in str(exc):
        raise exc
    logger.exception("未捕获的异常: %s", exc)
    return JSONResponse(status_code=500, content={"detail": f"内部服务器错误: {str(exc)}"})


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.get("/health")
async def health_check():
    checkpoint_db = getattr(app.state, "checkpoint_db", GUEST_CHECKPOINT_DB)
    return {
        "status": "healthy",
        "service": "guest-qa-api",
        "port": GUEST_SERVER_PORT,
        "checkpoint_db": checkpoint_db,
    }


def create_app() -> FastAPI:
    from backend.api.routers.auth import router as auth_router
    from backend.api.routers.conversations import router as conversations_router
    from backend.api.routers.files import router as files_router
    from backend.api.routers.guest_conversations import router as guest_conversations_router
    from backend.api.routers.guest_agent import router as guest_router

    app.include_router(auth_router, prefix="/api/v1")
    app.include_router(guest_conversations_router, prefix="/api/v2")
    app.include_router(conversations_router, prefix="/api/v2")
    app.include_router(files_router, prefix="/api/v2")
    app.include_router(guest_router, prefix="/api/v2")
    logger.info("[Guest] 路由已注册: auth, guest/conversations, guest, files")
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    bind_host = settings.server_host
    if bind_host in {"127.0.0.1", "localhost"}:
        bind_host = "0.0.0.0"

    uvicorn.run(
        "backend.api.guest_http_server:app",
        host=bind_host,
        port=GUEST_SERVER_PORT,
        workers=GUEST_SERVER_WORKERS,
    )
