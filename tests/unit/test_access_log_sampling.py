"""Access-log sampling must never hide the evidence of a problem.

At a few thousand requests per second a line per request is bytes nobody reads. Metrics
still count every request, so sampling routine successes is safe. A 5xx or a slow request
is exactly the line an incident review searches for, so those are always written.
"""

from __future__ import annotations

import pytest

from app.core.middleware import should_log_access


def _log(status: int = 200, duration: float = 10.0, rate: float = 0.05, roll: float = 0.99) -> bool:
    return should_log_access(
        status_code=status,
        duration_ms=duration,
        sample_rate=rate,
        slow_ms=500,
        roll=roll,
    )


def test_a_routine_success_can_be_sampled_away() -> None:
    assert _log() is False


def test_a_success_inside_the_sample_is_kept() -> None:
    assert _log(roll=0.01) is True


@pytest.mark.parametrize("status", [500, 502, 503])
def test_every_server_error_is_kept(status: int) -> None:
    assert _log(status=status) is True


def test_a_slow_request_is_kept_even_when_it_succeeded() -> None:
    assert _log(duration=500) is True


def test_rate_one_keeps_everything() -> None:
    assert _log(rate=1.0) is True


def test_out_of_range_rates_are_clamped() -> None:
    assert _log(rate=-1.0) is False
    assert _log(rate=2.0) is True
