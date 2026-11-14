"""Async engine / session factory with SQLite hardening.

Spec references: section 3 (SQLAlchemy 2.x + Alembic, SQLite WAL, local disk),
section 11.2 (the gate check and the accepted write must share one
transaction), section 13 (conditional updates instead of distributed locks).

Notes
-----
* WAL, ``foreign_keys=ON`` and a generous ``busy_timeout`` are set per
  connection.
* Write transactions are serialised by an in-process ``asyncio.Lock``. The
  application is a single container and the only writer of this SQLite file, so
  that is both simpler and safer than per-connection lock juggling: "check then
  write" inside one transaction stays atomic, readers in WAL mode never block, and
  a nested write transaction fails fast instead of deadlocking on a lock.
* The application never creates tables itself: schema changes only happen
  through Alembic.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import Pool

from app.logging import get_logger

_log = get_logger("db")

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: Guards against a write transaction being opened from inside another one in the
#: same task (a deadlock that would otherwise show up as a 30s SQLite timeout).
_TX_DEPTH: ContextVar[int] = ContextVar("db_tx_depth", default=0)


def _is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


class Database:
    """Owns the async engine and hands out sessions/transactions."""

    def __init__(self, url: str, *, echo: bool = False) -> None:
        if not url:
            raise ValueError("database url must not be empty")
        self.url = url
        self._is_sqlite = _is_sqlite(url)
        kwargs: dict[str, Any] = {"echo": echo, "future": True}
        if self._is_sqlite:
            kwargs["connect_args"] = {"timeout": 30}
        self.engine: AsyncEngine = create_async_engine(url, **kwargs)
        if self._is_sqlite:
            self._install_sqlite_pragmas(self.engine.sync_engine)
        self.session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self.engine, expire_on_commit=False, autoflush=False
        )
        self._write_lock = asyncio.Lock()

    @staticmethod
    def _install_sqlite_pragmas(engine: Any) -> None:
        @event.listens_for(engine, "connect")
        def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA busy_timeout=30000")
            finally:
                cursor.close()

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Read-mostly session; callers manage their own transaction."""

        async with self.session_factory() as session:
            yield session

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        """Explicit write transaction: everything commits or nothing does.

        Serialised per database instance so a gate check and its write cannot
        interleave with another writer. Read-only work must use :meth:`session`
        instead — opening a write transaction from inside one raises immediately.
        """

        if _TX_DEPTH.get():
            raise RuntimeError(
                "nested write transaction: use Database.session() for reads, or "
                "pass the existing session down"
            )
        token = _TX_DEPTH.set(1)
        try:
            async with self._write_lock:
                async with self.session_factory() as session, session.begin():
                    yield session
        finally:
            _TX_DEPTH.reset(token)

    async def ping(self) -> None:
        """Cheap connectivity check used by the readiness endpoint."""

        async with self.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))

    async def dispose(self) -> None:
        await self.engine.dispose()

    def close_pool(self) -> None:
        """Synchronous pool close, for interpreter-shutdown paths."""

        pool: Pool = self.engine.pool
        pool.dispose()


def run_migrations(database_url: str, *, alembic_ini: Path | None = None) -> None:
    """Apply ``alembic upgrade head`` programmatically (blocking)."""

    from alembic import command
    from alembic.config import Config

    ini_path = alembic_ini or PROJECT_ROOT / "alembic.ini"
    if not ini_path.exists():
        raise RuntimeError(f"alembic.ini not found at {ini_path}")
    config = Config(str(ini_path))
    config.set_main_option("script_location", str(PROJECT_ROOT / "app" / "db" / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    _log.info("db.migrate.start", url=_safe_url(database_url))
    command.upgrade(config, "head")
    _log.info("db.migrate.done")


async def migrate_async(database_url: str) -> None:
    """Run migrations in a worker thread so the event loop stays responsive."""

    await asyncio.to_thread(run_migrations, database_url)


def _safe_url(url: str) -> str:
    """Database URL with any password removed, for logs."""

    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, host = rest.rpartition("@")
    return f"{scheme}://***@{host}"
