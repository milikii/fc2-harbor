"""Health endpoints — the only HTTP surface (spec 3 / 19)."""

from __future__ import annotations

import httpx
import pytest
from app.api.health import create_health_app
from app.config import Settings
from app.db.enums import DownloadStatus
from app.db.session import Database

from tests.support import create_job, hex_hash


def make_client(db: Database, settings: Settings) -> httpx.AsyncClient:
    app = create_health_app(db=db, settings=settings)
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


async def test_healthz_is_liveness_only(db: Database, settings: Settings) -> None:
    async with make_client(db, settings) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_readyz_checks_the_database(db: Database, settings: Settings) -> None:
    async with make_client(db, settings) as client:
        response = await client.get("/readyz")
    assert response.status_code == 200
    assert response.json()["database"] == "ok"


async def test_readyz_reports_degraded_when_database_is_down(
    db: Database, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failing_ping() -> None:
        raise RuntimeError("database is gone")

    monkeypatch.setattr(db, "ping", failing_ping)
    async with make_client(db, settings) as client:
        response = await client.get("/readyz")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


async def test_status_reports_counts(db: Database, settings: Settings) -> None:
    await create_job(db, info_hash=hex_hash(401), status=DownloadStatus.DOWNLOADING)
    await create_job(
        db,
        info_hash=hex_hash(402),
        canonical_key="fc2_ppv:2222222",
        status=DownloadStatus.FAILED,
    )

    async with make_client(db, settings) as client:
        response = await client.get("/status")

    payload = response.json()
    assert payload["contents"] == 2
    assert payload["failed_jobs"] == 1
    assert payload["jobs"]["DOWNLOADING"] == 1
    assert payload["downloaders"] == ["qbittorrent"]
