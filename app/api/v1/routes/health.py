"""Liveness and readiness endpoints.

Liveness (``/health``) is dependency-free by design: if it checked the database, a
brief database blip would make Kubernetes restart every healthy pod, turning a
degraded outage into a crash loop. Readiness (``/ready``) is the one that checks
dependencies, because that is what should remove an instance from the load balancer.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.core.health import build_health_report

router = APIRouter(tags=["health"])


@router.get("/health", summary="Liveness probe")
async def health() -> dict[str, object]:
    """Process is alive. Never touches a dependency, so it cannot cascade failures."""
    report = await build_health_report(deep=False)
    return {
        "data": {"status": str(report.status), "version": report.version},
        "meta": {},
    }


@router.get("/ready", summary="Readiness probe")
async def ready(response: Response) -> dict[str, object]:
    """Instance can serve traffic. Verifies the database and cache."""
    report = await build_health_report(deep=True)
    if report.http_status != 200:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"data": report.to_dict(), "meta": {}}
