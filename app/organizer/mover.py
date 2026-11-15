"""File organizer — spec 16.3.

Guarantees, in order of importance:

1. **Never lose data.** On any failure the source files stay where they are and
   the caller keeps the download job; only the target ``.partial`` files written
   by the failed attempt are removed.
2. **Same filesystem:** create the target directory, ``os.replace`` (atomic
   rename), verify existence and size. ``st_dev`` cannot be trusted inside a
   container: two bind mounts of the same disk still return ``EXDEV`` from
   ``rename(2)``, so the rename is *optimistic* and falls back to the copy path
   on ``EXDEV`` instead of failing the job.
3. **Cross filesystem:** copy to ``<name>.partial`` in the target directory,
   flush + ``fsync``, verify size (and optionally SHA-256), atomically rename to
   the final name, and only after *every* file succeeded delete the sources and
   prune empty directories.
4. **Never overwrite.** Before every rename/copy the target is inspected:
   missing → move; present with the same size → skip as ``already_present``;
   present with a different size → :class:`OrganizeError` at stage ``conflict``
   and the task goes FAILED for a human. ``os.replace`` overwrites silently, so
   the check must happen first.
5. **Idempotent resume:** a file already present at the target with the same
   size is treated as moved, so a crash mid-organize can be retried without
   duplicating or overwriting data. Directory seeds are decided per file: some
   files may be skipped while the rest move.
"""

from __future__ import annotations

import errno
import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from app.db.enums import MoveStage
from app.logging import get_logger

_log = get_logger("organizer.mover")

#: qBittorrent temp/partial suffixes that must not be counted or moved.
TEMP_SUFFIXES: Final[tuple[str, ...]] = (".!qB", ".!ut", ".parts", ".part")

PARTIAL_SUFFIX = ".partial"


class OrganizeError(RuntimeError):
    """A failed organize attempt; the source files are still intact."""

    def __init__(
        self,
        message: str,
        *,
        stage: MoveStage,
        path: Path | None = None,
        moved: list[MovedFile] | None = None,
        cause: BaseException | None = None,
    ) -> None:
        super().__init__(message)
        self.stage = stage
        self.path = path
        self.moved = moved or []
        self.cause = cause

    def describe(self) -> str:
        location = f" at {self.path}" if self.path else ""
        return f"{self.stage.value}{location}: {self}"


@dataclass(frozen=True, slots=True)
class MovedFile:
    source: Path
    target: Path
    size_bytes: int


@dataclass(slots=True)
class OrganizeResult:
    source_root: Path
    target_dir: Path
    method: str
    moved: list[MovedFile] = field(default_factory=list)
    skipped: list[MovedFile] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.moved)

    @property
    def file_count(self) -> int:
        return len(self.moved) + len(self.skipped)


def is_temp_file(path: Path) -> bool:
    """True for qBittorrent partials such as ``movie.mkv.!qB`` / ``x.parts``."""

    name = path.name
    return any(name.endswith(suffix) for suffix in TEMP_SUFFIXES)


