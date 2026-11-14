"""File organizer — spec 16.3 / 22.1 (same disk, cross disk, failure safety)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from app.db.enums import MoveStage
from app.organizer.mover import OrganizeError, Organizer, is_temp_file


def make_download(tmp_path: Path) -> Path:
    directory = tmp_path / "downloads" / "fc2" / "job-1"
    directory.mkdir(parents=True)
    (directory / "video.mkv").write_bytes(b"x" * 1000)
    (directory / "info.nfo").write_bytes(b"y" * 20)
    return directory


def force_copy_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Organizer, "_same_device", staticmethod(lambda _s, _t: False))


def test_same_filesystem_uses_atomic_rename(tmp_path: Path) -> None:
    source = make_download(tmp_path)
    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"

    result = Organizer().organize(source, target)

    assert result.method == "rename"
    assert result.file_count == 2
    assert (target / "video.mkv").read_bytes() == b"x" * 1000
    assert (target / "info.nfo").exists()
    assert not source.exists()
    assert not any(target.glob("*.partial"))


def test_cross_filesystem_copy_verifies_and_cleans_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_copy_mode(monkeypatch)
    source = make_download(tmp_path)
    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"

    result = Organizer(verify_sha256=True).organize(source, target)

    assert result.method == "copy"
    assert (target / "video.mkv").read_bytes() == b"x" * 1000
    assert not source.exists()
    assert not any(target.rglob("*.partial"))


def test_nested_directories_are_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_copy_mode(monkeypatch)
    source = tmp_path / "downloads" / "jav" / "job-2"
    (source / "CD1").mkdir(parents=True)
    (source / "CD1" / "a.mkv").write_bytes(b"a" * 10)
    (source / "CD2").mkdir()
    (source / "CD2" / "b.mkv").write_bytes(b"b" * 20)
    target = tmp_path / "library" / "jav" / "ABC-123"

    Organizer().organize(source, target)

    assert (target / "CD1" / "a.mkv").exists()
    assert (target / "CD2" / "b.mkv").exists()


def test_conflicting_target_is_refused_and_source_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_copy_mode(monkeypatch)
    source = make_download(tmp_path)
    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"
    target.mkdir(parents=True)
    (target / "video.mkv").write_bytes(b"different size")

    with pytest.raises(OrganizeError) as excinfo:
        Organizer().organize(source, target)

    assert excinfo.value.stage is MoveStage.VERIFY
    assert (source / "video.mkv").exists()
    assert (source / "info.nfo").exists()
    assert not any(target.glob("*.partial"))


def test_partial_files_are_removed_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_copy_mode(monkeypatch)
    source = make_download(tmp_path)
    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"

    def broken_replace(_src: str, _dst: str) -> None:
        raise OSError("simulated rename failure")

    monkeypatch.setattr("app.organizer.mover.os.replace", broken_replace)

    with pytest.raises(OrganizeError) as excinfo:
        Organizer().organize(source, target)

    assert excinfo.value.stage is MoveStage.RENAME
    assert (source / "video.mkv").exists()
    assert (source / "info.nfo").exists()
    assert not any(target.rglob("*.partial"))


def test_resume_skips_already_moved_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_copy_mode(monkeypatch)
    source = make_download(tmp_path)
    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"
    target.mkdir(parents=True)
    # Simulate a crash after the copy but before the source was deleted.
    (target / "video.mkv").write_bytes(b"x" * 1000)

    result = Organizer().organize(source, target)

    assert [item.target.name for item in result.skipped] == ["video.mkv"]
    assert (target / "info.nfo").exists()
    assert not source.exists()


def test_temp_files_are_not_moved(tmp_path: Path) -> None:
    source = make_download(tmp_path)
    temp_file = source / "video.mkv.!qB"
    temp_file.write_bytes(b"partial")
    parts_file = source / "other.parts"
    parts_file.write_bytes(b"partial")

    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"
    Organizer().organize(source, target)

    assert not (target / "video.mkv.!qB").exists()
    assert not (target / "other.parts").exists()
    assert temp_file.exists()  # left behind, not deleted
    assert is_temp_file(temp_file)


def test_symlink_inside_download_is_refused(tmp_path: Path) -> None:
    source = make_download(tmp_path)
    (source / "link.mkv").symlink_to(source / "video.mkv")
    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"

    with pytest.raises(OrganizeError) as excinfo:
        Organizer().organize(source, target)

    assert excinfo.value.stage is MoveStage.VALIDATE
    assert (source / "video.mkv").exists()


def test_target_inside_source_is_refused(tmp_path: Path) -> None:
    source = make_download(tmp_path)
    with pytest.raises(OrganizeError):
        Organizer().organize(source, source / "nested")


def test_missing_source_is_refused(tmp_path: Path) -> None:
    with pytest.raises(OrganizeError) as excinfo:
        Organizer().organize(tmp_path / "nope", tmp_path / "target")
    assert excinfo.value.stage is MoveStage.VALIDATE


def test_single_file_source(tmp_path: Path) -> None:
    single = tmp_path / "downloads" / "one.mkv"
    single.parent.mkdir(parents=True)
    single.write_bytes(b"z" * 50)
    target = tmp_path / "library" / "cn" / "some-name"

    result = Organizer().organize(single, target)

    assert result.file_count == 1
    assert (target / "one.mkv").read_bytes() == b"z" * 50
    assert not single.exists()


def test_fsync_is_applied_to_copied_data(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    force_copy_mode(monkeypatch)
    calls: list[int] = []
    real_fsync = os.fsync

    def tracking_fsync(fd: int) -> None:
        calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr("app.organizer.mover.os.fsync", tracking_fsync)
    source = make_download(tmp_path)
    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"

    Organizer(fsync=True).organize(source, target)

    assert len(calls) >= 2  # one per file at least
