"""Real routers, real dispatchers — the callback router must reach the card's Bot.

Unit tests cover the wiring plan; this one builds the actual routers used at
startup and asserts the split, including that the subscription router really has
callback handlers.
"""

from __future__ import annotations

from aiogram import Router
from app.bots.commands import build_commands_router
from app.bots.manual import PendingSubmissionStore, build_manual_router
from app.bots.subscription import build_subscription_router
from app.bots.wiring import wire_dispatchers
from app.config import Settings
from app.db.enums import DownloaderName
from app.db.session import Database
from app.downloaders.coordinator import DownloaderRegistry
from app.services.acceptance import SubscriptionActions
from app.services.gate import ContentGate
from app.services.jobs import JobSubmitter
from app.services.manual_ingest import ManualIngestService
from app.services.notifications import MessageDeleter

from tests.support import FakeDownloader


def build_routers(
    db: Database, settings: Settings, downloader: FakeDownloader
) -> tuple[Router, Router]:
    registry = DownloaderRegistry({DownloaderName.QBITTORRENT: downloader})
    submitter = JobSubmitter(db=db, registry=registry, settings=settings)
    gate = ContentGate()
    actions = SubscriptionActions(db=db, gate=gate, submitter=submitter, settings=settings)
    deleter = MessageDeleter(db=db, bot=object())
    subscription = build_subscription_router(
        db=db,
        actions=actions,
        deleter=deleter,
        settings=settings,
        allowed_user_ids=[111],
    )

    manual = build_commands_router(db=db, submitter=submitter, allowed_user_ids=[111])
    manual.include_router(
        build_manual_router(
            ingest=ManualIngestService(db=db, gate=gate, submitter=submitter, settings=settings),
            store=PendingSubmissionStore(),
            allowed_user_ids=[111],
        )
    )
    return manual, subscription


async def test_real_routers_are_split_by_bot(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    manual_router, subscription_router = build_routers(db, settings, fake_downloader)

    manual_dispatcher, subscription_dispatcher = wire_dispatchers(
        manual_router=manual_router, subscription_router=subscription_router
    )

    assert subscription_router.name == "subscription"
    manual_names = [router.name for router in manual_dispatcher.sub_routers]
    assert "subscription" not in manual_names
    assert "manual-commands" in manual_names

    assert subscription_dispatcher is not None
    assert [router.name for router in subscription_dispatcher.sub_routers] == ["subscription"]


async def test_subscription_router_has_callback_handlers(
    db: Database, settings: Settings, fake_downloader: FakeDownloader
) -> None:
    _manual, subscription_router = build_routers(db, settings, fake_downloader)

    handlers = subscription_router.callback_query.handlers
    assert handlers, "the subscription router must register callback_query handlers"
    assert all(handler.callback is not None for handler in handlers)
