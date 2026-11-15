"""Structured logging with mandatory secret redaction.

Spec references: section 21 (structured fields), section 20 (secrets must be
redacted in logs), section 8.3 (record HTTP status, domain, provider, proxy and
latency, never credentials).

Every log record goes through :func:`redact_processor`, which

* masks values of keys that look like credentials (token, password, secret,
  api_key, cookie, authorization, session …);
* masks ``user:password@`` credentials inside any URL string;
* masks :class:`pydantic.SecretStr` instances;
* recurses into nested mappings and sequences.

The redactor is a pure function so it can be unit tested without configuring
structlog.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterator, Mapping, MutableMapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import structlog
from pydantic import SecretStr

REDACTED = "***"

#: Key names whose values are never logged. Matched case-insensitively as a
#: substring, so ``bot_token``, ``api_key`` and ``authorization_header`` all hit.
SENSITIVE_KEY_RE = re.compile(
    r"(token|passw|secret|api[_-]?key|cookie|authorization|auth|session|credential)",
    re.IGNORECASE,
)

#: ``scheme://user:password@host`` → ``scheme://***:***@host``
URL_CREDENTIALS_RE = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://[^/\s@]+@")

#: Telegram bot token embedded in a URL path, e.g. ``/bot123456:AA.../``.
TELEGRAM_BOT_PATH_RE = re.compile(r"/bot\d+:[A-Za-z0-9_-]+/")

#: Magnet links are long but not secret; they are truncated instead of masked.
MAX_VALUE_LENGTH = 512


def _redact_string(value: str) -> str:
    redacted = URL_CREDENTIALS_RE.sub(lambda m: f"{m.group('scheme')}://***:***@", value)
    redacted = TELEGRAM_BOT_PATH_RE.sub("/bot***/", redacted)
    if len(redacted) > MAX_VALUE_LENGTH:
        return redacted[:MAX_VALUE_LENGTH] + "…(truncated)"
    return redacted


def redact_text(value: str) -> str:
    """Public single-string redaction for values that end up in log fields."""

    return _redact_string(value)


#: Exception text is useful, but it can be long and may carry a URL or token.
ERROR_DETAIL_MAX = 300


def error_detail(exc: BaseException, limit: int = ERROR_DETAIL_MAX) -> str:
    """The exception message, whitespace-collapsed, redacted and truncated.

    Logging only ``type(exc).__name__`` loses the one line that explains the failure
    (``message to delete not found``), which is exactly what makes an incident
    diagnosable without reproducing it by hand.
    """

    text = redact_text(" ".join(str(exc).split()))
    if not text:
        return type(exc).__name__
    return text[:limit]


def _redact_value(value: Any, *, key: str | None = None) -> Any:
    if isinstance(value, SecretStr):
        return REDACTED
    if key is not None and SENSITIVE_KEY_RE.search(key):
        return REDACTED
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, Mapping):
        return {str(k): _redact_value(v, key=str(k)) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return type(value)(_redact_value(item) for item in value)
    return value


def redact_processor(
    logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor that removes credentials from an event dict."""

    for key in list(event_dict):
        event_dict[key] = _redact_value(event_dict[key], key=key)
    return event_dict


def _add_log_level(
    _logger: Any, method_name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Record the structlog level without depending on stdlib internals."""

    event_dict["level"] = method_name
    return event_dict


class _FileSink:
    """Mirror redacted records into a rotating file under /app/data/logs.

    Docker's json-file driver already keeps stdout, but the spec asks for logs that
    live on a volume and can be backed up (spec 19 / 24), so the same redacted JSON
    line is also appended to a rotating file.
    """

    def __init__(
        self, path: str | Path, *, max_bytes: int = 10 * 1024 * 1024, backups: int = 3
    ) -> None:
        import logging.handlers

        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        self._logger = logging.getLogger("fc2_harbor.file_sink")
        self._logger.setLevel(logging.INFO)
        self._logger.propagate = False
        if not self._logger.handlers:
            handler = logging.handlers.RotatingFileHandler(
                target, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            self._logger.addHandler(handler)

    def __call__(
        self, _logger: Any, _method_name: str, event_dict: MutableMapping[str, Any]
    ) -> MutableMapping[str, Any]:
        import json

        # Redact again on the way to disk: the file must stay safe even if the
        # processor order ever changes.
        safe = redact_processor(None, "", dict(event_dict))
        self._logger.info(json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str))
        return event_dict


def configure_logging(
    *,
    level: str = "INFO",
    json_logs: bool = True,
    logger_name: str = "fc2_harbor",
    log_file: str | Path | None = None,
) -> None:
    """Configure structlog once at process start."""

    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(level=log_level, format="%(message)s")

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True, key="timestamp"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        redact_processor,
    ]
    if log_file:
        shared_processors.append(_FileSink(log_file))

    renderer: Any = (
        structlog.processors.JSONRenderer(sort_keys=True)
        if json_logs
        else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*shared_processors, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.WriteLoggerFactory(),
        cache_logger_on_first_use=True,
    )
    _ = logger_name  # reserved for future per-component loggers


def get_logger(component: str, **initial_values: Any) -> structlog.stdlib.BoundLogger:
    """Return a bound logger tagged with the component name."""

    return structlog.get_logger(component=component, **initial_values)


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    """Bind fields (content_id, canonical_key, job_id …) for a code block."""

    structlog.contextvars.bind_contextvars(**fields)
    try:
        yield
    finally:
        structlog.contextvars.unbind_contextvars(*fields)


#: The structured fields required by spec section 21, kept in one place so that
#: call sites and tests agree on the spelling.
CONTEXT_FIELDS: tuple[str, ...] = (
    "content_id",
    "canonical_key",
    "release_id",
    "job_id",
    "source",
    "provider",
    "downloader",
    "proxy_profile",
    "domain",
    "status",
    "latency_ms",
    "retry_count",
    "error_code",
)
