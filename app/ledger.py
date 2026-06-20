"""Scan ledger: dedup + tiered-TTL freshness for page-level results."""
from __future__ import annotations

import datetime as dt
import uuid

import asyncpg


async def get_fresh(conn: asyncpg.Connection, url_hash: str) -> asyncpg.Record | None:
    """Return the ledger row iff it exists and its page TTL has not expired."""
    return await conn.fetchrow(
        """
        SELECT url_hash, url, domain, last_scan_id, last_scanned, expires_at
        FROM scan_ledger
        WHERE url_hash = $1 AND expires_at IS NOT NULL AND expires_at > now()
        """,
        url_hash,
    )


async def reserve(
    conn: asyncpg.Connection,
    *,
    url_hash: str,
    url: str,
    domain: str,
    scan_id: uuid.UUID,
) -> None:
    """Record intent to (re)scan: upsert the ledger pointing at the new scan_id.

    Leaves last_scanned/expires_at untouched until the worker completes.
    """
    await conn.execute(
        """
        INSERT INTO scan_ledger (url_hash, url, domain, last_scan_id)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (url_hash) DO UPDATE SET
            url = EXCLUDED.url,
            domain = EXCLUDED.domain,
            last_scan_id = EXCLUDED.last_scan_id
        """,
        url_hash, url, domain, scan_id,
    )


async def due_for_rescan(conn: asyncpg.Connection, limit: int) -> list[asyncpg.Record]:
    """Page results whose TTL has expired — candidates for re-scanning."""
    return await conn.fetch(
        """
        SELECT url FROM scan_ledger
        WHERE expires_at IS NOT NULL AND expires_at < now()
        ORDER BY expires_at
        LIMIT $1
        """,
        limit,
    )


async def mark_scanned(
    conn: asyncpg.Connection,
    *,
    url_hash: str,
    scan_id: uuid.UUID,
    page_ttl_seconds: int,
) -> None:
    """Stamp completion + set the page-TTL expiry boundary."""
    expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=page_ttl_seconds)
    await conn.execute(
        """
        UPDATE scan_ledger
        SET last_scan_id = $2, last_scanned = now(), expires_at = $3
        WHERE url_hash = $1
        """,
        url_hash, scan_id, expires_at,
    )
