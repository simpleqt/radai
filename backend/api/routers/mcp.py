"""MCP 路由 - 封装查询API供外部调用"""

import asyncio
import json
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from backend.api.dependencies.auth import ApiKeyAuth, verify_api_key
from backend.application.agent_service import AgentService
from backend.application.mcp.query_service import build_mcp_agent_request
from backend.application.event_stream.thinking_filter import split_thinking_segments
from backend.config.loggers import get_logger
from backend.config.settings import settings
from backend.domain.agents.events import AgentEvent

router = APIRouter()
logger = get_logger(__name__)


class McpQueryRequest(BaseModel):
    query: str = Field(..., description="用户查询")


class McpConfigResponse(BaseModel):
    type: str = Field(..., description="传输类型")
    url: str = Field(..., description="SSE服务地址")
    headers: dict = Field(..., description="请求头")


@router.get("/config", response_model=McpConfigResponse)
async def get_mcp_config(
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    from backend.config.settings import settings

    server_host = settings.server_host
    if server_host in {"127.0.0.1", "localhost"}:
        server_host = "0.0.0.0"

    return McpConfigResponse(
        type="sse",
        url=f"http://{server_host}:{settings.server_port}/api/v1/mcp/query",
        headers={
            "Authorization": f"Bearer {auth.api_key}",
            "Content-Type": "application/json"
        }
    )


def _get_public_base_url() -> str:
    """获取公共URL基础地址（如 https://radai2.example.com/files）"""
    import os
    from dotenv import dotenv_values
    from pathlib import Path

    # 优先从 os.environ 获取（RAG service 初始化后可能已 load_dotenv）
    public_base = (os.getenv("MINIO_PUBLIC_BASE_URL") or "").rstrip("/")
    if not public_base:
        # fallback: 直接读取 .env 文件（pydantic-settings extra="ignore" 不会加载到 os.environ）
        env_path = Path(__file__).resolve().parents[2] / ".env"
        if env_path.exists():
            env_values = dotenv_values(env_path)
            public_base = (env_values.get("MINIO_PUBLIC_BASE_URL") or "").rstrip("/")
    return public_base


def _get_minio_base_url() -> str:
    """获取原始MinIO地址"""
    minio_endpoint = settings.minio_endpoint.rstrip("/")
    bucket = (settings.minio_bucket_name or "").strip("/")
    return f"{minio_endpoint}/{bucket}"


class _UrlReplacer:
    """跨chunk的URL替换器，处理URL被分割到多个chunk中的情况。

    流式输出中，URL可能被分割到多个chunk，例如：
    - chunk 1: `...src="https://radai2.example.com/fi`
    - chunk 2: `les/431fdba6-206b-435d-8c1b-da5e521c039d/...`

    此类维护一个缓冲区，确保跨chunk的URL也能被正确替换。
    """

    def __init__(self, public_base: str, minio_base: str):
        self.public_base = public_base
        self.minio_base = minio_base
        self.carry = ""  # 缓冲区：可能包含URL前缀的部分文本

    def process(self, text: str) -> str:
        """处理一个chunk，返回替换后的文本。

        取消之前的逐字符缓充处理，改为通过匹配md格式图片来进行。
        只有在遇到图片输出（`![`）且未闭合时才进行缓存。
        """
        if not self.public_base:
            return text

        merged = f"{self.carry}{text}"
        self.carry = ""

        if not merged:
            return ""

        # 寻找最后一个 ![
        last_img_idx = merged.rfind('![')
        if last_img_idx != -1:
            # 如果在 ![ 之后没有对应的 )，并且没有换行符(防异常长文本截断)
            if ')' not in merged[last_img_idx:] and '\n' not in merged[last_img_idx:]:
                self.carry = merged[last_img_idx:]
                merged = merged[:last_img_idx]
        elif merged.endswith('!'):
            # 边缘情况：chunk刚好以 ! 结尾
            self.carry = '!'
            merged = merged[:-1]

        # 替换完整的URL
        if self.public_base in merged:
            merged = merged.replace(self.public_base, self.minio_base)

        return merged

    def flush(self) -> str:
        """刷新缓冲区，返回剩余的文本。"""
        result = self.carry
        self.carry = ""
        if self.public_base in result:
            result = result.replace(self.public_base, self.minio_base)
        return result


def _replace_public_url_with_minio_url(text: str) -> str:
    """将公共URL（如 https://radai2.example.com/files/）替换为原始MinIO地址。

    注意：此函数适用于非流式场景（如event payload）。
    流式场景请使用 _UrlReplacer 类。
    """
    public_base = _get_public_base_url()
    if not public_base:
        return text
    minio_base = _get_minio_base_url()
    if public_base in text:
        text = text.replace(public_base, minio_base)
    return text


async def stream_generator(question: str, *, user_id: str):
    """生成 SSE 流式响应 - 返回与前端 v2 接口相同格式的事件

    事件格式与 /api/v2/agent-runtime/query 一致：
    - type: "chunk"  → {"type": "chunk", "content": "..."}
    - type: "status" → {"type": "status", "status": "..."}
    - type: "event"  → {"type": "event", "event": {...}}
    - type: "error"  → {"type": "error", "message": "..."}
    图片 URL 会替换为原始 MinIO 地址。
    """
    try:
        # 初始化URL替换器，处理跨chunk的URL替换
        public_base = _get_public_base_url()
        minio_base = _get_minio_base_url()
        url_replacer = _UrlReplacer(public_base, minio_base)
        in_thought = False
        think_carry = ""

        async for event in _iter_mcp_edge_events(question=question, user_id=user_id):
            event_type = event.get("type")
            if event_type == "chunk":
                content = event.get("content", "")
                visible, _, in_thought, think_carry = split_thinking_segments(content, in_thought, think_carry)
                if visible:
                    # 使用跨chunk替换器处理URL
                    visible = url_replacer.process(visible)
                    if visible:
                        yield f"data: {json.dumps({'type': 'chunk', 'content': visible}, ensure_ascii=False)}\n\n"
            elif event_type == "status":
                yield f"data: {json.dumps({'type': 'status', 'status': event.get('content', '')}, ensure_ascii=False)}\n\n"
            elif event_type == "event":
                event_payload = event.get("event", {})
                # 对 event 中的图片 URL 也做 MinIO 替换
                event_payload_str = json.dumps(event_payload, ensure_ascii=False)
                event_payload_str = _replace_public_url_with_minio_url(event_payload_str)
                event_payload = json.loads(event_payload_str)
                yield f"data: {json.dumps({'type': 'event', 'event': event_payload}, ensure_ascii=False)}\n\n"
            elif event_type == "error":
                error_msg = event.get("message", event.get("content", ""))
                yield f"data: {json.dumps({'type': 'error', 'message': error_msg}, ensure_ascii=False)}\n\n"
            elif event_type == "done":
                break

        # 刷新URL替换器缓冲区
        remaining = url_replacer.flush()
        if remaining:
            yield f"data: {json.dumps({'type': 'chunk', 'content': remaining}, ensure_ascii=False)}\n\n"

        yield "data: [DONE]\n\n"
    except asyncio.CancelledError:
        logger.warning("MCP 流式连接被取消")
        yield f"data: {json.dumps({'type': 'error', 'message': '连接已取消'}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        logger.exception(f"MCP 流式查询失败: {e}")
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        yield "data: [DONE]\n\n"


async def _iter_mcp_edge_events(*, question: str, user_id: str) -> AsyncIterator[dict]:
    service = AgentService()
    agent_request = build_mcp_agent_request(
        question=question,
        user_id=user_id,
        source="mcp.query",
    )
    async for event in service.run_stream(agent_request):
        edge_event = _map_standard_event_to_mcp_edge(event)
        if edge_event is not None:
            yield edge_event
        if event.type == "done":
            break


def _map_standard_event_to_mcp_edge(event: AgentEvent) -> dict | None:
    if event.type == "message.delta":
        return {"type": "chunk", "content": str(event.payload.get("content") or "")}
    if event.type == "workflow.step":
        if isinstance(event.payload.get("event"), dict):
            return {"type": "event", "event": event.payload.get("event") or {}}
        return {"type": "status", "content": str(event.payload.get("content") or event.payload.get("status") or "")}
    if event.type == "reasoning.delta":
        return None
    if event.type == "workflow.started":
        return {"type": "status", "content": f"workflow.started:{event.payload.get('workflow', '')}"}
    if event.type == "error":
        return {"type": "error", "message": str(event.payload.get("message") or event.payload.get("error") or "")}
    if event.type == "done":
        return {"type": "done"}
    return None


@router.post("/query")
async def mcp_query_stream(
    request: McpQueryRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
):
    """MCP 流式查询接口"""
    try:
        return StreamingResponse(
            stream_generator(request.query, user_id=auth.user_id),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except Exception as e:
        logger.exception(f"MCP 流式查询初始化失败: {e}")
        raise HTTPException(status_code=500, detail=f"MCP 流式查询初始化失败: {str(e)}")
