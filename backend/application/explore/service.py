"""探索式学习服务层。

提供发现态主题、思维导图（生成/展开）、知识点详解（流式）、关联资源、
习题练习（生成/评分）等能力。所有专业内容均先经 `retrieve_context` 检索
知识库上下文后再交由 LLM 生成，保证接地（grounded）与可溯源。

LLM 调用范式与 `backend/custom/services/follow_up_suggestions.py` 保持一致：
`create_chat_model(...).bind(...)` + `ainvoke` / `astream`，并对 JSON 输出做
正则容错解析，失败时回退到模板兜底，避免前端崩溃。
"""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.config.settings import settings

logger = logging.getLogger(__name__)

# 优化: 脑图缓存机制
_mindmap_cache: dict[str, tuple[dict, float]] = {}  # cache_key -> (result, timestamp)
_MINDMAP_CACHE_TTL = 3600 * 24  # 24小时
_MAX_MINDMAP_CACHE_SIZE = 200  # 最大缓存条目数


def clear_explore_cache(kb_id: str | None = None) -> int:
    """清理探索式学习缓存。
    
    Args:
        kb_id: 如果提供,只清理该知识库相关的缓存;否则清理全部
        
    Returns:
        清理的缓存条目数
    """
    global _mindmap_cache
    
    if kb_id is None:
        count = len(_mindmap_cache)
        _mindmap_cache.clear()
        logger.info("[Explore] 清理全部缓存,共清理 %d 条", count)
        return count
    
    # 只清理指定知识库的缓存
    keys_to_remove = [k for k in _mindmap_cache if kb_id in k]
    for k in keys_to_remove:
        del _mindmap_cache[k]
    
    logger.info("[Explore] 清理知识库 %s 的缓存,共清理 %d 条", kb_id, len(keys_to_remove))
    return len(keys_to_remove)

# 默认学科/领域名（用于 Prompt）。可按部署需要在 settings 中覆盖。
DEFAULT_SUBJECT = getattr(settings, "explore_subject", None) or "核电子学"

# 前端领域标识 → 学科名映射。不同前端（RadAI / CSH-AI）传入不同 domain 以切换学科与预制题库。
SUBJECT_BY_DOMAIN: dict[str, str] = {
    "ne": "核电子学",
    "csh": "场地污染水文地质",
}


def resolve_subject(domain: str | None, subject: str | None = None) -> str:
    """根据 domain（或显式 subject）解析学科名。"""
    if subject and subject.strip():
        return subject.strip()
    return SUBJECT_BY_DOMAIN.get(str(domain or "").strip().lower(), DEFAULT_SUBJECT)

