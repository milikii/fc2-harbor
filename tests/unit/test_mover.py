"""File organizer — spec 16.3 / 22.1 (same disk, cross disk, failure safety)."""

from __future__ import annotations

import errno
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

    assert excinfo.value.stage is MoveStage.CONFLICT
    # The user's file is untouched and the source is still there.
    assert (target / "video.mkv").read_bytes() == b"different size"
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
    # The skipped file was not written by this run: keep its source, drop nothing.
    assert (source / "video.mkv").exists()
    assert not (source / "info.nfo").exists()


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


# -- target collision policy (never overwrite) -------------------------------


def test_rename_skips_a_target_with_the_same_size(tmp_path: Path) -> None:
    """Same size means the file is already there: leave it, drop the source."""

    source = make_download(tmp_path)
    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"
    target.mkdir(parents=True)
    existing = target / "video.mkv"
    existing.write_bytes(b"x" * 1000)
    os.utime(existing, (1_000_000, 1_000_000))
    stamp = existing.stat().st_mtime

    result = Organizer().organize(source, target)

    assert [item.target.name for item in result.skipped] == ["video.mkv"]
    assert [item.target.name for item in result.moved] == ["info.nfo"]
    assert existing.stat().st_mtime == stamp  # untouched
    # The skipped source is kept: we never delete a file we did not move.
    assert (source / "video.mkv").exists()


def test_rename_never_overwrites_a_different_file(tmp_path: Path) -> None:
    source = make_download(tmp_path)
    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"
    target.mkdir(parents=True)
    (target / "video.mkv").write_bytes(b"user's own file")

    with pytest.raises(OrganizeError) as excinfo:
        Organizer().organize(source, target)

    assert excinfo.value.stage is MoveStage.CONFLICT
    assert (target / "video.mkv").read_bytes() == b"user's own file"
    assert (source / "video.mkv").read_bytes() == b"x" * 1000


def test_rename_moves_the_missing_files_of_a_partly_present_directory(
    tmp_path: Path,
) -> None:
    source = make_download(tmp_path)
    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"
    target.mkdir(parents=True)
    (target / "video.mkv").write_bytes(b"x" * 1000)

    result = Organizer().organize(source, target)

    assert result.method == "rename"
    assert [item.target.name for item in result.skipped] == ["video.mkv"]
    assert [item.target.name for item in result.moved] == ["info.nfo"]
    assert (target / "info.nfo").read_bytes() == b"y" * 20
    assert (source / "video.mkv").exists()  # skipped, so kept
    assert not (source / "info.nfo").exists()


def test_copy_mode_conflict_does_not_leave_a_partial_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    force_copy_mode(monkeypatch)
    source = make_download(tmp_path)
    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"
    target.mkdir(parents=True)
    (target / "video.mkv").write_bytes(b"user's own file")

    with pytest.raises(OrganizeError) as excinfo:
        Organizer().organize(source, target)

    assert excinfo.value.stage is MoveStage.CONFLICT
    assert (target / "video.mkv").read_bytes() == b"user's own file"
    assert not any(target.rglob("*.partial"))


def test_conflict_stops_before_later_files_are_moved(tmp_path: Path) -> None:
    """A conflict must not leave a half-organized target behind."""

    source = make_download(tmp_path)
    (source / "zzz.bin").write_bytes(b"z" * 5)
    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"
    target.mkdir(parents=True)
    (target / "video.mkv").write_bytes(b"user's own file")

    with pytest.raises(OrganizeError):
        Organizer().organize(source, target)

    assert not (target / "zzz.bin").exists()
    assert (source / "zzz.bin").exists()


# -- EXDEV fallback (container bind mounts) ----------------------------------


def test_exdev_falls_back_to_copy_instead_of_failing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two bind mounts of the same disk: st_dev matches but rename(2) says EXDEV."""

    source = make_download(tmp_path)
    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"
    real_replace = os.replace
    calls: list[str] = []

    def replace_then_exdev(src: object, dst: object) -> None:
        calls.append("rename")
        if str(src).startswith(str(source)):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_replace(src, dst)

    monkeypatch.setattr("app.organizer.mover.os.replace", replace_then_exdev)

    result = Organizer().organize(source, target)

    # Two refused renames (one per file) plus two partial→final renames.
    assert calls.count("rename") == 4
    assert result.method == "rename"  # same-device attempt, served by copy
    assert (target / "video.mkv").read_bytes() == b"x" * 1000
    assert (target / "info.nfo").read_bytes() == b"y" * 20
    assert not source.exists()
    assert not any(target.rglob("*.partial"))


def test_exdev_fallback_is_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from structlog.testing import capture_logs

    source = make_download(tmp_path)
    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"
    real_replace = os.replace

    def replace_then_exdev(src: object, dst: object) -> None:
        if str(src).startswith(str(source)):
            raise OSError(errno.EXDEV, "Invalid cross-device link")
        real_replace(src, dst)

    monkeypatch.setattr("app.organizer.mover.os.replace", replace_then_exdev)

    with capture_logs() as logs:
        Organizer().organize(source, target)

    fallbacks = [entry for entry in logs if entry["event"] == "organizer.rename_fallback"]
    assert fallbacks and all(entry["reason"] == "exdev" for entry in fallbacks)


def test_other_rename_errors_still_fail_the_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = make_download(tmp_path)
    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"

    def broken_replace(_src: object, _dst: object) -> None:
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr("app.organizer.mover.os.replace", broken_replace)

    with pytest.raises(OrganizeError) as excinfo:
        Organizer().organize(source, target)

    assert excinfo.value.stage is MoveStage.RENAME
    assert (source / "video.mkv").exists()


def test_conflict_leaves_the_target_untouched(tmp_path: Path) -> None:
    source = make_download(tmp_path)
    target = tmp_path / "library" / "fc2" / "FC2-PPV-1234567"
    target.mkdir(parents=True)
    existing = target / "video.mkv"
    existing.write_bytes(b"user's own file")
    os.utime(existing, (1_000_000, 1_000_000))
    size, mtime = existing.stat().st_size, existing.stat().st_mtime

    with pytest.raises(OrganizeError) as excinfo:
        Organizer().organize(source, target)

    assert excinfo.value.stage is MoveStage.CONFLICT
    assert existing.stat().st_size == size
    assert existing.stat().st_mtime == mtime
    assert existing.read_bytes() == b"user's own file"
