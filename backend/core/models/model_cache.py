"""模型配置缓存与热更新逻辑"""

import asyncio
import json
import logging
import os
from typing import Dict, Optional

from backend.db.redis import get_redis

logger = logging.getLogger(__name__)

_DB_CACHE: Dict[str, dict] = {}       # f"{usage_scope}:{name}" → config_dict
_DB_CACHE_TS: float = 0               # 上次刷新时间
_DB_CACHE_TTL: float = 30.0           # 缓存 TTL（秒）

def set_cached_model_config(name: str, config: dict, usage_scope: str = "chat"):
    """存入缓存"""
    global _DB_CACHE_TS
    key = f"{usage_scope}:{name}"
    _DB_CACHE[key] = config
    import time
    _DB_CACHE_TS = time.time()

def get_cached_model_config(name: str | None, usage_scope: str = "chat") -> Optional[dict]:
    """带 TTL 的进程内缓存"""
    global _DB_CACHE_TS
    import time
    now = time.time()
    if _DB_CACHE and (now - _DB_CACHE_TS) < _DB_CACHE_TTL:
        key = f"{usage_scope}:{name or '__default__'}"
        return _DB_CACHE.get(key)
    return None

def invalidate_cache(name: str | None = None, usage_scope: str = "chat"):
    """主动失效（增删改时调用）"""
    global _DB_CACHE, _DB_CACHE_TS
    if name:
        key = f"{usage_scope}:{name}"
        _DB_CACHE.pop(key, None)
    else:
        _DB_CACHE.clear()
    _DB_CACHE_TS = 0

async def notify_model_change(name: str | None = None, usage_scope: str = "chat"):
    """通过 Redis 通知其他 Worker 更新缓存"""
    try:
        r = get_redis()
        try:
            await r.publish(
                "model_config_changed",
                json.dumps({"name": name, "usage_scope": usage_scope, "action": "update"}),
            )
        finally:
            await r.aclose()
    except Exception as e:
        logger.error(f"通知模型配置变更失败: {e}")

async def _subscribe_model_changes_once() -> None:
    """Open one Redis subscription and listen until it disconnects."""
    r = get_redis()
    pubsub = r.pubsub()
    try:
        await pubsub.subscribe("model_config_changed")
        logger.info("已订阅模型配置变更事件 'model_config_changed'")
        async for message in pubsub.listen():
            if message["type"] == "message":
                try:
                    data = json.loads(message["data"])
                except (TypeError, ValueError):
                    logger.warning("忽略格式无效的模型变更事件")
                    continue
                invalidate_cache(data.get("name"), data.get("usage_scope", "chat"))
                logger.info(f"收到模型变更事件，已清理缓存: {data}")
    finally:
        await pubsub.aclose()
        await r.aclose()


async def subscribe_model_changes():
    """Keep the Redis invalidation subscriber alive across transient failures."""
    initial_delay = max(
        0.1,
        float(os.getenv("MODEL_CACHE_RECONNECT_INITIAL_SECONDS", "1") or "1"),
    )
    max_delay = max(
        initial_delay,
        float(os.getenv("MODEL_CACHE_RECONNECT_MAX_SECONDS", "30") or "30"),
    )
    delay = initial_delay
    while True:
        try:
            await _subscribe_model_changes_once()
            delay = initial_delay
            logger.warning("模型配置变更订阅已结束，准备重新连接")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("模型配置变更订阅中断，%.1fs 后重试: %s", delay, exc)
        await asyncio.sleep(delay)
        delay = min(max_delay, delay * 2)
