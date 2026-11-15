"""Bot wiring — which Bot polls, which Bot only sends.

Telegram delivers a ``callback_query`` only to the Bot that sent the message. A card
sent by the subscription Bot therefore produces callbacks in *its* getUpdates
stream; if only the manual Bot polls, every button press is silently dropped. That
was a real deployment bug, so the wiring is now an explicit, testable plan:

* the manual Bot polls its own dispatcher (commands + magnet ingest);
* the subscription Bot polls its own dispatcher (card callbacks only);
* the subscription Bot is also the one that sends cards and deletes message groups,
  so the sender and the callback receiver are always the same identity.
"""

from __future__ import annotations

from dataclasses import dataclass

from aiogram import Dispatcher, Router

from app.config import Settings

MANUAL_BOT = "manual-bot"
SUBSCRIPTION_BOT = "subscription-bot"


@dataclass(frozen=True, slots=True)
class BotBinding:
    #: Task name, also used in startup logs.
    name: str
    #: ``manual`` or ``subscription``.
    role: str
    #: True when this Bot runs ``start_polling`` (i.e. it receives updates).
    polls: bool
    sends_cards: bool
    deletes_messages: bool
    purposes: tuple[str, ...]


def bot_bindings(settings: Settings) -> list[BotBinding]:
    """The Bot plan for this configuration (pure, so it can be asserted)."""

    bindings = [
        BotBinding(
            name=MANUAL_BOT,
            role="manual",
            polls=True,
            sends_cards=False,
            deletes_messages=False,
            purposes=("commands", "manual-ingest"),
        )
    ]
    if settings.telegram.subscription_bot_token is not None:
        bindings.append(
            BotBinding(
                name=SUBSCRIPTION_BOT,
                role="subscription",
                # Must poll: its own cards' callbacks are delivered here, nowhere else.
                polls=True,
                sends_cards=True,
                deletes_messages=True,
                purposes=("card-callbacks",),
            )
        )
    return bindings


def wire_dispatchers(
    *,
    manual_router: Router,
    subscription_router: Router | None = None,
) -> tuple[Dispatcher, Dispatcher | None]:
    """One dispatcher per Bot, each holding only its own routers."""

    manual = Dispatcher(name="manual")
    manual.include_router(manual_router)

    subscription: Dispatcher | None = None
    if subscription_router is not None:
        subscription = Dispatcher(name="subscription")
        subscription.include_router(subscription_router)
    return manual, subscription


def polling_summary(settings: Settings) -> dict[str, object]:
    """What ``app.ready`` logs, so an operator can see who polls and who only sends."""

    bindings = bot_bindings(settings)
    return {
        "polling_bots": {b.name: b.role for b in bindings if b.polls},
        "send_only_bots": [b.name for b in bindings if not b.polls],
        "sends_cards": [b.name for b in bindings if b.sends_cards],
        "deletes_messages": [b.name for b in bindings if b.deletes_messages],
    }
