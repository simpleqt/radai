"""探索式学习服务层。

提供发现态主题、思维导图（生成/展开）、知识点详解（流式）、关联资源、
习题练习（生成/评分）等能力。所有专业内容均先经 `retrieve_context` 检索
知识库上下文后再交由 LLM 生成，保证接地（grounded）与可溯源。

LLM 调用范式与 `backend/custom/services/follow_up_suggestions.py` 保持一致：
`create_chat_model(...).bind(...)` + `ainvoke` / `astream`，并对 JSON 输出做
正则容错解析，失败时回退到模板兜底，避免前端崩溃。
"""

from __future__ import annotations

import ast
import asyncio
import json
import logging
import mimetypes
import os
import re
import time
from pathlib import Path
from typing import Any, AsyncIterator
from urllib.parse import quote

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.application.llm_content import normalize_llm_content as _normalize_content
from backend.config.settings import settings

logger = logging.getLogger(__name__)

# 优化: 脑图缓存机制
_mindmap_cache: dict[str, tuple[dict, float]] = {}  # cache_key -> (result, timestamp)
_MINDMAP_CACHE_TTL = 3600 * 24  # 24小时
_MAX_MINDMAP_CACHE_SIZE = 200  # 最大缓存条目数


def _strip_resource_noise(value: Any, *, max_len: int | None = None) -> str:
    """清理知识库召回片段中的 HTML、链接和元信息噪音。"""
    text = str(value or "")
    text = re.sub(r"<!doctype[^>]*>", " ", text, flags=re.I)
    text = re.sub(r"<script[\s\S]*?</script>", " ", text, flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"&nbsp;|&#160;", " ", text, flags=re.I)
    text = re.sub(r"&amp;", "&", text, flags=re.I)
    text = re.sub(r"&lt;|&gt;|&quot;|&#39;", " ", text, flags=re.I)
    text = re.sub(
        r"\b(charset|viewport|initial-scale|device-width|base href|content=|meta name)\b[^，。；\n]*",
        " ",
        text,
        flags=re.I,
    )
    text = re.sub(r"\s+", " ", text).strip(" -_:\t\r\n")
    if max_len and len(text) > max_len:
        return f"{text[:max_len].rstrip()}..."
    return text


def _looks_like_machine_id(value: str) -> bool:
    text = (value or "").strip()
    return bool(
        re.match(r"^doc[-_][a-z0-9-]{12,}$", text, flags=re.I)
        or re.match(r"^[a-f0-9]{20,}$", text, flags=re.I)
        or re.match(r"^chunk[-_][a-z0-9-]{10,}$", text, flags=re.I)
    )


def _derive_resource_title(*candidates: Any) -> str:
    for candidate in candidates:
        text = _strip_resource_noise(candidate, max_len=72)
        if text and not _looks_like_machine_id(text) and not text.lower().startswith("visual analysis"):
            return text
    return "知识库文档"


def _title_from_content(content: str) -> str:
    text = _strip_resource_noise(content, max_len=96)
    if not text:
        return ""
    for sep in ("。", "；", ";", "\n"):
        if sep in text:
            text = text.split(sep, 1)[0]
            break
    text = text.strip(" -_：:")
    if text.lower().startswith("visual analysis"):
        text = text.replace("Visual Analysis:", "", 1).strip(" -_：:")
    return _strip_resource_noise(text, max_len=64)


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
DEFAULT_DOMAIN = "ne"

# 前端领域标识 → 学科名映射。不同前端（RadAI / CSH-AI）传入不同 domain 以切换学科与预制题库。
SUBJECT_BY_DOMAIN: dict[str, str] = {
    "ne": DEFAULT_SUBJECT,
    "csh": "场地污染水文地质",
    "fmca": "基础力学",
}
AGENT_CODE_BY_DOMAIN: dict[str, str] = {
    "ne": "ne",
    "csh": "csh",
    "fmca": "fmca",
}