# 发现态预制主题清单（按学科）。即时返回，避免 LLM 生成带来的等待与超时。
PRESET_TOPICS: dict[str, list[dict[str, str]]] = {
    "核电子学": [
        {"title": "核电子学基础", "hint": "信号链与测量系统总览"},
        {"title": "前置放大器", "hint": "微弱信号放大与阻抗匹配"},
        {"title": "主放大器", "hint": "信号放大与脉冲成形"},
        {"title": "脉冲成形电路", "hint": "CR-RC 成形与信噪比优化"},
        {"title": "多道分析器", "hint": "能谱测量与道址分配"},
        {"title": "甄别器与定时", "hint": "阈值甄别与时间测量"},
        {"title": "符合与反符合", "hint": "事件关联与本底抑制"},
        {"title": "模数转换", "hint": "ADC 原理与精度指标"},
        {"title": "数字脉冲处理", "hint": "数字成形与在线算法"},
        {"title": "探测器原理", "hint": "气体/闪烁/半导体探测器"},
        {"title": "闪烁探测器", "hint": "光电倍增与能量分辨"},
        {"title": "半导体探测器", "hint": "HPGe 与硅探测器"},
        {"title": "核辐射测量系统", "hint": "完整测量链路构成"},
        {"title": "能量分辨率", "hint": "影响因素与改善方法"},
        {"title": "能谱分析方法", "hint": "峰位标定与定量分析"},
        {"title": "符合定时技术", "hint": "时间分辨与定时方法"},
        {"title": "辐射剂量测量", "hint": "剂量率与累计剂量"},
        {"title": "辐射防护", "hint": "屏蔽、距离与时间"},
        {"title": "电荷灵敏放大", "hint": "电荷积分与噪声"},
        {"title": "噪声与信噪比", "hint": "噪声来源与抑制"},
        {"title": "中子探测技术", "hint": "慢化、转换与计数"},
        {"title": "时间幅度变换", "hint": "TAC 原理与应用"},
        {"title": "数据获取系统", "hint": "DAQ 架构与触发"},
        {"title": "高压电源", "hint": "探测器偏压与稳定性"},
        {"title": "标准核仪器", "hint": "NIM 机箱与模块"},
        {"title": "信号传输与匹配", "hint": "电缆、阻抗与反射"},
        {"title": "符合测量实验", "hint": "典型实验方法与流程"},
        {"title": "峰康比与本底", "hint": "谱线质量评价指标"},
    ],
    "场地污染水文地质": [
        {"title": "水文地质基础", "hint": "地下水系统总体认识"},
        {"title": "含水层与隔水层", "hint": "类型、特征与判别"},
        {"title": "地下水流动", "hint": "达西定律与流场"},
        {"title": "渗透系数", "hint": "测定方法与影响因素"},
        {"title": "包气带特征", "hint": "非饱和带水分运移"},
        {"title": "抽水试验", "hint": "参数求取与分析"},
        {"title": "地下水补给与排泄", "hint": "水均衡要素"},
        {"title": "污染物运移", "hint": "对流-弥散过程"},
        {"title": "吸附与降解", "hint": "迁移转化机理"},
        {"title": "污染羽演化", "hint": "范围预测与控制"},
        {"title": "污染源识别", "hint": "源解析与示踪"},
        {"title": "场地环境调查", "hint": "分阶段调查流程"},
        {"title": "采样与监测", "hint": "布点、采样与质控"},
        {"title": "地下水监测井", "hint": "建井与洗井规范"},
        {"title": "水文地质勘察", "hint": "勘探手段与编录"},
        {"title": "水质分析", "hint": "常规指标与解读"},
        {"title": "重金属污染", "hint": "形态与迁移特征"},
        {"title": "有机污染物", "hint": "VOCs/LNAPL/DNAPL"},
        {"title": "土壤污染", "hint": "成因与分布规律"},
        {"title": "风险评估", "hint": "暴露途径与管控值"},
        {"title": "概念模型", "hint": "水文地质概念建模"},
        {"title": "数值模拟", "hint": "MODFLOW/MT3D 应用"},
        {"title": "抽出处理", "hint": "P&T 修复技术"},
        {"title": "原位化学氧化", "hint": "ISCO 药剂与工艺"},
        {"title": "可渗透反应墙", "hint": "PRB 设计要点"},
        {"title": "监测自然衰减", "hint": "MNA 评估方法"},
        {"title": "阻隔与帷幕", "hint": "防渗与污染阻隔"},
        {"title": "修复效果评估", "hint": "验收与长期监测"},
    ],
}

_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")
_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")
_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)

# 各能力的 LLM 超时（秒）
TIMEOUT_MAP = 25.0
TIMEOUT_EXPAND = 20.0
TIMEOUT_QUIZ = 60.0
TIMEOUT_GRADE = 25.0


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def _resolve_model_name(model_name: str | None) -> str | None:
    resolved = str(model_name or "").strip()
    if resolved:
        return resolved
    try:
        from backend.core.agents.config_compat import get_default_model_name

        return get_default_model_name()
    except Exception:
        return None


def _normalize_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return "\n".join(parts)
    return str(content or "")


