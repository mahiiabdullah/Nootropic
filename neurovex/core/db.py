"""SQLite (WAL) database access via SQLAlchemy async.

One database file holds everything Neurovex knows (see docs/ARCHITECTURE.md §10).
Schema is created with `create_all` for now; Alembic migrations arrive with the
full Phase 0 scaffold (P0.1).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, event
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.types import TypeDecorator


class Base(DeclarativeBase):
    pass


def utcnow() -> datetime:
    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Timezone-aware datetimes on SQLite, which has no timezone support.

    Values are converted to UTC and stored naive; they come back tagged as UTC,
    so Python-side comparisons with `utcnow()` always work.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect) -> datetime | None:  # type: ignore[no-untyped-def]
        if value is None:
            return None
        if value.tzinfo is None:
            raise ValueError("Naive datetime passed to UTCDateTime; use an aware datetime")
        return value.astimezone(UTC).replace(tzinfo=None)

    def process_result_value(self, value: datetime | None, dialect) -> datetime | None:  # type: ignore[no-untyped-def]
        return value.replace(tzinfo=UTC) if value is not None else None


def create_engine(db_url: str) -> AsyncEngine:
    engine = create_async_engine(db_url, connect_args={"timeout": 30})

    @event.listens_for(engine.sync_engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record) -> None:  # type: ignore[no-untyped-def]
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.close()

    return engine


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)


async def init_db(engine: AsyncEngine) -> None:
    # Import models so they register on Base.metadata.
    import neurovex.conversation.models as conversation_models
    import neurovex.llm.usage  # noqa: F401
    import neurovex.memory.models as memory_models
    import neurovex.modules.news.models  # noqa: F401
    import neurovex.modules.tasks.models  # noqa: F401
    import neurovex.today.models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.run_sync(_add_missing_columns)
        # Extra DDL that create_all can't express (FTS5 virtual tables, triggers).
        for mod in (conversation_models, memory_models):
            await conn.run_sync(mod.install)


def ensure_fts(conn, *, table: str, fts: str, column: str) -> None:  # type: ignore[no-untyped-def]
    """Full-text index (FTS5, porter stemming) over `table.column`, synced by triggers.

    External-content table: the text is stored once, in `table`. Created on first
    run and filled from existing rows, so upgrading keeps old data searchable.
    """
    exists = conn.exec_driver_sql(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (fts,)
    ).first()
    conn.exec_driver_sql(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {fts} USING fts5({column}, content='{table}', "
        f"content_rowid='id', tokenize='porter unicode61')"
    )
    conn.exec_driver_sql(
        f"CREATE TRIGGER IF NOT EXISTS {fts}_ai AFTER INSERT ON {table} BEGIN "
        f"INSERT INTO {fts}(rowid, {column}) VALUES (new.id, new.{column}); END"
    )
    conn.exec_driver_sql(
        f"CREATE TRIGGER IF NOT EXISTS {fts}_ad AFTER DELETE ON {table} BEGIN "
        f"INSERT INTO {fts}({fts}, rowid, {column}) VALUES ('delete', old.id, old.{column}); END"
    )
    conn.exec_driver_sql(
        f"CREATE TRIGGER IF NOT EXISTS {fts}_au AFTER UPDATE OF {column} ON {table} BEGIN "
        f"INSERT INTO {fts}({fts}, rowid, {column}) VALUES ('delete', old.id, old.{column}); "
        f"INSERT INTO {fts}(rowid, {column}) VALUES (new.id, new.{column}); END"
    )
    if not exists:
        conn.exec_driver_sql(f"INSERT INTO {fts}({fts}) VALUES ('rebuild')")


def _add_missing_columns(conn) -> None:  # type: ignore[no-untyped-def]
    """Stopgap until Alembic: add new *nullable* columns to existing tables.

    `create_all` only creates missing tables, so a column added to a model would
    otherwise break an existing database. Non-nullable additions still need a
    real migration.
    """
    for table in Base.metadata.sorted_tables:
        existing = {row[1] for row in conn.exec_driver_sql(f'PRAGMA table_info("{table.name}")')}
        for col in table.columns:
            if col.name in existing:
                continue
            if not col.nullable:
                raise RuntimeError(
                    f"Column {table.name}.{col.name} is NOT NULL and needs a real migration"
                )
            ddl_type = col.type.compile(dialect=conn.dialect)
            conn.exec_driver_sql(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}')
