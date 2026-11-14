"""Metadata provider interface — spec 17.2 / 7.4.

Each provider is an independent adapter with its own URL construction, selectors,
timeout and proxy (spec 7.4). The coordinator merges results field by field in
the fixed priority order (spec 7.2); providers never merge themselves.

The nine adapters arrive in M3. This module only fixes the contract so the gate,
database and tests can be written against it now.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from app.db.models import Content
from app.schemas.metadata import MetadataResult, ProviderContext


@runtime_checkable
class MetadataProvider(Protocol):
    #: Provider id exactly as used in ``provider.priorities.jav_fc2``.
    id: str

    async def fetch(self, content: Content, ctx: ProviderContext) -> MetadataResult:
        """Fetch metadata for one content.

        Failures must raise; the coordinator records them as a failed
        ``provider_results`` row and continues with the next provider (spec 7.2
        rule 9).
        """
