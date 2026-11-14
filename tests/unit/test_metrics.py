"""Metrics registry — spec 21 (provider hit rate, proxy failure rate)."""

from __future__ import annotations

from app.services.metrics import Metrics


def test_provider_outcomes_are_counted_separately() -> None:
    metrics = Metrics()
    metrics.record_provider("Fc2", "hit", latency_ms=100)
    metrics.record_provider("Fc2", "hit", latency_ms=200)
    metrics.record_provider("Fc2", "miss", latency_ms=50)
    metrics.record_provider("Fc2", "structure", latency_ms=10)
    metrics.record_provider("Fc2", "error", latency_ms=20)

    data = metrics.provider_snapshot()["Fc2"]
    assert data["hit"] == 2
    assert data["miss"] == 1
    assert data["structure"] == 1
    assert data["error"] == 1
    assert data["calls"] == 5
    assert data["average_latency_ms"] == 76
    assert data["hit_rate"] == 0.4


def test_miss_and_structure_change_are_different_signals() -> None:
    metrics = Metrics()
    for _ in range(5):
        metrics.record_provider("Javdb", "miss")
    for _ in range(3):
        metrics.record_provider("Mmtv", "structure")

    unhealthy = metrics.unhealthy_providers(min_calls=3)
    assert "Mmtv" in unhealthy
    assert "Javdb" not in unhealthy


def test_unknown_outcome_is_counted_as_error() -> None:
    metrics = Metrics()
    metrics.record_provider("X", "nonsense")
    assert metrics.provider_snapshot()["X"]["error"] == 1


def test_proxy_metrics_track_region_and_failures() -> None:
    metrics = Metrics()
    metrics.record_proxy("japan", region="JP", ok=True, latency_ms=100)
    metrics.record_proxy("japan", region="JP", ok=False, latency_ms=300, error="ConnectError")
    metrics.record_proxy("japan", region="JP", ok=True, latency_ms=200)

    data = metrics.proxy_snapshot()["japan"]
    assert data["region"] == "JP"
    assert data["calls"] == 3
    assert data["failures"] == 1
    assert data["failure_rate"] == 0.3333
    assert data["last_error"] == "ConnectError"
    assert data["average_latency_ms"] == 200


def test_counters_and_reset() -> None:
    metrics = Metrics()
    metrics.inc("cards.published")
    metrics.inc("cards.published")
    metrics.inc("cards.failed")
    assert metrics.snapshot()["counters"] == {"cards.failed": 1, "cards.published": 2}

    metrics.reset()
    assert metrics.snapshot() == {"providers": {}, "proxies": {}, "counters": {}}
