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

Locks are kept per loop in a ``WeakKeyDictionary``, and the locks themselves are held
weakly. An ``asyncio.Lock`` binds to the loop that first *waits* on it, so keeping a
strong reference in a loop-keyed registry would make the value reference its own key
and the loop could never be collected — one leaked loop per test is not acceptable.
While any task is inside the lock it holds it strongly; once the last one leaves, the
lock is disposable and the next rebuild gets a fresh one.
"""

from __future__ import annotations

import asyncio
from weakref import WeakKeyDictionary, WeakValueDictionary

_locks: WeakKeyDictionary[asyncio.AbstractEventLoop, WeakValueDictionary[str, asyncio.Lock]] = (
    WeakKeyDictionary()
)


def flight_lock(key: str) -> asyncio.Lock:
    """The lock guarding a rebuild of ``key`` on the current event loop."""
    loop = asyncio.get_running_loop()
    keys = _locks.get(loop)
    if keys is None:
        keys = WeakValueDictionary()
        _locks[loop] = keys
    lock = keys.get(key)
    if lock is None:
        lock = asyncio.Lock()
        keys[key] = lock
    return lock
