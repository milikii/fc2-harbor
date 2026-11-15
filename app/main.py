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
from pathlib import Path
from types import FrameType

import uvicorn
from aiogram import Bot, Dispatcher

from app.api.health import create_health_app
from app.bots.commands import build_commands_router
from app.bots.manual import PendingSubmissionStore, build_manual_router
from app.bots.subscription import build_subscription_router
from app.bots.wiring import polling_summary, wire_dispatchers
from app.config import ConfigError, Settings, applied_env_overrides
from app.db.session import Database, migrate_async
from app.downloaders.coordinator import DownloaderRegistry
from app.logging import configure_logging, get_logger
from app.network.clients import HttpClient
from app.network.image_fetcher import ImageFetcher
from app.network.proxy import ProxyRouter
from app.organizer.monitor import DownloadMonitor, reconcile_startup, run_monitor_loop
from app.organizer.mover import Organizer
from app.providers.coordinator import MetadataCoordinator, build_providers
from app.services.acceptance import SubscriptionActions
from app.services.admin import AdminService
from app.services.gate import ContentGate
from app.services.jobs import JobSubmitter
from app.services.manual_ingest import ManualIngestService
from app.services.metadata import MetadataService, build_translator, derive_image_allowlist
from app.services.metrics import Metrics
from app.services.normalize import Fc2CodeRule
from app.services.notifications import MessageDeleter
from app.services.notify import TelegramAdminNotifier
from app.services.publisher import Publisher, run_publisher_loop
from app.services.releases import ReleaseRegistrar
from app.services.runtime import RuntimeState
from app.services.state import record_event
from app.services.subscription import SubscriptionPoller
from app.services.telegram_assets import TelegramAssetUploader
from app.sources.nyaa import NyaaSource

_log = get_logger("main")


