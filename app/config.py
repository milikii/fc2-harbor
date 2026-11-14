"""Configuration loading, `${ENV_VAR}` expansion and fail-fast validation.

Spec references: section 18 (configuration), section 8.2 (proxy routing),
section 7.1 (fixed provider order), section 20 (secrets never logged).

Design notes
------------
* The YAML file is the single source of truth for structure; secrets come from
  the environment via `${VAR}` placeholders.
* A placeholder that occupies a whole scalar and is unset expands to ``None`` so
  that pydantic reports the offending field, with the variable name attached to
  the error message. A placeholder embedded inside a longer string must resolve,
  because a partially substituted URL is never valid.
* Every model forbids unknown keys: a typo in the YAML must fail at startup
  instead of being silently ignored.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

# ---------------------------------------------------------------------------
# Constants shared with the rest of the application
# ---------------------------------------------------------------------------

CATEGORIES: tuple[str, ...] = ("fc2", "jav", "cn", "western", "review")

#: Fixed provider order from spec 7.1. Never reordered by hit rate (spec 25.4).
CANONICAL_FC2_PROVIDER_ORDER: tuple[str, ...] = (
    "Fc2",
    "fc2_hub",
    "Javdb",
    "Mmtv",
    "freejavbt",
    "miss_av",
    "Javlibrary",
    "avsox",
    "Madou",
)

#: Canonical hosts from spec 7.1; used to sanity-check the configuration.
CANONICAL_PROVIDER_HOSTS: Mapping[str, str] = {
    "Fc2": "adult.contents.fc2.com",
    "fc2_hub": "javten.com",
    "Javdb": "javdb.com",
    "Mmtv": "7mmtv.sx",
    "freejavbt": "freejavbt.com",
    "miss_av": "missav123.com",
    "Javlibrary": "javlibrary.com",
    "avsox": "avsox.click",
    "Madou": "madou.club",
}

PLACEHOLDER_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

#: The only environment overrides on top of the YAML file (env > yaml).
#: Deliberately a short list: a general env layer would make nested-structure
#: validation (fixed provider order, routes) hard to reason about.
ENV_OVERRIDES: Mapping[str, tuple[str, ...]] = {
    "FC2_LOG_LEVEL": ("app", "log_level"),
    "FC2_DATABASE_URL": ("app", "database_url"),
}
_UNSAFE_URL_CHARS = re.compile(r"[\s]")


class ConfigError(RuntimeError):
    """Raised when configuration is missing, malformed or contradictory."""


# ---------------------------------------------------------------------------
# Environment expansion
# ---------------------------------------------------------------------------


def _expand_value(
    value: Any,
    env: Mapping[str, str],
    missing: list[str],
    path: str,
) -> Any:
    if isinstance(value, dict):
        return {
            key: _expand_value(item, env, missing, f"{path}.{key}" if path else str(key))
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [
            _expand_value(item, env, missing, f"{path}[{index}]")
            for index, item in enumerate(value)
        ]
    if not isinstance(value, str):
        return value

    matches = list(PLACEHOLDER_RE.finditer(value))
    if not matches:
        return value

    # Whole-value placeholder: unset variables become None and are reported.
    if len(matches) == 1 and matches[0].group(0) == value:
        name = matches[0].group(1)
        resolved = env.get(name, "")
        if resolved:
            return resolved
        missing.append(name)
        return None

    def _substitute(match: re.Match[str]) -> str:
        name = match.group(1)
        resolved = env.get(name, "")
        if not resolved:
            raise ConfigError(
                f"environment variable {name} is not set but is embedded in "
                f"configuration field {path!r}; partial substitution is not allowed"
            )
        return resolved

    return PLACEHOLDER_RE.sub(_substitute, value)


def expand_env_placeholders(
    data: Any, env: Mapping[str, str] | None = None
) -> tuple[Any, list[str]]:
    """Expand `${VAR}` placeholders recursively.

    Returns the expanded structure and the list of unset variable names that
    occupied a whole scalar value.
    """

    environment = os.environ if env is None else env
    missing: list[str] = []
    expanded = _expand_value(data, environment, missing, "")
    return expanded, sorted(set(missing))


def apply_env_overrides(data: dict[str, Any], env: Mapping[str, str]) -> list[str]:
    """Apply the small whitelist of env overrides onto the YAML mapping.

    Returns the names of the variables that were actually applied, so startup can
    log them (an override that silently changes behaviour is hard to debug).
    """

    applied: list[str] = []
    for variable, path in ENV_OVERRIDES.items():
        value = env.get(variable)
        if not value:
            continue
        target: dict[str, Any] = data
        for key in path[:-1]:
            nested = target.get(key)
            if not isinstance(nested, dict):
                nested = {}
                target[key] = nested
            target = nested
        target[path[-1]] = value
        applied.append(variable)
    return sorted(applied)


def applied_env_overrides(env: Mapping[str, str] | None = None) -> list[str]:
    environment = os.environ if env is None else env
    return sorted(variable for variable in ENV_OVERRIDES if environment.get(variable))


def load_yaml_file(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(f"configuration file not found: {path}") from exc
    except OSError as exc:
        raise ConfigError(f"cannot read configuration file {path}: {exc}") from exc

    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"invalid YAML in {path}: {exc}") from exc

    if parsed is None:
        raise ConfigError(f"configuration file {path} is empty")
    if not isinstance(parsed, dict):
        raise ConfigError(f"configuration file {path} must contain a mapping at top level")
    return parsed


# ---------------------------------------------------------------------------
# Reusable validators / helpers
# ---------------------------------------------------------------------------


def _require_absolute_path(value: str, field: str) -> str:
    if not value or not value.startswith("/"):
        raise ValueError(f"{field} must be an absolute container path, got {value!r}")
    if "\x00" in value:
        raise ValueError(f"{field} must not contain NUL bytes")
    if "/../" in value or value.endswith("/.."):
        raise ValueError(f"{field} must not contain '..' segments, got {value!r}")
    normalized = value.rstrip("/") or "/"
    return normalized


def _validate_http_url(value: str, field: str) -> str:
    if not value:
        raise ValueError(f"{field} must not be empty")
    if _UNSAFE_URL_CHARS.search(value):
        raise ValueError(f"{field} must not contain whitespace")
    lowered = value.lower()
    if not (lowered.startswith("http://") or lowered.startswith("https://")):
        raise ValueError(f"{field} must be an http(s) URL, got {value!r}")
    return value.rstrip("/")


def _split_csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


class _FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


# ---------------------------------------------------------------------------
# Sections
# ---------------------------------------------------------------------------


class AppSection(_FrozenModel):
    timezone: str = "Asia/Shanghai"
    database_url: str = "sqlite:////app/data/app.db"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    log_json: bool = True
    # Bound inside the container network only; compose publishes no ports.
    health_host: str = "0.0.0.0"  # noqa: S104
    health_port: int = Field(default=8080, ge=1, le=65535)

    @field_validator("timezone")
    @classmethod
    def _check_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone {value!r}") from exc
        return value

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: str) -> str:
        if not value:
            raise ValueError("database_url must not be empty")
        if not value.startswith("sqlite"):
            # Spec 3: SQLite only for the first version; a single container owns
            # the database file. Anything else is a deliberate future change.
            raise ValueError(
                "database_url must be a sqlite URL for the first version, "
                f"got {value.split(':', 1)[0]!r}"
            )
        if ":memory:" in value:
            raise ValueError("database_url must point at a file, not an in-memory database")
        return value


class TelegramSection(_FrozenModel):
    subscription_bot_token: SecretStr | None = None
    manual_bot_token: SecretStr = Field(...)
    allowed_user_ids: list[int] = Field(min_length=1)
    proxy: str = "direct"

    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _parse_ids(cls, value: Any) -> Any:
        if isinstance(value, str):
            items = _split_csv(value)
            if not items:
                raise ValueError("allowed_user_ids must contain at least one Telegram user id")
            return items
        if isinstance(value, int):
            return [value]
        return value

    @field_validator("allowed_user_ids")
    @classmethod
    def _check_ids(cls, value: list[int]) -> list[int]:
        for user_id in value:
            if user_id <= 0:
                raise ValueError(f"invalid Telegram user id {user_id}")
        return list(dict.fromkeys(value))

    @field_validator("manual_bot_token", "subscription_bot_token", mode="before")
    @classmethod
    def _non_empty_token(cls, value: Any) -> Any:
        if isinstance(value, str) and not value.strip():
            raise ValueError("bot token must not be blank; unset the key instead")
        return value


class Fc2Section(_FrozenModel):
    """FC2 number recognition (spec 6.3).

    The digit range is configuration, not a constant: the spec lists the formats
    but not the width, so operators can widen it without a code change.
    """

    code_min_digits: int = Field(default=5, ge=3, le=9)
    code_max_digits: int = Field(default=8, ge=3, le=12)

    @model_validator(mode="after")
    def _check_range(self) -> Fc2Section:
        if self.code_min_digits > self.code_max_digits:
            raise ValueError(
                "fc2.code_min_digits must not exceed fc2.code_max_digits "
                f"({self.code_min_digits} > {self.code_max_digits})"
            )
        return self


class NyaaSection(_FrozenModel):
    base_url: str = "https://sukebei.nyaa.si"
    query: str = "FC2"
    scope: Literal["site"] = "site"
    poll_interval_seconds: int = Field(default=180, ge=120, le=300)
    initial_lookback_hours: int = Field(default=24, ge=1, le=168)
    proxy: str = "direct"
    #: Random jitter added to every poll (spec 6.2).
    jitter_seconds: int = Field(default=20, ge=0, le=60)

    #: RSS first (spec 6.2); the query must stay site-wide, never u=offkab.
    rss_path: str = "/?page=rss&q=FC2&c=0_0&f=0"
    #: Search page used to catch up when one round overflows the RSS window.
    search_path: str = "/?f=0&c=0_0&q=FC2&p={page}"
    rss_page_size: int = Field(default=75, ge=10, le=200)
    search_max_pages: int = Field(default=5, ge=1, le=20)
    #: How many item ids are remembered for the overlap re-check (spec 6.2).
    overlap_item_ids: int = Field(default=200, ge=10, le=2000)
    #: Trackers appended to magnets built from an RSS info hash.
    trackers: list[str] = Field(default_factory=list)
    user_agent: str = "fc2-harbor/0.1 (+https://github.com/milikii/fc2-harbor)"
    timeout_seconds: float = Field(default=20.0, gt=0, le=120)
    retries: int = Field(default=2, ge=0, le=5)
    #: Per-domain concurrency (spec 6.2 / 8.3).
    concurrency: int = Field(default=2, ge=1, le=4)
    max_retry_after_seconds: float = Field(default=60.0, ge=0, le=600)

    @field_validator("base_url")
    @classmethod
    def _check_base_url(cls, value: str) -> str:
        return _validate_http_url(value, "nyaa.base_url")

    @field_validator("query")
    @classmethod
    def _check_query(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("nyaa.query must not be empty")
        if "u=" in value:
            raise ValueError(
                "nyaa.query must stay site-wide; uploader filters such as u=offkab "
                "are forbidden (spec 6.1)"
            )
        return value

    @field_validator("rss_path")
    @classmethod
    def _check_rss_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("nyaa.rss_path must be an absolute site path")
        if "u=" in value:
            raise ValueError("nyaa.rss_path must not restrict the uploader (spec 6.1)")
        return value

    @field_validator("search_path")
    @classmethod
    def _check_search_path(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("nyaa.search_path must be an absolute site path")
        if "{page}" not in value:
            raise ValueError("nyaa.search_path must contain the {page} placeholder")
        if "u=" in value:
            raise ValueError("nyaa.search_path must not restrict the uploader (spec 6.1)")
        return value

    @field_validator("trackers")
    @classmethod
    def _check_trackers(cls, value: list[str]) -> list[str]:
        for tracker in value:
            if not tracker.startswith(("udp://", "http://", "https://")):
                raise ValueError(f"invalid tracker url: {tracker!r}")
        return value


class ProviderSourceConfig(_FrozenModel):
    host: str
    enabled: bool = True
    proxy: str = "direct"
    timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    concurrency: int = Field(default=2, ge=1, le=4)
    retries: int = Field(default=2, ge=0, le=5)

    @field_validator("host")
    @classmethod
    def _check_host(cls, value: str) -> str:
        host = value.strip().lower()
        if not host or "/" in host or ":" in host:
            raise ValueError(f"provider host must be a bare hostname, got {value!r}")
        return host


class ProviderSection(_FrozenModel):
    priorities: dict[str, list[str]]
    sources: dict[str, ProviderSourceConfig]
    #: Spec 7.1 wants all nine enabled. Strict by default; an operator can
    #: explicitly relax it to switch off one broken source, which is then
    #: logged and audited at startup.
    strict_enabled: bool = True

    @field_validator("priorities")
    @classmethod
    def _check_priorities(cls, value: dict[str, list[str]]) -> dict[str, list[str]]:
        if "jav_fc2" not in value:
            raise ValueError("provider.priorities.jav_fc2 is required (spec 7.1)")
        order = tuple(value["jav_fc2"])
        if order != CANONICAL_FC2_PROVIDER_ORDER:
            raise ValueError(
                "provider.priorities.jav_fc2 must keep the fixed order "
                f"{list(CANONICAL_FC2_PROVIDER_ORDER)}, got {list(order)}; "
                "the order must never be rewritten by hit rate (spec 7.1 / 25.4)"
            )
        return value

    @model_validator(mode="after")
    def _check_sources(self) -> ProviderSection:
        missing = [name for name in CANONICAL_FC2_PROVIDER_ORDER if name not in self.sources]
        if missing:
            raise ValueError(f"provider.sources is missing entries: {missing}")
        disabled = self.disabled_providers()
        if disabled and self.strict_enabled:
            raise ValueError(
                f"all nine FC2 providers must stay enabled (spec 7.1), disabled: {disabled}; "
                "set provider.strict_enabled=false only to switch a broken source off "
                "temporarily (this is logged and audited at startup)"
            )
        mismatched = {
            name: (self.sources[name].host, CANONICAL_PROVIDER_HOSTS[name])
            for name in CANONICAL_FC2_PROVIDER_ORDER
            if self.sources[name].host != CANONICAL_PROVIDER_HOSTS[name]
        }
        if mismatched:
            raise ValueError(
                f"provider host mismatch (configured, expected): {mismatched}; "
                "adapters are written against these hosts"
            )
        return self

    def disabled_providers(self) -> list[str]:
        """Canonical providers that are switched off, in priority order."""

        return [name for name in CANONICAL_FC2_PROVIDER_ORDER if not self.sources[name].enabled]


class ProxyProfile(_FrozenModel):
    type: Literal["direct", "http", "socks5h"]
    url: str | None = None
    region: str | None = None

    @model_validator(mode="after")
    def _check_url(self) -> ProxyProfile:
        if self.type == "direct":
            if self.url:
                raise ValueError("direct proxy profile must not define a url")
            return self
        if not self.url:
            raise ValueError(f"proxy profile of type {self.type!r} requires a url")
        scheme = self.url.split("://", 1)[0].lower() if "://" in self.url else ""
        allowed = {"http": {"http"}, "socks5h": {"socks5h"}}[self.type]
        if scheme not in allowed:
            raise ValueError(
                f"proxy profile type {self.type!r} requires scheme {sorted(allowed)}, "
                f"got {scheme!r}"
            )
        if "@" not in self.url and self.type == "socks5h":
            # Allowed (unauthenticated proxy), just validated for shape below.
            pass
        if _UNSAFE_URL_CHARS.search(self.url):
            raise ValueError("proxy url must not contain whitespace")
        return self

    def redacted_url(self) -> str | None:
        """URL with any credentials replaced, safe for logs and errors."""
        if not self.url:
            return None
        return redact_url_credentials(self.url)


def redact_url_credentials(url: str) -> str:
    """Replace ``user:password@`` in a URL with ``***:***@``."""

    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    if "@" not in rest:
        return url
    credentials, _, host_part = rest.rpartition("@")
    if ":" in credentials:
        return f"{scheme}://***:***@{host_part}"
    return f"{scheme}://***@{host_part}"


class ImageFetchSection(_FrozenModel):
    default_proxy: str = "inherit_provider"
    max_bytes: int = Field(default=20 * 1024 * 1024, ge=1024, le=200 * 1024 * 1024)
    max_previews: int = Field(default=9, ge=1, le=10)
    min_preview_images: int = Field(default=3, ge=0, le=10)
    cdn_routes: dict[str, str] = Field(default_factory=dict)
    allowed_hosts: list[str] = Field(default_factory=list)

    @field_validator("cdn_routes")
    @classmethod
    def _check_cdn_hosts(cls, value: dict[str, str]) -> dict[str, str]:
        for host in value:
            if not host or "/" in host or " " in host:
                raise ValueError(f"cdn_routes key must be a bare hostname, got {host!r}")
        return {host.lower(): profile for host, profile in value.items()}

    @model_validator(mode="after")
    def _check_previews(self) -> ImageFetchSection:
        if self.min_preview_images > self.max_previews:
            raise ValueError("image_fetch.min_preview_images must not exceed max_previews")
        return self


class TranslationSection(_FrozenModel):
    enabled: bool = False
    provider: Literal["openai_compatible"] = "openai_compatible"
    base_url: str | None = None
    api_key: SecretStr | None = None
    model: str | None = None
    target_language: str = "zh-CN"
    timeout_seconds: float = Field(default=30.0, gt=0, le=120)

    @model_validator(mode="after")
    def _check_enabled(self) -> TranslationSection:
        if not self.enabled:
            return self
        missing = [
            name
            for name, value in (
                ("base_url", self.base_url),
                ("api_key", self.api_key),
                ("model", self.model),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"translation.enabled requires: {missing}")
        if self.base_url:
            _validate_http_url(self.base_url, "translation.base_url")
        return self


class DownloadSection(_FrozenModel):
    default_downloader: Literal["qbittorrent", "transmission"] = "qbittorrent"
    routes: dict[str, str] = Field(default_factory=lambda: dict.fromkeys(CATEGORIES, "qbittorrent"))
    #: Automatic resubmission cap (startup reconciliation). A job that cannot be
    #: submitted after this many attempts is marked FAILED and reported instead
    #: of being retried forever.
    max_retries: int = Field(default=5, ge=0, le=50)

    @field_validator("routes")
    @classmethod
    def _check_routes(cls, value: dict[str, str]) -> dict[str, str]:
        unknown = sorted(set(value) - set(CATEGORIES))
        if unknown:
            raise ValueError(f"download.routes has unknown categories: {unknown}")
        missing = sorted(set(CATEGORIES) - set(value))
        if missing:
            raise ValueError(f"download.routes is missing categories: {missing}")
        for category, downloader in value.items():
            if downloader not in {"qbittorrent", "transmission"}:
                raise ValueError(
                    f"download.routes[{category!r}] must name a known downloader, "
                    f"got {downloader!r}"
                )
        return value


class QbittorrentSection(_FrozenModel):
    enabled: bool = True
    url: str | None = None
    username: str | None = None
    password: SecretStr | None = None
    save_path_template: str = "/downloads/{category}/{job_id}"
    category: str = "fc2-harbor"
    tag_prefix: str = "fc2h"
    timeout_seconds: float = Field(default=15.0, gt=0, le=120)
    poll_interval_seconds: int = Field(default=30, ge=5, le=600)
    stability_checks: int = Field(default=2, ge=2, le=5)
    stability_interval_seconds: float = Field(default=10.0, gt=0, le=120)
    max_stability_wait_seconds: float = Field(default=600.0, gt=0, le=7200)
    verify_tls: bool = True

    @model_validator(mode="after")
    def _check_enabled(self) -> QbittorrentSection:
        if not self.enabled:
            return self
        if not self.url:
            raise ValueError("downloaders.qbittorrent.url is required when enabled")
        _validate_http_url(self.url, "downloaders.qbittorrent.url")
        if not self.username:
            raise ValueError("downloaders.qbittorrent.username is required when enabled")
        if self.password is None:
            raise ValueError("downloaders.qbittorrent.password is required when enabled")
        for token in ("{category}", "{job_id}"):
            if token not in self.save_path_template:
                raise ValueError(f"downloaders.qbittorrent.save_path_template must contain {token}")
        return self

    @field_validator("save_path_template")
    @classmethod
    def _check_template(cls, value: str) -> str:
        if not value.startswith("/"):
            raise ValueError("save_path_template must be an absolute remote path")
        return value.rstrip("/")


class TransmissionSection(_FrozenModel):
    enabled: bool = False
    url: str | None = None
    username: str | None = None
    password: SecretStr | None = None
    timeout_seconds: float = Field(default=15.0, gt=0, le=120)

    @model_validator(mode="after")
    def _check_enabled(self) -> TransmissionSection:
        if not self.enabled:
            return self
        if not self.url:
            raise ValueError("downloaders.transmission.url is required when enabled")
        _validate_http_url(self.url, "downloaders.transmission.url")
        return self


class DownloadersSection(_FrozenModel):
    qbittorrent: QbittorrentSection = Field(default_factory=QbittorrentSection)
    transmission: TransmissionSection = Field(default_factory=TransmissionSection)

    def enabled_names(self) -> list[str]:
        names: list[str] = []
        if self.qbittorrent.enabled:
            names.append("qbittorrent")
        if self.transmission.enabled:
            names.append("transmission")
        return names


class PathMapping(_FrozenModel):
    remote: str
    local: str

    @field_validator("remote", "local")
    @classmethod
    def _absolute(cls, value: str) -> str:
        return _require_absolute_path(value, "storage.path_mappings")

    @model_validator(mode="after")
    def _check_remote(self) -> PathMapping:
        if self.remote == "/":
            raise ValueError(
                "storage.path_mappings must not map '/' — map the downloader's "
                "download root explicitly instead"
            )
        return self


class StorageSection(_FrozenModel):
    path_mappings: list[PathMapping] = Field(min_length=1)
    final_roots: dict[str, str]
    dir_name_max_length: int = Field(default=120, ge=16, le=200)
    verify_sha256_on_copy: bool = False
    fsync_on_copy: bool = True
    copy_chunk_bytes: int = Field(default=4 * 1024 * 1024, ge=64 * 1024, le=64 * 1024 * 1024)

    @field_validator("final_roots")
    @classmethod
    def _check_roots(cls, value: dict[str, str]) -> dict[str, str]:
        missing = sorted(set(CATEGORIES) - set(value))
        if missing:
            raise ValueError(f"storage.final_roots is missing categories: {missing}")
        unknown = sorted(set(value) - set(CATEGORIES))
        if unknown:
            raise ValueError(f"storage.final_roots has unknown categories: {unknown}")
        return {
            category: _require_absolute_path(path, f"storage.final_roots.{category}")
            for category, path in value.items()
        }

    @model_validator(mode="after")
    def _check_layout(self) -> StorageSection:
        roots = list(self.final_roots.values())
        if len(set(roots)) != len(roots):
            raise ValueError("storage.final_roots must use distinct directories")
        for root in roots:
            for other in roots:
                if root != other and root.startswith(other + "/"):
                    raise ValueError(
                        f"storage.final_roots must not be nested: {root!r} inside {other!r}"
                    )
        return self


# ---------------------------------------------------------------------------
# Top level
# ---------------------------------------------------------------------------


class Settings(_FrozenModel):
    app: AppSection = Field(default_factory=AppSection)
    telegram: TelegramSection
    fc2: Fc2Section = Field(default_factory=Fc2Section)
    nyaa: NyaaSection = Field(default_factory=NyaaSection)
    provider: ProviderSection
    proxy_profiles: dict[str, ProxyProfile]
    image_fetch: ImageFetchSection = Field(default_factory=ImageFetchSection)
    translation: TranslationSection = Field(default_factory=TranslationSection)
    download: DownloadSection = Field(default_factory=DownloadSection)
    downloaders: DownloadersSection = Field(default_factory=DownloadersSection)
    storage: StorageSection

    # -- cross-section validation -------------------------------------------

    def proxy_names(self) -> set[str]:
        return set(self.proxy_profiles)

    @model_validator(mode="after")
    def _check_proxy_references(self) -> Settings:
        names = self.proxy_names()
        if "direct" not in names:
            raise ValueError("proxy_profiles must define the 'direct' profile")

        referenced: list[tuple[str, str]] = [
            ("telegram.proxy", self.telegram.proxy),
            ("nyaa.proxy", self.nyaa.proxy),
        ]
        referenced.extend(
            (f"provider.sources.{name}.proxy", source.proxy)
            for name, source in self.provider.sources.items()
        )
        referenced.extend(
            (f"image_fetch.cdn_routes.{host}", profile)
            for host, profile in self.image_fetch.cdn_routes.items()
        )
        if self.image_fetch.default_proxy != "inherit_provider":
            referenced.append(("image_fetch.default_proxy", self.image_fetch.default_proxy))

        unknown = [f"{field}={profile}" for field, profile in referenced if profile not in names]
        if unknown:
            raise ValueError(
                f"unknown proxy profile references: {unknown}; defined profiles: {sorted(names)}"
            )
        return self

    @model_validator(mode="after")
    def _check_downloaders(self) -> Settings:
        enabled = set(self.downloaders.enabled_names())
        if not enabled:
            raise ValueError("at least one downloader must be enabled")
        if self.download.default_downloader not in enabled:
            raise ValueError(
                f"download.default_downloader {self.download.default_downloader!r} "
                f"is not enabled; enabled: {sorted(enabled)}"
            )
        for category, downloader in self.download.routes.items():
            if downloader not in enabled:
                raise ValueError(
                    f"download.routes[{category!r}] uses disabled downloader {downloader!r}"
                )
        return self

    @model_validator(mode="after")
    def _check_translation_scope(self) -> Settings:
        # Translation ships in M3. Failing fast beats silently ignoring it.
        if self.translation.enabled:
            raise ValueError(
                "translation.enabled=true is not supported yet (delivered in M3); "
                "keep it disabled until the translation service exists"
            )
        return self

    # -- constructors -------------------------------------------------------

    @classmethod
    def from_mapping(
        cls, data: Mapping[str, Any], env: Mapping[str, str] | None = None
    ) -> Settings:
        expanded, missing = expand_env_placeholders(dict(data), env)
        try:
            return cls.model_validate(expanded)
        except ValidationError as exc:
            hint = f" (unset environment variables: {', '.join(missing)})" if missing else ""
            raise ConfigError(f"invalid configuration{hint}:\n{exc}") from exc

    @classmethod
    def load(cls, path: str | Path | None = None, env: Mapping[str, str] | None = None) -> Settings:
        """Load and validate configuration, raising :class:`ConfigError` on failure."""

        if path is None:
            candidate = (os.environ if env is None else env).get("FC2_CONFIG")
            path = Path(candidate) if candidate else Path("config/config.yaml")
        config_path = Path(path)
        data = load_yaml_file(config_path)
        apply_env_overrides(data, os.environ if env is None else env)
        return cls.from_mapping(data, env=env)

    # -- helpers ------------------------------------------------------------

    def downloader_for_category(self, category: str) -> str:
        return self.download.routes.get(category, self.download.default_downloader)

    def final_root(self, category: str) -> Path:
        return Path(self.storage.final_roots[category])

    def proxy_profile(self, name: str) -> ProxyProfile:
        try:
            return self.proxy_profiles[name]
        except KeyError as exc:  # pragma: no cover - guarded by validation
            raise ConfigError(f"unknown proxy profile {name!r}") from exc
