"""Prometheus metrics, without a Prometheus client dependency.

The API had log lines and ``/ready`` and nothing else: no request rate, no latency
distribution, no error ratio per route. That is enough to notice an outage after a
learner reports it, and not enough to see one forming. This module keeps the three
signals that actually drive the scaling plan — request count, latency histogram and
in-flight requests — plus database-pool saturation, which is the first resource to run
out under load.

Design constraints, in order:

1. **Bounded cardinality.** Labels are the *route template* (``/content/lessons/{id}``),
   never the concrete path. A raw path would create one series per lesson and take the
   scrape endpoint — and the process — down with it.
2. **No request-path allocation on the hot path.** One dict lookup and one integer
   increment per request; histograms are fixed-size lists.
3. **No third-party package.** ``prometheus_client`` is a fine library, but the
   application already controls its exposition format and a dependency-free renderer is
   one less thing between a process and its metrics when the process is unhealthy.

The registry is process-local by design. Under multiple uvicorn workers each exports
its own series; Prometheus scrapes every pod and sums them, which is exactly right.
"""

from __future__ import annotations

import time
from typing import Final

#: Latency buckets in seconds. The upper edges follow the SLOs in the scaling plan:
#: p95 200 ms on reads, p99 500 ms, and a 10 s edge so a timeout is visible as a
#: timeout rather than folded into ``+Inf``.
_BUCKETS: Final[tuple[float, ...]] = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
)

#: (method, route, status) -> count
_requests: dict[tuple[str, str, str], int] = {}
#: (method, route) -> histogram
_durations: dict[tuple[str, str], _Histogram] = {}
_inflight: int = 0
_started_at: float = time.monotonic()


class _Histogram:
    __slots__ = ("buckets", "count", "total")

    def __init__(self) -> None:
        self.buckets: list[int] = [0] * len(_BUCKETS)
        self.count: int = 0
        self.total: float = 0.0

    def observe(self, seconds: float) -> None:
        self.count += 1
        self.total += seconds
        for index, edge in enumerate(_BUCKETS):
            if seconds <= edge:
                self.buckets[index] += 1


def request_started() -> None:
    global _inflight
    _inflight += 1


def request_finished() -> None:
    global _inflight
    _inflight -= 1


def observe_request(method: str, route: str, status: str, seconds: float) -> None:
    """Record one finished request. Cheap enough for the hot path."""
    key = (method.upper(), route, status)
    _requests[key] = _requests.get(key, 0) + 1
    histogram = _durations.get((key[0], key[1]))
    if histogram is None:
        histogram = _Histogram()
        _durations[(key[0], key[1])] = histogram
    histogram.observe(seconds)


def reset_metrics() -> None:
    """Clear the registry. Tests only: a process never resets its own counters."""
    _requests.clear()
    _durations.clear()
    global _inflight, _started_at
    _inflight = 0
    _started_at = time.monotonic()


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**labels: str) -> str:
    return ",".join(f'{name}="{_escape(value)}"' for name, value in labels.items())


def _pool_lines() -> list[str]:
    """Database-pool gauges, gathered lazily so ``/metrics`` never creates an engine.

    ``pool.checkedout()`` is the number that matters: when it reaches
    ``pool.size() + max_overflow`` every later request waits for the pool timeout and
    then fails. Watching it climb is how pool exhaustion is caught before it pages.
    """
    try:
        from app.core import database

        engine = database._engine
        if engine is None:
            return []
        pool = engine.pool
        lines = [
            "# HELP natiqo_db_pool_checked_out Connections currently checked out.",
            "# TYPE natiqo_db_pool_checked_out gauge",
            f"natiqo_db_pool_checked_out {pool.checkedout()}",
            "# HELP natiqo_db_pool_size Configured pool size.",
            "# TYPE natiqo_db_pool_size gauge",
            f"natiqo_db_pool_size {pool.size()}",
            "# HELP natiqo_db_pool_overflow Current overflow beyond the pool size.",
            "# TYPE natiqo_db_pool_overflow gauge",
            f"natiqo_db_pool_overflow {pool.overflow()}",
        ]
        return lines
    except Exception:  # noqa: BLE001 - a scrape must never fail because a gauge failed
        return []


def render_prometheus() -> str:
    """Render every series in the 0.0.4 text exposition format."""
    lines: list[str] = [
        "# HELP natiqo_requests_in_flight Requests currently being served.",
        "# TYPE natiqo_requests_in_flight gauge",
        f"natiqo_requests_in_flight {max(_inflight, 0)}",
        "# HELP natiqo_process_uptime_seconds Seconds since this process started.",
        "# TYPE natiqo_process_uptime_seconds gauge",
        f"natiqo_process_uptime_seconds {time.monotonic() - _started_at:.3f}",
    ]

    if _requests:
        lines.append("# HELP natiqo_http_requests_total Requests by route and status.")
        lines.append("# TYPE natiqo_http_requests_total counter")
        for (method, route, status), count in sorted(_requests.items()):
            labels = _labels(method=method, route=route, status=status)
            lines.append(f"natiqo_http_requests_total{{{labels}}} {count}")

    if _durations:
        lines.append("# HELP natiqo_http_request_duration_seconds Request duration in seconds.")
        lines.append("# TYPE natiqo_http_request_duration_seconds histogram")
        for (method, route), histogram in sorted(_durations.items()):
            base = _labels(method=method, route=route)
            for index, edge in enumerate(_BUCKETS):
                labels = _labels(method=method, route=route, le=f"{edge:g}")
                lines.append(
                    f"natiqo_http_request_duration_seconds_bucket{{{labels}}} "
                    f"{histogram.buckets[index]}"
                )
            lines.append(
                f'natiqo_http_request_duration_seconds_bucket{{{base},le="+Inf"}} {histogram.count}'
            )
            lines.append(
                f"natiqo_http_request_duration_seconds_sum{{{base}}} {histogram.total:.6f}"
            )
            lines.append(f"natiqo_http_request_duration_seconds_count{{{base}}} {histogram.count}")

    lines.extend(_pool_lines())
    return "\n".join(lines) + "\n"