def _parse_json(content: str) -> Any | None:
    """从 LLM 输出中稳健提取 JSON（兼容 ```json 包裹 / 前后多余文本）。"""
    text = str(content or "").strip()
    if not text:
        return None

    candidates: list[str] = []
    fence = _FENCE_RE.search(text)
    if fence:
        candidates.append(fence.group(1).strip())
    candidates.append(text)
    obj = _JSON_OBJ_RE.search(text)
    if obj:
        candidates.append(obj.group(0))
    arr = _JSON_ARRAY_RE.search(text)
    if arr:
        candidates.append(arr.group(0))

    for cand in candidates:
        try:
            return json.loads(cand)
        except (json.JSONDecodeError, TypeError):
            continue
    return None


async def _retrieve_context(query: str, kb_id: str | None, *, chunk_top_k: int = 8) -> tuple[str, list[dict[str, Any]]]:
    """调用统一检索入口，返回 (context_str, sources)。失败时降级为空。
    
    优化: 探索式学习场景直接传入知识点标题作为外部关键词,跳过LLM关键词提取
    """
    try:
        from backend.api.routers.rag import retrieve_context

        filtered_data, context_str, _images, _stats = await retrieve_context(
            query=query,
            kb_id=kb_id,
            chunk_top_k=chunk_top_k,
            external_hl_keywords=[query],  # 优化: 直接传入知识点标题,跳过关键词提取
            external_ll_keywords=[],
            disable_stage1_oversample=True,  # 优化: 关闭过采样,减少召回量
        )
        sources = _extract_sources(filtered_data)
        return (context_str or ""), sources
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Explore] retrieve_context 失败 (kb=%s, q=%s): %s", kb_id, query[:40], exc)
        return "", []


def build_resource_download_url(kb_id: str | None, doc_id: str | None) -> str:
    """构建知识库文档下载 URL（非用户文件中心 file_id）。"""
    kb = str(kb_id or "").strip()
    doc = str(doc_id or "").strip()
    if not kb or not doc:
        return ""
    return f"/api/v2/explore/resources/download?kb_id={quote(kb)}&doc_id={quote(doc)}"


def _guess_mime_type(filename: str) -> str:
    mime, _ = mimetypes.guess_type(filename)
    return mime or "application/octet-stream"


def _build_content_disposition(filename: str) -> str:
    display_name = (filename or "download").strip() or "download"
    try:
        display_name.encode("ascii")
        return f'attachment; filename="{display_name}"'
    except UnicodeEncodeError:
        encoded = quote(display_name, safe="")
        return f'attachment; filename="download"; filename*=UTF-8\'\'{encoded}'


async def _resolve_kb_document_storage(
    session: AsyncSession,
    kb_id: str,
    doc_id: str,
) -> tuple[str, str, str]:
    """解析知识库文档的存储位置，返回 (storage_key, display_name, source_kind)。"""
    from backend.db.crud.knowledge_base_crud import KnowledgeBaseDocumentCRUD
    from backend.db.models import PendingFile

    doc_crud = KnowledgeBaseDocumentCRUD()
    doc = await doc_crud.get_by_doc_id(session, kb_id, doc_id)
    if not doc:
        raise ValueError("文档不存在")

    file_name = str(doc.file_name or "download").strip() or "download"
    file_path = str(doc.file_path or "").strip()

    if file_path:
        path_obj = Path(file_path)
        if path_obj.is_absolute() and path_obj.exists() and path_obj.is_file():
            return str(path_obj), file_name, "local"

        normalized = file_path.replace("\\", "/").lstrip("/")
        if normalized.startswith(("pending/", "files/", "kb/", "uploads/")):
            return normalized, file_name, "minio"

    if file_path:
        pending_by_path = await session.execute(
            select(PendingFile)
            .where(PendingFile.kb_id == kb_id)
            .where(PendingFile.minio_path == file_path)
            .where(PendingFile.status == 2)
            .order_by(PendingFile.created_at.desc())
            .limit(1)
        )
        pending = pending_by_path.scalar_one_or_none()
        if pending:
            return pending.minio_path, file_name, "minio"

    pending_by_name = await session.execute(
        select(PendingFile)
        .where(PendingFile.kb_id == kb_id)
        .where(PendingFile.status == 2)
        .where(
            or_(
                PendingFile.file_name == file_name,
                PendingFile.file_name == file_path,
            )
        )
        .order_by(PendingFile.created_at.desc())
        .limit(1)
    )
    pending = pending_by_name.scalar_one_or_none()
    if pending:
        return pending.minio_path, file_name, "minio"

    if file_path:
        cache_candidate = Path(__file__).resolve().parents[2] / "data" / "cache" / Path(file_path).name
        if cache_candidate.exists() and cache_candidate.is_file():
            return str(cache_candidate), file_name, "local"

    raise ValueError("找不到文档原始文件")


