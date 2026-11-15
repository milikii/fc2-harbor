"""``contents.updated_at`` must mean "when the metadata was fetched".

Bookkeeping writes (filling a missing label, flipping ``backfilled``) must not move
it; real metadata changes (enrich, translation) must.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from app.db.enums import ContentType
from app.db.models import Content
from app.db.session import Database
from app.schemas.release import NormalizedContent, ReleaseCandidate
from app.services.gate import ContentGate, GateSubject
from app.services.releases import ReleaseRegistrar

from tests.support import hex_hash

#: Naive on purpose: SQLite hands datetimes back without a tzinfo.
STAMP = datetime(2020, 11, 1, 12, 0)


async def seed_content(
    db: Database,
    *,
    key: str = "fc2_ppv:1234567",
    display_code: str | None = None,
    title: str | None = None,
) -> str:
    async with db.transaction() as session:
        content = Content(
            content_type=ContentType.FC2,
            canonical_key=key,
            display_code=display_code,
            title_ja=title,
            updated_at=STAMP,
        )
        session.add(content)
        await session.flush()
        return content.id


async def updated_at_of(db: Database, content_id: str) -> datetime:
    async with db.session() as session:
        content = await session.get(Content, content_id)
        assert content is not None
        return content.updated_at


def candidate(**overrides: object) -> ReleaseCandidate:
    base: dict[str, object] = {
        "source": "nyaa",
        "source_item_id": "1900001",
        "source_url": "https://sukebei.nyaa.si/view/1900001",
        "title": "FC2-PPV-1234567 作品",
        "magnet": f"magnet:?xt=urn:btih:{hex_hash(701)}",
        "info_hash": hex_hash(701),
        "content_hint": NormalizedContent(
            content_type=ContentType.FC2,
            canonical_key="fc2_ppv:1234567",
            display_code="FC2-PPV-1234567",
        ),
        "raw_payload": {"uploader": "Uploader A"},
    }
    base.update(overrides)
    return ReleaseCandidate(**base)  # type: ignore[arg-type]


async def test_filling_a_missing_label_does_not_touch_updated_at(db: Database) -> None:
    content_id = await seed_content(db)  # no display_code, no title
    gate = ContentGate()
    subject = GateSubject(
        content_type=ContentType.FC2,
        canonical_key="fc2_ppv:1234567",
        display_code="FC2-PPV-1234567",
        info_hash=hex_hash(701),
        source="nyaa",
        source_item_id="1900001",
        magnet=f"magnet:?xt=urn:btih:{hex_hash(701)}",
        raw_title="FC2-PPV-1234567 作品",
    )
    async with db.transaction() as session:
        content = await gate.ensure_content(session, subject)

    assert content.display_code == "FC2-PPV-1234567"
    assert content.title_ja == "FC2-PPV-1234567 作品"
    assert await updated_at_of(db, content_id) == STAMP


async def test_backfill_flag_does_not_touch_updated_at(db: Database) -> None:
    content_id = await seed_content(db, display_code="FC2-PPV-1234567")
    registrar = ReleaseRegistrar(db=db, gate=ContentGate())

    await registrar.handle(candidate(), backfill=True)

    async with db.session() as session:
        content = await session.get(Content, content_id)
    assert content is not None
    assert content.backfilled is True
    assert content.updated_at == STAMP


async def test_regular_registration_does_not_touch_updated_at(db: Database) -> None:
    content_id = await seed_content(db, display_code="FC2-PPV-1234567")
    registrar = ReleaseRegistrar(db=db, gate=ContentGate())

    await registrar.handle(candidate(), backfill=False)

    assert await updated_at_of(db, content_id) == STAMP


async def test_metadata_enrich_does_touch_updated_at(db: Database, tmp_path: Path) -> None:
    """The normal path must still move the timestamp — do not over-pin it."""

    from app.providers.coordinator import AggregatedMetadata
    from app.services.metadata import MetadataService

    from tests.support import make_settings

    content_id = await seed_content(db, display_code="FC2-PPV-1234567")
    service = MetadataService(
        db=db,
        coordinator=None,  # type: ignore[arg-type] - only _persist_metadata is used
        fetcher=None,  # type: ignore[arg-type]
        settings=make_settings(tmp_path),
        router=None,  # type: ignore[arg-type]
    )
    aggregated = AggregatedMetadata(
        content_key="fc2_ppv:1234567",
        title_ja="取得したタイトル",
        description_ja="取得した説明",
    )

    await service._persist_metadata(content_id, aggregated)

    async with db.session() as session:
        content = await session.get(Content, content_id)
    assert content is not None
    assert content.title_ja == "取得したタイトル"
    assert content.updated_at > STAMP


async def test_translation_does_touch_updated_at(db: Database, tmp_path: Path) -> None:
    from app.services.metadata import MetadataService

    from tests.support import make_settings

    class FakeTranslator:
        async def translate(self, text: str | None) -> str | None:
            return "中文译文"

        async def aclose(self) -> None:  # pragma: no cover - not used here
            return None

    content_id = await seed_content(db, display_code="FC2-PPV-1234567")
    service = MetadataService(
        db=db,
        coordinator=None,  # type: ignore[arg-type]
        fetcher=None,  # type: ignore[arg-type]
        settings=make_settings(tmp_path),
        router=None,  # type: ignore[arg-type]
        translator=FakeTranslator(),
    )

    translated = await service._translate(content_id, "説明文です。")

    assert translated == "中文译文"
    async with db.session() as session:
        content = await session.get(Content, content_id)
    assert content is not None
    assert content.description_zh == "中文译文"
    assert content.updated_at > STAMP
