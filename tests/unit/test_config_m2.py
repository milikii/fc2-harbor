"""M2 configuration surface — env overrides, FC2 width, provider strictness.

Spec 18 / 6.3 / 7.1, plus the reviewer's decisions: only two env overrides, the
FC2 digit width is configuration, and disabling a provider is possible but
explicit, logged and audited.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from app.config import (
    ConfigError,
    Settings,
    applied_env_overrides,
    apply_env_overrides,
)
from app.db.models import DecisionEvent
from app.db.session import Database
from app.main import _audit_provider_configuration
from app.services.normalize import Fc2CodeRule
from sqlalchemy import select

from tests.support import deep_update, make_config

BASE_ENV = {
    "MANUAL_BOT_TOKEN": "123456:TEST",
    "TELEGRAM_ALLOWED_USER_IDS": "111",
}


def write_config(tmp_path: Path, **overrides: object) -> Path:
    config = deep_update(make_config(tmp_path), overrides)  # type: ignore[arg-type]
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config), encoding="utf-8")
    return path


def test_env_overrides_win_over_yaml(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    env = {
        **BASE_ENV,
        "FC2_LOG_LEVEL": "DEBUG",
        "FC2_DATABASE_URL": f"sqlite:///{tmp_path / 'override.db'}",
    }

    settings = Settings.load(path, env=env)

    assert settings.app.log_level == "DEBUG"
    assert settings.app.database_url.endswith("override.db")
    assert applied_env_overrides(env) == ["FC2_DATABASE_URL", "FC2_LOG_LEVEL"]


def test_yaml_values_are_kept_without_overrides(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    settings = Settings.load(path, env=BASE_ENV)
    assert settings.app.log_level == "INFO"
    assert settings.app.database_url.endswith("app.db")
    assert applied_env_overrides(BASE_ENV) == []


def test_invalid_env_override_fails_fast(tmp_path: Path) -> None:
    path = write_config(tmp_path)
    with pytest.raises(ConfigError):
        Settings.load(path, env={**BASE_ENV, "FC2_LOG_LEVEL": "LOUD"})


def test_apply_env_overrides_returns_applied_names(tmp_path: Path) -> None:
    data = {"app": {"log_level": "INFO"}}
    applied = apply_env_overrides(data, {"FC2_LOG_LEVEL": "WARNING", "OTHER": "x"})
    assert applied == ["FC2_LOG_LEVEL"]
    assert data["app"]["log_level"] == "WARNING"


def test_fc2_digit_width_is_configurable(tmp_path: Path) -> None:
    settings = Settings.from_mapping(
        deep_update(make_config(tmp_path), {"fc2": {"code_min_digits": 4, "code_max_digits": 4}}),
        env={},
    )
    rule = Fc2CodeRule(settings.fc2.code_min_digits, settings.fc2.code_max_digits)
    assert rule.normalize("FC2-PPV-1234") is not None
    assert rule.normalize("FC2-PPV-12345") is None


def test_fc2_digit_range_must_be_ordered(tmp_path: Path) -> None:
    config = deep_update(
        make_config(tmp_path), {"fc2": {"code_min_digits": 9, "code_max_digits": 5}}
    )
    with pytest.raises(ConfigError):
        Settings.from_mapping(config, env={})


def test_strict_provider_mode_rejects_disabled_source(tmp_path: Path) -> None:
    config = deep_update(
        make_config(tmp_path), {"provider": {"sources": {"Madou": {"enabled": False}}}}
    )
    with pytest.raises(ConfigError) as excinfo:
        Settings.from_mapping(config, env={})
    assert "strict_enabled" in str(excinfo.value)


def test_relaxed_provider_mode_allows_disabled_source(tmp_path: Path) -> None:
    config = deep_update(
        make_config(tmp_path),
        {
            "provider": {
                "strict_enabled": False,
                "sources": {"Madou": {"enabled": False}},
            }
        },
    )
    settings = Settings.from_mapping(config, env={})
    assert settings.provider.disabled_providers() == ["Madou"]


async def test_disabled_providers_are_audited(db: Database, tmp_path: Path) -> None:
    config = deep_update(
        make_config(tmp_path),
        {
            "provider": {
                "strict_enabled": False,
                "sources": {"Madou": {"enabled": False}, "avsox": {"enabled": False}},
            }
        },
    )
    settings = Settings.from_mapping(config, env={})

    await _audit_provider_configuration(db, settings)

    async with db.session() as session:
        events = list(
            (
                await session.scalars(
                    select(DecisionEvent).where(DecisionEvent.event_type == "provider.disabled")
                )
            ).all()
        )
    assert len(events) == 1
    # Canonical priority order, so the audit trail is deterministic.
    assert events[0].reason == "avsox,Madou"


async def test_no_event_when_all_providers_enabled(db: Database, tmp_path: Path) -> None:
    settings = Settings.from_mapping(make_config(tmp_path), env={})
    await _audit_provider_configuration(db, settings)

    async with db.session() as session:
        events = list(
            (
                await session.scalars(
                    select(DecisionEvent).where(DecisionEvent.event_type == "provider.disabled")
                )
            ).all()
        )
    assert events == []
