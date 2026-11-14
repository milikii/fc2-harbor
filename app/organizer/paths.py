"""Path mapping and directory-name sanitising — spec 16.1 / 16.2.

* Downloader paths are mapped to container paths **only** through the explicit
  ``storage.path_mappings`` list. There is no string replacement fallback: an
  unmapped path is an error, never a guess.
* Directory names are sanitised (illegal characters, control characters,
  length, trailing dots/spaces) and the final path is proven to stay inside the
  configured media root, so a malicious torrent name cannot escape ``/library``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from app.config import PathMapping

#: Characters that are illegal on Linux-adjacent filesystems or that would break
#: path handling (Windows clients read these shares too).
ILLEGAL_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
WHITESPACE_RE = re.compile(r"\s+")

MAX_FILENAME_BYTES = 200
FALLBACK_NAME = "_untitled"


class PathMappingError(RuntimeError):
    """A downloader path is not covered by ``storage.path_mappings``."""


class PathTraversalError(RuntimeError):
    """A computed path escaped its configured root."""


def _truncate_bytes(text: str, max_bytes: int) -> str:
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore")


def sanitize_component(
    name: str,
    *,
    max_length: int = 120,
    max_bytes: int = MAX_FILENAME_BYTES,
) -> str:
    """Turn an arbitrary title into one safe path component."""

    text = unicodedata.normalize("NFKC", name or "")
    text = ILLEGAL_CHARS_RE.sub("_", text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    # No hidden directories and no "." / ".." components.
    text = text.strip(" .").lstrip(".")
    if not text:
        return FALLBACK_NAME
    text = _truncate_bytes(text, max_bytes)
    text = text[:max_length].strip(" .")
    return text or FALLBACK_NAME


@dataclass(frozen=True, slots=True)
class _Mapping:
    remote: PurePosixPath
    local: Path


class PathMapper:
    """Maps a downloader-side path to the container path."""

    def __init__(self, mappings: Iterable[PathMapping]) -> None:
        parsed = [
            _Mapping(remote=PurePosixPath(mapping.remote), local=Path(mapping.local))
            for mapping in mappings
        ]
        if not parsed:
            raise PathMappingError("storage.path_mappings is empty")
        for mapping in parsed:
            if not mapping.remote.is_absolute():
                raise PathMappingError(f"remote mapping must be absolute: {mapping.remote}")
        # Longest remote prefix wins, so /downloads/fc2 can override /downloads.
        self._mappings = sorted(parsed, key=lambda item: len(item.remote.parts), reverse=True)

    def to_local(self, remote_path: str) -> Path:
        if not remote_path:
            raise PathMappingError("empty remote path")
        remote = PurePosixPath(remote_path)
        if not remote.is_absolute():
            raise PathMappingError(f"remote path must be absolute: {remote_path!r}")

        for mapping in self._mappings:
            prefix = mapping.remote.parts
            if remote.parts[: len(prefix)] != prefix:
                continue
            remainder = remote.parts[len(prefix) :]
            if any(part in {"..", "."} for part in remainder):
                raise PathMappingError(f"remote path contains traversal segments: {remote_path!r}")
            local = mapping.local.joinpath(*remainder) if remainder else mapping.local
            return local

        configured = ", ".join(str(mapping.remote) for mapping in self._mappings)
        raise PathMappingError(
            f"remote path {remote_path!r} is not covered by storage.path_mappings ({configured})"
        )

    def to_remote(self, local_path: Path) -> str:
        """Inverse mapping, used by tests and diagnostics only."""

        for mapping in self._mappings:
            try:
                relative = local_path.relative_to(mapping.local)
            except ValueError:
                continue
            return str(mapping.remote.joinpath(*relative.parts))
        raise PathMappingError(f"local path {local_path} is not covered by any mapping")


def build_directory(
    root: Path,
    name: str,
    *,
    max_length: int = 120,
    max_bytes: int = MAX_FILENAME_BYTES,
) -> Path:
    """Return ``root/<sanitised name>``, proven to stay inside ``root``."""

    component = sanitize_component(name, max_length=max_length, max_bytes=max_bytes)
    root_path = Path(root)
    if not root_path.is_absolute():
        raise PathTraversalError(f"media root must be absolute: {root_path}")
    root_resolved = root_path.resolve()
    candidate = (root_resolved / component).resolve()
    if candidate == root_resolved or not candidate.is_relative_to(root_resolved):
        raise PathTraversalError(f"refusing to use {candidate} outside media root {root_resolved}")
    return candidate
