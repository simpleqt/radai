"""RAG 路由"""

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from backend.api.dependencies.auth import (
    ApiKeyAuth,
    get_db_session,
    require_kb_access,
    verify_api_key,
)
from backend.application.retrieval import (
    delete_rag_document,
    execute_internal_multi_kb_search,
    get_default_kb_id as _get_default_kb_id,
    get_rag_health,
    insert_content_list,
    insert_docx_document,
    list_chat_models,
    process_multimodal_document,
    reset_rag_runtime,
    retrieve_context,
)
from backend.config.loggers import get_logger

router = APIRouter()
logger = get_logger(__name__)


def _retrieval_timeout_seconds() -> float:
    try:
        return max(
            1.0,
            float(os.getenv("RAG_RETRIEVAL_TIMEOUT_SECONDS", "60") or "60"),
        )
    except (TypeError, ValueError):
        return 60.0


async def _retrieve_context_with_timeout(**kwargs):
    return await asyncio.wait_for(
        retrieve_context(**kwargs),
        timeout=_retrieval_timeout_seconds(),
    )


async def _resolve_readable_kb_id(
    requested_kb_id: str | None,
    auth: ApiKeyAuth,
    session: AsyncSession,
) -> str:
    kb_id = requested_kb_id or await _get_default_kb_id()
    if not kb_id:
        raise HTTPException(status_code=400, detail="未指定知识库且无法获取默认知识库")
    await require_kb_access(session, kb_id, auth, access="read")
    return kb_id


async def _resolve_writable_kb_id(
    requested_kb_id: str | None,
    auth: ApiKeyAuth,
    session: AsyncSession,
) -> str:
    kb_id = requested_kb_id or await _get_default_kb_id()
    if not kb_id:
        raise HTTPException(status_code=400, detail="未指定知识库且无法获取默认知识库")
    await require_kb_access(session, kb_id, auth, access="write")
    return kb_id


def _require_internal_secret(x_internal_secret: str | None, *, label: str) -> None:
    from backend.config.settings import settings as app_settings

    expected_secret = str(app_settings.internal_api_secret or "").strip()
    if not expected_secret:
        logger.error("[%s] INTERNAL_API_SECRET 未配置，拒绝启动内部 API", label)
        raise HTTPException(
            status_code=503, detail="Internal retrieval is not configured"
        )
    supplied = str(x_internal_secret or "").strip()
    if not supplied or not secrets.compare_digest(supplied, expected_secret):
        logger.warning("[%s] 拒绝未授权请求", label)
        raise HTTPException(
            status_code=403, detail="Forbidden: invalid internal secret"
        )


def _require_safe_server_file_path(file_path: str) -> str:
    from backend.config.settings import settings

    raw_path = str(file_path or "").strip()
    if not raw_path:
        raise HTTPException(status_code=400, detail="缺少文件路径")
    resolved = Path(raw_path).expanduser().resolve()
    allowed_roots = [
        settings.upload_temp_dir,
        settings.storage_base_path,
        settings.documents_path,
        settings.cache_path,
    ]
    for root in allowed_roots:
        if not root:
            continue
        try:
            resolved.relative_to(Path(str(root)).expanduser().resolve())
            return str(resolved)
        except ValueError:
            continue
    raise HTTPException(status_code=403, detail="文件路径不在允许的上传/存储目录内")


class RagRetrieveRequest(BaseModel):
    query: str = Field(..., description="用户查询")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    top_k: int = Field(default=30, ge=1, le=80, description="检索数量")
    chunk_top_k: int = Field(default=10, ge=1, le=60, description="文档片段数量")


