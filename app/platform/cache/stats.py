"""Process-local hit/miss counters for the shared cache.

The plan's Phase-1 exit criterion is a cache hit ratio above 90%, and until now nothing
counted it: the in-memory adapter had test-only counters and the Redis adapter had none.
This is the smallest registry that makes the criterion measurable on ``/metrics``.

Labels are deliberately *areas* (the first key segment after the namespace), not full
keys. ``catalogue``, ``progress``, ``ratelimit`` and ``idempotency`` are a closed set;
using keys would create a series per learner and take the scrape endpoint down — the same
cardinality rule the HTTP metrics follow.
"""

from __future__ import annotations

from typing import Final

_NAMESPACE_PREFIX: Final = "natiqo:"
_UNKNOWN: Final = "other"

#: (area, result) -> count. Two strings and an int per cache read, no allocation of a
#: label set; this runs on every cached request.
_counters: dict[tuple[str, str], int] = {}


def area_of(key: str) -> str:
    """The bounded label for a key, e.g. ``natiqo:catalogue:tree:v1`` → ``catalogue``."""
    if key.startswith(_NAMESPACE_PREFIX):
        remainder = key[len(_NAMESPACE_PREFIX) :]
    else:
        remainder = key
    area = remainder.split(":", 1)[0]
    return area or _UNKNOWN


def record(key: str, *, hit: bool) -> None:
    """Count one cache read. Called by every adapter, including failures-as-misses."""
    counter_key = (area_of(key), "hit" if hit else "miss")
    _counters[counter_key] = _counters.get(counter_key, 0) + 1


def snapshot() -> dict[tuple[str, str], int]:
    """A copy for rendering; the caller must not hold the live dict."""
    return dict(_counters)


def reset() -> None:
    """Tests only: the process registry is never reset in production."""
    _counters.clear()