def resolve_domain(domain: str | None) -> str:
    """将 Explore 领域归一化为已注册标识。

    省略值、``None`` 和空白值保持历史默认 ``ne``；显式非空未知值
    不得降级到默认领域，避免在错误的专业语境中生成内容。
    """
    normalized = str(domain or "").strip().lower()
    if not normalized:
        return DEFAULT_DOMAIN
    if normalized not in SUBJECT_BY_DOMAIN:
        supported = ", ".join(sorted(SUBJECT_BY_DOMAIN))
        raise ValueError(
            f"Unsupported explore domain {normalized!r}; supported domains: {supported}"
        )
    return normalized


def resolve_subject(domain: str | None, subject: str | None = None) -> str:
    """根据 domain（或显式 subject）解析学科名。"""
    normalized_domain = resolve_domain(domain)
    if subject and subject.strip():
        return subject.strip()
    return SUBJECT_BY_DOMAIN[normalized_domain]


def resolve_agent_code(domain: str | None) -> str:
    """解析 Explore 详解请求应使用的运行时智能体代码。"""
    normalized_domain = resolve_domain(domain)
    try:
        return AGENT_CODE_BY_DOMAIN[normalized_domain]
    except KeyError as exc:  # 新领域必须同时声明运行时智能体。
        raise ValueError(
            f"Explore domain {normalized_domain!r} has no registered runtime agent"
        ) from exc

# 发现态预制主题清单（按学科）。即时返回，避免 LLM 生成带来的等待与超时。
PRESET_TOPICS: dict[str, list[dict[str, str]]] = {
    "基础力学": [
        {"title": "质点运动学", "hint": "位置、速度与加速度"},
        {"title": "牛顿运动定律", "hint": "受力分析与运动方程"},
        {"title": "常见力与约束", "hint": "重力、弹力、摩擦与张力"},
        {"title": "功与动能", "hint": "功—能定理及其应用"},
        {"title": "势能与机械能", "hint": "保守力与能量守恒"},
        {"title": "动量与冲量", "hint": "动量定理和碰撞问题"},
        {"title": "质心运动", "hint": "质点系的整体描述"},
        {"title": "角动量", "hint": "角动量定理与守恒"},
        {"title": "圆周运动", "hint": "切向与法向运动分析"},
        {"title": "万有引力", "hint": "引力场与轨道运动"},
        {"title": "刚体定轴转动", "hint": "角速度、角加速度与转矩"},
        {"title": "转动惯量", "hint": "计算方法与平行轴定理"},
        {"title": "刚体的功和能", "hint": "转动动能与能量方法"},
        {"title": "静力学平衡", "hint": "力和力矩的平衡条件"},
        {"title": "简谐振动", "hint": "运动方程、周期与能量"},
        {"title": "阻尼与受迫振动", "hint": "衰减、共振与稳态响应"},
        {"title": "机械波基础", "hint": "波动方程与能量传播"},
        {"title": "参考系与相对运动", "hint": "速度和加速度变换"},
        {"title": "非惯性系", "hint": "惯性力与转动参考系"},
        {"title": "力学建模方法", "hint": "受力图、约束与量纲检查"},
    ],
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


def _load_preset_maps() -> dict[str, dict[str, Any]]:
    """加载离线生成的脑图，兼容 JSON 文件和历史 Python 数据文件。"""
    base_dir = Path(__file__).resolve().parent
    preset_maps: dict[str, dict[str, Any]] = {}

    for path in (base_dir / "preset_maps.py", base_dir / "preset_maps.json"):
        if not path.exists():
            continue
        try:
            payload = path.read_text(encoding="utf-8")
            if path.suffix == ".py":
                marker = "PRESET_MAPS ="
                if marker not in payload:
                    continue
                payload = payload.split(marker, 1)[1].strip()
            try:
                parsed = json.loads(payload)
            except json.JSONDecodeError:
                parsed = ast.literal_eval(payload)
            if isinstance(parsed, dict):
                preset_maps.update({str(key): value for key, value in parsed.items() if isinstance(value, dict)})
        except (OSError, SyntaxError, ValueError) as exc:
            logger.warning("[Explore] 预制脑图加载失败 (%s): %s", path.name, exc)

    return preset_maps


_PRESET_MAPS = _load_preset_maps()

_JSON_OBJ_RE = re.compile(r"\{[\s\S]*\}")
_JSON_ARRAY_RE = re.compile(r"\[[\s\S]*\]")
_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.IGNORECASE)
_INVALID_JSON_BACKSLASH_RE = re.compile(r"\\(?![\"\\/bfnrtu])")

