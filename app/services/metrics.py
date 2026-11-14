"""In-process metrics — spec 21.

Everything the operator needs to see at a glance:

* per provider: hit / miss / structure change / error, call count and average
  latency. "Miss" (the site has no such entry) and "structure change" (the selectors
  broke) are separate counters, because they mean completely different things.
* per proxy profile: calls, failures, average latency and the configured region, so
  a dead Japan exit is visible immediately.
* pipeline counters: cards sent/failed, jobs submitted/failed, images rejected,
  deletions deferred.

The registry is a plain object owned by the process; it is not persisted, because
every value is a counter that restarts with the container. ``/status`` exposes a
snapshot.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field

PROVIDER_OUTCOMES = ("hit", "miss", "structure", "error")


@dataclass(slots=True)
class ProviderMetrics:
    hit: int = 0
    miss: int = 0
    structure: int = 0
    error: int = 0
    calls: int = 0
    latency_ms_total: int = 0

    @property
    def average_latency_ms(self) -> int:
        return int(self.latency_ms_total / self.calls) if self.calls else 0

    @property
    def hit_rate(self) -> float:
        return round(self.hit / self.calls, 4) if self.calls else 0.0

    def snapshot(self) -> dict[str, float | int]:
        data = asdict(self)
        data["average_latency_ms"] = self.average_latency_ms
        data["hit_rate"] = self.hit_rate
        return data


@dataclass(slots=True)
class ProxyMetrics:
    profile: str
    region: str | None = None
    calls: int = 0
    failures: int = 0
    latency_ms_total: int = 0
    last_error: str | None = None

    @property
    def failure_rate(self) -> float:
        return round(self.failures / self.calls, 4) if self.calls else 0.0

    @property
    def average_latency_ms(self) -> int:
        return int(self.latency_ms_total / self.calls) if self.calls else 0

    def snapshot(self) -> dict[str, object]:
        return {
            "profile": self.profile,
            "region": self.region,
            "calls": self.calls,
            "failures": self.failures,
            "failure_rate": self.failure_rate,
            "average_latency_ms": self.average_latency_ms,
            "last_error": self.last_error,
        }


@dataclass(slots=True)
class Metrics:
    providers: dict[str, ProviderMetrics] = field(default_factory=dict)
    proxies: dict[str, ProxyMetrics] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=dict)

    # -- recording ----------------------------------------------------------

    def record_provider(self, provider: str, outcome: str, *, latency_ms: int = 0) -> None:
        metrics = self.providers.setdefault(provider, ProviderMetrics())
        metrics.calls += 1
        metrics.latency_ms_total += max(0, latency_ms)
        if outcome in PROVIDER_OUTCOMES:
            setattr(metrics, outcome, getattr(metrics, outcome) + 1)
        else:  # pragma: no cover - guarded by callers
            metrics.error += 1

    def record_proxy(
        self,
        profile: str,
        *,
        region: str | None = None,
        ok: bool,
        latency_ms: int = 0,
        error: str | None = None,
    ) -> None:
        metrics = self.proxies.setdefault(profile, ProxyMetrics(profile=profile, region=region))
        if region and metrics.region is None:
            metrics.region = region
        metrics.calls += 1
        metrics.latency_ms_total += max(0, latency_ms)
        if not ok:
            metrics.failures += 1
            metrics.last_error = error

    def inc(self, name: str, value: int = 1) -> None:
        self.counters[name] = self.counters.get(name, 0) + value

    # -- reading ------------------------------------------------------------

    def provider_snapshot(self) -> dict[str, dict[str, float | int]]:
        return {name: metrics.snapshot() for name, metrics in sorted(self.providers.items())}

    def proxy_snapshot(self) -> dict[str, dict[str, object]]:
        return {name: metrics.snapshot() for name, metrics in sorted(self.proxies.items())}

    def snapshot(self) -> dict[str, object]:
        return {
            "providers": self.provider_snapshot(),
            "proxies": self.proxy_snapshot(),
            "counters": dict(sorted(self.counters.items())),
        }

    def unhealthy_providers(self, *, min_calls: int = 3) -> list[str]:
        """Providers whose last calls were mostly structure changes or errors."""

        bad: list[str] = []
        for name, metrics in sorted(self.providers.items()):
            if metrics.calls < min_calls:
                continue
            if metrics.structure + metrics.error > metrics.hit + metrics.miss:
                bad.append(name)
        return bad

    def reset(self) -> None:
        self.providers.clear()
        self.proxies.clear()
        self.counters.clear()
