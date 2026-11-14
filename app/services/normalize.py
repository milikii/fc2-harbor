"""Content normalisation: FC2 numbers and magnet URIs.

Spec references: section 6.3 (FC2 number formats and canonical output), section
10 (manual magnet submission), section 11.1 (canonical key is the dedupe key).

Rules
-----
* ``FC2-PPV-1234567``, ``FC2PPV1234567``, ``FC2 PPV 1234567`` and
  ``FC2-1234567`` all normalise to ``fc2_ppv:1234567`` / ``FC2-PPV-1234567``.
* A number must be 5–8 digits and must not be glued to other letters/digits, so
  a random ``FC2-1234`` in prose is not treated as a content id.
* If no reliable number is found we return ``None`` and the caller records a
  filter reason; we never guess (spec 6.3).
* A generic resource without a stable code is keyed by its info hash, so the
  gate still deduplicates it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import parse_qs, unquote, urlsplit

from app.db.enums import ContentType
from app.schemas.release import MagnetInfo, NormalizedContent

#: Default FC2 PPV width. The real range is configuration
#: (``fc2.code_min_digits`` / ``fc2.code_max_digits``); this is only the default
#: used by tests and callers that do not load configuration.
FC2_MIN_DIGITS = 5
FC2_MAX_DIGITS = 8

_SEP = r"[\s\-_.]*"


@lru_cache(maxsize=32)
def _compile_fc2_patterns(
    min_digits: int, max_digits: int
) -> tuple[re.Pattern[str], re.Pattern[str]]:
    digits = rf"\d{{{min_digits},{max_digits}}}"
    boundary = r"(?<![0-9A-Za-z])"
    trailing = r"(?![0-9A-Za-z])"
    ppv = re.compile(rf"{boundary}FC2{_SEP}PPV{_SEP}({digits}){trailing}", re.IGNORECASE)
    bare = re.compile(rf"{boundary}FC2{_SEP}({digits}){trailing}", re.IGNORECASE)
    return ppv, bare


@dataclass(frozen=True, slots=True)
class Fc2CodeRule:
    """Digit range used when recognising an FC2 PPV number (spec 6.3)."""

    min_digits: int = FC2_MIN_DIGITS
    max_digits: int = FC2_MAX_DIGITS

    def __post_init__(self) -> None:
        if not 3 <= self.min_digits <= self.max_digits <= 12:
            raise ValueError(
                "invalid FC2 digit range: "
                f"{self.min_digits}..{self.max_digits} (expected 3 <= min <= max <= 12)"
            )

    def normalize(self, text: str | None) -> NormalizedContent | None:
        if not text:
            return None
        ppv_re, bare_re = _compile_fc2_patterns(self.min_digits, self.max_digits)
        for pattern in (ppv_re, bare_re):
            match = pattern.search(text)
            if not match:
                continue
            number = match.group(1)
            if int(number) == 0:
                # "FC2-00000" is not a content id; refuse rather than invent one.
                return None
            return NormalizedContent(
                content_type=ContentType.FC2,
                canonical_key=canonical_fc2_key(number),
                display_code=display_fc2_code(number),
                matched_text=match.group(0),
            )
        return None


DEFAULT_FC2_RULE = Fc2CodeRule()

_HEX_HASH_RE = re.compile(r"^[0-9a-f]{40}$")
_BASE32_HASH_RE = re.compile(r"^[A-Za-z2-7]{32}$")
_MAGNET_PREFIX_RE = re.compile(r"magnet:\?", re.IGNORECASE)


class MagnetError(ValueError):
    """Raised when a magnet URI cannot be parsed reliably."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_fc2_key(number: str) -> str:
    """``1234567`` → ``fc2_ppv:1234567``."""

    return f"fc2_ppv:{number}"


def display_fc2_code(number: str) -> str:
    """``1234567`` → ``FC2-PPV-1234567``."""

    return f"FC2-PPV-{number}"


