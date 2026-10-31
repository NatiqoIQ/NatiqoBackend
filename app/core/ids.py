"""Time-ordered UUIDv7 primary keys (F21).

UUIDv4 is random, so every INSERT lands on a random leaf of the B-tree: the working set
of index pages is the whole index, and a bulk insert dirties pages scattered across it.
UUIDv7 puts a millisecond timestamp in the high bits, so new rows append near the right
edge — the same locality a sequence gives you, without giving up client-side generation
or leaking a count of users through an incrementing id.

The timestamp is still visible (that is the point), but it is not enumerable and carries
no tenant or sequence information.
"""

from __future__ import annotations

import secrets
import threading
import time
import uuid

_lock = threading.Lock()
_last_ms = 0
_last_seq = 0
_MAX_SEQ = 0x0FFF


def uuid7() -> uuid.UUID:
    """Return a monotonic UUIDv7.

    Within one millisecond the 12-bit ``rand_a`` field is used as a counter, so two ids
    generated in the same millisecond still sort in creation order. If the clock moves
    backwards, the last millisecond is kept and the counter advances, so ordering never
    regresses.
    """
    global _last_ms, _last_seq

    with _lock:
        now_ms = time.time_ns() // 1_000_000
        if now_ms > _last_ms:
            _last_ms = now_ms
            _last_seq = secrets.randbits(12)
        else:
            _last_seq += 1
            if _last_seq > _MAX_SEQ:
                _last_ms += 1
                _last_seq = 0
        timestamp = _last_ms
        sequence = _last_seq

    value = (timestamp & 0xFFFF_FFFF_FFFF) << 80
    value |= 0x7 << 76
    value |= sequence << 64
    value |= 0x2 << 62
    value |= secrets.randbits(62)
    return uuid.UUID(int=value)


__all__ = ["uuid7"]
