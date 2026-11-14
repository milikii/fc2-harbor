"""Application entry point — spec 19 (single container, graceful shutdown).

Startup order matters:

1. load and validate configuration (fail fast, before any I/O),
2. configure logging, apply Alembic migrations,
3. build downloader registry and reconcile unfinished jobs with qBittorrent,
4. start the manual bot (long polling), the monitor loop and the health server,
5. on SIGTERM/SIGINT stop polling, stop the monitor, close network resources and
   the database. No job is left half-advanced: every write is transactional.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from types import FrameType

import uvicorn
from aiogram import Bot, Dispatcher

from app.api.health import create_health_app
from app.bots.commands import build_commands_router
from app.bots.manual import PendingSubmissionStore, build_manual_router
from app.config import ConfigError, Settings
from app.db.session import Database, migrate_async
from app.downloaders.coordinator import DownloaderRegistry
from app.logging import configure_logging, get_logger
from app.organizer.monitor import DownloadMonitor, reconcile_startup, run_monitor_loop
from app.organizer.mover import Organizer
from app.services.gate import ContentGate
from app.services.jobs import JobSubmitter
from app.services.manual_ingest import ManualIngestService
from app.services.notify import TelegramAdminNotifier

_log = get_logger("main")


async def run(settings: Settings) -> None:
    configure_logging(level=settings.app.log_level, json_logs=settings.app.log_json)
    _log.info(
        "app.starting",
        timezone=settings.app.timezone,
        log_level=settings.app.log_level,
        downloaders=sorted(settings.downloaders.enabled_names()),
    )

    if settings.telegram.subscription_bot_token is None:
        _log.warning(
            "app.subscription_bot_disabled",
            reason="token not configured (delivered in M4)",
        )

    db = Database(settings.app.database_url)
    registry = DownloaderRegistry.from_settings(settings)
    bot: Bot | None = None
    dispatcher: Dispatcher | None = None
    server: uvicorn.Server | None = None
    stop_event = asyncio.Event()

    try:
        await migrate_async(settings.app.database_url)
        await db.ping()

        organizer = Organizer(
            verify_sha256=settings.storage.verify_sha256_on_copy,
            fsync=settings.storage.fsync_on_copy,
            chunk_bytes=settings.storage.copy_chunk_bytes,
        )
        gate = ContentGate()
        submitter = JobSubmitter(db=db, registry=registry, settings=settings)

        bot = Bot(token=settings.telegram.manual_bot_token.get_secret_value())
        notifier = TelegramAdminNotifier(bot, settings.telegram.allowed_user_ids)
        monitor = DownloadMonitor(
            db=db,
            registry=registry,
            organizer=organizer,
            settings=settings,
            notifier=notifier,
        )

        await reconcile_startup(db=db, submitter=submitter, settings=settings)

        ingest = ManualIngestService(db=db, gate=gate, submitter=submitter, settings=settings)
        store = PendingSubmissionStore()
        dispatcher = Dispatcher()
        dispatcher.include_router(
            build_commands_router(
                db=db,
                submitter=submitter,
                allowed_user_ids=settings.telegram.allowed_user_ids,
            )
        )
        dispatcher.include_router(
            build_manual_router(
                ingest=ingest,
                store=store,
                allowed_user_ids=settings.telegram.allowed_user_ids,
            )
        )

        health_app = create_health_app(db=db, settings=settings)
        server = uvicorn.Server(
            uvicorn.Config(
                health_app,
                host=settings.app.health_host,
                port=settings.app.health_port,
                log_config=None,
                access_log=False,
                lifespan="on",
            )
        )

        _install_signal_handlers(stop_event)

        tasks = [
            asyncio.create_task(server.serve(), name="health-server"),
            asyncio.create_task(
                dispatcher.start_polling(
                    bot, allowed_updates=dispatcher.resolve_used_update_types()
                ),
                name="manual-bot",
            ),
            asyncio.create_task(
                run_monitor_loop(
                    monitor,
                    interval_seconds=settings.downloaders.qbittorrent.poll_interval_seconds,
                    stop_event=stop_event,
                ),
                name="monitor",
            ),
        ]
        _log.info("app.ready", tasks=[task.get_name() for task in tasks])

        await stop_event.wait()
        _log.info("app.stopping")

        if server is not None:
            server.should_exit = True
        if dispatcher is not None:
            with contextlib.suppress(RuntimeError):
                await dispatcher.stop_polling()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    finally:
        if bot is not None:
            with contextlib.suppress(Exception):
                await bot.session.close()
        with contextlib.suppress(Exception):
            await registry.close_all()
        with contextlib.suppress(Exception):
            await db.dispose()
        _log.info("app.stopped")


def _install_signal_handlers(stop_event: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()

    def _handler(signum: int, _frame: FrameType | None) -> None:
        _log.info("app.signal", signal=signal.Signals(signum).name)
        stop_event.set()

    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError, RuntimeError):
            loop.add_signal_handler(sig, _handler, sig, None)


def main() -> int:
    try:
        settings = Settings.load()
    except ConfigError as exc:
        # Fail fast with a readable message; logging is not configured yet.
        print(f"configuration error:\n{exc}")
        return 2
    try:
        asyncio.run(run(settings))
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
