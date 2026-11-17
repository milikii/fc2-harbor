"""Configuration loading, env expansion and fail-fast validation — spec 18 / 22.1."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import (
    CANONICAL_FC2_PROVIDER_ORDER,
    ConfigError,
    Settings,
    expand_env_placeholders,
    redact_url_credentials,
)

from tests.support import deep_update, make_config


def test_valid_configuration_parses(tmp_path: Path) -> None:
    settings = Settings.from_mapping(make_config(tmp_path), env={})
    assert settings.telegram.allowed_user_ids == [111, 222]
    assert settings.telegram.proxy == "direct"
    assert settings.provider.priorities["jav_fc2"] == list(CANONICAL_FC2_PROVIDER_ORDER)
    assert settings.downloader_for_category("fc2") == "qbittorrent"
    assert settings.final_root("fc2") == tmp_path / "library" / "fc2"
    # Secrets never appear in their clear form.
    assert "TEST-MANUAL-TOKEN" not in repr(settings.telegram.manual_bot_token)
    assert settings.telegram.manual_bot_token.get_secret_value() == "123456:TEST-MANUAL-TOKEN"


def test_env_placeholder_expansion(tmp_path: Path) -> None:
    config = deep_update(
        make_config(tmp_path),
        {"telegram": {"manual_bot_token": "${MANUAL_TOKEN}", "allowed_user_ids": "${IDS}"}},
    )
    settings = Settings.from_mapping(config, env={"MANUAL_TOKEN": "999:ABC", "IDS": "5, 6,7"})
    assert settings.telegram.manual_bot_token.get_secret_value() == "999:ABC"
    assert settings.telegram.allowed_user_ids == [5, 6, 7]


def test_whole_value_placeholder_reports_variable_name(tmp_path: Path) -> None:
    config = deep_update(
        make_config(tmp_path), {"telegram": {"manual_bot_token": "${MISSING_TOKEN}"}}
    )
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_mapping(config, env={})
    assert "MISSING_TOKEN" in str(excinfo.value)


def test_embedded_placeholder_must_resolve() -> None:
    with pytest.raises(ConfigError):
        expand_env_placeholders({"url": "http://${HOST}:8080"}, env={})
    expanded, missing = expand_env_placeholders(
        {"url": "http://${HOST}:8080"}, env={"HOST": "example.com"}
    )
    assert expanded == {"url": "http://example.com:8080"}
    assert missing == []


def test_provider_order_cannot_be_reordered(tmp_path: Path) -> None:
    reordered = list(CANONICAL_FC2_PROVIDER_ORDER)
    reordered.reverse()
    config = deep_update(
        make_config(tmp_path), {"provider": {"priorities": {"jav_fc2": reordered}}}
    )
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_mapping(config, env={})
    assert "fixed order" in str(excinfo.value)


def test_provider_cannot_be_disabled(tmp_path: Path) -> None:
    config = deep_update(
        make_config(tmp_path), {"provider": {"sources": {"Madou": {"enabled": False}}}}
    )
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_mapping(config, env={})
    assert "enabled" in str(excinfo.value)


def test_provider_host_must_match_adapter(tmp_path: Path) -> None:
    config = deep_update(
        make_config(tmp_path), {"provider": {"sources": {"Madou": {"host": "evil.example"}}}}
    )
    with pytest.raises(ConfigError):
        Settings.from_mapping(config, env={})


def test_unknown_proxy_reference_is_rejected(tmp_path: Path) -> None:
    config = deep_update(make_config(tmp_path), {"nyaa": {"proxy": "nonexistent"}})
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_mapping(config, env={})
    assert "unknown proxy profile" in str(excinfo.value)


def test_direct_profile_is_required(tmp_path: Path) -> None:
    config = deep_update(make_config(tmp_path), {"proxy_profiles": {"japan": {"type": "direct"}}})
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_mapping(config, env={})
    assert "direct" in str(excinfo.value)


def test_nyaa_paths_reject_uploader_filters(tmp_path: Path) -> None:
    """Spec 6.1: the subscription must stay site-wide, never u=offkab."""

    for overrides in (
        {"query": "FC2&u=offkab"},
        {"rss_path": "/?page=rss&q=FC2&u=offkab"},
        {"search_path": "/?f=0&c=0_0&q=FC2&u=offkab&p={page}"},
    ):
        config = deep_update(make_config(tmp_path), {"nyaa": overrides})
        with pytest.raises(ConfigError) as excinfo:
            Settings.from_mapping(config, env={})
        assert "site-wide" in str(excinfo.value)


def test_poll_interval_bounds(tmp_path: Path) -> None:
    config = deep_update(make_config(tmp_path), {"nyaa": {"poll_interval_seconds": 100}})
    with pytest.raises(ConfigError):
        Settings.from_mapping(config, env={})


def test_openai_translation_requires_credentials_when_enabled(tmp_path: Path) -> None:
    config = deep_update(
        make_config(tmp_path),
        {"translation": {"enabled": True, "provider": "openai_compatible"}},
    )
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_mapping(config, env={})
    assert "translation.enabled requires" in str(excinfo.value)


def test_google_free_translation_needs_no_credentials(tmp_path: Path) -> None:
    config = deep_update(make_config(tmp_path), {"translation": {"enabled": True}})
    settings = Settings.from_mapping(config, env={})
    assert settings.translation.provider == "google_free"
    assert settings.translation.proxy == "japan"


def test_google_free_translation_rejects_an_unknown_proxy(tmp_path: Path) -> None:
    config = deep_update(
        make_config(tmp_path), {"translation": {"enabled": True, "proxy": "nope"}}
    )
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_mapping(config, env={})
    assert "translation.proxy" in str(excinfo.value)


def test_translation_loads_when_fully_configured(tmp_path: Path) -> None:
    config = deep_update(
        make_config(tmp_path),
        {
            "translation": {
                "enabled": True,
                "provider": "openai_compatible",
                "base_url": "https://translate.example/v1",
                "api_key": "key",
                "model": "model",
            }
        },
    )
    settings = Settings.from_mapping(config, env={})
    assert settings.translation.enabled is True
    assert settings.translation.model == "model"


def test_route_must_reference_enabled_downloader(tmp_path: Path) -> None:
    config = deep_update(make_config(tmp_path), {"download": {"routes": {"fc2": "transmission"}}})
    with pytest.raises(ConfigError):
        Settings.from_mapping(config, env={})


def test_save_path_template_requires_placeholders(tmp_path: Path) -> None:
    config = deep_update(
        make_config(tmp_path),
        {"downloaders": {"qbittorrent": {"save_path_template": "/downloads/fixed"}}},
    )
    with pytest.raises(ConfigError):
        Settings.from_mapping(config, env={})


def test_nested_final_roots_rejected(tmp_path: Path) -> None:
    config = deep_update(
        make_config(tmp_path),
        {
            "storage": {
                "final_roots": {
                    "fc2": str(tmp_path / "library" / "fc2"),
                    "jav": str(tmp_path / "library" / "fc2" / "jav"),
                    "cn": str(tmp_path / "library" / "cn"),
                    "western": str(tmp_path / "library" / "western"),
                    "review": str(tmp_path / "library" / "review"),
                }
            }
        },
    )
    with pytest.raises(ConfigError):
        Settings.from_mapping(config, env={})


def test_database_url_must_be_sqlite(tmp_path: Path) -> None:
    config = deep_update(
        make_config(tmp_path),
        {"app": {"database_url": "postgresql://user@host/db"}},
    )
    with pytest.raises(ConfigError):
        Settings.from_mapping(config, env={})


def test_unknown_key_is_rejected(tmp_path: Path) -> None:
    config = deep_update(make_config(tmp_path), {"app": {"loglevel": "DEBUG"}})
    with pytest.raises(ConfigError):
        Settings.from_mapping(config, env={})


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(ConfigError):
        Settings.load(tmp_path / "nope.yaml", env={})


def test_redact_url_credentials() -> None:
    assert (
        redact_url_credentials("socks5h://user:pass@proxy.example:1080")
        == "socks5h://***:***@proxy.example:1080"
    )
    assert (
        redact_url_credentials("http://token@proxy.example:8080") == "http://***@proxy.example:8080"
    )
    assert redact_url_credentials("http://proxy.example:8080") == "http://proxy.example:8080"


def test_flaresolverr_url_is_optional(tmp_path: Path) -> None:
    settings = Settings.from_mapping(make_config(tmp_path), env={})
    assert settings.provider.flaresolverr_url is None


def test_flaresolverr_url_is_expanded_from_the_environment(tmp_path: Path) -> None:
    config = deep_update(
        make_config(tmp_path), {"provider": {"flaresolverr_url": "${FLARESOLVERR_URL}"}}
    )
    settings = Settings.from_mapping(
        config, env={"FLARESOLVERR_URL": "http://192.168.1.220:8191"}
    )
    assert settings.provider.flaresolverr_url == "http://192.168.1.220:8191"


def test_flaresolverr_url_must_be_http(tmp_path: Path) -> None:
    config = deep_update(
        make_config(tmp_path), {"provider": {"flaresolverr_url": "ftp://byparr:8191"}}
    )
    with pytest.raises(ConfigError):
        Settings.from_mapping(config, env={})
