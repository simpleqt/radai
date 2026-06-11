"""模型配置缓存与热更新逻辑"""

import json
import logging
from typing import Dict, Optional

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

def _redis_url() -> str:
    from backend.config.settings import settings as s
    if getattr(s, 'redis_password', None):
        return f"redis://:{s.redis_password}@{s.redis_host}:{s.redis_port}/{s.redis_db}"
    return f"redis://{s.redis_host}:{s.redis_port}/{s.redis_db}"

async def notify_model_change(name: str | None = None, usage_scope: str = "chat"):
    """通过 Redis 通知其他 Worker 更新缓存"""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(_redis_url(), decode_responses=True)
        try:
            await r.publish(
                "model_config_changed",
                json.dumps({"name": name, "usage_scope": usage_scope, "action": "update"}),
            )
        finally:
            await r.aclose()
    except Exception as e:
        logger.error(f"通知模型配置变更失败: {e}")

async def subscribe_model_changes():
    """订阅模型变更消息（需在 app lifespan 中启动）"""
    try:
        import redis.asyncio as aioredis
        r = aioredis.from_url(_redis_url(), decode_responses=True)
        pubsub = r.pubsub()
        await pubsub.subscribe("model_config_changed")
        logger.info("已订阅模型配置变更事件 'model_config_changed'")
        async for message in pubsub.listen():
            if message["type"] == "message":
                data = json.loads(message["data"])
                invalidate_cache(data.get("name"), data.get("usage_scope", "chat"))
                logger.info(f"收到模型变更事件，已清理缓存: {data}")
    except Exception as e:
        logger.error(f"订阅模型配置变更失败: {e}")
