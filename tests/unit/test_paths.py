"""Path mapping, name sanitising and traversal protection — spec 16.1 / 16.2."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.config import PathMapping
from app.organizer.paths import (
    PathMapper,
    PathMappingError,
    PathTraversalError,
    build_directory,
    sanitize_component,
)


def _mapper(tmp_path: Path) -> PathMapper:
    return PathMapper(
        [
            PathMapping(remote="/downloads", local=str(tmp_path / "downloads")),
            PathMapping(remote="/downloads/fc2", local=str(tmp_path / "fc2-only")),
        ]
    )


def test_explicit_mapping(tmp_path: Path) -> None:
    mapper = _mapper(tmp_path)
    assert mapper.to_local("/downloads/jav/abc/file.mkv") == (
        tmp_path / "downloads" / "jav" / "abc" / "file.mkv"
    )


def test_longest_prefix_wins(tmp_path: Path) -> None:
    mapper = _mapper(tmp_path)
    assert mapper.to_local("/downloads/fc2/job1/x.mkv") == (
        tmp_path / "fc2-only" / "job1" / "x.mkv"
    )


def test_unmapped_path_is_an_error_not_a_guess(tmp_path: Path) -> None:
    mapper = _mapper(tmp_path)
    with pytest.raises(PathMappingError):
        mapper.to_local("/mnt/other/downloads/file.mkv")


def test_relative_and_traversal_paths_are_refused(tmp_path: Path) -> None:
    mapper = _mapper(tmp_path)
    with pytest.raises(PathMappingError):
        mapper.to_local("downloads/file.mkv")
    with pytest.raises(PathMappingError):
        mapper.to_local("/downloads/../etc/passwd")


def test_component_prefix_is_not_a_path_match(tmp_path: Path) -> None:
    """``/downloads2`` must not match the ``/downloads`` mapping."""

    mapper = _mapper(tmp_path)
    with pytest.raises(PathMappingError):
        mapper.to_local("/downloads2/file.mkv")


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("normal name", "normal name"),
        ("a/b\\c:d*e?f", "a_b_c_d_e_f"),
        ("  spaced   name  ", "spaced name"),
        ("trailing dots...", "trailing dots"),
        ("...hidden", "hidden"),
        ("tab\tnewline\nname", "tab_newline_name"),
        ("", "_untitled"),
        ("...", "_untitled"),
    ],
)
def test_sanitize_component(raw: str, expected: str) -> None:
    assert sanitize_component(raw) == expected


def test_sanitize_limits_characters_and_bytes() -> None:
    long_ascii = "a" * 500
    assert len(sanitize_component(long_ascii, max_length=40)) == 40

    long_cjk = "作" * 200  # 3 bytes per character
    result = sanitize_component(long_cjk, max_length=120, max_bytes=200)
    assert len(result.encode("utf-8")) <= 200
    assert result


def test_build_directory_stays_inside_root(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    target = build_directory(root, "../../etc/passwd")
    assert target.parent == root.resolve()
    assert target.is_relative_to(root.resolve())


def test_build_directory_rejects_relative_root(tmp_path: Path) -> None:
    with pytest.raises(PathTraversalError):
        build_directory(Path("relative/library"), "x")


def test_build_directory_uses_display_code(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    assert build_directory(root, "FC2-PPV-1234567") == root.resolve() / "FC2-PPV-1234567"
