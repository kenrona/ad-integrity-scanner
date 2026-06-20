"""Shared result persistence used by both the static and render workers."""
from __future__ import annotations

import json

import asyncpg

from app import ledger, queue
from app.config import Settings
from app.queue import Job


async def persist(
    conn: asyncpg.Connection, job: Job, result: dict, settings: Settings
) -> None:
    """Upsert the scan_results row, stamp the ledger, and close the queue job."""
    async with conn.transaction():
        await conn.execute(
            """
            INSERT INTO scan_results (
                scan_id, url_hash, url, domain, scan_tier, confidence,
                signals, metrics, sub_scores, score_breakdown, integrity_score,
                scanner_version, scoring_version
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13)
            ON CONFLICT (scan_id) DO UPDATE SET
                scan_tier = EXCLUDED.scan_tier,
                confidence = EXCLUDED.confidence,
                signals = EXCLUDED.signals,
                metrics = EXCLUDED.metrics,
                sub_scores = EXCLUDED.sub_scores,
                score_breakdown = EXCLUDED.score_breakdown,
                integrity_score = EXCLUDED.integrity_score,
                scanned_at = now()
            """,
            job.scan_id, job.url_hash, job.url, job.domain,
            result["scan_tier"], result["confidence"],
            json.dumps(result["signals"]), json.dumps(result["metrics"]),
            json.dumps(result["sub_scores"]), json.dumps(result.get("score_breakdown", {})),
            result["integrity_score"],
            settings.scanner_version, settings.scoring_version,
        )
        await ledger.mark_scanned(
            conn, url_hash=job.url_hash, scan_id=job.scan_id,
            page_ttl_seconds=settings.page_ttl_seconds,
        )
        await queue.mark_done(conn, job.id)
