"""Redis cache adapter (production).

Constructed lazily by the composition root only when ``REDIS_URL`` is set, so the
dependency is present in every environment but never required in development.
"""

from __future__ import annotations

from typing import Any

from app.core.logging import get_logger

logger = get_logger("cache.redis")


class RedisCache:
    """Thin, explicit wrapper. No implicit serialisation surprises: values are str."""

    def __init__(self, redis: Any, namespace: str = "natiqo") -> None:
        self._redis = redis
        self._namespace = namespace

    @classmethod
    def from_url(cls, url: str, *, namespace: str = "natiqo") -> RedisCache:
        from redis.asyncio import Redis

        client = Redis.from_url(url, encoding="utf-8", decode_responses=True)
        logger.info("cache.redis_initialised")
        return cls(client, namespace)

    def build_key(self, *parts: str) -> str:
        return ":".join([self._namespace, *(p for p in parts if p)])

    async def get(self, key: str) -> str | None:
        return await self._redis.get(key)

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        if ttl_seconds:
            await self._redis.set(key, value, ex=ttl_seconds)
        else:
            await self._redis.set(key, value)

    async def delete(self, *keys: str) -> int:
        return int(await self._redis.delete(*keys)) if keys else 0

    async def exists(self, key: str) -> bool:
        return bool(await self._redis.exists(key))

    async def increment(self, key: str, *, amount: int = 1, ttl_seconds: int | None = None) -> int:
        pipe = self._redis.pipeline()
        pipe.incrby(key, amount)
        if ttl_seconds:
            pipe.expire(key, ttl_seconds, nx=True)
        results = await pipe.execute()
        return int(results[0])

    async def ping(self) -> bool:
        try:
            return bool(await self._redis.ping())
        except Exception as exc:  # noqa: BLE001 - readiness probe must not raise
            logger.warning("cache.ping_failed", error=str(exc))
            return False

    async def close(self) -> None:
        await self._redis.aclose()