class RagRetrieveWithKeywordsRequest(BaseModel):
    """带关键词的内部检索请求，供子智能体线程通过 HTTP API 调用"""

    query: str = Field(..., description="用户查询")
    kb_id: str = Field(..., description="知识库 ID")
    top_k: int = Field(default=30, ge=1, le=80, description="检索数量")
    chunk_top_k: int = Field(default=10, ge=1, le=60, description="文档片段数量")
    hl_keywords: list[str] = Field(default_factory=list, description="高级关键词")
    ll_keywords: list[str] = Field(default_factory=list, description="低级关键词")
    query_type: str = Field(default="general", description="查询类型")
    disable_stage1_oversample: bool = Field(
        default=True,
        description="为 True 时阶段1不做过采样，按 top_k/chunk_top_k 原样召回",
    )


class InternalMultiKBSearchRequest(BaseModel):
    """Trusted request sent by the lightweight agent API to the RAG service."""

    query: str = Field(..., min_length=1, description="用户查询")
    kb_ids: list[str] = Field(default_factory=list, description="授权的知识库 ID")
    top_k: int = Field(default=10, ge=1, le=80)
    chunk_top_k: int = Field(default=6, ge=1, le=60)
    merge_strategy: str = Field(default="rrf")
    external_hl_keywords: list[str] | None = None
    external_ll_keywords: list[str] | None = None
    external_query_type: str | None = None
    content_filter_mode: str | None = None
    max_result_chars: int | None = Field(default=None, ge=1)


class ImageInfo(BaseModel):
    marker: str = Field(..., description="图片标识")
    url: str = Field(..., description="图片访问URL")
    path: str = Field(..., description="图片原始路径")


class ChunkMeta(BaseModel):
    content: str = Field(default="", description="文本块内容")
    file_path: str = Field(default="", description="来源文件路径")
    fused_score: float = Field(default=0.0, description="融合得分（含时间衰减）")
    ppr_score: float = Field(default=0.0, description="PPR 得分")
    rerank_score: float = Field(default=0.0, description="Rerank 得分")
    time_score: float = Field(default=0.0, description="时间衰减得分")
    time_alpha: float = Field(default=0.0, description="时间敏感度")
    time_beta: float = Field(default=0.0, description="时间可信度")
    doc_time_year: int | None = Field(default=None, description="文档年份")
    doc_time_month: int | None = Field(default=None, description="文档月份")
    doc_time_confidence: float = Field(default=0.0, description="时间可信度系数")
    doc_id: str = Field(default="", description="文档ID")
    full_doc_id: str = Field(default="", description="完整文档ID")


class RagRetrieveResponse(BaseModel):
    context: str = Field(..., description="检索到的上下文内容")
    images: List[ImageInfo] = Field(default_factory=list, description="相关图片列表")
    entity_count: int = Field(default=0, description="实体数量")
    relation_count: int = Field(default=0, description="关系数量")
    chunk_count: int = Field(default=0, description="文档片段数量")
    knowledge_paths: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="知识路径（证据链条），每条路径包含 nodes/relations/score",
    )
    chunks: List[ChunkMeta] = Field(
        default_factory=list,
        description="文本块元数据（含融合得分、时间衰减等），供多知识库合并使用",
    )


def _extract_chunk_metas_from_filtered(filtered_data: Any | None) -> list[ChunkMeta]:
    if not filtered_data:
        return []
    raw_chunks = filtered_data.chunks or []
    metas: list[ChunkMeta] = []
    for chunk in raw_chunks:
        if not isinstance(chunk, dict):
            continue
        raw_content = chunk.get("content", "")
        normalized_content = raw_content if raw_content else ""
        metas.append(
            ChunkMeta(
                content=normalized_content,
                file_path=chunk.get("file_path", chunk.get("source", "")),
                fused_score=float(chunk.get("_fused_score", 0.0)),
                ppr_score=float(chunk.get("_ppr_score", 0.0)),
                rerank_score=float(chunk.get("_rerank_score", 0.0)),
                time_score=float(chunk.get("_time_score", 0.0)),
                time_alpha=float(chunk.get("_time_alpha", 0.0)),
                time_beta=float(chunk.get("_time_beta", 0.0)),
                doc_time_year=chunk.get("_doc_time_year"),
                doc_time_month=chunk.get("_doc_time_month"),
                doc_time_confidence=float(chunk.get("_doc_time_confidence", 0.0)),
                doc_id=chunk.get("doc_id", chunk.get("_doc_id", "")),
                full_doc_id=chunk.get("full_doc_id", ""),
            )
        )
    return metas


