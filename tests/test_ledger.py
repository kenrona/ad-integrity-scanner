"""Ledger + queue + submit integration tests.

Skipped automatically unless a Postgres test DB is reachable. Point AI_DATABASE_URL
at a throwaway DB (e.g. ad_integrity_test) before running — these tables get written.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _pool_or_skip():
    try:
        from app.db import init_pool
        return await init_pool()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no postgres available: {e}")


@pytest.fixture
async def pool():
    if not os.environ.get("AI_DATABASE_URL"):
        pytest.skip("set AI_DATABASE_URL to a throwaway test DB to run integration tests")
    p = await _pool_or_skip()
    # Clean slate for deterministic assertions.
    async with p.acquire() as conn:
        await conn.execute("TRUNCATE scan_queue, scan_ledger, scan_results, domain_signals")
    yield p
    from app.db import close_pool
    await close_pool()


async def test_submit_enqueues_then_dedups_inflight(pool):
    from app import service

    first = await service.submit_scan(pool, "https://example.com/article?utm_source=x")
    assert first.status == "queued"

    # Same logical URL (cosmetic differences only) while still queued -> inflight
    # dedup, same scan_id. Scheme is significant, so both stay https.
    second = await service.submit_scan(pool, "https://EXAMPLE.com/article/#frag")
    assert second.status == "inflight"
    assert second.scan_id == first.scan_id
    assert second.url_hash == first.url_hash


async def test_claim_and_complete_flow(pool):
    from app import ledger, queue, service
    from app.config import get_settings

    accepted = await service.submit_scan(pool, "https://example.com/p1")
    async with pool.acquire() as conn:
        jobs = await queue.claim(conn, tier="static", batch=10)
    assert len(jobs) == 1
    job = jobs[0]

    async with pool.acquire() as conn:
        await ledger.mark_scanned(
            conn, url_hash=job.url_hash, scan_id=job.scan_id,
            page_ttl_seconds=get_settings().page_ttl_seconds,
        )
        await queue.mark_done(conn, job.id)
        fresh = await ledger.get_fresh(conn, job.url_hash)
    assert fresh is not None  # within TTL now

    # Re-submitting a fresh URL returns the cached scan without new work.
    again = await service.submit_scan(pool, "https://example.com/p1")
    assert again.status == "fresh"
    assert again.scan_id == accepted.scan_id


async def test_failed_job_requeues_then_parks(pool):
    from app import queue, service

    await service.submit_scan(pool, "https://example.com/flaky")
    # max_attempts defaults to 3: attempts 1 and 2 requeue, attempt 3 parks.
    for expected_attempt, should_requeue in [(1, True), (2, True), (3, False)]:
        async with pool.acquire() as conn:
            jobs = await queue.claim(conn, tier="static", batch=10)
            assert len(jobs) == 1
            job = jobs[0]
            assert job.attempts == expected_attempt
            if should_requeue:
                await queue.requeue(conn, job.id, "boom")
            else:
                await queue.mark_error(conn, job.id, "boom")

    async with pool.acquire() as conn:
        # Nothing left claimable; the job is parked as error.
        assert await queue.claim(conn, tier="static", batch=10) == []
        stats = await queue.get_stats(conn)
    assert stats["queue"]["static"].get("error") == 1


async def test_get_stats_shape(pool):
    from app import queue, service

    await service.submit_scan(pool, "https://example.com/a")
    await service.submit_scan(pool, "https://example.com/b")
    async with pool.acquire() as conn:
        stats = await queue.get_stats(conn)
    assert stats["queue"]["static"]["queued"] == 2
    assert stats["results_total"] == 0


async def test_reaper_requeues_stuck_then_parks(pool):
    from app import queue, service

    await service.submit_scan(pool, "https://example.com/stuck")
    for attempt, expect_requeue in [(1, True), (2, True), (3, False)]:
        async with pool.acquire() as conn:
            jobs = await queue.claim(conn, tier="static", batch=5)
            assert len(jobs) == 1 and jobs[0].attempts == attempt
            # Simulate a worker that crashed mid-job.
            await conn.execute(
                "UPDATE scan_queue SET claimed_at = now() - interval '1 hour' WHERE id = $1",
                jobs[0].id)
            reaped = await queue.reap_stale(conn, timeout_seconds=60, max_attempts=3)
        if expect_requeue:
            assert reaped == {"requeued": 1, "parked": 0}
        else:
            assert reaped == {"requeued": 0, "parked": 1}
    async with pool.acquire() as conn:
        assert await queue.claim(conn, tier="static", batch=5) == []  # parked


async def test_prune_removes_old_terminal_rows(pool):
    from app import queue, service

    a = await service.submit_scan(pool, "https://example.com/keep")
    async with pool.acquire() as conn:
        jobs = await queue.claim(conn, tier="static", batch=5)
        await queue.mark_done(conn, jobs[0].id)
        # Age the done row beyond retention.
        await conn.execute("UPDATE scan_queue SET enqueued_at = now() - interval '2 days'")
        removed = await queue.prune(conn, retention_seconds=86400)
    assert removed == 1


async def test_due_for_rescan_finds_expired(pool):
    from app import ledger, service

    await service.submit_scan(pool, "https://example.com/old")
    async with pool.acquire() as conn:
        await conn.execute("UPDATE scan_ledger SET expires_at = now() - interval '1 day'")
        due = await ledger.due_for_rescan(conn, limit=10)
    assert len(due) == 1 and due[0]["url"] == "https://example.com/old"
