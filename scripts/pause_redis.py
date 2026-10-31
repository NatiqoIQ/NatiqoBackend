"""Pause every Redis client for a bounded window — a chaos drill, not a tool for prod.

``CLIENT PAUSE`` is the cheapest honest simulation of a Redis that accepts connections
and stops answering: a dead server gives an immediate connection error, while a *hung*
one is what actually tests the cache timeout and the database fallback. The command is
server-wide and deliberately time-limited; Redis resumes by itself even if this process
is killed.

Usage:
    REDIS_URL=redis://127.0.0.1:6379/0 python -m scripts.pause_redis 60

Never run this against production. It blocks the queue, the cache and every other
client of that Redis for the whole window.
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "seconds",
        type=int,
        help="How long to pause Redis clients (1-300). The pause ends automatically.",
    )
    args = parser.parse_args()
    if not 1 <= args.seconds <= 300:
        parser.error("seconds must be between 1 and 300")

    url = os.environ.get("REDIS_URL")
    if not url:
        print("REDIS_URL is not set; refusing to guess a server to pause", file=sys.stderr)
        return 2

    import redis

    # The reply may not arrive until the pause that this very command starts has ended,
    # so the socket timeout must outlive the window. A failure here does not necessarily
    # mean the pause did not start.
    client = redis.Redis.from_url(
        url,
        socket_timeout=args.seconds + 10,
        socket_connect_timeout=2,
    )
    try:
        client.execute_command("CLIENT", "PAUSE", args.seconds * 1000, "ALL")
    except Exception as exc:  # noqa: BLE001 - the operator needs the reason, not a trace
        print(f"CLIENT PAUSE failed: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    print(f"Redis paused for {args.seconds}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