async def download_kb_resource(
    session: AsyncSession,
    kb_id: str,
    doc_id: str,
) -> tuple[bytes | Path, str, str, str]:
    """下载知识库文档，返回 (payload_or_path, filename, media_type, source_kind)。"""
    storage_key, filename, source_kind = await _resolve_kb_document_storage(session, kb_id, doc_id)
    media_type = _guess_mime_type(filename)

    if source_kind == "local":
        return Path(storage_key), filename, media_type, source_kind

    from backend.config import settings
    from backend.storage import _get_minio_client, _normalize_minio_object_name

    def _read_minio() -> bytes:
        client = _get_minio_client()
        candidates = [storage_key]
        normalized = _normalize_minio_object_name(storage_key)
        if normalized and normalized not in candidates:
            candidates.append(normalized)
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                response = client.get_object(settings.minio_bucket_name, candidate)
                try:
                    return response.read()
                finally:
                    response.close()
                    response.release_conn()
            except Exception as exc:  # noqa: BLE001
                last_error = exc
        assert last_error is not None
        raise last_error

    content = await asyncio.to_thread(_read_minio)
    return content, filename, media_type, source_kind


def _extract_sources(filtered_data: Any) -> list[dict[str, Any]]:
    """从 FilteredData 提取关联资源（去重，保留最高分）。"""
    if not filtered_data:
        return []
    chunks = getattr(filtered_data, "chunks", None) or []
    by_doc: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        content = _strip_image_blocks(str(chunk.get("content", "") or "")).strip()
        file_path = str(chunk.get("file_path", chunk.get("source", "")) or "")
        doc_id = str(chunk.get("doc_id", chunk.get("_doc_id", "")) or "")
        full_doc_id = str(chunk.get("full_doc_id", "") or "")
        score = float(chunk.get("_fused_score", chunk.get("_rerank_score", 0.0)) or 0.0)
        key = doc_id or full_doc_id or file_path or content[:32]
        if not key:
            continue
        snippet = content[:240]
        doc_name = file_path.rsplit("/", 1)[-1] if file_path else (doc_id or "知识库文档")
        existing = by_doc.get(key)
        if existing is None:
            by_doc[key] = {
                "doc_id": doc_id or full_doc_id,
                "doc_name": doc_name,
                "file_path": file_path,
                "snippet": snippet,
                "score": round(score, 4),
            }
            order.append(key)
        elif score > existing.get("score", 0.0):
            existing["score"] = round(score, 4)
            if len(snippet) > len(existing.get("snippet", "")):
                existing["snippet"] = snippet
    resources = [by_doc[k] for k in order]
    resources.sort(key=lambda r: r.get("score", 0.0), reverse=True)
    return resources