# 各能力的 LLM 超时（秒）
TIMEOUT_MAP = 25.0
TIMEOUT_EXPAND = 20.0
TIMEOUT_QUIZ = float(os.environ.get("EXPLORE_QUIZ_MODEL_TIMEOUT", "25") or 25)
TIMEOUT_QUIZ_RETRIEVE = float(os.environ.get("EXPLORE_QUIZ_RETRIEVE_TIMEOUT", "2") or 2)
TIMEOUT_GRADE = 25.0
TIMEOUT_DETAIL = 75.0
QUIZ_CONTEXT_CHUNK_TOP_K = int(os.environ.get("EXPLORE_QUIZ_CONTEXT_CHUNK_TOP_K", "4") or 4)
QUIZ_CONTEXT_MAX_CHARS = int(os.environ.get("EXPLORE_QUIZ_CONTEXT_MAX_CHARS", "6000") or 6000)
DEFAULT_LEARNING_FAST_MODEL_NAME = (
    os.environ.get("EXPLORE_FAST_MODEL_NAME")
    or os.environ.get("CUSTOM_LEARNING_FAST_MODEL_NAME")
    or os.environ.get("EXPLORE_EXPAND_MODEL_NAME")
    or "qwen3.6-35b"
).strip() or "qwen3.6-35b"


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def resolve_learning_fast_model_name(model_name: str | None = None) -> str:
    return str(model_name or "").strip() or DEFAULT_LEARNING_FAST_MODEL_NAME


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
            try:
                repaired = _INVALID_JSON_BACKSLASH_RE.sub(r"\\\\", cand)
                return json.loads(repaired)
            except (json.JSONDecodeError, TypeError):
                continue
    return None


async def _retrieve_context(query: str, kb_id: str | None, *, chunk_top_k: int = 8) -> tuple[str, list[dict[str, Any]]]:
    """调用统一检索入口，返回 (context_str, sources)。失败时降级为空。
    
    优化: 探索式学习场景直接传入知识点标题作为外部关键词,跳过LLM关键词提取
    """
    try:
        from backend.rag.retrieval_pipeline import retrieve_context

        filtered_data, context_str, _images, _stats = await retrieve_context(
            query=query,
            kb_id=kb_id,
            chunk_top_k=chunk_top_k,
            external_hl_keywords=[query],  # 优化: 直接传入知识点标题,跳过关键词提取
            external_ll_keywords=[],
            seed_weights={query: 1.0},
            disable_stage1_oversample=True,  # 优化: 关闭过采样,减少召回量
            retrieval_profile_override="lightweight",
        )
        sources = _extract_sources(filtered_data)
        return (context_str or ""), sources
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Explore] retrieve_context 失败 (kb=%s, q=%s): %s", kb_id, query[:40], exc)
        return "", []


