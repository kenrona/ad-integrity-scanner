"""Submit/status orchestration shared by the API (and usable from scripts)."""
from __future__ import annotations

import json
import uuid

import asyncpg

from app import ledger, queue
from app.config import get_settings
from app.logging_config import get_logger, kv
from app.models import ScanAccepted, ScanStatus
from app.normalize import normalize_url

log = get_logger("service")


async def submit_scan(pool: asyncpg.Pool, raw_url: str) -> ScanAccepted:
    """Normalize, dedup against the tiered TTL ledger, and enqueue if needed."""
    settings = get_settings()
    norm = normalize_url(raw_url, strip_tracking=settings.strip_tracking_params)

    async with pool.acquire() as conn:
        async with conn.transaction():
            fresh = await ledger.get_fresh(conn, norm.url_hash)
            if fresh is not None:
                return ScanAccepted(
                    scan_id=fresh["last_scan_id"], url=norm.url,
                    url_hash=norm.url_hash, domain=norm.domain, status="fresh",
                )

            inflight = await queue.find_inflight(conn, norm.url_hash)
            if inflight is not None:
                return ScanAccepted(
                    scan_id=inflight, url=norm.url, url_hash=norm.url_hash,
                    domain=norm.domain, status="inflight",
                )

            scan_id = uuid.uuid4()
            await ledger.reserve(
                conn, url_hash=norm.url_hash, url=norm.url,
                domain=norm.domain, scan_id=scan_id,
            )
            await queue.enqueue(
                conn, scan_id=scan_id, url_hash=norm.url_hash,
                url=norm.url, domain=norm.domain, tier="static",
            )
            log.info("queued %s", kv(scan_id=scan_id, domain=norm.domain,
                                     url_hash=norm.url_hash[:12]))
            return ScanAccepted(
                scan_id=scan_id, url=norm.url, url_hash=norm.url_hash,
                domain=norm.domain, status="queued",
            )


async def get_status(pool: asyncpg.Pool, scan_id: uuid.UUID) -> ScanStatus:
    async with pool.acquire() as conn:
        result = await conn.fetchrow(
            """
            SELECT scan_id, url_hash, url, domain, scan_tier, confidence,
                   integrity_score, sub_scores, score_breakdown, metrics, scanned_at
            FROM scan_results WHERE scan_id = $1
            """,
            scan_id,
        )
        if result is not None:
            return ScanStatus(
                scan_id=result["scan_id"], url_hash=result["url_hash"],
                url=result["url"], domain=result["domain"], state="done",
                scan_tier=result["scan_tier"], confidence=result["confidence"],
                integrity_score=result["integrity_score"],
                sub_scores=json.loads(result["sub_scores"]),
                score_breakdown=json.loads(result["score_breakdown"]) if result["score_breakdown"] else {},
                metrics=json.loads(result["metrics"]),
                scanned_at=result["scanned_at"],
            )

        q = await conn.fetchrow(
            """
            SELECT scan_id, url_hash, url, domain, status, last_error
            FROM scan_queue WHERE scan_id = $1
            ORDER BY enqueued_at DESC LIMIT 1
            """,
            scan_id,
        )
        if q is not None:
            state = "queued" if q["status"] == "queued" else (
                "processing" if q["status"] == "processing" else q["status"]
            )
            return ScanStatus(
                scan_id=q["scan_id"], url_hash=q["url_hash"], url=q["url"],
                domain=q["domain"], state=state, last_error=q["last_error"],
            )

        return ScanStatus(
            scan_id=scan_id, url_hash="", url="", domain="", state="unknown",
        )