def sha256_file(path: Path, *, chunk_bytes: int = 4 * 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(chunk_bytes):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        # Some filesystems refuse directory fsync; the file data is already safe.
        pass
    finally:
        os.close(descriptor)


class Organizer:
    """Moves a finished download into its media directory."""

    def __init__(
        self,
        *,
        verify_sha256: bool = False,
        fsync: bool = True,
        chunk_bytes: int = 4 * 1024 * 1024,
    ) -> None:
        if chunk_bytes < 64 * 1024:
            raise ValueError("chunk_bytes must be at least 64 KiB")
        self.verify_sha256 = verify_sha256
        self.fsync = fsync
        self.chunk_bytes = chunk_bytes

    # -- public API ---------------------------------------------------------

    def organize(self, source: Path, target_dir: Path) -> OrganizeResult:
        source_path = Path(source)
        target = Path(target_dir)
        self._validate(source_path, target)

        files = self._collect(source_path)
        if not files:
            raise OrganizeError(
                "no regular files to organize", stage=MoveStage.VALIDATE, path=source_path
            )

        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise OrganizeError(
                f"cannot create target directory: {exc}",
                stage=MoveStage.CREATE_DIR,
                path=target,
                cause=exc,
            ) from exc

        method = "rename" if self._same_device(source_path, target) else "copy"
        moved: list[MovedFile] = []
        skipped: list[MovedFile] = []
        partials: list[Path] = []

        try:
            for src, relative in files:
                dst = target / relative
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise OrganizeError(
                        f"cannot create {dst.parent}: {exc}",
                        stage=MoveStage.CREATE_DIR,
                        path=dst.parent,
                        moved=moved,
                        cause=exc,
                    ) from exc

                size = src.stat().st_size
                if self._check_target(src, dst, size):
                    skipped.append(MovedFile(src, dst, size))
                    continue

                if method == "rename":
                    completed = self._rename_one(src, dst, size, moved, partials)
                else:
                    completed = self._copy_one(src, dst, size, moved, partials)
                # False means the target appeared between the check and the move:
                # treat it as already present instead of claiming we moved it.
                if completed:
                    moved.append(MovedFile(src, dst, size))
                else:
                    skipped.append(MovedFile(src, dst, size))

            self._cleanup_sources(source_path, [item.source for item in moved])
        except OrganizeError:
            self._discard_partials(partials)
            raise
        except OSError as exc:
            self._discard_partials(partials)
            raise OrganizeError(
                f"filesystem error: {exc}",
                stage=MoveStage.COPY if method == "copy" else MoveStage.RENAME,
                moved=moved,
                cause=exc,
            ) from exc

        result = OrganizeResult(
            source_root=source_path,
            target_dir=target,
            method=method,
            moved=moved,
            skipped=skipped,
        )
        _log.info(
            "organizer.moved",
            source=str(source_path),
            target=str(target),
            method=method,
            files=result.file_count,
            bytes=result.total_bytes,
        )
        return result

    # -- validation / discovery --------------------------------------------

    @staticmethod
    def _validate(source: Path, target: Path) -> None:
        if not target.is_absolute():
            raise OrganizeError(
                f"target directory must be absolute: {target}",
                stage=MoveStage.VALIDATE,
                path=target,
            )
        if not source.exists():
            raise OrganizeError(
                f"source does not exist: {source}", stage=MoveStage.VALIDATE, path=source
            )
        if source.is_symlink():
            raise OrganizeError(
                f"source is a symlink, refusing to follow it: {source}",
                stage=MoveStage.VALIDATE,
                path=source,
            )
        resolved_source = source.resolve()
        resolved_target = target.resolve()
        if resolved_source == resolved_target:
            raise OrganizeError(
                "source and target are the same path", stage=MoveStage.VALIDATE, path=source
            )
        if resolved_target.is_relative_to(resolved_source):
            raise OrganizeError(
                "target directory is inside the source directory",
                stage=MoveStage.VALIDATE,
                path=target,
            )
        if resolved_source.is_relative_to(resolved_target):
            raise OrganizeError(
                "source is inside the target directory", stage=MoveStage.VALIDATE, path=source
            )

    @staticmethod
    def _collect(source: Path) -> list[tuple[Path, Path]]:
        if source.is_file():
            return [(source, Path(source.name))]

        collected: list[tuple[Path, Path]] = []
        for path in sorted(source.rglob("*")):
            if path.is_symlink():
                raise OrganizeError(
                    f"symlink inside download, refusing to move: {path}",
                    stage=MoveStage.VALIDATE,
                    path=path,
                )
            if not path.is_file() or is_temp_file(path):
                continue
            collected.append((path, path.relative_to(source)))
        return collected

    @staticmethod
    def _same_device(source: Path, target: Path) -> bool:
        try:
            return os.stat(source).st_dev == os.stat(target).st_dev
        except OSError:
            # Unknown device (e.g. target on a mount that just appeared): be
            # conservative and use the copy path, which verifies every byte.
            return False

    # -- target collision policy -------------------------------------------

    def _check_target(self, src: Path, dst: Path, size: int) -> bool:
        """Return True when ``dst`` already holds this file (same size).

        Raises :class:`OrganizeError` at stage ``conflict`` when a different file
        sits at the destination: user data is never overwritten.
        """

        try:
            existing = dst.stat().st_size
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise OrganizeError(
                f"cannot stat existing target: {exc}",
                stage=MoveStage.VERIFY,
                path=dst,
                cause=exc,
            ) from exc

        if existing == size:
            _log.info(
                "organizer.already_present",
                source=str(src),
                target=str(dst),
                size_bytes=size,
            )
            return True
        raise OrganizeError(
            f"target_conflict: {dst} exists with {existing} bytes, source has {size}",
            stage=MoveStage.CONFLICT,
            path=dst,
        )

    # -- single file operations --------------------------------------------

    def _rename_one(
        self,
        src: Path,
        dst: Path,
        size: int,
        moved: list[MovedFile],
        partials: list[Path],
    ) -> bool:
        if self._check_target(src, dst, size):
            return False
        try:
            os.replace(src, dst)
            _fsync_dir(dst.parent)
        except OSError as exc:
            if exc.errno == errno.EXDEV:
                # Same st_dev but a different mount point: the kernel refuses the
                # rename. Fall back to the verified copy path instead of failing.
                _log.warning(
                    "organizer.rename_fallback",
                    reason="exdev",
                    source=str(src),
                    target=str(dst),
                )
                return self._copy_one(src, dst, size, moved, partials)
            raise OrganizeError(
                f"atomic rename failed: {exc}",
                stage=MoveStage.RENAME,
                path=src,
                moved=moved,
                cause=exc,
            ) from exc
        if not dst.exists() or dst.stat().st_size != size:
            raise OrganizeError(
                "post-rename verification failed",
                stage=MoveStage.VERIFY,
                path=dst,
                moved=moved,
            )
        return True

    def _copy_one(
        self,
        src: Path,
        dst: Path,
        size: int,
        moved: list[MovedFile],
        partials: list[Path],
    ) -> bool:
        partial = dst.with_name(dst.name + PARTIAL_SUFFIX)
        partials.append(partial)
        digest = hashlib.sha256() if self.verify_sha256 else None
        written = 0
        try:
            with open(src, "rb") as reader, open(partial, "wb") as writer:
                while chunk := reader.read(self.chunk_bytes):
                    writer.write(chunk)
                    written += len(chunk)
                    if digest is not None:
                        digest.update(chunk)
                writer.flush()
                if self.fsync:
                    os.fsync(writer.fileno())
        except OSError as exc:
            raise OrganizeError(
                f"copy failed: {exc}", stage=MoveStage.COPY, path=src, moved=moved, cause=exc
            ) from exc

        if written != size:
            raise OrganizeError(
                f"size mismatch after copy: wrote {written}, expected {size}",
                stage=MoveStage.VERIFY,
                path=partial,
                moved=moved,
            )

        if digest is not None:
            source_digest = sha256_file(src, chunk_bytes=self.chunk_bytes)
            if source_digest != digest.hexdigest():
                raise OrganizeError(
                    "sha256 mismatch after copy", stage=MoveStage.VERIFY, path=partial, moved=moved
                )

        if self._check_target(src, dst, size):
            partials.remove(partial)
            try:
                partial.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort cleanup
                _log.warning("organizer.partial_cleanup_failed", path=str(partial))
            return False

        try:
            os.replace(partial, dst)
            _fsync_dir(dst.parent)
        except OSError as exc:
            raise OrganizeError(
                f"atomic rename of partial failed: {exc}",
                stage=MoveStage.RENAME,
                path=partial,
                moved=moved,
                cause=exc,
            ) from exc
        partials.remove(partial)

        if not dst.exists() or dst.stat().st_size != size:
            raise OrganizeError(
                "post-copy verification failed",
                stage=MoveStage.VERIFY,
                path=dst,
                moved=moved,
            )
        return True

    # -- cleanup ------------------------------------------------------------

    def _cleanup_sources(self, source: Path, moved_sources: list[Path]) -> None:
        """Delete the sources we moved, only after every target file was verified.

        Files skipped as ``already_present`` are **kept**: the target file was not
        written by this run, so deleting the source would be a guess. The leftover
        is logged and the directory stays; nothing is lost.
        """

        for src in moved_sources:
            if not src.exists():
                continue
            try:
                src.unlink()
            except OSError as exc:
                raise OrganizeError(
                    f"cannot delete source file: {exc}",
                    stage=MoveStage.CLEANUP,
                    path=src,
                    cause=exc,
                ) from exc

        if not source.is_dir():
            return
        for current, _dirs, _names in os.walk(source, topdown=False):
            try:
                os.rmdir(current)
            except OSError:
                # Non-empty (leftover temp files) or not removable: keep it.
                pass

    @staticmethod
    def _discard_partials(partials: list[Path]) -> None:
        for partial in partials:
            try:
                partial.unlink(missing_ok=True)
            except OSError:  # pragma: no cover - best effort cleanup
                _log.warning("organizer.partial_cleanup_failed", path=str(partial))
