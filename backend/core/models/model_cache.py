"""模型配置缓存与热更新逻辑"""

import asyncio
import json
import logging
import os
import threading
import time

from backend.db.redis import get_redis

logger = logging.getLogger(__name__)

# key → (expires_at, generation, config)。逐条目过期时间:旧实现用单一全局
# 时间戳,任意一次 set 会为所有旧条目"续命",热点模型抢先回填时冷门条目可
# 远超 TTL 无上界(R4-LM-5)。generation 用于关闭 invalidate/回填竞态:先于
# invalidate 开始的 DB 读取不得用旧行复活缓存。
_DB_CACHE: dict[str, tuple[float, int, dict]] = {}
_DB_CACHE_TTL: float = 30.0  # 缓存 TTL（秒）
_DB_CACHE_GENERATION: int = 0
_DB_CACHE_LOCK = threading.RLock()


def current_cache_generation() -> int:
    """当前失效代数。回填方在发起 DB 读取前捕获,写入时校验。"""
    with _DB_CACHE_LOCK:
        return _DB_CACHE_GENERATION


def set_cached_model_config(
    name: str,
    config: dict,
    usage_scope: str = "chat",
    *,
    generation: int | None = None,
):
    """存入缓存。

    ``generation`` 为回填方在 DB 读取前捕获的失效代数:期间发生过
    invalidate 则丢弃本次写入——否则一个先于失效开始的读取会用旧行回填,
    让已失效的配置复活一个 TTL 周期(R4-LM-5 竞态)。
    """
    key = f"{usage_scope}:{name}"
    with _DB_CACHE_LOCK:
        if generation is not None and generation != _DB_CACHE_GENERATION:
            return
        _DB_CACHE[key] = (
            time.time() + _DB_CACHE_TTL,
            _DB_CACHE_GENERATION,
            config,
        )


def get_cached_model_config(name: str | None, usage_scope: str = "chat") -> dict | None:
    """带逐条目 TTL 的进程内缓存"""
    key = f"{usage_scope}:{name or '__default__'}"
    now = time.time()
    with _DB_CACHE_LOCK:
        entry = _DB_CACHE.get(key)
        if entry is None:
            return None
        expires_at, generation, config = entry
        if now >= expires_at or generation != _DB_CACHE_GENERATION:
            _DB_CACHE.pop(key, None)
            return None
        return config


def invalidate_cache(name: str | None = None, usage_scope: str = "chat"):
    """主动失效（增删改时调用）"""
    global _DB_CACHE_GENERATION
    with _DB_CACHE_LOCK:
        if name:
            key = f"{usage_scope}:{name}"
            _DB_CACHE.pop(key, None)
        else:
            prefix = f"{usage_scope}:"
            for key in [item for item in _DB_CACHE if item.startswith(prefix)]:
                _DB_CACHE.pop(key, None)
        # 代数推进使所有存量条目失效并拒绝先于本次失效开始的回填。配置
        # 增删改是低频管理操作,全量重拉一轮的代价可接受。
        _DB_CACHE_GENERATION += 1
    try:
        from backend.core.agents.config_compat import invalidate_model_catalog_cache

        invalidate_model_catalog_cache()
    except ImportError:
        pass


async def notify_model_change(name: str | None = None, usage_scope: str = "chat"):
    """通过 Redis 通知其他 Worker 更新缓存"""
    try:
        r = get_redis()
        try:
            await r.publish(
                "model_config_changed",
                json.dumps(
                    {"name": name, "usage_scope": usage_scope, "action": "update"}
                ),
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