async def run(settings: Settings) -> None:
    configure_logging(
        level=settings.app.log_level,
        json_logs=settings.app.log_json,
        log_file=settings.app.log_file,
    )
    _log.info(
        "app.starting",
        timezone=settings.app.timezone,
        log_level=settings.app.log_level,
        downloaders=sorted(settings.downloaders.enabled_names()),
        env_overrides=applied_env_overrides(),
    )

    if settings.telegram.subscription_bot_token is None:
        _log.warning(
            "app.subscription_bot_disabled",
            reason="token not configured (delivered in M4)",
        )

    db = Database(settings.app.database_url)
    metrics = Metrics()
    runtime = RuntimeState()
    registry = DownloaderRegistry.from_settings(settings)
    nyaa_source: NyaaSource | None = None
    http_client: HttpClient | None = None
    image_fetcher: ImageFetcher | None = None
    translator = None
    subscription_bot: Bot | None = None
    bot: Bot | None = None
    dispatcher: Dispatcher | None = None
    server: uvicorn.Server | None = None
    stop_event = asyncio.Event()

    try:
        await migrate_async(settings.app.database_url)
        await db.ping()
        await _audit_provider_configuration(db, settings)

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

        await reconcile_startup(db=db, submitter=submitter, settings=settings, notifier=notifier)

        nyaa_source = NyaaSource(settings=settings)
        registrar = ReleaseRegistrar(db=db, gate=gate)
        poller = SubscriptionPoller(
            db=db, source=nyaa_source, registrar=registrar, settings=settings, state=runtime
        )

        proxy_router = ProxyRouter(settings.proxy_profiles)
        http_client = HttpClient(
            metrics=metrics,
            user_agent=settings.nyaa.user_agent,
            timeout_seconds=settings.nyaa.timeout_seconds,
            retries=settings.nyaa.retries,
            per_domain_concurrency=settings.nyaa.concurrency,
            max_retry_after_seconds=settings.nyaa.max_retry_after_seconds,
        )
        providers, fallback = build_providers(settings, client=http_client, router=proxy_router)
        coordinator = MetadataCoordinator(
            providers=providers, settings=settings, nyaa_fallback=fallback, metrics=metrics
        )
        image_fetcher = ImageFetcher(
            cache_dir=Path(settings.image_fetch.cache_dir),
            allowed_hosts=derive_image_allowlist(settings),
            max_bytes=settings.image_fetch.max_bytes,
            user_agent=settings.nyaa.user_agent,
        )
        translator = build_translator(settings)
        metadata_service = MetadataService(
            db=db,
            coordinator=coordinator,
            fetcher=image_fetcher,
            settings=settings,
            router=proxy_router,
            translator=translator,
            metrics=metrics,
        )

        admin = AdminService(
            db=db,
            metrics=metrics,
            rule=Fc2CodeRule(settings.fc2.code_min_digits, settings.fc2.code_max_digits),
        )

        tasks_extra: list[asyncio.Task[None]] = []
        actions = SubscriptionActions(db=db, gate=gate, submitter=submitter, settings=settings)
        deleter: MessageDeleter | None = None
        publisher: Publisher | None = None

        # Card callbacks are delivered only to the Bot that sent the card, so the
        # subscription Bot gets its own dispatcher and its own polling task.
        subscription_router = None
        if settings.telegram.subscription_bot_token is not None:
            subscription_bot = Bot(
                token=settings.telegram.subscription_bot_token.get_secret_value()
            )
            uploader = TelegramAssetUploader(db=db, bot_name="subscription")
            publisher = Publisher(
                db=db,
                metadata=metadata_service,
                uploader=uploader,
                bot=subscription_bot,
                settings=settings,
                metrics=metrics,
            )
            deleter = MessageDeleter(db=db, bot=subscription_bot)
            subscription_router = build_subscription_router(
                db=db,
                actions=actions,
                deleter=deleter,
                settings=settings,
                allowed_user_ids=settings.telegram.allowed_user_ids,
            )
            tasks_extra.append(
                asyncio.create_task(
                    run_publisher_loop(publisher, interval_seconds=60, stop_event=stop_event),
                    name="publisher",
                )
            )
            tasks_extra.append(asyncio.create_task(deleter.run(stop_event), name="message-deleter"))

        ingest = ManualIngestService(db=db, gate=gate, submitter=submitter, settings=settings)
        store = PendingSubmissionStore()
        manual_router = build_commands_router(
            db=db,
            submitter=submitter,
            allowed_user_ids=settings.telegram.allowed_user_ids,
            admin=admin,
            state=runtime,
        )
        manual_router.include_router(
            build_manual_router(
                ingest=ingest,
                store=store,
                allowed_user_ids=settings.telegram.allowed_user_ids,
            )
        )
        dispatcher, subscription_dispatcher = wire_dispatchers(
            manual_router=manual_router,
            subscription_router=subscription_router,
        )

        health_app = create_health_app(db=db, settings=settings, metrics=metrics)
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
        if settings.subscription.polling_enabled:
            tasks.append(asyncio.create_task(poller.run(stop_event), name="subscription-poll"))
        else:
            _log.warning(
                "app.subscription_polling_disabled",
                reason="subscription.polling_enabled=false",
            )
        if subscription_dispatcher is not None and subscription_bot is not None:
            # The Bot that sent the card must be the Bot that receives its callbacks.
            tasks.append(
                asyncio.create_task(
                    subscription_dispatcher.start_polling(
                        subscription_bot, allowed_updates=["callback_query"]
                    ),
                    name="subscription-bot",
                )
            )
        tasks.extend(tasks_extra)
        _log.info(
            "app.ready",
            tasks=[task.get_name() for task in tasks],
            **polling_summary(settings),
            subscription_polling=settings.subscription.polling_enabled,
            max_push_per_round=settings.subscription.max_push_per_round,
            initial_backfill_silent=settings.nyaa.initial_backfill_silent,
        )

        await stop_event.wait()
        _log.info("app.stopping")

        if server is not None:
            server.should_exit = True
        for running_dispatcher in (dispatcher, subscription_dispatcher):
            if running_dispatcher is not None:
                with contextlib.suppress(RuntimeError):
                    await running_dispatcher.stop_polling()
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    finally:
        if subscription_bot is not None:
            with contextlib.suppress(Exception):
                await subscription_bot.session.close()
        if translator is not None:
            with contextlib.suppress(Exception):
                await translator.aclose()
        if image_fetcher is not None:
            with contextlib.suppress(Exception):
                await image_fetcher.aclose()
        if http_client is not None:
            with contextlib.suppress(Exception):
                await http_client.aclose()
        if bot is not None:
            with contextlib.suppress(Exception):
                await bot.session.close()
        if nyaa_source is not None:
            with contextlib.suppress(Exception):
                await nyaa_source.aclose()
        with contextlib.suppress(Exception):
            await registry.close_all()
        with contextlib.suppress(Exception):
            await db.dispose()
        _log.info("app.stopped")


async def _audit_provider_configuration(db: Database, settings: Settings) -> None:
    """Record an explicitly relaxed provider configuration (spec 7.1).

    Strict mode already failed at config load. Reaching this point with disabled
    providers means the operator set ``provider.strict_enabled=false`` on purpose,
    so the deviation is logged loudly and written to the audit trail.
    """

    disabled = settings.provider.disabled_providers()
    if not disabled:
        return
    _log.warning(
        "provider.disabled",
        providers=disabled,
        strict_enabled=settings.provider.strict_enabled,
        detail="provider.strict_enabled=false; spec 7.1 expects all nine enabled",
    )
    async with db.transaction() as session:
        await record_event(
            session,
            "provider.disabled",
            actor="system",
            reason=",".join(disabled),
        )


def planned_tasks(settings: Settings) -> list[str]:
    """Names of the supervised tasks, derived from configuration only.

    Used by startup logging and by the tests: with ``subscription.polling_enabled``
    false the poller must not appear at all, so the operator can run the manual bot
    without touching Nyaa.
    """

    names = ["health-server", "manual-bot", "monitor"]
    if settings.subscription.polling_enabled:
        names.append("subscription-poll")
    if settings.telegram.subscription_bot_token is not None:
        # The subscription Bot polls its own dispatcher for card callbacks.
        names.extend(["subscription-bot", "publisher", "message-deleter"])
    return names


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