async def _retrieve_quiz_context(query: str, kb_id: str | None) -> tuple[str, list[dict[str, Any]], str]:
    """习题生成优先保证响应速度；检索慢或失败时降级为通用命题。"""
    start = time.perf_counter()
    task = asyncio.create_task(_retrieve_context(query, kb_id, chunk_top_k=QUIZ_CONTEXT_CHUNK_TOP_K))

    def _consume_late_result(done: asyncio.Task[tuple[str, list[dict[str, Any]]]]) -> None:
        try:
            done.result()
        except Exception as exc:  # noqa: BLE001
            logger.debug("[Explore] 习题后台检索结束于异常: %s", exc)
        except asyncio.CancelledError:
            logger.debug("[Explore] 习题后台检索被取消")

    try:
        context, sources = await asyncio.wait_for(asyncio.shield(task), timeout=max(1.0, TIMEOUT_QUIZ_RETRIEVE))
        elapsed = (time.perf_counter() - start) * 1000
        if len(context) > QUIZ_CONTEXT_MAX_CHARS:
            context = f"{context[:QUIZ_CONTEXT_MAX_CHARS].rstrip()}\n\n[以上为知识库上下文节选]"
        logger.info(
            "[Explore] 习题检索完成: q=%s, chunks=%d, chars=%d, elapsed=%.1fms",
            query[:40],
            len(sources),
            len(context),
            elapsed,
        )
        return context, sources, "retrieved"
    except asyncio.TimeoutError:
        task.add_done_callback(_consume_late_result)
        logger.info(
            "[Explore] 习题检索超时降级: q=%s, timeout=%.1fs",
            query[:40],
            TIMEOUT_QUIZ_RETRIEVE,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Explore] 习题检索失败降级: q=%s, err=%s", query[:40], exc)
    return "", [], "skipped"


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
    *,
    stream: bool = False,
) -> tuple[Any | Path, str, str, str]:
    """下载知识库文档，返回 (payload_or_path, filename, media_type, source_kind)。"""
    storage_key, filename, source_kind = await _resolve_kb_document_storage(session, kb_id, doc_id)
    media_type = _guess_mime_type(filename)

    if source_kind == "local":
        return Path(storage_key), filename, media_type, source_kind

    from backend.config import settings
    from backend.storage import _get_minio_client, _normalize_minio_object_name

    def _read_minio() -> Any:
        client = _get_minio_client()
        candidates = [storage_key]
        normalized = _normalize_minio_object_name(storage_key)
        if normalized and normalized not in candidates:
            candidates.append(normalized)
        last_error: Exception | None = None
        for candidate in candidates:
            try:
                response = client.get_object(settings.minio_bucket_name, candidate)
                if stream:
                    return response
                try:
                    return response.read()
                finally:
                    try:
                        response.close()
                    finally:
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
        raw_content = _strip_image_blocks(str(chunk.get("content", "") or "")).strip()
        content = _strip_resource_noise(raw_content)
        file_path = str(chunk.get("file_path", chunk.get("source", "")) or "")
        doc_id = str(chunk.get("doc_id", chunk.get("_doc_id", "")) or "")
        full_doc_id = str(chunk.get("full_doc_id", "") or "")
        score = float(chunk.get("_fused_score", chunk.get("_rerank_score", 0.0)) or 0.0)
        key = doc_id or full_doc_id or file_path or content[:32]
        if not key:
            continue
        snippet = _strip_resource_noise(content, max_len=240) or "该资源与当前知识点相关，可作为补充阅读材料。"
        file_name = file_path.rsplit("/", 1)[-1] if file_path else ""
        doc_name = _derive_resource_title(file_name, _title_from_content(content), doc_id)
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
        model = create_chat_model(name=resolve_learning_fast_model_name(model_name), thinking_enabled=False)
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
        content_parts = []
        reasoning_parts = []
        for chunk in chunks:
            c = getattr(chunk, "content", "")
            if c:
                content_parts.append(_normalize_content(c))
            
            r = ""
            if hasattr(chunk, "additional_kwargs") and isinstance(chunk.additional_kwargs, dict):
                r = chunk.additional_kwargs.get("reasoning_content") or chunk.additional_kwargs.get("reasoning") or ""
            if not r and hasattr(chunk, "reasoning_content"):
                r = getattr(chunk, "reasoning_content", "")
            if r:
                reasoning_parts.append(_normalize_content(r))
                
        full_content = "".join(content_parts)
        res = _parse_json(full_content)
        if res is not None:
            return res
            
        full_reasoning = "".join(reasoning_parts)
        res = _parse_json(full_reasoning)
        if res is not None:
            return res
            
        return _parse_json(full_content + "\n" + full_reasoning)
        
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Explore] LLM 调用失败: %s", exc)
        return None


