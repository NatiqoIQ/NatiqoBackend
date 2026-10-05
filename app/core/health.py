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
    version: str = "0.1.0"
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


async def build_health_report(*, deep: bool) -> HealthReport:
    from app.core.config import get_settings

    settings = get_settings()
    if not deep:
        return HealthReport(
            status=HealthStatus.HEALTHY,
            environment=settings.app_env,
            checked_at=utc_now().isoformat(),
        )

    checks = [await database_check()]
    from app.api.deps import get_cache

    cache = get_cache()
    checks.append(await cache_check(cache.ping))

    if any(c.status is HealthStatus.UNHEALTHY for c in checks):
        status = HealthStatus.UNHEALTHY
    elif any(c.name == "cache" for c in checks) and not settings.uses_redis:
        # Running on the in-memory adapter: functional, but not production-grade.
        status = HealthStatus.DEGRADED
    else:
        status = HealthStatus.HEALTHY
    return HealthReport(
        status=status,
        checks=checks,
        environment=settings.app_env,
        checked_at=utc_now().isoformat(),
    )