def normalize_fc2(
    text: str | None, rule: Fc2CodeRule = DEFAULT_FC2_RULE
) -> NormalizedContent | None:
    """Extract and normalise an FC2 PPV number from free text.

    Returns ``None`` when no reliable number is present.
    """

    return rule.normalize(text)


def normalize_info_hash(value: str) -> str:
    """Normalise a BitTorrent v1 info hash to 40 lowercase hex characters."""

    candidate = value.strip()
    if _HEX_HASH_RE.match(candidate.lower()):
        return candidate.lower()
    if _BASE32_HASH_RE.match(candidate):
        import base64
        import binascii

        try:
            raw = base64.b32decode(candidate.upper())
        except (binascii.Error, ValueError) as exc:
            raise MagnetError("invalid_hash", f"无法解析 info hash: {candidate!r}") from exc
        if len(raw) != 20:
            raise MagnetError("invalid_hash", f"info hash 长度非法: {candidate!r}")
        return raw.hex()
    raise MagnetError(
        "invalid_hash",
        f"info hash 必须是 40 位十六进制或 32 位 base32: {candidate!r}",
    )


def extract_magnet(text: str) -> str | None:
    """Find the first ``magnet:?`` URI inside arbitrary message text."""

    if not text:
        return None
    match = _MAGNET_PREFIX_RE.search(text)
    if not match:
        return None
    tail = text[match.start() :]
    # Magnet URIs contain no whitespace; stop there and trim trailing punctuation
    # that Telegram users commonly append.
    token = re.split(r"[\s\n\r\t]", tail, maxsplit=1)[0]
    return token.rstrip("。，,、;；)）】]>")


def parse_magnet(text: str) -> MagnetInfo:
    """Parse a magnet URI into :class:`MagnetInfo`.

    Raises :class:`MagnetError` with a stable ``code`` so the bot can answer in
    Chinese without leaking internals.
    """

    raw = extract_magnet(text)
    if not raw:
        raise MagnetError("not_magnet", "没有找到 magnet 链接")

    parts = urlsplit(raw)
    if parts.scheme.lower() != "magnet":
        raise MagnetError("not_magnet", "不是有效的 magnet 链接")

    params = parse_qs(parts.query, keep_blank_values=False)
    xt_values = params.get("xt", [])
    if not xt_values:
        raise MagnetError("missing_xt", "magnet 缺少 xt 参数（info hash）")

    info_hash: str | None = None
    for value in xt_values:
        lowered = value.strip()
        if lowered.lower().startswith("urn:btih:"):
            info_hash = normalize_info_hash(lowered[len("urn:btih:") :])
            break
    if info_hash is None:
        raise MagnetError("unsupported_xt", "magnet 只支持 urn:btih 形式的 info hash")

    display_name = params.get("dn", [None])[0]
    if display_name:
        display_name = unquote(display_name).strip() or None

    trackers = tuple(unquote(item) for item in params.get("tr", []) if item)
    return MagnetInfo(raw=raw, info_hash=info_hash, display_name=display_name, trackers=trackers)


def generic_content_key(info_hash: str) -> str:
    """Dedupe key for a resource with no stable content code."""

    return f"infohash:{normalize_info_hash(info_hash)}"


def normalize_candidate(
    text: str | None,
    info_hash: str | None,
    rule: Fc2CodeRule = DEFAULT_FC2_RULE,
) -> NormalizedContent:
    """Normalise a manual submission into a content identity.

    FC2 numbers win; otherwise the info hash identifies the content (spec 10.6).
    """

    fc2 = normalize_fc2(text, rule)
    if fc2 is not None:
        return fc2
    if not info_hash:
        raise MagnetError("missing_hash", "无法确定内容标识：既没有 FC2 编号，也没有 info hash")
    return NormalizedContent(
        content_type=ContentType.GENERIC,
        canonical_key=generic_content_key(info_hash),
        display_code=None,
        matched_text=None,
    )
