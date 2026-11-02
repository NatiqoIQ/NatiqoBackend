"""Prometheus metrics, without a Prometheus client dependency.

The API had log lines and ``/ready`` and nothing else: no request rate, no latency
distribution, no error ratio per route. That is enough to notice an outage after a
learner reports it, and not enough to see one forming. This module keeps the three
signals that actually drive capacity planning — request count, latency histogram and
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

**Multiple workers per pod.** The registry is process-local, and that is right in one
sense: each uvicorn worker is a distinct process with its own counters. It breaks in
another: all of a pod's workers accept connections on the *same listening socket*, so
each HTTP scrape lands on a different worker at random. A scrape therefore reports a
different partial counter every time, and ``rate()`` over
those samples is meaningless — which corrupts the HPA signal and every alert and canary
analysis that reads request rates.

When ``PROMETHEUS_MULTIPROC_DIR`` points at a writable directory, the scrape turns into
a merge: the worker that answers writes *its own* full snapshot into the directory,
reads every *other live* worker's snapshot, sums the counters and renders the result.
The write happens only here, on the scrape path, never per request. Snapshots are
keyed by pid, files of dead pids are pruned on sight, and the answering worker excludes
its own file (its data is fresher in memory). Per-worker gauges that must not be summed
are aggregated explicitly in :func:`_merge`: in-flight is summed, uptime and replica
lag take the max, and pool gauges are summed by role. Nothing enforces that every
worker was scraped recently, so a merged gauge can lag one scrape cycle behind —
counters only grow, which is the base rate correctness that matters.
"""

from __future__ import annotations

import json
import os
import sys
import time
from typing import Any, Final

#: Set to a writable directory (an ``emptyDir`` volume is the k8s answer) to make
#: ``/metrics`` aggregate every worker of the pod. Unset — the default — keeps the
#: registry purely process-local, which is what the single-worker and test paths want.
_MULTIPROC_DIR_ENV: Final = "PROMETHEUS_MULTIPROC_DIR"

#: Latency buckets in seconds. The upper edges follow the SLOs:
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


def _multiproc_dir() -> str | None:
    """Resolve the aggregation directory on every scrape, so tests can toggle it."""
    return os.environ.get(_MULTIPROC_DIR_ENV, "").strip() or None


def _pid_alive(pid: int) -> bool:
    """Whether ``pid`` names a process we could still merge.

    ``os.kill(pid, 0)`` is the POSIX answer. On Windows it is fatal — Python maps an
    arbitrary signal to ``TerminateProcess`` — so existence is probed through
    ``OpenProcess`` instead. Any probe error conservatively reports "alive": a phantom
    merge is truer than a dropped real worker.
    """
    if sys.platform == "win32":
        try:
            import ctypes

            query_limited = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(query_limited, False, pid)
            if not handle:
                return False
            kernel32.CloseHandle(handle)
            return True
        except Exception:  # noqa: BLE001 - an uncertain probe answers "alive"
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True
    return True


def _extra_values() -> dict[str, Any]:
    """Optional operational gauges as *values*, so they can be merged.

    A single-database deployment has no read engine and the AI module may not even be
    imported, and pool APIs differ (``StaticPool`` has no ``size()``). Every failure
    mode simply drops its entry; a scrape must never fail because a gauge failed.
    """
    extras: dict[str, Any] = {"pool": {}, "lag": 0.0, "ai_circuit": 0, "cache": {}}
    try:
        from app.core import database

        for role, engine in (("primary", database._engine), ("replica", database._read_engine)):
            if engine is None:
                continue
            pool = engine.pool
            for metric, method in (
                ("checked_out", "checkedout"),
                ("size", "size"),
                ("overflow", "overflow"),
            ):
                try:
                    value = getattr(pool, method)()
                except Exception:  # noqa: BLE001, S112 - StaticPool has no size
                    continue
                extras["pool"][f"{role}|{metric}"] = value
    except Exception:  # noqa: BLE001, S110 - a scrape must never fail because a gauge failed
        pass
    try:
        from app.platform.ai.service import circuit_open

        extras["lag"] = float(getattr(database, "_lag_seconds", 0.0))
        extras["ai_circuit"] = 1 if circuit_open() else 0
    except Exception:  # noqa: BLE001, S110 - the AI module may not be loaded
        pass
    try:
        from app.platform.cache import stats as cache_stats

        counters = cache_stats.snapshot()
        if counters:
            extras["cache"] = {
                f"{area}|{result}": count for (area, result), count in counters.items()
            }
    except Exception:  # noqa: BLE001, S110 - a scrape must never fail because a gauge failed
        pass
    return extras


