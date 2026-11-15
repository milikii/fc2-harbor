#!/usr/bin/env python3
"""Recompute ``contents.censorship`` for content that is already in the database.

The tag used to be read from explicit wording on the FC2 page only, so almost every
row ended up ``unknown``. The vocabulary now covers ``【無】`` / ``【有】`` / ``【薄】``
and friends, but enrichment never runs again for content that already has assets, so
the historical rows stay wrong until something recomputes them.

This script does **only** that:

* reads the texts that are already stored (``contents.title_ja``,
  ``contents.description_ja`` and ``releases.raw_title``) — **no HTTP requests**;
* updates ``contents.censorship`` and merges ``censorship_evidence`` into the
  existing ``provider_results.result_json["raw"]`` of the row that produced the tag;
* touches nothing else: no assets, no ``metadata_status``, no translation, no card,
  and ``updated_at`` is left alone because it describes the metadata fetch.

Defaults to fixing ``unknown`` rows only; ``--force`` also overwrites a tag that is
already set (so a manual correction is never lost by accident).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db.enums import Censorship
from app.db.models import Content, ProviderResult, Release
from app.db.session import Database, migrate_async
from app.providers.base import infer_censorship
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

#: The provider whose row is authoritative for the official page.
PREFERRED_PROVIDER = "Fc2"

CENSORSHIP_LABELS: dict[Censorship, str] = {
    Censorship.CENSORED: "有码",
    Censorship.UNCENSORED: "无码",
    Censorship.THIN: "薄码",
    Censorship.UNKNOWN: "未知",
}


class _DryRunRollbackError(Exception):
    """Raised inside the transaction so ``--dry-run`` writes nothing."""


def label(value: Censorship) -> str:
    return CENSORSHIP_LABELS.get(value, str(value))


@dataclass(slots=True)
class RecensorReport:
    dry_run: bool
    scanned: int = 0
    changed: int = 0
    #: Non-unknown tag kept as it was (``--force`` not given).
    kept: int = 0
    #: The verdict equals the stored value: nothing to do (idempotent re-run).
    unchanged: int = 0
    #: No wording in any field — the row stays unknown, by design.
    no_evidence: int = 0
    raw_updated: int = 0
    #: Rows that changed but had no ``provider_results`` row to merge evidence into.
    raw_missing: int = 0
    before: Counter[str] = field(default_factory=Counter)
    after: Counter[str] = field(default_factory=Counter)
    changes: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "counts": {
                "scanned": self.scanned,
                "changed": self.changed,
                "kept": self.kept,
                "unchanged": self.unchanged,
                "no_evidence": self.no_evidence,
                "raw_updated": self.raw_updated,
                "raw_missing": self.raw_missing,
            },
            "distribution": {
                "before": _ordered(self.before),
                "after": _ordered(self.after),
            },
            "changes": self.changes,
        }


def _ordered(counter: Counter[str]) -> dict[str, int]:
    order = ["有码", "无码", "薄码", "未知"]
    return {name: counter.get(name, 0) for name in order}


async def recensor(
    session: AsyncSession,
    *,
    force: bool,
    report: RecensorReport,
) -> RecensorReport:
    """Recompute the tag for every content row; returns the filled report."""

    contents = list((await session.scalars(select(Content).order_by(Content.created_at))).all())
    for content in contents:
        report.scanned += 1
        report.before[label(content.censorship)] += 1

        fields = {
            "title": content.title_ja,
            "description": content.description_ja,
            "release_title": await _release_titles(session, content.id),
        }
        verdict, evidence = infer_censorship(fields)

        if verdict is None:
            report.no_evidence += 1
            report.after[label(content.censorship)] += 1
            continue
        if verdict is content.censorship:
            # Already the computed value: a second run changes nothing.
            report.unchanged += 1
            report.after[label(content.censorship)] += 1
            continue
        if content.censorship is not Censorship.UNKNOWN and not force:
            # A tag that is already set may have been corrected by hand.
            report.kept += 1
            report.after[label(content.censorship)] += 1
            continue

        old = content.censorship
        content.censorship = verdict
        report.changed += 1
        report.after[label(verdict)] += 1

        merged = await _merge_evidence(session, content.id, evidence)
        if merged:
            report.raw_updated += 1
        else:
            report.raw_missing += 1

        report.changes.append(
            {
                "canonical_key": content.canonical_key,
                "display_code": content.display_code,
                "old": old.value,
                "new": verdict.value,
                "fields": sorted({str(item["field"]) for item in evidence["matches"]}),
                "tokens": sorted({str(item["token"]) for item in evidence["matches"]}),
                "raw_updated": merged,
            }
        )
    return report


async def _release_titles(session: AsyncSession, content_id: str) -> str | None:
    titles = list(
        (
            await session.scalars(
                select(Release.raw_title)
                .where(Release.content_id == content_id, Release.raw_title.is_not(None))
                .order_by(Release.published_at.asc().nullslast(), Release.created_at)
            )
        ).all()
    )
    joined = " | ".join(title for title in titles if title)
    return joined or None


async def _merge_evidence(
    session: AsyncSession, content_id: str, evidence: dict[str, object]
) -> bool:
    """Merge ``censorship_evidence`` into the existing ``raw`` payload.

    Merging (not replacing) keeps every other key the provider wrote there.
    """

    rows = list(
        (
            await session.scalars(
                select(ProviderResult)
                .where(ProviderResult.content_id == content_id)
                .order_by(ProviderResult.fetched_at.desc())
            )
        ).all()
    )
    if not rows:
        return False
    target = next((row for row in rows if row.provider == PREFERRED_PROVIDER), rows[0])
    payload = dict(target.result_json or {})
    raw = dict(payload.get("raw") or {})
    raw["censorship_evidence"] = evidence
    payload["raw"] = raw
    target.result_json = payload
    return True


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Recompute contents.censorship from stored titles and descriptions"
    )
    parser.add_argument("--config", default=None, help="config.yaml (default: FC2_CONFIG)")
    parser.add_argument(
        "--dry-run", action="store_true", help="print the plan, write nothing"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="also overwrite tags that are already set (default: only unknown)",
    )
    parser.add_argument(
        "--report", default="recensor-report.json", help="report path"
    )
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> RecensorReport:
    settings = Settings.load(args.config)
    await migrate_async(settings.app.database_url)
    database = Database(settings.app.database_url)
    report = RecensorReport(dry_run=bool(args.dry_run))
    try:
        try:
            async with database.transaction() as session:
                await recensor(session, force=bool(args.force), report=report)
                if args.dry_run:
                    raise _DryRunRollbackError
        except _DryRunRollbackError:
            pass
    finally:
        await database.dispose()
    return report


def print_summary(report: RecensorReport) -> None:
    counts = report.as_dict()["counts"]
    assert isinstance(counts, dict)
    prefix = "dry-run: " if report.dry_run else ""
    print(
        f"{prefix}扫描 {counts['scanned']} 条，将变化 {counts['changed']} 条"
        f"（保留已设置 {counts['kept']}，无依据 {counts['no_evidence']}，"
        f"已是目标值 {counts['unchanged']}）"
    )
    before = report.as_dict()["distribution"]
    assert isinstance(before, dict)
    print(f"  变化前：{before['before']}")
    print(f"  变化后：{before['after']}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.monotonic()
    try:
        report = asyncio.run(run(args))
    except Exception as exc:  # pragma: no cover - CLI surface
        print(f"recensor failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "force": bool(args.force),
        **report.as_dict(),
    }
    Path(args.report).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print_summary(report)
    print(f"  报告：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