async def _llm_json(prompt: str, *, model_name: str | None, timeout: float, max_tokens: int = 2048, temperature: float = 0.3) -> Any | None:
    """调用 LLM 并解析 JSON 输出，带超时与异常兜底。"""
    from langchain_core.messages import HumanMessage

    from backend.core.models import create_chat_model

    try:
        model = create_chat_model(name=_resolve_model_name(model_name), thinking_enabled=False)
        model = model.bind(temperature=temperature, max_tokens=max_tokens)
        
        # 使用 astream 获取流式输出
        stream = model.astream([HumanMessage(content=prompt)])
        iterator = stream.__aiter__()
        
        # 1. 尝试在指定超时时间内获取首个 chunk
        try:
            first_chunk = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return None
        except asyncio.TimeoutError:
            logger.info("[Explore] LLM 首 token 超时 (timeout=%.1fs)", timeout)
            return None
            
        chunks = [first_chunk]
        
        # 2. 已有首包输出，后续接收不设限（或设置较长的总保护时间），读取完整个流
        # 为了防止极端情况下的无限挂起，使用 300 秒长超时保护
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=300.0)
                chunks.append(chunk)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                logger.warning("[Explore] LLM 读取后续 token 超时保护触发")
                break
                
        # 3. 拼接并解析 JSON
        full_content = ""
        for chunk in chunks:
            content = getattr(chunk, "content", "")
            if content:
                full_content += _normalize_content(content)
                
        return _parse_json(full_content)
        
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Explore] LLM 调用失败: %s", exc)
        return None


_IMG_BLOCK_RE = re.compile(
    r"Image Content Analysis:\s*\n?Image Path:[^\n]*\n?(?:Captions:[^\n]*\n?)?(?:Footnotes:[^\n]*\n?)?",
    re.IGNORECASE,
)
_IMG_PATH_RE = re.compile(r"Image Path:\s*\S+", re.IGNORECASE)


def _strip_image_blocks(text: str) -> str:
    """移除片段中的图片分析块/原始图片路径，避免在关联资源里露出预签名 URL 文本。"""
    cleaned = _IMG_BLOCK_RE.sub("", text or "")
    cleaned = _IMG_PATH_RE.sub("", cleaned)
    return cleaned


def _gen_id(prefix: str) -> str:
    import uuid

    return f"{prefix}_{uuid.uuid4().hex[:10]}"


def _clean_text(value: Any, limit: int = 120) -> str:
    text = str(value or "").strip()
    if len(text) > limit:
        text = text[:limit]
    return text


# ---------------------------------------------------------------------------
# 能力函数
# ---------------------------------------------------------------------------

async def generate_topics(
    *, kb_id: str | None, count: int = 24, seed: str = "", subject: str | None = None, model_name: str | None = None
) -> dict[str, Any]:
    """发现态主题为**预制清单**（即时返回、不调用 LLM），每次随机抽取以支持「换一批」。"""
    import random

    subject = subject or DEFAULT_SUBJECT
    count = max(8, min(28, int(count or 24)))
    presets = list(PRESET_TOPICS.get(subject) or PRESET_TOPICS.get(DEFAULT_SUBJECT) or [])
    random.shuffle(presets)
    selected = presets[:count]
    topics = [{"id": _gen_id("t"), "title": t["title"], "hint": t.get("hint", "")} for t in selected]
    return {"topics": topics, "kb_id": kb_id, "source": "preset"}