def _build_rag_retrieve_response(
    filtered_data: Any | None,
    prompt: str,
    images: List[dict[str, Any]],
    stats: dict[str, int],
) -> RagRetrieveResponse:
    image_infos = [
        ImageInfo(
            marker=img.get("marker", ""),
            url=img.get("url", ""),
            path=img.get("path", ""),
        )
        for img in images
        if isinstance(img, dict)
    ]
    return RagRetrieveResponse(
        context=prompt,
        images=image_infos,
        entity_count=stats.get("entities", 0),
        relation_count=stats.get("relationships", 0),
        chunk_count=stats.get("chunks", 0),
        knowledge_paths=(filtered_data.knowledge_paths if filtered_data else []),
        chunks=_extract_chunk_metas_from_filtered(filtered_data),
    )


class RagQueryStreamRequest(BaseModel):
    query: str = Field(..., description="用户查询")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    model_id: str | None = Field(default=None, description="模型ID（可选）")
    top_k: int = Field(default=30, description="检索数量")
    chunk_top_k: int = Field(default=10, description="文档片段数量")
    temperature: float = Field(default=0.7, description="生成温度")
    max_tokens: int | None = Field(
        default=None, description="最大生成token数（默认从 MySQL 模型配置读取）"
    )


DEFAULT_RAG_SYSTEM_PROMPT = """你是一个专业的知识库问答助手。请根据提供的上下文信息（包括结构化实体关系和文档内容）回答用户问题。

## 回答要求
1. 基于上下文信息准确回答，不要编造内容
2. 充分利用提供的**相关实体**和**相关关系**来理解知识脉络，将零散的文档内容串联起来
3. 如果上下文中没有相关信息，请明确告知用户
4. 使用清晰的结构组织回答，可以使用标题、列表、表格等格式
5. 保持回答简洁，突出重点

## 图片展示
当上下文中包含 `[IMAGE:xxx]` 图片标识符时，必须只使用该标识符嵌入图片：
- 使用语法：`![图片描述]([IMAGE:xxx])`
- 禁止输出 `/files/`、MinIO、代理地址或任何图片 URL
"""


@router.post("/retrieve", response_model=RagRetrieveResponse)
async def rag_retrieve(
    request: RagRetrieveRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    """
    仅检索知识库，不生成答案

    返回检索到的上下文内容和相关图片信息，供调用方自行处理。
    与 Agent 工具共用 ``retrieve_context``（DirectRetriever + Profile 驱动 Stage-2）。
    """
    total_start = time.time()

    try:
        kb_id = await _resolve_readable_kb_id(request.kb_id, auth, session)

        filtered_data, prompt, images, stats = await _retrieve_context_with_timeout(
            query=request.query,
            kb_id=kb_id,
            top_k=request.top_k,
            chunk_top_k=request.chunk_top_k,
        )

        total_duration = time.time() - total_start
        logger.info("[检索流水线] 总耗时: %.1fms", total_duration * 1000)
        return _build_rag_retrieve_response(filtered_data, prompt, images, stats)
    except HTTPException:
        raise
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="RAG 检索超时") from exc
    except Exception as e:
        logger.exception(f"RAG 检索失败: {e}")
        raise HTTPException(status_code=500, detail="RAG 检索失败")


