"""Per-domain signal cache (ads.txt / app-ads.txt / robots / ...).

Domain-level files are identical across every page of a domain, so we fetch them
once and cache with the domain TTL. At 1M page URLs/day this collapses to far
fewer domain fetches.
"""
from __future__ import annotations

import datetime as dt
import json
from typing import Any

import asyncpg


async def get(conn: asyncpg.Connection, domain: str) -> dict[str, Any] | None:
    """Return cached domain signals iff present and not past the domain TTL."""
    row = await conn.fetchrow(
        """
        SELECT signals FROM domain_signals
        WHERE domain = $1 AND expires_at IS NOT NULL AND expires_at > now()
        """,
        domain,
    )
    return json.loads(row["signals"]) if row else None


async def put(
    conn: asyncpg.Connection, domain: str, signals: dict[str, Any], ttl_seconds: int
) -> None:
    expires_at = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=ttl_seconds)
    await conn.execute(
        """
        INSERT INTO domain_signals (domain, signals, fetched_at, expires_at)
        VALUES ($1, $2, now(), $3)
        ON CONFLICT (domain) DO UPDATE SET
            signals = EXCLUDED.signals,
            fetched_at = EXCLUDED.fetched_at,
            expires_at = EXCLUDED.expires_at
        """,
        domain, json.dumps(signals), expires_at,
    )