async def _llm_text(
    prompt: str,
    *,
    model_name: str | None,
    timeout: float,
    max_tokens: int = 2048,
    temperature: float = 0.3,
) -> str | None:
    """调用 LLM 并返回 Markdown 文本，带超时与异常兜底。"""
    from langchain_core.messages import HumanMessage

    from backend.core.models import create_chat_model

    try:
        model = create_chat_model(name=resolve_learning_fast_model_name(model_name), thinking_enabled=False)
        model = model.bind(temperature=temperature, max_tokens=max_tokens)

        stream = model.astream([HumanMessage(content=prompt)])
        iterator = stream.__aiter__()

        try:
            first_chunk = await asyncio.wait_for(iterator.__anext__(), timeout=timeout)
        except StopAsyncIteration:
            return None
        except asyncio.TimeoutError:
            logger.info("[Explore] LLM 详解首 token 超时 (timeout=%.1fs)", timeout)
            return None

        chunks = [first_chunk]
        while True:
            try:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=300.0)
                chunks.append(chunk)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                logger.warning("[Explore] LLM 详解读取后续 token 超时保护触发")
                break

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        for chunk in chunks:
            chunk_content = getattr(chunk, "content", "")
            if chunk_content:
                content_parts.append(_normalize_content(chunk_content))

            reasoning = ""
            if hasattr(chunk, "additional_kwargs") and isinstance(chunk.additional_kwargs, dict):
                reasoning = (
                    chunk.additional_kwargs.get("reasoning_content")
                    or chunk.additional_kwargs.get("reasoning")
                    or ""
                )
            if not reasoning and hasattr(chunk, "reasoning_content"):
                reasoning = getattr(chunk, "reasoning_content", "")
            if reasoning:
                reasoning_parts.append(_normalize_content(reasoning))

        full_content = "".join(content_parts).strip()
        if full_content:
            return full_content
        return "".join(reasoning_parts).strip() or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Explore] LLM 文本生成失败: %s", exc)
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


_PLACEHOLDER_TITLES = {
    "子知识点",
    "子节点",
    "知识点",
    "节点",
    "主题名",
    "标题",
    "title",
    "node title",
}
_PLACEHOLDER_SUMMARIES = {
    "一句话说明",
    "一句话概述",
    "一句话引导",
    "简要说明",
    "说明",
    "summary",
}


def _is_placeholder_text(text: str, placeholders: set[str]) -> bool:
    normalized = re.sub(r"[\s:：,，.。;；、\-_/\\|]+", "", str(text or "").strip()).lower()
    return not normalized or normalized in {p.lower() for p in placeholders}


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

    # 首页预制主题直接使用离线脑图，不进入模型调用链路。
    preset_titles = {item["title"] for item in PRESET_TOPICS.get(subject, [])}
    preset = _PRESET_MAPS.get(f"{subject}:{topic}")
    if preset is None and topic in preset_titles:
        preset = _PRESET_MAPS.get(topic)
    if isinstance(preset, dict):
        raw_root = preset.get("root") if isinstance(preset.get("root"), dict) else {}
        children = _normalize_nodes(preset.get("children"), max_children)
        if children:
            logger.info("[Explore] 命中预制脑图: subject=%s, topic=%s", subject, topic)
            return {
                "root": {
                    "id": "n_root",
                    "title": _clean_text(raw_root.get("title"), 40) or topic,
                    "summary": _clean_text(raw_root.get("summary"), 60),
                },
                "children": children,
                "kb_id": kb_id,
                "source": "preset",
            }
    
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
    parsed = await _llm_json(
        prompt,
        model_name=resolve_learning_fast_model_name(model_name),
        timeout=TIMEOUT_EXPAND,
        max_tokens=1024,
        temperature=0.5,
    )

    children: list[dict[str, Any]] = []
    if isinstance(parsed, dict):
        children = _normalize_nodes(parsed.get("children"), max_children)
    elif isinstance(parsed, list):
        children = _normalize_nodes(parsed, max_children)

    source = "llm"
    if not children:
        children = [
            {"id": _gen_id("n"), "title": title, "summary": summary, "expandable": True}
            for title, summary in _fallback_child_nodes(node_title, subject)
        ][:max_children]
        source = "fallback"
    
    result = {"children": children, "source": source}
    
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
    started_at = time.perf_counter()
    context, sources, context_source = await _retrieve_quiz_context(node_title, kb_id)

    from backend.application.explore.prompts import build_quiz_prompt

    prompt = build_quiz_prompt(
        subject=subject, node_title=node_title, types=valid_types, difficulty=difficulty, count=count, context=context
    )
    parsed = await _llm_json(prompt, model_name=model_name, timeout=TIMEOUT_QUIZ, max_tokens=1800, temperature=0.45)

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

    llm_question_count = len(questions)
    source = "llm"
    if len(questions) < count:
        fallback_questions = _fallback_questions(node_title, valid_types, difficulty, count)
        existing_stems = {q.get("stem") for q in questions}
        for q in fallback_questions:
            if q.get("stem") in existing_stems:
                continue
            questions.append(q)
            existing_stems.add(q.get("stem"))
            if len(questions) >= count:
                break
        source = "mixed" if llm_question_count else "fallback"

    logger.info(
        "[Explore] 习题生成完成: node=%s, source=%s, context=%s, count=%d, elapsed=%.1fms",
        node_title,
        source,
        context_source,
        len(questions),
        (time.perf_counter() - started_at) * 1000,
    )
    return {"questions": questions, "source": source}


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


