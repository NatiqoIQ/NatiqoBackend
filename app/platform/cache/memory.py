"""In-process cache adapter.

Used for local development and the test suite. Single-process only — this is
explicitly *not* a production adapter.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass


def _expiry(ttl_seconds: int | None) -> float | None:
    """Absolute expiry for a TTL, or ``None`` when there is none.

    ``is not None`` rather than a truthiness test on purpose: ``ttl_seconds=0`` is falsy but
    unambiguous, meaning "already expired". Collapsing it to "no TTL" pins such an entry in
    the cache forever, which is the worst way for a cache bug to fail.
    """
    return None if ttl_seconds is None else time.monotonic() + ttl_seconds


@dataclass(slots=True)
class _Entry:
    value: str
    expires_at: float | None


class InMemoryCache:
    """TTL cache with atomic counters, safe under asyncio concurrency."""

    def __init__(self, namespace: str = "natiqo") -> None:
        self._namespace = namespace
        self._store: dict[str, _Entry] = {}
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    def build_key(self, *parts: str) -> str:
        return ":".join([self._namespace, *(p for p in parts if p)])

    def _live(self, key: str) -> _Entry | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        if entry.expires_at is not None and entry.expires_at <= time.monotonic():
            self._store.pop(key, None)
            return None
        return entry

    async def get(self, key: str) -> str | None:
        async with self._lock:
            entry = self._live(key)
            if entry is None:
                self.misses += 1
                return None
            self.hits += 1
            return entry.value

    async def set(self, key: str, value: str, *, ttl_seconds: int | None = None) -> None:
        async with self._lock:
            self._store[key] = _Entry(value=value, expires_at=_expiry(ttl_seconds))

    async def delete(self, *keys: str) -> int:
        async with self._lock:
            return sum(1 for key in keys if self._store.pop(key, None) is not None)

    async def exists(self, key: str) -> bool:
        async with self._lock:
            return self._live(key) is not None

    async def increment(self, key: str, *, amount: int = 1, ttl_seconds: int | None = None) -> int:
        async with self._lock:
            entry = self._live(key)
            if entry is None:
                self._store[key] = _Entry(value=str(amount), expires_at=_expiry(ttl_seconds))
                return amount
            new_value = int(entry.value) + amount
            entry.value = str(new_value)
            return new_value

    async def ping(self) -> bool:
        return True

    def clear(self) -> None:
        self._store.clear()
