"""Deployment artefacts — spec 19 / 25.10.

These tests keep the shipped Dockerfile, compose file, example configuration and
requirements honest: they are part of the deliverable, not documentation.
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config import CANONICAL_FC2_PROVIDER_ORDER, Settings

ROOT = Path(__file__).resolve().parents[2]


def test_dockerfile_matches_spec() -> None:
    text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "python:3.12-slim" in text
    assert re.search(r"^USER app$", text, re.MULTILINE)
    assert "HEALTHCHECK" in text
    assert "STOPSIGNAL SIGTERM" in text
    assert "groupadd --gid 1000 app" in text
    # No build tools left behind and no root entrypoint.
    assert "sudo" not in text


def test_compose_matches_spec() -> None:
    text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert "ports: []" in text
    assert "restart: unless-stopped" in text
    assert "docker.sock" not in text
    assert "./config:/app/config:ro" in text
    assert "./data:/app/data" in text
    assert "/mnt/storage/downloads:/downloads" in text
    assert "/mnt/storage/library:/library" in text
    assert "no-new-privileges:true" in text
    assert "stop_grace_period" in text


def test_env_example_contains_placeholders_only() -> None:
    text = (ROOT / ".env.example").read_text(encoding="utf-8")
    assert "SUBSCRIPTION_BOT_TOKEN=\n" in text  # empty until M4
    assert "MANUAL_BOT_TOKEN=\n" in text
    assert "TELEGRAM_ALLOWED_USER_IDS=\n" in text
    assert "QBITTORRENT_PASSWORD=change-me" in text
    # No long token-like values anywhere in the template.
    assert not re.search(r"[0-9]{6,}:[A-Za-z0-9_-]{30,}", text)


def test_example_config_is_valid_and_keeps_provider_order() -> None:
    env = {
        "MANUAL_BOT_TOKEN": "123456:TEST",
        "TELEGRAM_ALLOWED_USER_IDS": "111",
        "JP_PROXY_URL": "socks5h://user:password@proxy.example.com:1080",
        "JP_BACKUP_PROXY_URL": "http://user:password@proxy-backup.example.com:8080",
        "QBITTORRENT_URL": "http://192.168.1.10:8080",
        "QBITTORRENT_USERNAME": "admin",
        "QBITTORRENT_PASSWORD": "change-me",
        "TRANSLATION_BASE_URL": "https://translate.example/v1",
        "TRANSLATION_API_KEY": "change-me",
        "TRANSLATION_MODEL": "gpt-4o-mini",
    }
    settings = Settings.load(ROOT / "config" / "example.yaml", env=env)

    assert tuple(settings.provider.priorities["jav_fc2"]) == CANONICAL_FC2_PROVIDER_ORDER
    assert all(source.enabled for source in settings.provider.sources.values())
    assert len(settings.provider.sources) == 9
    assert settings.nyaa.scope == "site"
    assert settings.nyaa.proxy == "direct"
    assert settings.download.default_downloader == "qbittorrent"
    assert settings.downloaders.transmission.enabled is False
    assert settings.translation.enabled is True
    assert settings.image_fetch.cache_dir == "/app/data/cache/images"


def test_gitignore_covers_runtime_secrets() -> None:
    text = (ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "data/", ".secrets/", "config/config.yaml", "*.db"):
        assert pattern in text


def test_requirements_are_pinned() -> None:
    lines = []
    for raw in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        lines.append(line)
    assert lines
    for line in lines:
        assert "==" in line, f"unpinned requirement: {line}"
    joined = "\n".join(lines)
    for package in ("aiogram", "sqlalchemy", "alembic", "pydantic", "structlog", "httpx"):
        assert package in joined.lower()


def test_spec_is_versioned_in_the_repository() -> None:
    spec = ROOT / "docs" / "BT_AUTOMATION_SPEC.md"
    assert spec.exists()
    text = spec.read_text(encoding="utf-8")
    assert "## 11. 统一内容门禁" in text
    assert "## 23. 分阶段开发顺序" in text


def test_backup_script_is_executable_and_uses_the_sqlite_backup_api() -> None:
    script = ROOT / "scripts" / "backup.sh"
    assert script.exists()
    assert script.stat().st_mode & 0o111
    text = script.read_text(encoding="utf-8")
    assert "src.backup(dst)" in text  # online backup, not a raw file copy
    assert "config-" in text and "tar" in text
    assert "head -n -14" in text  # keeps the last 14 archives


def test_decision_log_and_runbook_exist() -> None:
    deviations = ROOT / "docs" / "deviations.md"
    runbook = ROOT / "docs" / "runbook-smoke.md"
    assert deviations.exists()
    assert runbook.exists()
    assert "pydantic-settings" in deviations.read_text(encoding="utf-8")
    assert "通过标准" in runbook.read_text(encoding="utf-8")