async def generate_detail(
    *,
    node_title: str,
    path: list[str],
    kb_id: str | None,
    subject: str | None = None,
    model_name: str | None = None,
) -> dict[str, Any]:
    """生成知识点详解（非流式，基于知识库检索 + LLM）。"""
    subject = subject or DEFAULT_SUBJECT
    node_title = _clean_text(node_title, 60) or subject
    context, sources = await _retrieve_context(node_title, kb_id, chunk_top_k=10)
    if not context.strip():
        return {"content": "", "sources": sources, "grounded": False}

    from backend.application.explore.prompts import build_detail_prompt

    prompt = build_detail_prompt(
        subject=subject,
        node_title=node_title,
        path=path or [node_title],
        context=context,
    )
    content = await _llm_text(
        prompt,
        model_name=model_name,
        timeout=TIMEOUT_DETAIL,
        max_tokens=2048,
        temperature=0.3,
    )
    content = str(content or "").strip()
    for source in sources:
        source["kb_id"] = kb_id
        source["download_url"] = build_resource_download_url(kb_id, source.get("doc_id"))
    return {
        "content": content,
        "sources": sources[:8],
        "grounded": bool(content and sources),
    }


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


def build_detail_followup_question(
    *,
    node_title: str,
    path: list[str],
    user_question: str,
    detail_content: str = "",
    history: list[dict[str, Any]] | None = None,
) -> str:
    """构造详解输出内容的多轮追问提示。"""
    node_title = _clean_text(node_title, 60)
    path_text = " → ".join([_clean_text(p, 40) for p in (path or []) if str(p).strip()])
    detail = str(detail_content or "").strip()
    if len(detail) > 8000:
        detail = f"{detail[:8000]}\n\n[以上为当前详解内容节选]"
    history_lines: list[str] = []
    for item in (history or [])[-6:]:
        role = "用户" if item.get("role") == "user" else "助手"
        content = _clean_text(item.get("content"), 500)
        if content:
            history_lines.append(f"{role}: {content}")
    history_block = "\n".join(history_lines) if history_lines else "无"
    return (
        "你是探索式学习中的详解内容追问助手。请围绕用户正在阅读的详解内容回答追问，"
        "回答要直接、准确、适合继续学习；不要输出思考过程。\n\n"
        f"当前知识点：{node_title}\n"
        f"学习路径：{path_text or node_title}\n"
        f"当前详解内容：\n{detail or '当前详解内容仍在生成或为空，请结合知识点和知识库上下文回答。'}\n\n"
        f"最近对话：\n{history_block}\n\n"
        f"用户追问：{_clean_text(user_question, 1200)}\n\n"
        "回答要求：\n"
        "1. 先直接回答问题，再给出必要解释。\n"
        "2. 若问题适合延伸，补充 1-3 个可继续探索的方向。\n"
        "3. 如果知识库证据不足，请明确说明，并用通用专业知识谨慎补充。\n"
        "4. 使用 Markdown，避免过长。"
    )