async def generate_map(
    *, topic: str, kb_id: str | None, max_children: int = 6, subject: str | None = None, model_name: str | None = None
) -> dict[str, Any]:
    """生成思维导图（根 + 一级子节点）。
    
    优化: 添加服务端缓存机制,相同主题直接返回缓存结果
    """
    subject = subject or DEFAULT_SUBJECT
    topic = _clean_text(topic, 40) or subject
    max_children = max(3, min(8, int(max_children or 6)))
    
    # 优化: 检查缓存
    cache_key = f"map:{topic}:{kb_id}:{max_children}"
    if cache_key in _mindmap_cache:
        result, timestamp = _mindmap_cache[cache_key]
        if time.time() - timestamp < _MINDMAP_CACHE_TTL:
            logger.info("[Explore] 命中脑图缓存: %s", cache_key)
            return {**result, "source": "cache"}
    
    # 轻量化构建：脑图仅依据提示词直接调用模型生成，不检索知识库（避免延迟与超时）。
    from backend.application.explore.prompts import build_map_prompt

    prompt = build_map_prompt(subject=subject, topic=topic, max_children=max_children, context="")
    parsed = await _llm_json(prompt, model_name=model_name, timeout=TIMEOUT_MAP, max_tokens=1024, temperature=0.5)

    root = {"id": "n_root", "title": topic, "summary": ""}
    children: list[dict[str, Any]] = []
    if isinstance(parsed, dict):
        raw_root = parsed.get("root")
        if isinstance(raw_root, dict):
            root["title"] = _clean_text(raw_root.get("title"), 40) or topic
            root["summary"] = _clean_text(raw_root.get("summary"), 60)
        children = _normalize_nodes(parsed.get("children"), max_children)

    source = "llm"
    if not children:
        children = [
            {"id": _gen_id("n"), "title": t, "summary": "", "expandable": True}
            for t in _fallback_children(topic)
        ][:max_children]
        source = "fallback"
    
    result = {"root": root, "children": children, "kb_id": kb_id, "source": source}
    
    # 优化: 写入缓存
    _mindmap_cache[cache_key] = (result, time.time())
    if len(_mindmap_cache) > _MAX_MINDMAP_CACHE_SIZE:
        oldest_key = min(_mindmap_cache, key=lambda k: _mindmap_cache[k][1])
        del _mindmap_cache[oldest_key]
        logger.info("[Explore] 脑图缓存已满,清理最旧条目: %s", oldest_key)
    
    return result


async def expand_node(
    *, node_title: str, path: list[str], kb_id: str | None, max_children: int = 5, subject: str | None = None, model_name: str | None = None
) -> dict[str, Any]:
    """展开节点的子知识点。
    
    优化: 添加服务端缓存机制,相同节点直接返回缓存结果
    """
    subject = subject or DEFAULT_SUBJECT
    node_title = _clean_text(node_title, 40)
    max_children = max(2, min(6, int(max_children or 5)))
    
    # 优化: 检查缓存
    cache_key = f"expand:{node_title}:{'|'.join(path or [])}:{kb_id}:{max_children}"
    if cache_key in _mindmap_cache:
        result, timestamp = _mindmap_cache[cache_key]
        if time.time() - timestamp < _MINDMAP_CACHE_TTL:
            logger.info("[Explore] 命中节点展开缓存: %s", cache_key)
            return {**result, "source": "cache"}
    
    # 轻量化构建：节点展开仅依据提示词直接调用模型生成，不检索知识库。
    from backend.application.explore.prompts import build_expand_prompt

    prompt = build_expand_prompt(
        subject=subject, node_title=node_title, path=path or [], max_children=max_children, context=""
    )
    parsed = await _llm_json(prompt, model_name=model_name, timeout=TIMEOUT_EXPAND, max_tokens=1024, temperature=0.5)

    children: list[dict[str, Any]] = []
    if isinstance(parsed, dict):
        children = _normalize_nodes(parsed.get("children"), max_children)
    elif isinstance(parsed, list):
        children = _normalize_nodes(parsed, max_children)
    
    result = {"children": children, "source": "llm" if children else "fallback"}
    
    # 优化: 写入缓存
    _mindmap_cache[cache_key] = (result, time.time())
    if len(_mindmap_cache) > _MAX_MINDMAP_CACHE_SIZE:
        oldest_key = min(_mindmap_cache, key=lambda k: _mindmap_cache[k][1])
        del _mindmap_cache[oldest_key]
        logger.info("[Explore] 节点展开缓存已满,清理最旧条目: %s", oldest_key)
    
    return result


