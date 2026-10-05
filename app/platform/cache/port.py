"""Cache port.

The application never imports a concrete cache client. It depends on
:class:`CachePort`; the composition root picks Redis in production and an
in-process implementation for development and tests.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable


@runtime_checkable
class CachePort(Protocol):
    """Async key/value cache with TTL semantics."""

    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None: ...

    async def delete(self, *keys: str) -> int: ...

    async def exists(self, key: str) -> bool: ...

    async def increment(self, key: str, *, amount: int = 1, ttl_seconds: int | None = None) -> int:
        """Atomically add ``amount`` and return the new value. Used for rate limits."""
        ...

    async def ping(self) -> bool: ...

    def build_key(self, *parts: str) -> str:
        """Namespaced key builder so modules cannot collide."""
        ...


class NullCache:
    """No-op cache. Keeps endpoints correct (just uncached) when caching is off."""

    async def get(self, key: str) -> str | None:
        return None

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        return None

    async def delete(self, *keys: str) -> int:
        return 0

    async def exists(self, key: str) -> bool:
        return False

    async def increment(self, key: str, *, amount: int = 1, ttl_seconds: int | None = None) -> int:
        return amount

    async def ping(self) -> bool:
        return True

    def build_key(self, *parts: str) -> str:
        return ":".join(p for p in parts if p)
