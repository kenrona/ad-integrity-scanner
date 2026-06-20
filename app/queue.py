"""Postgres-backed work queue.

Single-machine deploy uses Postgres (already running) instead of Redis. Workers
claim jobs with ``FOR UPDATE SKIP LOCKED`` so many workers can pull concurrently
without double-processing. Swap for Redis/arq later without touching callers.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass

import asyncpg


@dataclass
class Job:
    id: int
    scan_id: uuid.UUID
    url_hash: str
    url: str
    domain: str
    tier: str
    attempts: int


async def find_inflight(conn: asyncpg.Connection, url_hash: str) -> uuid.UUID | None:
    """Return the scan_id of an already-queued/processing job for this URL, if any."""
    row = await conn.fetchrow(
        """
        SELECT scan_id FROM scan_queue
        WHERE url_hash = $1 AND status IN ('queued', 'processing')
        ORDER BY enqueued_at DESC
        LIMIT 1
        """,
        url_hash,
    )
    return row["scan_id"] if row else None


async def enqueue(
    conn: asyncpg.Connection,
    *,
    scan_id: uuid.UUID,
    url_hash: str,
    url: str,
    domain: str,
    tier: str = "static",
) -> None:
    await conn.execute(
        """
        INSERT INTO scan_queue (scan_id, url_hash, url, domain, tier)
        VALUES ($1, $2, $3, $4, $5)
        """,
        scan_id, url_hash, url, domain, tier,
    )


async def claim(conn: asyncpg.Connection, *, tier: str, batch: int) -> list[Job]:
    """Atomically claim up to `batch` queued jobs of `tier`, marking them processing."""
    rows = await conn.fetch(
        """
        UPDATE scan_queue SET
            status = 'processing',
            claimed_at = now(),
            attempts = attempts + 1
        WHERE id IN (
            SELECT id FROM scan_queue
            WHERE status = 'queued' AND tier = $1
            ORDER BY enqueued_at
            FOR UPDATE SKIP LOCKED
            LIMIT $2
        )
        RETURNING id, scan_id, url_hash, url, domain, tier, attempts
        """,
        tier, batch,
    )
    return [
        Job(
            id=r["id"], scan_id=r["scan_id"], url_hash=r["url_hash"],
            url=r["url"], domain=r["domain"], tier=r["tier"], attempts=r["attempts"],
        )
        for r in rows
    ]


async def mark_done(conn: asyncpg.Connection, job_id: int) -> None:
    await conn.execute("UPDATE scan_queue SET status = 'done' WHERE id = $1", job_id)


async def requeue(conn: asyncpg.Connection, job_id: int, err: str) -> None:
    """Return a failed job to the queue for another attempt (keeps attempts count)."""
    await conn.execute(
        """
        UPDATE scan_queue
        SET status = 'queued', claimed_at = NULL, last_error = $2
        WHERE id = $1
        """,
        job_id, err[:2000],
    )


async def mark_error(conn: asyncpg.Connection, job_id: int, err: str) -> None:
    """Park a job as permanently failed (attempts exhausted)."""
    await conn.execute(
        "UPDATE scan_queue SET status = 'error', last_error = $2 WHERE id = $1",
        job_id, err[:2000],
    )


async def reap_stale(
    conn: asyncpg.Connection, *, timeout_seconds: int, max_attempts: int
) -> dict[str, int]:
    """Recover jobs stuck in 'processing' (worker crashed mid-job).

    Requeue if attempts remain, else park as error. Without this, a crash leaks
    rows that are never retried.
    """
    rows = await conn.fetch(
        """
        UPDATE scan_queue SET
            status = CASE WHEN attempts < $2 THEN 'queued' ELSE 'error' END,
            claimed_at = NULL,
            last_error = COALESCE(last_error, 'reaped: visibility timeout')
        WHERE status = 'processing'
          AND claimed_at < now() - make_interval(secs => $1)
        RETURNING status
        """,
        timeout_seconds, max_attempts,
    )
    requeued = sum(1 for r in rows if r["status"] == "queued")
    return {"requeued": requeued, "parked": len(rows) - requeued}


async def prune(conn: asyncpg.Connection, retention_seconds: int) -> int:
    """Delete terminal (done/error) rows older than retention to bound table growth."""
    res = await conn.execute(
        """
        DELETE FROM scan_queue
        WHERE status IN ('done', 'error')
          AND enqueued_at < now() - make_interval(secs => $1)
        """,
        retention_seconds,
    )
    return int(res.split()[-1])  # "DELETE <n>"


async def get_stats(conn: asyncpg.Connection) -> dict:
    """Queue depth by (tier, status) + completed-results count, for /stats."""
    rows = await conn.fetch(
        "SELECT tier, status, count(*) AS n FROM scan_queue GROUP BY tier, status"
    )
    queue: dict[str, dict[str, int]] = {}
    for r in rows:
        queue.setdefault(r["tier"], {})[r["status"]] = r["n"]
    results = await conn.fetchval("SELECT count(*) FROM scan_results")
    return {"queue": queue, "results_total": results}