async def gather_resources(
    *, node_title: str, kb_id: str | None, top_k: int = 8
) -> dict[str, Any]:
    """获取知识点关联资源。
    
    优化: 添加缓存机制,相同知识点直接返回缓存结果
    """
    node_title = _clean_text(node_title, 60)
    top_k = max(3, min(15, int(top_k or 8)))
    
    # 优化: 检查缓存
    cache_key = f"resources:{node_title}:{kb_id}:{top_k}"
    if cache_key in _mindmap_cache:
        result, timestamp = _mindmap_cache[cache_key]
        if time.time() - timestamp < _MINDMAP_CACHE_TTL:
            logger.info("[Explore] 命中资源缓存: %s", cache_key)
            return result
    
    _context, sources = await _retrieve_context(node_title, kb_id, chunk_top_k=top_k)
    for s in sources:
        s["kb_id"] = kb_id
        s["download_url"] = build_resource_download_url(kb_id, s.get("doc_id"))
    
    result = {"resources": sources[:top_k], "grounded": bool(sources)}
    
    # 优化: 写入缓存
    _mindmap_cache[cache_key] = (result, time.time())
    if len(_mindmap_cache) > _MAX_MINDMAP_CACHE_SIZE:
        oldest_key = min(_mindmap_cache, key=lambda k: _mindmap_cache[k][1])
        del _mindmap_cache[oldest_key]
        logger.info("[Explore] 资源缓存已满,清理最旧条目: %s", oldest_key)
    
    return result


async def generate_quiz(
    *,
    node_title: str,
    kb_id: str | None,
    types: list[str],
    difficulty: str = "all",
    count: int = 6,
    subject: str | None = None,
    model_name: str | None = None,
) -> dict[str, Any]:
    subject = subject or DEFAULT_SUBJECT
    node_title = _clean_text(node_title, 60)
    valid_types = [t for t in (types or []) if t in {"choice", "multi", "judge", "short", "calc"}]
    if not valid_types:
        valid_types = ["choice", "judge", "short"]
    difficulty = difficulty if difficulty in {"all", "easy", "medium", "hard"} else "all"
    count = max(2, min(12, int(count or 6)))
    context, sources = await _retrieve_context(node_title, kb_id, chunk_top_k=10)

    from backend.application.explore.prompts import build_quiz_prompt

    prompt = build_quiz_prompt(
        subject=subject, node_title=node_title, types=valid_types, difficulty=difficulty, count=count, context=context
    )
    parsed = await _llm_json(prompt, model_name=model_name, timeout=TIMEOUT_QUIZ, max_tokens=3072, temperature=0.5)

    questions: list[dict[str, Any]] = []
    raw_questions = []
    if isinstance(parsed, dict):
        raw_questions = parsed.get("questions") or []
    elif isinstance(parsed, list):
        raw_questions = parsed
    source_ids = [s.get("doc_id") for s in sources if s.get("doc_id")][:3]
    for item in raw_questions:
        q = _normalize_question(item, source_ids)
        if q:
            questions.append(q)
        if len(questions) >= count:
            break

    return {"questions": questions, "source": "llm" if questions else "fallback"}


async def grade_answer(
    *, question: dict[str, Any], user_answer: str, subject: str | None = None, model_name: str | None = None
) -> dict[str, Any]:
    subject = subject or DEFAULT_SUBJECT
    stem = _clean_text(question.get("stem"), 400)
    reference = _clean_text(question.get("reference_answer"), 800)
    scoring_points = question.get("scoring_points") or []
    if not isinstance(scoring_points, list):
        scoring_points = []

    from backend.application.explore.prompts import build_grade_prompt

    prompt = build_grade_prompt(
        subject=subject,
        stem=stem,
        reference_answer=reference,
        scoring_points=scoring_points,
        user_answer=_clean_text(user_answer, 1500),
    )
    parsed = await _llm_json(prompt, model_name=model_name, timeout=TIMEOUT_GRADE, max_tokens=1024, temperature=0.2)
    if isinstance(parsed, dict):
        try:
            score = int(parsed.get("score", 0))
        except (TypeError, ValueError):
            score = 0
        score = max(0, min(100, score))
        verdict = str(parsed.get("verdict", "")).strip() or ("正确" if score >= 80 else "部分正确" if score >= 40 else "错误")
        missing = parsed.get("missing_points") or []
        if not isinstance(missing, list):
            missing = []
        return {
            "score": score,
            "verdict": verdict,
            "feedback": _clean_text(parsed.get("feedback"), 200),
            "missing_points": [_clean_text(m, 60) for m in missing if str(m).strip()],
        }
    return {"score": 0, "verdict": "无法评分", "feedback": "评分服务暂时不可用，请稍后重试。", "missing_points": []}


