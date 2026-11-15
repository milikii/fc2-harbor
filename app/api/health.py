"""Internal health and status endpoints — spec 3 / 19.

Only reachable inside the container network: compose publishes no ports and the
Telegram bots use long polling (spec 19). ``/healthz`` is liveness (no I/O),
``/readyz`` checks the database, ``/status`` returns counters for the NAS
operator.
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.config import Settings
from app.db.enums import DownloadStatus
from app.db.models import AcceptedEntry, BlacklistEntry, Content, DownloadJob
from app.db.session import Database
from app.logging import error_detail, get_logger

_log = get_logger("api.health")


def create_health_app(
    *,
    db: Database,
    settings: Settings,
    metrics: object | None = None,
    supervisor: object | None = None,
) -> FastAPI:
    app = FastAPI(
        title="fc2-harbor",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz() -> JSONResponse:
        try:
            await db.ping()
        except Exception as exc:
            _log.error(
                "health.db_unavailable",
                error_code=type(exc).__name__,
                error_detail=error_detail(exc),
            )
            return JSONResponse({"status": "degraded", "database": "unavailable"}, status_code=503)
        return JSONResponse({"status": "ready", "database": "ok"})

    @app.get("/status")
    async def status() -> dict[str, object]:
        async with db.session() as session:
            job_rows = (
                await session.execute(
                    select(DownloadJob.status, func.count()).group_by(DownloadJob.status)
                )
            ).all()
            contents = await session.scalar(select(func.count()).select_from(Content)) or 0
            accepted = await session.scalar(select(func.count()).select_from(AcceptedEntry)) or 0
            blacklisted = (
                await session.scalar(select(func.count()).select_from(BlacklistEntry)) or 0
            )
            failed = (
                await session.scalar(
                    select(func.count())
                    .select_from(DownloadJob)
                    .where(DownloadJob.status == DownloadStatus.FAILED)
                )
                or 0
            )

        jobs = {status_value.value: int(count) for status_value, count in job_rows}
        snapshot = metrics.snapshot() if metrics is not None else {}  # type: ignore[attr-defined]
        tasks = supervisor.snapshot() if supervisor is not None else {}  # type: ignore[attr-defined]
        return {
            "status": "ok",
            "downloaders": sorted(settings.downloaders.enabled_names()),
            "contents": int(contents),
            "accepted": int(accepted),
            "blacklisted": int(blacklisted),
            "failed_jobs": int(failed),
            "jobs": jobs,
            "tasks": tasks,
            "metrics": snapshot,
        }

    return app