@router.post("/retrieve_internal", response_model=RagRetrieveResponse)
async def rag_retrieve_internal(
    request: RagRetrieveWithKeywordsRequest,
    x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
):
    """
    内部检索接口（需携带内部共享密钥），供子智能体线程通过 HTTP API 调用。

    与 /retrieve 的区别：
    - 使用 X-Internal-Secret 头认证（非 API Key）
    - 接受外部传入的关键词，跳过关键词提取步骤
    - 在主线程/主事件循环中执行，复用已预热的缓存
    """
    _require_internal_secret(x_internal_secret, label="内部检索")

    total_start = time.time()

    try:
        filtered_data, prompt, images, stats = await _retrieve_context_with_timeout(
            query=request.query,
            kb_id=request.kb_id,
            top_k=request.top_k,
            chunk_top_k=request.chunk_top_k,
            external_hl_keywords=request.hl_keywords,
            external_ll_keywords=request.ll_keywords,
            external_query_type=request.query_type,
            disable_stage1_oversample=request.disable_stage1_oversample,
        )

        total_duration = time.time() - total_start
        logger.info("[内部检索流水线] 总耗时: %.1fms", total_duration * 1000)
        return _build_rag_retrieve_response(filtered_data, prompt, images, stats)
    except Exception as e:
        logger.exception(f"内部 RAG 检索失败: {e}")
        raise HTTPException(status_code=500, detail="内部 RAG 检索失败")


