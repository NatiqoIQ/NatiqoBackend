"""The readiness verdict: PostgreSQL is fatal, everything else degrades.

This is the rule that decides whether a load balancer removes an instance. Getting it
wrong in the generous direction keeps a broken instance in rotation; getting it wrong in
the strict direction turns a Redis hiccup into a full outage, because every replica is
removed at once. The tests pin both directions.
"""

from __future__ import annotations

from app.core.health import CheckResult, HealthStatus, overall_status


def _check(name: str, status: HealthStatus) -> CheckResult:
    return CheckResult(name=name, status=status, latency_ms=1.0)


def _healthy_dependencies() -> list[CheckResult]:
    return [
        _check("database", HealthStatus.HEALTHY),
        _check("cache", HealthStatus.HEALTHY),
        _check("queue", HealthStatus.HEALTHY),
    ]


def test_everything_up_is_healthy() -> None:
    assert overall_status(_healthy_dependencies(), uses_redis=True) is HealthStatus.HEALTHY


def test_a_dead_database_is_unhealthy() -> None:
    checks = _healthy_dependencies()
    checks[0] = _check("database", HealthStatus.UNHEALTHY)

    assert overall_status(checks, uses_redis=True) is HealthStatus.UNHEALTHY


def test_a_dead_cache_only_degrades() -> None:
    """The cache is fail-open: removing the instance would make the outage worse."""
    checks = _healthy_dependencies()
    checks[1] = _check("cache", HealthStatus.UNHEALTHY)

    assert overall_status(checks, uses_redis=True) is HealthStatus.DEGRADED


def test_a_dead_queue_only_degrades() -> None:
    """Background work is stalled; lessons and answers still serve."""
    checks = _healthy_dependencies()
    checks[2] = _check("queue", HealthStatus.UNHEALTHY)

    assert overall_status(checks, uses_redis=True) is HealthStatus.DEGRADED


def test_a_dead_database_outranks_a_dead_cache() -> None:
    checks = [
        _check("database", HealthStatus.UNHEALTHY),
        _check("cache", HealthStatus.UNHEALTHY),
    ]

    assert overall_status(checks, uses_redis=True) is HealthStatus.UNHEALTHY


def test_the_in_memory_adapter_is_degraded_even_when_every_check_passes() -> None:
    """Functional, but not production-grade — and the report should say so."""
    assert overall_status(_healthy_dependencies(), uses_redis=False) is HealthStatus.DEGRADED
