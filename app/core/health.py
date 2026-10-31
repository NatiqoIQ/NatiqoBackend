"""Health and readiness probes.

Liveness answers "is the process alive?" and must never touch a dependency.
Readiness answers "can this instance serve traffic?" and therefore *does* check the
database and cache — a pod that cannot reach PostgreSQL must be pulled from the load
balancer.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

from app.core.database import check_database
from app.core.logging import get_logger
from app.core.time import utc_now

logger = get_logger("health")


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"


@dataclass(frozen=True, slots=True)
class CheckResult:
    name: str
    status: HealthStatus
    latency_ms: float
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    status: HealthStatus
    checks: list[CheckResult] = field(default_factory=list)
    version: str = "0.1.1"
    environment: str = "development"
    checked_at: str = ""

    @property
    def http_status(self) -> int:
        return 200 if self.status in (HealthStatus.HEALTHY, HealthStatus.DEGRADED) else 503

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "version": self.version,
            "environment": self.environment,
            "checked_at": self.checked_at or utc_now().isoformat(),
            "checks": [
                {
                    "name": c.name,
                    "status": str(c.status),
                    "latency_ms": round(c.latency_ms, 2),
                    **({"detail": c.detail} if c.detail else {}),
                }
                for c in self.checks
            ],
        }


@runtime_checkable
class Probe(Protocol):
    async def __call__(self) -> CheckResult: ...


async def database_check() -> CheckResult:
    started = time.perf_counter()
    ok = await check_database()
    return CheckResult(
        name="database",
        status=HealthStatus.HEALTHY if ok else HealthStatus.UNHEALTHY,
        latency_ms=(time.perf_counter() - started) * 1000,
        detail=None if ok else "database unreachable",
    )


async def cache_check(ping: Any) -> CheckResult:
    started = time.perf_counter()
    try:
        ok = await ping()
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            name="cache", status=HealthStatus.UNHEALTHY, latency_ms=0.0, detail=str(exc)[:200]
        )
    return CheckResult(
        name="cache",
        status=HealthStatus.HEALTHY if ok else HealthStatus.UNHEALTHY,
        latency_ms=(time.perf_counter() - started) * 1000,
    )


async def queue_check(ping: Any) -> CheckResult:
    """Whether the job queue answers.

    A queue that is down does not stop the API from serving a lesson, an answer or a
    progress read — all of those are synchronous. What stops is background work: the
    nightly cleanup, and later notification delivery. So this check degrades the report
    rather than marking the instance unhealthy: pulling the only API out of the load
    balancer because Redis blinked would turn a stalled cron into a total outage.
    """
    started = time.perf_counter()
    try:
        ok = await ping()
    except Exception as exc:  # noqa: BLE001 — a probe reports, never raises
        return CheckResult(
            name="queue", status=HealthStatus.UNHEALTHY, latency_ms=0.0, detail=str(exc)[:200]
        )
    return CheckResult(
        name="queue",
        status=HealthStatus.HEALTHY if ok else HealthStatus.UNHEALTHY,
        latency_ms=(time.perf_counter() - started) * 1000,
    )


def overall_status(checks: list[CheckResult], *, uses_redis: bool) -> HealthStatus:
    """Reduce dependency checks to one verdict.

    PostgreSQL is the *only* dependency that can fail the request path: the cache is
    fail-open (see ``deps.rate_limit``), idempotency degrades to a normal execution,
    and the queue is asynchronous. Reporting a Redis blip as UNHEALTHY would pull every
    instance out of the load balancer and turn a degraded background worker into a full
    outage — the opposite of what the readiness probe is for. Only a dead database is
    fatal.
    """
    unhealthy = [check for check in checks if check.status is HealthStatus.UNHEALTHY]
    if any(check.name == "database" for check in unhealthy):
        return HealthStatus.UNHEALTHY
    if unhealthy:
        return HealthStatus.DEGRADED
    if not uses_redis:
        # Running on the in-memory adapter: functional, but not production-grade.
        return HealthStatus.DEGRADED
    return HealthStatus.HEALTHY


#: The last deep report and when it was taken, in ``time.monotonic`` seconds. A probe is
#: liveness/readiness signalling, not a monitoring query: every orchestrator replica polls
#: every few seconds, and each poll otherwise opens a database connection to answer a
#: question whose answer cannot have changed that fast.
_ready_cache: tuple[float, HealthReport] | None = None


def reset_health_cache() -> None:
    """Drop the cached readiness report. Tests and a forced probe use this."""
    global _ready_cache
    _ready_cache = None


async def build_health_report(*, deep: bool, force: bool = False) -> HealthReport:
    from app.core.config import get_settings

    global _ready_cache
    settings = get_settings()
    if not deep:
        return HealthReport(
            status=HealthStatus.HEALTHY,
            environment=settings.app_env,
            checked_at=utc_now().isoformat(),
        )

    ttl = settings.ready_cache_ttl_seconds
    now = time.monotonic()
    if not force and _ready_cache is not None and now - _ready_cache[0] < ttl:
        return _ready_cache[1]

    checks = [await database_check()]
    from app.api.deps import get_cache, get_queue

    cache = get_cache()
    checks.append(await cache_check(cache.ping))
    checks.append(await queue_check(get_queue().ping))

    status = overall_status(checks, uses_redis=settings.uses_redis)
    report = HealthReport(
        status=status,
        checks=checks,
        environment=settings.app_env,
        checked_at=utc_now().isoformat(),
    )
    _ready_cache = (now, report)
    return report
