"""Liveness and readiness endpoints.

Liveness (``/health``) is dependency-free by design: if it checked the database, a
brief database blip would make Kubernetes restart every healthy pod, turning a
degraded outage into a crash loop. Readiness (``/ready``) is the one that checks
dependencies, because that is what should remove an instance from the load balancer.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Response, status

from app.core.config import get_settings
from app.core.health import build_health_report
from app.core.logging import get_logger
from app.core.metrics import render_prometheus

logger = get_logger("health")

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


@router.get("/metrics", include_in_schema=False)
async def metrics() -> Response:
    """Prometheus exposition.

    Deliberately unversioned, like the probes: a scraper should not have to follow the
    API prefix. It is *not* a public endpoint in production — the ingress must restrict
    it to the monitoring network, because route names and traffic volumes are
    reconnaissance.
    """
    settings = get_settings()
    if not settings.metrics_enabled:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)

    body = render_prometheus()

    #: The queue depth is the one gauge that needs I/O. It is appended rather than
    #: rendered inside ``render_prometheus`` so a hung Redis cannot stall the scrape:
    #: a failure here simply omits the series for this scrape.
    try:
        from app.api.deps import get_queue

        depth = await get_queue().size()
        body += "# HELP natiqo_queue_depth Jobs waiting in the queue.\n"
        body += f"# TYPE natiqo_queue_depth gauge\nnatiqo_queue_depth {depth}\n"
    except Exception as exc:  # noqa: BLE001 - a scrape reports what it can
        logger.warning("metrics.queue_depth_unavailable", error=str(exc)[:200])

    return Response(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )
