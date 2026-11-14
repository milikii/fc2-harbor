"""Source adapter interface — spec 17.1.

A source only turns "what is new" into :class:`ReleaseCandidate` objects. It must
not call Telegram, the gate or the downloader (spec 4: 来源适配器不得直接调用
Telegram 或下载器). The Nyaa implementation arrives in M2.

New sites are added by writing one adapter plus configuration — nothing else in
the system changes (spec 17.1).
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.schemas.release import ReleaseCandidate


@runtime_checkable
class SourceAdapter(Protocol):
    #: Stable identifier stored in ``releases.source`` and ``source_cursors``.
    name: str

    async def discover(self, cursor: dict[str, object] | None) -> list[ReleaseCandidate]:
        """Return new candidates; ``cursor`` is the previous persisted state."""
