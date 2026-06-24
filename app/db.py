"""Async Postgres pool + schema bootstrap."""
from __future__ import annotations

import asyncio
import pathlib

import asyncpg

from app.config import get_settings

_SCHEMA_PATH = pathlib.Path(__file__).resolve().parent.parent / "sql" / "schema.sql"

_pool: asyncpg.Pool | None = None

# Postgres errors that are safe to retry: the transaction was aborted by the
# server (deadlock victim / serialization failure) but the operation itself is
# valid, so re-running it in a fresh transaction normally succeeds. Expected
# under heavy concurrent enqueue + multi-worker write load.
_TRANSIENT_ERRORS = (
    asyncpg.DeadlockDetectedError,
    asyncpg.SerializationError,
)


async def with_retry(make_coro, *, attempts: int = 5, base_delay: float = 0.05):
    """Run an async DB operation, retrying transient errors with backoff.

    `make_coro` is a zero-arg async callable invoked FRESH on each attempt (so it
    opens a new connection/transaction each time). Non-transient errors propagate
    immediately. Raises the last transient error if all attempts are exhausted.
    """
    for i in range(attempts):
        try:
            return await make_coro()
        except _TRANSIENT_ERRORS:
            if i == attempts - 1:
                raise
            await asyncio.sleep(base_delay * (2 ** i))


async def init_pool(*, apply_schema: bool = True) -> asyncpg.Pool:
    """Create the connection pool, optionally applying the (idempotent) schema.

    apply_schema runs sql/schema.sql, which is DDL (CREATE/ALTER/VIEW) needing
    ACCESS EXCLUSIVE locks. Under live write load (active workers/enqueue) that DDL
    can DEADLOCK against row writes, so long-running workers that attach to an
    already-migrated DB should pass apply_schema=False. The app/migration path
    applies the schema once; workers assume it exists.
    """
    global _pool
    if _pool is not None:
        return _pool
    settings = get_settings()
    _pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.db_pool_min,
        max_size=settings.db_pool_max,
    )
    if apply_schema:
        schema_sql = _SCHEMA_PATH.read_text(encoding="utf-8")
        async with _pool.acquire() as conn:
            await conn.execute(schema_sql)
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("db pool not initialized; call init_pool() first")
    return _pool