def _local_snapshot() -> dict[str, Any]:
    """This process's full metric state, JSON-safe, encoded with flat ``|`` keys."""
    extras = _extra_values()
    return {
        "pid": os.getpid(),
        "requests": {f"{m}|{r}|{s}": c for (m, r, s), c in _requests.items()},
        "durations": {
            f"{m}|{r}": {"buckets": h.buckets, "count": h.count, "total": h.total}
            for (m, r), h in _durations.items()
        },
        "inflight": max(_inflight, 0),
        "uptime": time.monotonic() - _started_at,
        "pool": extras["pool"],
        "lag": extras["lag"],
        "ai_circuit": extras["ai_circuit"],
        "cache": extras["cache"],
    }


def _merge(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    """Sum the cumulative series and aggregate the gauges across worker snapshots."""
    requests: dict[str, int] = {}
    durations: dict[str, dict[str, Any]] = {}
    pool: dict[str, float] = {}
    cache: dict[str, int] = {}
    inflight = 0
    uptime = 0.0
    lag = 0.0
    ai_circuit = 0

    for snapshot in snapshots:
        for key, count in snapshot.get("requests", {}).items():
            requests[key] = requests.get(key, 0) + int(count)
        for key, histogram in snapshot.get("durations", {}).items():
            merged = durations.get(key)
            if merged is None:
                durations[key] = {
                    "buckets": [int(b) for b in histogram.get("buckets", [])],
                    "count": int(histogram.get("count", 0)),
                    "total": float(histogram.get("total", 0.0)),
                }
                continue
            merged["count"] += int(histogram.get("count", 0))
            merged["total"] += float(histogram.get("total", 0.0))
            buckets = merged["buckets"]
            for index, value in enumerate(histogram.get("buckets", [])):
                if index < len(buckets):
                    buckets[index] += int(value)
        for key, value in snapshot.get("pool", {}).items():
            pool[key] = pool.get(key, 0) + float(value)
        for key, count in snapshot.get("cache", {}).items():
            cache[key] = cache.get(key, 0) + int(count)
        inflight += int(snapshot.get("inflight", 0))
        uptime = max(uptime, float(snapshot.get("uptime", 0.0)))
        lag = max(lag, float(snapshot.get("lag", 0.0)))
        ai_circuit = max(ai_circuit, int(snapshot.get("ai_circuit", 0)))

    return {
        "pid": 0,
        "requests": requests,
        "durations": durations,
        "inflight": inflight,
        "uptime": uptime,
        "pool": pool,
        "lag": lag,
        "ai_circuit": ai_circuit,
        "cache": cache,
    }


def _write_snapshot(directory: str, snapshot: dict[str, Any]) -> None:
    """Persist one pid's snapshot atomically. Best-effort: a lost snapshot is a stale
    merge, not a failed scrape, and only this pid ever writes its own file."""
    target = os.path.join(directory, f"{snapshot['pid']}.json")
    tmp = f"{target}.tmp"
    try:
        os.makedirs(directory, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle)
        os.replace(tmp, target)
    except OSError:
        return


def _read_snapshots(directory: str) -> list[dict[str, Any]]:
    """Every live worker's snapshot except our own: our file can be one stale write
    behind, while ``_local_snapshot()`` is current."""
    snapshots: list[dict[str, Any]] = []
    try:
        names = os.listdir(directory)
    except OSError:
        return snapshots
    for name in names:
        if not name.endswith(".json"):
            continue
        try:
            pid = int(os.path.splitext(name)[0])
        except ValueError:
            continue
        if pid == os.getpid():
            continue
        if not _pid_alive(pid):
            try:
                os.unlink(os.path.join(directory, name))
            except OSError:
                pass
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                payload = json.load(handle)
            if isinstance(payload, dict):
                snapshots.append(payload)
        except (OSError, ValueError):
            continue
    return snapshots


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _labels(**labels: str) -> str:
    return ",".join(f'{name}="{_escape(value)}"' for name, value in labels.items())


def _render(snapshot: dict[str, Any]) -> str:
    """Render one (possibly merged) snapshot in the 0.0.4 text exposition format."""
    lines: list[str] = [
        "# HELP natiqo_replica_lag_seconds Replica replay delay, from the cached probe.",
        "# TYPE natiqo_replica_lag_seconds gauge",
        "# HELP natiqo_ai_circuit_open 1 when the AI provider breaker is open.",
        "# TYPE natiqo_ai_circuit_open gauge",
        "# HELP natiqo_db_pool_checked_out Connections currently checked out, per role.",
        "# TYPE natiqo_db_pool_checked_out gauge",
        "# HELP natiqo_db_pool_size Configured pool size, per role.",
        "# TYPE natiqo_db_pool_size gauge",
        "# HELP natiqo_db_pool_overflow Current overflow beyond the pool size, per role.",
        "# TYPE natiqo_db_pool_overflow gauge",
        "# HELP natiqo_requests_in_flight Requests currently being served.",
        "# TYPE natiqo_requests_in_flight gauge",
        f"natiqo_requests_in_flight {max(snapshot.get('inflight', 0), 0)}",
        "# HELP natiqo_process_uptime_seconds Seconds since this process started.",
        "# TYPE natiqo_process_uptime_seconds gauge",
        f"natiqo_process_uptime_seconds {float(snapshot.get('uptime', 0.0)):.3f}",
    ]

    requests = snapshot.get("requests") or {}
    if requests:
        lines.append("# HELP natiqo_http_requests_total Requests by route and status.")
        lines.append("# TYPE natiqo_http_requests_total counter")
        for key in sorted(requests):
            method, route, status = key.split("|", 2)
            labels = _labels(method=method, route=route, status=status)
            lines.append(f"natiqo_http_requests_total{{{labels}}} {requests[key]}")

    durations = snapshot.get("durations") or {}
    if durations:
        lines.append("# HELP natiqo_http_request_duration_seconds Request duration in seconds.")
        lines.append("# TYPE natiqo_http_request_duration_seconds histogram")
        for key in sorted(durations):
            method, route = key.split("|", 1)
            histogram = durations[key]
            base = _labels(method=method, route=route)
            for index, edge in enumerate(_BUCKETS):
                labels = _labels(method=method, route=route, le=f"{edge:g}")
                buckets = histogram.get("buckets") or []
                count = buckets[index] if index < len(buckets) else 0
                lines.append(f"natiqo_http_request_duration_seconds_bucket{{{labels}}} {count}")
            lines.append(
                f'natiqo_http_request_duration_seconds_bucket{{{base},le="+Inf"}}'
                f" {histogram.get('count', 0)}"
            )
            lines.append(
                f"natiqo_http_request_duration_seconds_sum{{{base}}}"
                f" {float(histogram.get('total', 0.0)):.6f}"
            )
            lines.append(
                f"natiqo_http_request_duration_seconds_count{{{base}}} {histogram.get('count', 0)}"
            )

    pool = snapshot.get("pool") or {}
    for key in sorted(pool):
        role, metric = key.split("|", 1)
        labels = _labels(role=role)
        lines.append(f"natiqo_db_pool_{metric}{{{labels}}} {int(float(pool[key]))}")
    lines.append(f"natiqo_replica_lag_seconds {float(snapshot.get('lag', 0.0)):.3f}")
    lines.append(f"natiqo_ai_circuit_open {int(snapshot.get('ai_circuit', 0))}")

    cache = snapshot.get("cache") or {}
    if cache:
        lines.append("# HELP natiqo_cache_requests_total Cache reads by area and result.")
        lines.append("# TYPE natiqo_cache_requests_total counter")
        for key in sorted(cache):
            area, result = key.split("|", 1)
            labels = _labels(area=area, result=result)
            lines.append(f"natiqo_cache_requests_total{{{labels}}} {cache[key]}")

    return "\n".join(lines) + "\n"


def render_prometheus() -> str:
    """Render the scrape. With ``PROMETHEUS_MULTIPROC_DIR`` set, one worker's write +
    merge of every *live* worker's snapshot replaces a random worker's private view."""
    directory = _multiproc_dir()
    if directory is None:
        return _render(_local_snapshot())
    local = _local_snapshot()
    _write_snapshot(directory, local)
    return _render(_merge([local, *_read_snapshots(directory)]))
