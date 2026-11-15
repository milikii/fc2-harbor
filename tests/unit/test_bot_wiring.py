"""Bot wiring — Telegram delivers a callback only to the Bot that sent the message.

The deployment bug this covers: cards were sent by the subscription Bot while only
the manual Bot polled, so every button press was silently dropped. The wiring is now
a data structure that can be asserted.
"""

from __future__ import annotations

from pathlib import Path

from aiogram import Router
from app.bots.wiring import (
    MANUAL_BOT,
    SUBSCRIPTION_BOT,
    bot_bindings,
    polling_summary,
    wire_dispatchers,
)

from tests.support import make_settings

SUB_TOKEN = "123456:SUBSCRIPTION"


def settings_with_two_bots(tmp_path: Path):
    return make_settings(
        tmp_path,
        telegram={"manual_bot_token": "654321:MANUAL", "subscription_bot_token": SUB_TOKEN},
    )


def test_both_bots_poll_when_both_tokens_exist(tmp_path: Path) -> None:
    bindings = bot_bindings(settings_with_two_bots(tmp_path))
    names = [binding.name for binding in bindings]

    assert names == [MANUAL_BOT, SUBSCRIPTION_BOT]
    assert all(binding.polls for binding in bindings)
    # Two polling tasks: each Bot receives its own updates.
    assert sum(1 for binding in bindings if binding.polls) == 2


def test_card_sender_must_be_the_callback_receiver(tmp_path: Path) -> None:
    """The guard for the real bug: whoever sends cards must also poll."""

    bindings = bot_bindings(settings_with_two_bots(tmp_path))
    senders = [binding for binding in bindings if binding.sends_cards]

    assert len(senders) == 1
    sender = senders[0]
    assert sender.name == SUBSCRIPTION_BOT
    assert sender.polls is True, "the Bot that sends cards must poll its own callbacks"
    assert "card-callbacks" in sender.purposes
    # Deleting a message also requires the Bot that sent it.
    assert sender.deletes_messages is True


def test_without_subscription_token_only_the_manual_bot_runs(tmp_path: Path) -> None:
    settings = make_settings(tmp_path, telegram={"subscription_bot_token": None})
    bindings = bot_bindings(settings)

    assert [binding.name for binding in bindings] == [MANUAL_BOT]
    assert bindings[0].polls is True
    assert bindings[0].sends_cards is False
    assert all(not binding.sends_cards for binding in bindings)


def test_dispatchers_do_not_share_routers() -> None:
    manual_router = Router(name="manual-commands")
    subscription_router = Router(name="subscription")

    manual_dispatcher, subscription_dispatcher = wire_dispatchers(
        manual_router=manual_router, subscription_router=subscription_router
    )

    manual_names = [router.name for router in manual_dispatcher.sub_routers]
    assert "manual-commands" in manual_names
    assert "subscription" not in manual_names, "callback router must not live on the manual bot"

    assert subscription_dispatcher is not None
    subscription_names = [router.name for router in subscription_dispatcher.sub_routers]
    assert subscription_names == ["subscription"]


def test_no_subscription_dispatcher_without_a_router() -> None:
    manual_dispatcher, subscription_dispatcher = wire_dispatchers(
        manual_router=Router(name="manual-commands")
    )
    assert subscription_dispatcher is None
    assert [router.name for router in manual_dispatcher.sub_routers] == ["manual-commands"]


def test_polling_summary_is_explicit_for_logs(tmp_path: Path) -> None:
    summary = polling_summary(settings_with_two_bots(tmp_path))

    assert summary["polling_bots"] == {MANUAL_BOT: "manual", SUBSCRIPTION_BOT: "subscription"}
    assert summary["send_only_bots"] == []
    assert summary["sends_cards"] == [SUBSCRIPTION_BOT]
    assert summary["deletes_messages"] == [SUBSCRIPTION_BOT]


def test_polling_summary_with_one_bot(tmp_path: Path) -> None:
    summary = polling_summary(make_settings(tmp_path, telegram={"subscription_bot_token": None}))

    assert summary["polling_bots"] == {MANUAL_BOT: "manual"}
    assert summary["sends_cards"] == []
