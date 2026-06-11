import logging
from typing import AsyncIterator

from redis.asyncio import Redis, from_url

from backend.config.settings import settings

logger = logging.getLogger(__name__)

async def get_redis_client() -> AsyncIterator[Redis]:
    """Get async Redis client generator for dependency injection"""
    redis_url = f"redis://:{settings.redis_password}@{settings.redis_host}:{settings.redis_port}/{settings.redis_db}" if settings.redis_password else f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_db}"
    client = from_url(redis_url, decode_responses=True)
    try:
        yield client
    finally:
        await client.aclose()

def get_redis() -> Redis:
    """Get standalone async Redis client"""
    redis_url = f"redis://:{settings.redis_password}@{settings.redis_host}:{settings.redis_port}/{settings.redis_db}" if settings.redis_password else f"redis://{settings.redis_host}:{settings.redis_port}/{settings.redis_db}"
    return from_url(redis_url, decode_responses=True)