@router.post("/internal/multi-kb-search")
async def internal_multi_kb_search(
    request: InternalMultiKBSearchRequest,
    x_internal_secret: str | None = Header(None, alias="X-Internal-Secret"),
):
    """Run the agent retrieval tool inside the long-lived knowledge service."""
    _require_internal_secret(x_internal_secret, label="内部多库检索")

    try:
        return await execute_internal_multi_kb_search(
            query=request.query,
            kb_ids=request.kb_ids,
            top_k=request.top_k,
            chunk_top_k=request.chunk_top_k,
            merge_strategy=request.merge_strategy,
            external_hl_keywords=request.external_hl_keywords,
            external_ll_keywords=request.external_ll_keywords,
            external_query_type=request.external_query_type,
            content_filter_mode=request.content_filter_mode,
            max_result_chars=request.max_result_chars,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("[内部多库检索] 执行失败")
        raise HTTPException(
            status_code=500, detail="Internal multi-KB retrieval failed"
        ) from exc


async def _rag_stream_generator(
    query: str,
    kb_id: str | None,
    model_config: dict | None,
    temperature: float,
    max_tokens: int,
    top_k: int,
    chunk_top_k: int,
) -> AsyncGenerator[str, None]:
    """
    生成 RAG 流式响应

    完整检索流程：
    1. 关键词提取（向量搜索 + BM25 + 图拓扑融合）
    2. 阶段1：召回原始数据（实体、关系、文本块）
    3. 阶段2：PPR 图过滤
    4. Rerank 加权融合
    5. MMR 多样性重排序
    6. 阶段3：资产组装
    7. 流式生成答案
    """
    total_start = time.time()

    try:
        if not kb_id:
            yield f"data: {json.dumps({'type': 'error', 'content': '未指定知识库'})}\n\n"
            return

        yield f"data: {json.dumps({'type': 'status', 'content': '正在检索知识库...'})}\n\n"

        filtered_data, context_prompt, images, stats = await _retrieve_context_with_timeout(
            query=query,
            kb_id=kb_id,
            top_k=top_k,
            chunk_top_k=chunk_top_k,
        )
        from backend.application.agent_executors.image_url_registry import (
            StreamingTokenResolver,
            inject_missing_captioned_images,
            register_image_url,
            resolve_image_tokens,
            reset_registry,
        )
        from backend.application.image_pipeline import (
            replace_image_urls_for_model_context,
            sanitize_answer_image_urls,
        )

        reset_registry()
        context_prompt = replace_image_urls_for_model_context(
            context_prompt,
            register_image=register_image_url,
        )
        logger.info(
            "[检索] 流式问答召回完成 | 实体=%s, 关系=%s, 文本块=%s",
            stats.get("entities", 0),
            stats.get("relationships", 0),
            stats.get("chunks", 0),
        )

        image_urls = []
        for img in images:
            if isinstance(img, dict):
                path = img.get("path", "")
                url = img.get("url", "")
                if path and url:
                    image_urls.append({"path": path, "url": url})

        yield f"data: {json.dumps({'type': 'context', 'images': image_urls})}\n\n"

        yield f"data: {json.dumps({'type': 'status', 'content': '正在生成回答...'})}\n\n"

        from backend.config.settings import settings

        if model_config:
            llm_config = model_config
        else:
            llm_config = settings.active_llm_config

        from backend.core.models.context_limits import resolve_max_tokens

        api_base = (llm_config.get("base_url") or "").rstrip("/")
        api_key = llm_config.get("api_key")
        model = llm_config.get("model")
        effective_max_tokens = (
            max_tokens
            if max_tokens is not None
            else resolve_max_tokens(llm_config, api_model=model)
        )

        messages = [
            {"role": "system", "content": DEFAULT_RAG_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"# 用户问题\n\n{query}\n\n---\n\n# 相关上下文\n\n{context_prompt}\n\n---\n\n# 请回答以下问题\n\n{query}",
            },
        ]

        import httpx

        request_json = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": effective_max_tokens,
            "stream": True,
        }

        if "qwen" in (model or "").lower():
            request_json["thinking"] = {"type": "off"}

        token_resolver = StreamingTokenResolver()
        answer_parts: list[str] = []

        async with httpx.AsyncClient(trust_env=False, timeout=300.0) as client:
            async with client.stream(
                "POST",
                f"{api_base}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=request_json,
            ) as response:
                response.raise_for_status()
                async for line in response.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data.get("choices", [{}])[0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                answer_parts.append(content)
                                resolved = token_resolver.feed(content)
                                if resolved:
                                    yield f"data: {json.dumps({'type': 'chunk', 'content': resolved})}\n\n"
                        except json.JSONDecodeError:
                            continue
        flushed = token_resolver.flush()
        if flushed:
            yield f"data: {json.dumps({'type': 'chunk', 'content': flushed})}\n\n"
        answer_text = "".join(answer_parts)
        corrected_answer = inject_missing_captioned_images(answer_text)
        corrected_answer = sanitize_answer_image_urls(corrected_answer)
        if corrected_answer != answer_text:
            corrected_answer = resolve_image_tokens(corrected_answer)
            yield f"data: {json.dumps({'type': 'message.replace', 'content': corrected_answer})}\n\n"

        total_duration = time.time() - total_start
        logger.info(f"[检索流水线] 总耗时: {total_duration*1000:.1f}ms")

        yield "data: [DONE]\n\n"

    except asyncio.CancelledError:
        logger.warning("RAG 流式连接被取消")
        yield f"data: {json.dumps({'type': 'error', 'message': '连接已取消'})}\n\n"
        yield "data: [DONE]\n\n"
    except Exception as e:
        logger.exception(f"流式查询失败: {e}")
        yield f"data: {json.dumps({'type': 'error', 'message': str(e)})}\n\n"
        yield "data: [DONE]\n\n"


@router.post("/query_stream")
async def rag_query_stream(
    request: RagQueryStreamRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    """
    检索知识库并流式生成答案

    返回 SSE (Server-Sent Events) 格式的流式响应：
    - type: "status" - 状态更新
    - type: "context" - 检索上下文和图片信息
    - type: "chunk" - 生成的文本片段
    - type: "error" - 错误信息

    完整检索流程：
    1. 关键词提取（向量搜索 + BM25 + 图拓扑融合）
    2. 阶段1：召回原始数据
    3. 阶段2：PPR 图过滤
    4. Rerank 加权融合
    5. MMR 多样性重排序
    6. 阶段3：资产组装
    7. 流式生成答案
    """
    try:
        from backend.config.settings import settings

        model_config = None
        if request.model_id:
            model_config = settings.get_model_config(request.model_id)
            if not model_config:
                raise HTTPException(
                    status_code=400, detail=f"不支持的模型: {request.model_id}"
                )

        kb_id = await _resolve_readable_kb_id(request.kb_id, auth, session)

        return StreamingResponse(
            _rag_stream_generator(
                request.query,
                kb_id,
                model_config,
                request.temperature,
                request.max_tokens,
                request.top_k,
                request.chunk_top_k,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"流式查询初始化失败: {e}")
        raise HTTPException(status_code=500, detail="流式查询初始化失败")


class RagInsertRequest(BaseModel):
    content_list: List[Dict[str, Any]] = Field(..., description="OCR content_list")
    file_path: str = Field(default="unknown_document", description="来源文件路径")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="额外参数")


class RagInsertResponse(BaseModel):
    result: Dict[str, Any]


class RagProcessMultimodalRequest(BaseModel):
    file_path: str = Field(..., description="文件路径")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="额外参数")


class RagInsertDocxRequest(BaseModel):
    file_path: str = Field(..., description="DOCX 文件路径")
    kb_id: str | None = Field(default=None, description="知识库 ID")
    kwargs: Dict[str, Any] = Field(default_factory=dict, description="额外参数")


class ImageInfoData(BaseModel):
    marker: str
    url: str
    path: str


class ImageUsageInfo(BaseModel):
    total: int
    used: int
    unused: int
    used_list: List[ImageInfoData] = Field(default_factory=list)
    unused_list: List[ImageInfoData] = Field(default_factory=list)
    all: List[ImageInfoData] = Field(default_factory=list)


class AcademicFormatInfo(BaseModel):
    has_abstract: bool = False
    has_introduction: bool = False
    has_conclusion: bool = False
    has_references: bool = False
    has_appendix: bool = False


class MetadataInfo(BaseModel):
    title: str
    intent: str
    intent_label: str
    confidence_level: str
    confidence_label: str
    knowledge_base_coverage: float
    coverage_percentage: str
    template_used: str


class RetrievalInfo(BaseModel):
    mode: str
    query_expansion_enabled: bool
    multi_hop_enabled: bool
    expanded_queries: List[str] = Field(default_factory=list)
    is_multi_hop: bool = False
    sub_queries: List[str] = Field(default_factory=list)


class RagResetRequest(BaseModel):
    new_work_dir: str | None = Field(default=None, description="新的 RAG_WORK_DIR")
    clear_old_work_dir: bool = Field(default=True, description="是否清空旧工作目录")
    kb_id: str | None = Field(default=None, description="知识库 ID")


class RagResetResponse(BaseModel):
    result: Dict[str, Any]


class RagDeleteDocRequest(BaseModel):
    doc_id: str = Field(..., description="需要删除的 doc_id")
    delete_llm_cache: bool = Field(default=False, description="是否删除 LLM 缓存")
    kb_id: str | None = Field(default=None, description="知识库 ID")


class RagDeleteDocResponse(BaseModel):
    result: Dict[str, Any]


class ModelInfo(BaseModel):
    id: str = Field(..., description="模型ID")
    name: str = Field(..., description="模型显示名称")
    provider: str = Field(..., description="服务商")
    description: str | None = Field(default=None, description="模型描述")


class ModelsListResponse(BaseModel):
    models: List[ModelInfo] = Field(default_factory=list, description="可用模型列表")
    default_model: str = Field(..., description="默认模型ID")


@router.get("/models", response_model=ModelsListResponse)
async def get_available_models():
    """获取可用对话模型列表 — 优先从数据库读取（usage_scope='chat'）"""
    result = await list_chat_models()
    return ModelsListResponse(
        models=[
            ModelInfo(
                id=model.get("id", ""),
                name=model.get("name", model.get("id", "")),
                provider=model.get("provider", "custom"),
                description=model.get("description"),
            )
            for model in result.get("models", [])
        ],
        default_model=result.get("default_model") or "qwen3-max",
    )


@router.get("/health")
async def rag_health(auth: ApiKeyAuth = Depends(verify_api_key)):
    return await get_rag_health()


@router.post("/insert", response_model=RagInsertResponse)
async def rag_insert(
    request: RagInsertRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    """
    插入文档到知识库（OCR content_list 格式）

    Args:
        request: 包含 content_list、file_path、kb_id 等参数

    Returns:
        插入结果
    """
    try:
        kb_id = await _resolve_writable_kb_id(request.kb_id, auth, session)

        result = await insert_content_list(
            content_list=request.content_list,
            file_path=request.file_path,
            kb_id=kb_id,
            **request.kwargs,
        )

        return RagInsertResponse(result=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"文档插入失败: {e}")
        raise HTTPException(status_code=500, detail="文档插入失败")


@router.post("/process_multimodal")
async def rag_process_multimodal(
    request: RagProcessMultimodalRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    """
    处理多模态文档（PDF、图片等）

    使用 OCR 服务处理文档，然后插入到知识库
    """
    try:
        kb_id = await _resolve_writable_kb_id(request.kb_id, auth, session)
        safe_file_path = _require_safe_server_file_path(request.file_path)

        result = await process_multimodal_document(
            file_path=safe_file_path,
            kb_id=kb_id,
            **request.kwargs,
        )

        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"多模态文档处理失败: {e}")
        raise HTTPException(status_code=500, detail="多模态文档处理失败")


@router.post("/insert_docx")
async def rag_insert_docx(
    request: RagInsertDocxRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    """
    插入 DOCX 文档到知识库
    """
    try:
        kb_id = await _resolve_writable_kb_id(request.kb_id, auth, session)
        safe_file_path = _require_safe_server_file_path(request.file_path)

        result = await insert_docx_document(
            file_path=safe_file_path,
            kb_id=kb_id,
            **request.kwargs,
        )

        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"DOCX 插入失败: {e}")
        raise HTTPException(status_code=500, detail="DOCX 插入失败")


@router.post("/reset", response_model=RagResetResponse)
async def rag_reset(
    request: RagResetRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    """
    重置 RAG 服务

    可选参数：
    - new_work_dir: 新的工作目录
    - clear_old_work_dir: 是否清空旧工作目录
    - kb_id: 指定知识库
    """
    try:
        if request.kb_id:
            await require_kb_access(session, request.kb_id, auth, access="write")
        elif (auth.role or "").lower() != "admin":
            raise HTTPException(
                status_code=403, detail="重置默认 RAG 服务需要管理员权限"
            )

        result = await reset_rag_runtime(
            new_work_dir=request.new_work_dir,
            clear_old_work_dir=request.clear_old_work_dir,
        )

        return RagResetResponse(result=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"RAG 重置失败: {e}")
        raise HTTPException(status_code=500, detail="RAG 重置失败")


@router.post("/delete_doc", response_model=RagDeleteDocResponse)
async def rag_delete_doc(
    request: RagDeleteDocRequest,
    auth: ApiKeyAuth = Depends(verify_api_key),
    session: AsyncSession = Depends(get_db_session),
):
    """
    删除文档

    Args:
        doc_id: 需要删除的文档 ID
        delete_llm_cache: 是否同时删除 LLM 缓存
        kb_id: 指定知识库
    """
    try:
        if request.kb_id:
            await require_kb_access(session, request.kb_id, auth, access="write")
        elif (auth.role or "").lower() != "admin":
            raise HTTPException(
                status_code=403, detail="删除默认知识库文档需要管理员权限"
            )

        result = await delete_rag_document(
            doc_id=request.doc_id,
            delete_llm_cache=request.delete_llm_cache,
            kb_id=request.kb_id,
        )

        return RagDeleteDocResponse(result=result)
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"文档删除失败: {e}")
        raise HTTPException(status_code=500, detail="文档删除失败")
