"""Proxy pool and routing — spec 8.1 / 8.2.

Rules encoded here:

* every request names a profile explicitly; there is no ambient proxy and no
  global fallback to ``direct`` for a Japan-restricted source;
* an image inherits its provider's profile unless a CDN-domain route overrides it
  (exact host or parent domain);
* failover stays inside the same region: a JP source may move between Japanese
  exits (``japan`` → ``japan_backup``) but never to ``direct``;
* qBittorrent, Transmission, the NAS and the database never go through this
  router at all — they use ``trust_env=False`` clients with no proxy.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from app.config import ConfigError, ProxyProfile


@dataclass(frozen=True, slots=True)
class ProxyTarget:
    profile: str
    url: str | None
    region: str | None

    @property
    def is_direct(self) -> bool:
        return self.url is None


class ProxyRouter:
    def __init__(self, profiles: Mapping[str, ProxyProfile]) -> None:
        if "direct" not in profiles:
            raise ConfigError("proxy_profiles must define 'direct'")
        self._profiles = dict(profiles)

    # -- lookups ------------------------------------------------------------

    def target(self, profile_name: str) -> ProxyTarget:
        profile = self._profiles.get(profile_name)
        if profile is None:
            raise ConfigError(f"unknown proxy profile {profile_name!r}")
        return ProxyTarget(
            profile=profile_name,
            url=None if profile.type == "direct" else profile.url,
            region=profile.region,
        )

    def for_provider(self, provider_profile: str) -> ProxyTarget:
        return self.target(provider_profile)

    def for_image(
        self,
        provider_profile: str,
        cdn_host: str | None,
        cdn_routes: Mapping[str, str],
    ) -> ProxyTarget:
        """Image proxy: CDN override wins, otherwise inherit the provider."""

        override = self._match_cdn_route(cdn_host, cdn_routes)
        if override is not None:
            return self.target(override)
        return self.target(provider_profile)

    def failover_chain(self, profile_name: str) -> list[ProxyTarget]:
        """Profiles to try in order when a request fails.

        Same region only, and never ``direct`` for a regional profile: spec 8.2
        forbids silently falling back to a direct connection for JP sources.
        """

        primary = self.target(profile_name)
        if primary.is_direct:
            return [primary]

        chain = [primary]
        if primary.region:
            for name, profile in self._profiles.items():
                if name == profile_name or profile.type == "direct":
                    continue
                if (profile.region or "").upper() != primary.region.upper():
                    continue
                chain.append(
                    ProxyTarget(
                        profile=name,
                        url=profile.url,
                        region=profile.region,
                    )
                )
        return chain

    # -- helpers ------------------------------------------------------------

    @staticmethod
    def _match_cdn_route(cdn_host: str | None, cdn_routes: Mapping[str, str]) -> str | None:
        if not cdn_host or not cdn_routes:
            return None
        host = cdn_host.strip().lower()
        if host in cdn_routes:
            return cdn_routes[host]
        # Parent-domain route: "example.com" covers "img.example.com".
        for domain, profile in cdn_routes.items():
            domain = domain.lower()
            if host.endswith("." + domain):
                return profile
        return None
