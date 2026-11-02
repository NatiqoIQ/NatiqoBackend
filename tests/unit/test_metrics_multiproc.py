"""Multi-worker aggregation for ``/metrics``.

uvicorn with ``--workers N`` runs N processes behind one listening socket, so a scrape
lands on a different worker each time and the process-local registry would report a
different partial count per scrape. These tests pin the contract of the snapshot files:
a stale counter from a dead pid is pruned, a live pid is merged, counts are summed
elementwise (latency buckets included), and the answering worker never merges its own
file twice.
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

from app.core import metrics


@pytest.fixture()
def multiproc_dir(tmp_path: pytest.Path, monkeypatch: pytest.MonkeyPatch) -> str:
    directory = str(tmp_path / "prom")
    monkeypatch.setenv(metrics._MULTIPROC_DIR_ENV, directory)
    yield directory
    monkeypatch.delenv(metrics._MULTIPROC_DIR_ENV, raising=False)


def _snapshot(pid: int, requests: dict[str, int] | None = None) -> dict[str, Any]:
    return {
        "pid": pid,
        "requests": requests or {},
        "durations": {},
        "inflight": 3,
        "uptime": 10.0,
        "pool": {"primary|checked_out": 2},
        "lag": 0.5,
        "ai_circuit": 0,
        "cache": {"catalogue|hit": 4},
    }


def test_single_process_env_renders_locally(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(metrics._MULTIPROC_DIR_ENV, raising=False)
    directory_value = metrics._multiproc_dir()
    assert directory_value is None
    text = metrics.render_prometheus()
    assert "natiqo_requests_in_flight" in text


def test_merge_sums_counters_and_buckets_across_workers() -> None:
    left = {
        "pid": 1,
        "requests": {"GET|/x|200": 2},
        "durations": {
            "GET|/x": {"buckets": [1, 2, 0, 0], "count": 3, "total": 0.4},
        },
        "inflight": 2,
        "uptime": 5.0,
        "pool": {"primary|checked_out": 1},
        "lag": 0.0,
        "ai_circuit": 0,
        "cache": {"catalogue|hit": 1},
    }
    right = {
        "pid": 2,
        "requests": {"GET|/x|200": 3, "GET|/y|500": 1},
        "durations": {
            "GET|/x": {"buckets": [0, 1, 1, 0], "count": 2, "total": 1.6},
        },
        "inflight": 5,
        "uptime": 9.0,
        "pool": {"primary|checked_out": 4},
        "lag": 0.25,
        "ai_circuit": 1,
        "cache": {"catalogue|hit": 2, "catalogue|miss": 1},
    }

    merged = metrics._merge([left, right])

    assert merged["requests"] == {"GET|/x|200": 5, "GET|/y|500": 1}
    histogram = merged["durations"]["GET|/x"]
    assert histogram["buckets"] == [1, 3, 1, 0]
    assert histogram["count"] == 5
    assert merged["inflight"] == 7
    assert merged["uptime"] == 9.0
    assert merged["pool"] == {"primary|checked_out": 5}
    assert merged["lag"] == 0.25
    assert merged["ai_circuit"] == 1
    assert merged["cache"] == {"catalogue|hit": 3, "catalogue|miss": 1}


def test_scrape_merges_live_worker_and_prunes_dead_one(
    multiproc_dir: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics._write_snapshot(multiproc_dir, _snapshot(424242, requests={"GET|/x|200": 9}))
    metrics._write_snapshot(multiproc_dir, _snapshot(99999901, requests={"GET|/x|200": 99}))

    monkeypatch.setattr(metrics, "_pid_alive", lambda pid: pid == 424242)

    text = metrics.render_prometheus()

    assert 'natiqo_http_requests_total{method="GET",route="/x",status="200"} 9' in text
    assert not os.path.exists(os.path.join(multiproc_dir, "99999901.json"))
    assert os.path.exists(os.path.join(multiproc_dir, "424242.json"))


def test_own_file_is_never_merged_twice(
    multiproc_dir: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    metrics.observe_request("GET", "/x", "200", 0.01)
    snapshot = metrics._local_snapshot()
    metrics._write_snapshot(multiproc_dir, snapshot)

    own_pid = snapshot["pid"]
    assert _read_pids(multiproc_dir) == {own_pid}

    text = metrics.render_prometheus()
    expected = 1
    assert request_count(text, "/x", "200") == expected
    monkeypatch.delenv(metrics._MULTIPROC_DIR_ENV, raising=False)


def _read_pids(directory: str) -> set[int]:
    return {
        int(os.path.splitext(name)[0]) for name in os.listdir(directory) if name.endswith(".json")
    }


def request_count(text: str, route: str, status: str) -> int:
    for line in text.splitlines():
        label = f'natiqo_http_requests_total{{method="GET",route="{route}",status="{status}"}}'
        if line.startswith(label):
            payload = json.loads(line.rsplit(" ", 1)[1])
            return int(payload)
    raise AssertionError(f"series for {route} {status} missing")
