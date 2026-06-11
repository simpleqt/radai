"""
知识库检索 Profile：按 KB 类型选择 Stage-1 路径与 Stage-2 过滤策略。

基于 HotpotQA / 垂直领域消融结论：
- general_qa：关 Path-A、跳过 Stage-2（RAW），适合百科多跳 QA
- domain：保留 Path-A，MMR_ONLY（跳过 PPR），适合医疗/核电子等垂直库
- lightweight：轻量搜索，无过采样、MMR_ONLY
- default：完整流水线（向后兼容）
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, replace
from functools import lru_cache
from typing import Dict, Optional

from backend.config.loggers import get_logger
from backend.rag.content_filter.models import FilterMode
from backend.rag.retriever.config import DirectRetrieverConfig, direct_retriever_config_from_env

logger = get_logger(__name__)

# kb_code 前缀 → profile 名（小写匹配）
_KB_CODE_PREFIX_PROFILES: tuple[tuple[str, str], ...] = (
    ("hotpotqa_fullwiki", "general_qa"),
    ("hotpotqa", "general_qa"),
    ("rag_nuclear_electronics", "domain"),
    ("nuclearelectronicspaperrepository", "domain"),
)

# 已知 kb_id → profile（避免子线程无法查 DB 时丢失策略）
# 通过环境变量 KB_PROFILE_MAP 配置，格式: "kb_id1=profile1,kb_id2=profile2"
_KNOWN_KB_PROFILES: dict[str, str] = {
    **dict(
        item.split("=", 1)
        for item in os.getenv("KB_PROFILE_MAP", "").split(",")
        if "=" in item
    ),
}


@dataclass(frozen=True)
class RetrievalProfile:
    name: str
    content_filter_mode: FilterMode = FilterMode.FULL
    enable_path_a: bool = True
    enable_path_b: bool = True
    enable_path_c: bool = True
    enable_path_d: bool = True
    disable_stage1_oversample: bool = False


RETRIEVAL_PROFILES: Dict[str, RetrievalProfile] = {
    "default": RetrievalProfile(
        name="default",
        content_filter_mode=FilterMode.FULL,
    ),
    "general_qa": RetrievalProfile(
        name="general_qa",
        content_filter_mode=FilterMode.RAW,
        enable_path_a=False,
        disable_stage1_oversample=True,
    ),
    "domain": RetrievalProfile(
        name="domain",
        content_filter_mode=FilterMode.MMR_ONLY,
    ),
    "lightweight": RetrievalProfile(
        name="lightweight",
        content_filter_mode=FilterMode.MMR_ONLY,
        disable_stage1_oversample=True,
    ),
}


def _profile_from_env() -> Optional[str]:
    raw = os.getenv("RAG_RETRIEVAL_PROFILE", "").strip().lower()
    return raw or None


@lru_cache(maxsize=1)
def _kb_profile_map_from_settings() -> dict[str, str]:
    try:
        from backend.config.settings import settings

        raw = getattr(settings, "rag_kb_retrieval_profile_map", "{}") or "{}"
        data = json.loads(raw) if isinstance(raw, str) else raw
        if isinstance(data, dict):
            return {str(k): str(v).lower() for k, v in data.items()}
    except Exception as e:
        logger.warning("[RetrievalProfile] 解析 rag_kb_retrieval_profile_map 失败: %s", e)
    return {}


def _default_profile_name() -> str:
    try:
        from backend.config.settings import settings

        name = getattr(settings, "rag_default_retrieval_profile", "default") or "default"
        return str(name).strip().lower()
    except Exception:
        return "default"


def _resolve_profile_name(kb_id: str | None, kb_code: str | None) -> str:
    if forced := _profile_from_env():
        return forced if forced in RETRIEVAL_PROFILES else "default"

    if kb_id:
        if mapped := _kb_profile_map_from_settings().get(kb_id):
            if mapped in RETRIEVAL_PROFILES:
                return mapped
        if mapped := _KNOWN_KB_PROFILES.get(kb_id):
            if mapped in RETRIEVAL_PROFILES:
                return mapped

    code = (kb_code or "").strip().lower()
    for prefix, profile_name in _KB_CODE_PREFIX_PROFILES:
        if code.startswith(prefix) and profile_name in RETRIEVAL_PROFILES:
            return profile_name

    default_name = _default_profile_name()
    return default_name if default_name in RETRIEVAL_PROFILES else "default"


def resolve_retrieval_profile(
    kb_id: str | None = None,
    kb_code: str | None = None,
    *,
    override: str | None = None,
) -> RetrievalProfile:
    """解析 KB 适用的检索 Profile。"""
    if override:
        key = override.strip().lower()
        if key in RETRIEVAL_PROFILES:
            return RETRIEVAL_PROFILES[key]

    name = _resolve_profile_name(kb_id, kb_code)
    profile = RETRIEVAL_PROFILES[name]
    logger.debug(
        "[RetrievalProfile] kb_id=%s kb_code=%s -> %s (filter=%s, path_a=%s)",
        kb_id,
        kb_code,
        profile.name,
        profile.content_filter_mode.value,
        profile.enable_path_a,
    )
    return profile


def build_direct_retriever_config_for_profile(
    profile: RetrievalProfile,
) -> DirectRetrieverConfig:
    """将 Profile 应用到 DirectRetrieverConfig（基于环境变量默认值）。"""
    base = direct_retriever_config_from_env()
    return replace(
        base,
        enable_path_a=profile.enable_path_a,
        enable_path_b=profile.enable_path_b,
        enable_path_c=profile.enable_path_c,
        enable_path_d=profile.enable_path_d,
    )
