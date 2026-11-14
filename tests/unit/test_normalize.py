"""FC2 normalisation and magnet parsing — spec 6.3 / 10 / 22.1."""

from __future__ import annotations

import base64

import pytest
from app.db.enums import ContentType
from app.services.normalize import (
    MagnetError,
    generic_content_key,
    normalize_candidate,
    normalize_fc2,
    normalize_info_hash,
    parse_magnet,
)


@pytest.mark.parametrize(
    "text",
    [
        "FC2-PPV-1234567",
        "FC2PPV1234567",
        "FC2 PPV 1234567",
        "FC2-1234567",
        "fc2-ppv-1234567",
        "【FC2-PPV-1234567】作品介绍",
        "FC2_PPV_1234567",
        "FC2.PPV.1234567",
        "[FC2-PPV-1234567] タイトル",
        "FC2PPV-1234567-A",
    ],
)
def test_all_documented_formats_normalise(text: str) -> None:
    result = normalize_fc2(text)
    assert result is not None
    assert result.content_type is ContentType.FC2
    assert result.canonical_key == "fc2_ppv:1234567"
    assert result.display_code == "FC2-PPV-1234567"


def test_first_number_wins() -> None:
    result = normalize_fc2("FC2-PPV-1111111 and FC2-PPV-2222222")
    assert result is not None
    assert result.canonical_key == "fc2_ppv:1111111"


@pytest.mark.parametrize(
    "text",
    [
        None,
        "",
        "no code here",
        "FC2-1234",  # too short to be a content id
        "FC2-PPV-123456789",  # too long
        "AFC2-1234567",  # glued to letters
        "FC2-1234567X",
        "FC2-00000",  # zero is not a content id
        "1234567",
    ],
)
def test_non_matches_return_none(text: str | None) -> None:
    assert normalize_fc2(text) is None


def test_normalize_info_hash_accepts_hex_and_base32() -> None:
    hex_hash = "0123456789abcdef0123456789abcdef01234567"
    assert normalize_info_hash(hex_hash.upper()) == hex_hash

    raw = bytes(range(20))
    base32 = base64.b32encode(raw).decode()
    assert normalize_info_hash(base32) == raw.hex()


@pytest.mark.parametrize("value", ["", "xyz", "0" * 39, "0" * 41])
def test_normalize_info_hash_rejects_garbage(value: str) -> None:
    with pytest.raises(MagnetError) as excinfo:
        normalize_info_hash(value)
    assert excinfo.value.code == "invalid_hash"


def test_parse_magnet_extracts_hash_and_name() -> None:
    info_hash = "0123456789abcdef0123456789abcdef01234567"
    magnet = (
        f"magnet:?xt=urn:btih:{info_hash}"
        "&dn=FC2-PPV-1234567+Sample+Title"
        "&tr=udp%3A%2F%2Ftracker.example%3A6969%2Fannounce"
    )
    parsed = parse_magnet(f"看一下这个 {magnet}。")

    assert parsed.info_hash == info_hash
    assert parsed.display_name == "FC2-PPV-1234567 Sample Title"
    assert parsed.trackers == ("udp://tracker.example:6969/announce",)
    assert parsed.dedupe_key == f"infohash:{info_hash}"


@pytest.mark.parametrize(
    ("text", "code"),
    [
        ("just some text", "not_magnet"),
        ("magnet:?dn=nohash", "missing_xt"),
        ("magnet:?xt=urn:sha1:abc", "unsupported_xt"),
        ("magnet:?xt=urn:btih:nothex", "invalid_hash"),
    ],
)
def test_parse_magnet_errors(text: str, code: str) -> None:
    with pytest.raises(MagnetError) as excinfo:
        parse_magnet(text)
    assert excinfo.value.code == code


def test_normalize_candidate_prefers_fc2_then_hash() -> None:
    info_hash = "0123456789abcdef0123456789abcdef01234567"
    fc2 = normalize_candidate("FC2-PPV-7654321", info_hash)
    assert fc2.content_type is ContentType.FC2
    assert fc2.canonical_key == "fc2_ppv:7654321"

    generic = normalize_candidate("some random title", info_hash)
    assert generic.content_type is ContentType.GENERIC
    assert generic.canonical_key == generic_content_key(info_hash)
    assert generic.display_code is None


def test_generic_key_requires_hash() -> None:
    with pytest.raises(MagnetError):
        normalize_candidate("no code", None)
