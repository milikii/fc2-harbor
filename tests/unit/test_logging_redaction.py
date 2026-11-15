"""Log redaction — spec 20 (tokens, passwords and proxy credentials never logged)."""

from __future__ import annotations

from typing import Any

from app.logging import REDACTED, redact_processor
from pydantic import SecretStr


def _process(event: dict[str, Any]) -> dict[str, Any]:
    return dict(redact_processor(None, "info", dict(event)))


def test_credential_keys_are_masked() -> None:
    event = _process(
        {
            "event": "login",
            "bot_token": "123456:AAbbCCdd",
            "password": "hunter2",
            "api_key": "sk-123",
            "authorization": "Bearer abc",
            "cookie": "SID=abc",
            "proxy_password": "pw",
        }
    )
    assert event["bot_token"] == REDACTED
    assert event["password"] == REDACTED
    assert event["api_key"] == REDACTED
    assert event["authorization"] == REDACTED
    assert event["cookie"] == REDACTED
    assert event["proxy_password"] == REDACTED
    assert event["event"] == "login"


def test_secretstr_is_masked() -> None:
    event = _process({"value": SecretStr("super-secret")})
    assert event["value"] == REDACTED


def test_url_credentials_are_masked_in_nested_structures() -> None:
    event = _process(
        {
            "event": "proxy",
            "routes": [
                {"proxy": "socks5h://user:pass@jp.example:1080"},
                "http://user:pw@backup.example:8080",
            ],
        }
    )
    assert "user:pass" not in str(event)
    assert event["routes"][0]["proxy"] == "socks5h://***:***@jp.example:1080"
    assert event["routes"][1] == "http://***:***@backup.example:8080"


def test_telegram_bot_token_in_url_is_masked() -> None:
    event = _process({"url": "https://api.telegram.org/bot123456:AAbbCCdd/sendPhoto"})
    assert "AAbbCCdd" not in event["url"]
    assert event["url"] == "https://api.telegram.org/bot***/sendPhoto"


def test_long_values_are_truncated_not_dropped() -> None:
    magnet = "magnet:?xt=urn:btih:" + "a" * 40 + "&dn=" + "x" * 600
    event = _process({"magnet": magnet})
    assert event["magnet"].endswith("…(truncated)")
    assert len(event["magnet"]) < len(magnet)


def test_file_sink_writes_redacted_json(tmp_path) -> None:
    from app.logging import _FileSink

    target = tmp_path / "logs" / "app.log"
    sink = _FileSink(target)
    sink(None, "info", {"event": "login", "bot_token": "secret", "canonical_key": "fc2_ppv:1"})

    line = target.read_text(encoding="utf-8").strip()
    assert "fc2_ppv:1" in line
    assert "secret" not in line
    assert line.startswith("{")


def test_regular_fields_pass_through() -> None:
    event = _process(
        {
            "event": "gate.rejected",
            "canonical_key": "fc2_ppv:1234567",
            "latency_ms": 12,
            "retry_count": 1,
            "domain": "sukebei.nyaa.si",
        }
    )
    assert event["canonical_key"] == "fc2_ppv:1234567"
    assert event["latency_ms"] == 12


def test_error_detail_keeps_the_message_and_redacts_secrets() -> None:
    """``error_code`` alone is not enough: the sentence is what explains a failure."""

    from app.logging import error_detail

    detail = error_detail(RuntimeError("Bad Request: message to delete not found"))
    assert detail == "Bad Request: message to delete not found"

    redacted = error_detail(
        RuntimeError("proxy http://user:secret@192.0.2.1:7890 refused the connection")
    )
    assert "secret" not in redacted
    assert "***:***@" in redacted

    collapsed = error_detail(RuntimeError("line one\n\n   line two"))
    assert collapsed == "line one line two"


def test_error_detail_is_truncated() -> None:
    from app.logging import ERROR_DETAIL_MAX, error_detail

    detail = error_detail(RuntimeError("x" * 1000))
    assert len(detail) == ERROR_DETAIL_MAX
    assert detail == "x" * ERROR_DETAIL_MAX


def test_error_detail_falls_back_to_the_type_when_empty() -> None:
    from app.logging import error_detail

    assert error_detail(RuntimeError("")) == "RuntimeError"
