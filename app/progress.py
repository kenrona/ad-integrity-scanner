"""job_runs CRUD for UI-polled progress on long server operations.

Each helper takes a caller-supplied connection and does a single statement so the
update commits promptly (autocommit on a pooled connection). Background tasks
should acquire a fresh connection per advance batch rather than holding one for
the whole job, so the UI's /jobs/{id} poll observes progress as it happens.
"""
from __future__ import annotations

import asyncpg

_VALID_KINDS = {"ingest", "scan_batch", "refresh", "profile"}


async def create_job(conn: asyncpg.Connection, *, kind: str, total: int = 0,
                     message: str | None = None, dataset_id: int | None = None) -> int:
    """Insert a 'running' job_runs row; return its id.

    kind in {'ingest','scan_batch','refresh','profile'}.
    """
    if kind not in _VALID_KINDS:
        raise ValueError(f"invalid job kind: {kind!r}")
    return await conn.fetchval(
        """
        INSERT INTO job_runs (kind, status, total, done, message, dataset_id)
        VALUES ($1, 'running', $2, 0, $3, $4)
        RETURNING id
        """,
        kind, total, message, dataset_id,
    )


async def advance(conn: asyncpg.Connection, job_id: int, *, done_delta: int = 1,
                  message: str | None = None) -> None:
    """Increment done by done_delta; optionally update message.

    Used inside a work loop. When message is None the existing message is kept.
    """
    await conn.execute(
        """
        UPDATE job_runs
        SET done = done + $2,
            message = COALESCE($3, message)
        WHERE id = $1
        """,
        job_id, done_delta, message,
    )


async def set_total(conn: asyncpg.Connection, job_id: int, total: int) -> None:
    """Set the known total once it is computed (e.g. after parsing a file)."""
    await conn.execute(
        "UPDATE job_runs SET total = $2 WHERE id = $1",
        job_id, total,
    )


async def finish(conn: asyncpg.Connection, job_id: int, *, message: str | None = None) -> None:
    """Mark status='done', finished_at=now()."""
    await conn.execute(
        """
        UPDATE job_runs
        SET status = 'done',
            finished_at = now(),
            message = COALESCE($2, message)
        WHERE id = $1
        """,
        job_id, message,
    )


async def fail(conn: asyncpg.Connection, job_id: int, error: str) -> None:
    """Mark status='error', error=error[:4000], finished_at=now()."""
    await conn.execute(
        """
        UPDATE job_runs
        SET status = 'error',
            error = $2,
            finished_at = now()
        WHERE id = $1
        """,
        job_id, error[:4000],
    )


async def get_job(conn: asyncpg.Connection, job_id: int) -> dict | None:
    """Return the job_runs row as a dict, or None if missing. For GET /jobs/{id}."""
    row = await conn.fetchrow(
        """
        SELECT id, kind, status, total, done, message, dataset_id, error,
               started_at, finished_at
        FROM job_runs WHERE id = $1
        """,
        job_id,
    )
    return dict(row) if row is not None else None
