"""Proxy routing — spec 8.1 / 8.2 (per provider, per CDN, JP never falls back to direct)."""

from __future__ import annotations

import pytest
from app.config import ConfigError, ProxyProfile
from app.network.proxy import ProxyRouter

PROFILES = {
    "direct": ProxyProfile(type="direct"),
    "japan": ProxyProfile(type="socks5h", url="socks5h://u:p@jp1:1080", region="JP"),
    "japan_backup": ProxyProfile(type="http", url="http://u:p@jp2:8080", region="JP"),
    "us": ProxyProfile(type="http", url="http://us1:8080", region="US"),
    "cdn_direct": ProxyProfile(type="direct"),
}


def router() -> ProxyRouter:
    return ProxyRouter(PROFILES)


def test_provider_proxy_is_used_as_configured() -> None:
    target = router().for_provider("japan")
    assert target.url == "socks5h://u:p@jp1:1080"
    assert target.region == "JP"
    assert router().for_provider("direct").is_direct is True


def test_image_inherits_provider_proxy() -> None:
    target = router().for_image("japan", "img.example.com", {})
    assert target.profile == "japan"


def test_cdn_route_overrides_provider_proxy() -> None:
    target = router().for_image("japan", "static.cdn.example", {"static.cdn.example": "direct"})
    assert target.profile == "direct"
    assert target.is_direct is True


def test_cdn_route_matches_parent_domain() -> None:
    target = router().for_image("japan", "img.cdn.example", {"cdn.example": "us"})
    assert target.profile == "us"


def test_cdn_route_does_not_match_a_lookalike_host() -> None:
    target = router().for_image("japan", "notcdn.example.org", {"cdn.example": "us"})
    assert target.profile == "japan"


def test_jp_failover_stays_inside_jp_and_never_direct() -> None:
    chain = router().failover_chain("japan")
    names = [item.profile for item in chain]

    assert names[0] == "japan"
    assert "japan_backup" in names
    assert "direct" not in names
    assert "us" not in names


def test_direct_has_no_failover() -> None:
    assert [item.profile for item in router().failover_chain("direct")] == ["direct"]


def test_unknown_profile_is_rejected() -> None:
    with pytest.raises(ConfigError):
        router().for_provider("nope")


def test_direct_profile_is_required() -> None:
    with pytest.raises(ConfigError):
        ProxyRouter({"japan": PROFILES["japan"]})