async def stream_detail_followup_answer(
    *,
    node_title: str,
    path: list[str],
    user_question: str,
    detail_content: str = "",
    kb_id: str | None,
    subject: str | None = None,
    history: list[dict[str, Any]] | None = None,
    model_name: str | None = None,
) -> AsyncIterator[tuple[str, Any]]:
    """流式生成围绕当前详解输出内容的多轮追问，yield (event_type, payload)。"""
    from langchain_core.messages import HumanMessage

    from backend.core.models import create_chat_model

    subject = subject or DEFAULT_SUBJECT
    question = _clean_text(user_question, 1200)
    if not question:
        yield ("error", "请输入想追问的问题")
        return

    retrieval_query = f"{node_title} {question}".strip()
    context, sources = await _retrieve_context(retrieval_query, kb_id, chunk_top_k=8)
    for source in sources:
        source["kb_id"] = kb_id
        source["download_url"] = build_resource_download_url(kb_id, source.get("doc_id"))
    if sources:
        yield ("sources", sources[:8])

    prompt = build_detail_followup_question(
        node_title=node_title,
        path=path,
        user_question=question,
        detail_content=detail_content,
        history=history,
    )
    if context.strip():
        prompt = (
            f"{prompt}\n\n"
            f"学科领域：{subject}\n"
            "可参考的知识库片段如下。请优先依据这些片段回答，并在不确定时说明限制。\n"
            f"{context}"
        )
    else:
        prompt = (
            f"{prompt}\n\n"
            f"学科领域：{subject}\n"
            "当前未检索到可用知识库片段，请用通用专业知识谨慎回答。"
        )

    try:
        model = create_chat_model(
            name=resolve_learning_fast_model_name(model_name),
            thinking_enabled=False,
        ).bind(temperature=0.2, max_tokens=1400)
        async for chunk in model.astream([HumanMessage(content=prompt)]):
            content = _normalize_content(getattr(chunk, "content", ""))
            if content:
                yield ("chunk", content)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[Explore] 详解追问失败: %s", exc)
        yield ("error", "追问回答生成失败，请稍后重试")


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
            title = _clean_text(
                item.get("title")
                or item.get("name")
                or item.get("label")
                or item.get("text")
                or item.get("knowledge_point")
                or item.get("point"),
                28,
            )
            summary = _clean_text(
                item.get("summary")
                or item.get("description")
                or item.get("desc")
                or item.get("hint")
                or item.get("explanation"),
                60,
            )
            expandable = item.get("expandable", True)
            if isinstance(expandable, str):
                expandable = expandable.strip().lower() not in {"false", "0", "no", "否", "不可展开"}
            else:
                expandable = bool(expandable)
        else:
            title, summary, expandable = _clean_text(item, 28), "", True
        title = re.sub(r"^\s*(?:[-*]|\d+[.)、]|[A-Z][.)、])\s*", "", title).strip()
        if _is_placeholder_text(title, _PLACEHOLDER_TITLES):
            continue
        if _is_placeholder_text(summary, _PLACEHOLDER_SUMMARIES):
            summary = ""
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
    qtype = str(item.get("type") or item.get("question_type") or item.get("题型") or "").strip().lower()
    qtype = {
        "单选": "choice",
        "单选题": "choice",
        "选择题": "choice",
        "多选": "multi",
        "多选题": "multi",
        "判断": "judge",
        "判断题": "judge",
        "简答": "short",
        "简答题": "short",
        "问答题": "short",
        "计算": "calc",
        "计算题": "calc",
    }.get(qtype, qtype)
    if qtype not in {"choice", "multi", "judge", "short", "calc"}:
        return None
    stem = _clean_text(item.get("stem") or item.get("question") or item.get("title") or item.get("题干"), 400)
    if not stem:
        return None
    difficulty = str(item.get("difficulty") or item.get("难度") or "medium").strip().lower()
    difficulty = {"简单": "easy", "容易": "easy", "中等": "medium", "困难": "hard", "较难": "hard"}.get(
        difficulty,
        difficulty,
    )
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
        if isinstance(raw_options, dict):
            raw_options = [{"key": key, "text": value} for key, value in raw_options.items()]
        for idx, opt in enumerate(raw_options):
            if isinstance(opt, dict):
                key = str(opt.get("key", "")).strip() or chr(ord("A") + idx)
                text = _clean_text(opt.get("text") or opt.get("label") or opt.get("content") or opt.get("value"), 160)
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
        q["reference_answer"] = _clean_text(
            item.get("reference_answer") or item.get("answer") or item.get("参考答案"),
            800,
        )
        points = item.get("scoring_points") or []
        if isinstance(points, list):
            q["scoring_points"] = [_clean_text(p, 80) for p in points if str(p).strip()]
        else:
            q["scoring_points"] = []
    return q


def _fallback_children(topic: str) -> list[str]:
    return [f"{topic}·基本概念", f"{topic}·工作原理", f"{topic}·典型结构", f"{topic}·关键要点", f"{topic}·应用"]


