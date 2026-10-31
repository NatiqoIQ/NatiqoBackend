"""Coalesce concurrent cache misses so one process rebuilds a hot key once.

A shared cache removes the *steady-state* cost of a hot read, but not the cost of its
expiry: when a popular key lapses, every in-flight request that misses it starts the
same expensive query at the same moment. A short TTL makes that window recur, and the
database sees the spike exactly when the cache was supposed to protect it.

This helper keeps one ``asyncio.Lock`` per key per running event loop. The first caller
builds; the rest wait and then re-check the cache, which the builder has just filled.
It is deliberately *process-local*: a handful of API workers may each build once, and
coordinating that through Redis would add a round-trip and a failure mode to a path
whose entire purpose is to be optional. Per-process coalescing plus a short TTL turns
a thundering herd into at most one query per worker.

Locks are kept per loop in a ``WeakKeyDictionary``: an ``asyncio.Lock`` binds to the
loop that first waits on it, and the test suite creates a fresh loop per test. Keying
by loop keeps a lock from a finished loop out of the next one's way.
"""

from __future__ import annotations

import asyncio
from weakref import WeakKeyDictionary

_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, dict[str, asyncio.Lock]] = WeakKeyDictionary()


def flight_lock(key: str) -> asyncio.Lock:
    """The lock guarding a rebuild of ``key`` on the current event loop."""
    loop = asyncio.get_running_loop()
    keys = _locks.get(loop)
    if keys is None:
        keys = {}
        _locks[loop] = keys
    lock = keys.get(key)
    if lock is None:
        lock = asyncio.Lock()
        keys[key] = lock
    return lock