def build_detail_question(node_title: str, path: list[str]) -> str:
    """将知识点构造为面向轻量问答智能体的提问。"""
    node_title = _clean_text(node_title, 60)
    ctx = " → ".join([p for p in (path or []) if p][-3:])
    ctx_hint = f"（学习脉络：{ctx}）" if ctx else ""
    return (
        f"请系统、详细地讲解知识点「{node_title}」{ctx_hint}。"
        "要求使用规范 Markdown 分节组织（基本定义、核心原理、典型结构、关键要点），"
        "如有公式请使用 KaTeX 语法，内容需基于知识库、专业准确。"
    )


# ---------------------------------------------------------------------------
# 归一化与兜底
# ---------------------------------------------------------------------------

def _normalize_nodes(raw: Any, max_children: int) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return nodes
    seen: set[str] = set()
    for item in raw:
        if isinstance(item, dict):
            title = _clean_text(item.get("title"), 28)
            summary = _clean_text(item.get("summary"), 60)
            expandable = bool(item.get("expandable", True))
        else:
            title, summary, expandable = _clean_text(item, 28), "", True
        if not title or title in seen:
            continue
        seen.add(title)
        nodes.append({"id": _gen_id("n"), "title": title, "summary": summary, "expandable": expandable})
        if len(nodes) >= max_children:
            break
    return nodes


def _normalize_question(item: Any, source_ids: list[str]) -> dict[str, Any] | None:
    if not isinstance(item, dict):
        return None
    qtype = str(item.get("type", "")).strip().lower()
    if qtype not in {"choice", "multi", "judge", "short", "calc"}:
        return None
    stem = _clean_text(item.get("stem"), 400)
    if not stem:
        return None
    difficulty = str(item.get("difficulty", "medium")).strip().lower()
    if difficulty not in {"easy", "medium", "hard"}:
        difficulty = "medium"
    q: dict[str, Any] = {
        "id": _gen_id("q"),
        "type": qtype,
        "difficulty": difficulty,
        "stem": stem,
        "explanation": _clean_text(item.get("explanation"), 400),
    }
    if source_ids:
        q["source_refs"] = source_ids

    if qtype in {"choice", "multi"}:
        options = []
        raw_options = item.get("options") or []
        for idx, opt in enumerate(raw_options):
            if isinstance(opt, dict):
                key = str(opt.get("key", "")).strip() or chr(ord("A") + idx)
                text = _clean_text(opt.get("text"), 160)
            else:
                key = chr(ord("A") + idx)
                text = _clean_text(opt, 160)
            if text:
                options.append({"key": key, "text": text})
        if len(options) < 2:
            return None
        q["options"] = options
        answer = item.get("answer")
        if isinstance(answer, list):
            q["answer"] = "".join(str(a).strip() for a in answer)
        else:
            q["answer"] = str(answer or "").strip()
    elif qtype == "judge":
        ans = str(item.get("answer", "")).strip()
        q["answer"] = "正确" if ans in {"正确", "对", "true", "True", "T", "是"} else "错误"
    else:  # short / calc
        q["reference_answer"] = _clean_text(item.get("reference_answer") or item.get("answer"), 800)
        points = item.get("scoring_points") or []
        if isinstance(points, list):
            q["scoring_points"] = [_clean_text(p, 80) for p in points if str(p).strip()]
        else:
            q["scoring_points"] = []
    return q


def _fallback_topics(subject: str) -> list[str]:
    return [
        f"{subject}基础", "核心概念", "基本原理", "典型结构", "关键技术",
        "测量方法", "信号处理", "系统组成", "应用场景", "常见问题",
        "发展趋势", "实验方法",
    ]


def _fallback_children(topic: str) -> list[str]:
    return [f"{topic}·基本概念", f"{topic}·工作原理", f"{topic}·典型结构", f"{topic}·关键要点", f"{topic}·应用"]