def _fallback_child_nodes(topic: str, subject: str) -> list[tuple[str, str]]:
    topic = _clean_text(topic, 18) or _clean_text(subject, 18) or "知识点"
    if _is_placeholder_text(topic, _PLACEHOLDER_TITLES):
        topic = _clean_text(subject, 18) or "知识点"
    templates = [
        ("基本概念", "明确术语内涵与学习边界"),
        ("工作原理", "理解关键机制与作用过程"),
        ("系统组成", "梳理主要模块及其关系"),
        ("关键参数", "掌握影响性能的核心指标"),
        ("典型应用", "联系实际场景与使用方法"),
        ("常见问题", "辨析易错点与处理思路"),
    ]
    return [(f"{topic}{suffix}", summary) for suffix, summary in templates]


def _fallback_questions(
    node_title: str,
    types: list[str],
    difficulty: str,
    count: int,
) -> list[dict[str, Any]]:
    node = _clean_text(node_title, 40) or "该知识点"
    requested = [t for t in types if t in {"choice", "multi", "judge", "short", "calc"}] or ["choice", "judge", "short"]
    diff = difficulty if difficulty in {"easy", "medium", "hard"} else "medium"
    factories = {
        "choice": lambda: {
            "id": _gen_id("q"),
            "type": "choice",
            "difficulty": diff,
            "stem": f"学习「{node}」时，首先应重点把握哪一类内容？",
            "options": [
                {"key": "A", "text": "概念定义、基本原理与适用条件"},
                {"key": "B", "text": "与主题无关的历史轶事"},
                {"key": "C", "text": "脱离场景的随机参数"},
                {"key": "D", "text": "不需要验证的经验结论"},
            ],
            "answer": "A",
            "explanation": "概念、原理和适用条件是继续深入学习的基础。",
        },
        "multi": lambda: {
            "id": _gen_id("q"),
            "type": "multi",
            "difficulty": diff,
            "stem": f"围绕「{node}」做知识梳理时，哪些维度通常是有价值的？",
            "options": [
                {"key": "A", "text": "核心概念"},
                {"key": "B", "text": "工作机理"},
                {"key": "C", "text": "关键参数或影响因素"},
                {"key": "D", "text": "完全无关的背景信息"},
            ],
            "answer": "ABC",
            "explanation": "知识梳理应优先覆盖概念、机理和关键影响因素。",
        },
        "judge": lambda: {
            "id": _gen_id("q"),
            "type": "judge",
            "difficulty": diff,
            "stem": f"判断：学习「{node}」时，只记住名称而不理解原理也能支持后续应用。",
            "answer": "错误",
            "explanation": "名称记忆不足以支撑分析和应用，仍需理解原理与条件。",
        },
        "short": lambda: {
            "id": _gen_id("q"),
            "type": "short",
            "difficulty": diff,
            "stem": f"请简述「{node}」的学习要点。",
            "reference_answer": "可从基本定义、核心原理、系统组成、关键参数、典型应用和常见问题等方面概括。",
            "scoring_points": ["说明基本定义", "概括核心原理", "列出关键影响因素", "联系典型应用"],
            "explanation": "开放题重点考查是否形成结构化理解。",
        },
        "calc": lambda: {
            "id": _gen_id("q"),
            "type": "calc",
            "difficulty": diff,
            "stem": f"若「{node}」涉及定量分析，请说明建立计算题解题步骤时应包含哪些环节。",
            "reference_answer": "应先明确已知量与待求量，再选择适用公式或模型，代入计算，最后检查单位、量级和适用条件。",
            "scoring_points": ["明确已知量和待求量", "选择适用关系式", "完成计算过程", "检查单位和条件"],
            "explanation": "未掌握具体数值时，可先考查定量分析流程。",
        },
    }
    questions: list[dict[str, Any]] = []
    idx = 0
    while len(questions) < count and idx < count * 2:
        qtype = requested[idx % len(requested)]
        q = factories[qtype]()
        round_no = idx // len(requested)
        if round_no:
            q["stem"] = f"{q['stem']}（拓展{round_no + 1}）"
        questions.append(q)
        idx += 1
    return questions[:count]
