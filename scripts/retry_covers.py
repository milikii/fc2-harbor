#!/usr/bin/env python3
"""Give contents that ran out of cover retries one more chance.

``publisher.require_cover`` stops pushing a card after ``cover_retry_limit`` rounds
without a cover and marks the row ``cover_status='give_up'``. When the metadata
sources improve (for example the Nyaa fallback was fixed), those rows deserve a new
attempt — otherwise they stay invisible forever.

This script resets them:

* ``cover_status='give_up'`` → ``pending``
* ``cover_retry_count`` → ``0``

Nothing else is touched. The publisher picks them up on its next round because a
content without a notification row stays in the push queue.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from app.config import Settings
from app.db.enums import CoverStatus
from app.db.models import Content
from app.db.session import Database, migrate_async
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession


class _DryRunRollbackError(Exception):
    """Raised inside the transaction so ``--dry-run`` writes nothing."""


@dataclass(slots=True)
class RetryReport:
    dry_run: bool
    scanned: int = 0
    reset: int = 0
    skipped: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "counts": {"scanned": self.scanned, "reset": self.reset, "skipped": self.skipped},
            "contents": self.rows,
        }


async def retry_covers(
    session: AsyncSession,
    *,
    since_days: int | None = None,
    only: str | None = None,
    report: RetryReport,
) -> RetryReport:
    """Reset every ``give_up`` row (optionally filtered); idempotent."""

    stmt = select(Content).where(Content.cover_status == CoverStatus.GIVE_UP.value)
    if only:
        stmt = stmt.where(Content.canonical_key == only)
    if since_days is not None:
        cutoff = datetime.now(UTC) - timedelta(days=since_days)
        # ``created_at``, not ``updated_at``: the latter is metadata-fetch time and
        # has been polluted by older tooling (docs/deviations.md §40).
        stmt = stmt.where(Content.created_at >= cutoff)
    stmt = stmt.order_by(Content.created_at)

    for content in (await session.scalars(stmt)).all():
        report.scanned += 1
        await session.execute(
            update(Content)
            .where(Content.id == content.id)
            .values(
                cover_status=CoverStatus.PENDING.value,
                cover_retry_count=0,
                # Bookkeeping only: never touch ``updated_at`` (§42).
                updated_at=Content.updated_at,
            )
            .execution_options(synchronize_session=False)
        )
        report.reset += 1
        report.rows.append(
            {
                "canonical_key": content.canonical_key,
                "display_code": content.display_code,
                "was_retry_count": content.cover_retry_count,
            }
        )
    return report


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Reset contents that gave up on getting a cover"
    )
    parser.add_argument("--config", default=None, help="config.yaml (default: FC2_CONFIG)")
    parser.add_argument("--dry-run", action="store_true", help="count only, write nothing")
    parser.add_argument(
        "--since",
        type=int,
        default=None,
        metavar="DAYS",
        help="only contents created in the last DAYS days",
    )
    parser.add_argument(
        "--only", default=None, metavar="CANONICAL_KEY", help="only this canonical key"
    )
    parser.add_argument("--report", default="retry-covers-report.json", help="report path")
    return parser.parse_args(argv)


async def run(args: argparse.Namespace) -> RetryReport:
    settings = Settings.load(args.config)
    await migrate_async(settings.app.database_url)
    database = Database(settings.app.database_url)
    report = RetryReport(dry_run=bool(args.dry_run))
    try:
        try:
            async with database.transaction() as session:
                await retry_covers(
                    session,
                    since_days=args.since,
                    only=args.only,
                    report=report,
                )
                if args.dry_run:
                    raise _DryRunRollbackError
        except _DryRunRollbackError:
            pass
    finally:
        await database.dispose()
    return report


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.monotonic()
    try:
        report = asyncio.run(run(args))
    except Exception as exc:  # pragma: no cover - CLI surface
        print(f"retry_covers failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "since_days": args.since,
        "only": args.only,
        **report.as_dict(),
    }
    Path(args.report).write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    prefix = "dry-run: " if report.dry_run else ""
    print(
        f"{prefix}扫描 {report.scanned} 条 give_up，"
        f"{'将重置' if report.dry_run else '已重置'} {report.reset} 条为 pending（retry_count=0）"
    )
    print(f"  报告：{args.report}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
