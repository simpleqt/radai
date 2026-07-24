"""Cross-process invalidation for the local Skill provider cache."""

from __future__ import annotations

import asyncio
import json
import logging
import os

from backend.core.skills.scoped_skills_provider import invalidate_all_skill_caches
from backend.db.redis import get_redis

logger = logging.getLogger(__name__)


async def _subscribe_skill_changes_once() -> None:
    client = get_redis()
    pubsub = client.pubsub()
    try:
        await pubsub.subscribe("skill:catalog:changed")
        logger.info("已订阅 Skill 目录变更事件 'skill:catalog:changed'")
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                payload = json.loads(message.get("data") or "{}")
            except (TypeError, ValueError):
                logger.warning("忽略格式无效的 Skill 变更事件")
                continue
            invalidate_all_skill_caches()
            logger.info("收到 Skill 变更事件，已清理本地缓存: %s", payload)
    finally:
        await pubsub.aclose()
        await client.aclose()


async def subscribe_skill_changes() -> None:
    initial_delay = max(
        0.1,
        float(os.getenv("SKILL_CACHE_RECONNECT_INITIAL_SECONDS", "1") or "1"),
    )
    max_delay = max(
        initial_delay,
        float(os.getenv("SKILL_CACHE_RECONNECT_MAX_SECONDS", "30") or "30"),
    )
    delay = initial_delay
    while True:
        try:
            await _subscribe_skill_changes_once()
            delay = initial_delay
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Skill 变更订阅中断，%.1fs 后重试: %s", delay, exc)
        await asyncio.sleep(delay)
        delay = min(max_delay, delay * 2)
