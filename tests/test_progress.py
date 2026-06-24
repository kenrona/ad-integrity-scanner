"""job_runs progress lifecycle tests (app.progress).

Skipped automatically unless a Postgres test DB is reachable. Point
AI_DATABASE_URL at a throwaway DB (e.g. ad_integrity_test) before running.
"""
from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def pool():
    if not os.environ.get("AI_DATABASE_URL"):
        pytest.skip("set AI_DATABASE_URL to a throwaway test DB to run integration tests")
    try:
        from app.db import init_pool
        p = await init_pool()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no postgres available: {e}")
    async with p.acquire() as conn:
        await conn.execute("TRUNCATE job_runs RESTART IDENTITY")
    yield p
    from app.db import close_pool
    await close_pool()


async def test_create_advance_finish(pool):
    from app import progress

    async with pool.acquire() as conn:
        job_id = await progress.create_job(conn, kind="ingest", total=0, message="start")
        assert isinstance(job_id, int)

        await progress.set_total(conn, job_id, 10)
        await progress.advance(conn, job_id, done_delta=3, message="working")
        await progress.advance(conn, job_id, done_delta=2)

        mid = await progress.get_job(conn, job_id)
        assert mid["status"] == "running"
        assert mid["total"] == 10
        assert mid["done"] == 5
        assert mid["message"] == "working"  # kept across the None-message advance
        assert mid["finished_at"] is None

        await progress.finish(conn, job_id, message="all done")
        done = await progress.get_job(conn, job_id)
    assert done["status"] == "done"
    assert done["message"] == "all done"
    assert done["finished_at"] is not None


async def test_fail_records_error(pool):
    from app import progress

    async with pool.acquire() as conn:
        job_id = await progress.create_job(conn, kind="scan_batch")
        await progress.fail(conn, job_id, "boom " * 2000)  # > 4000 chars
        job = await progress.get_job(conn, job_id)
    assert job["status"] == "error"
    assert job["error"] is not None
    assert len(job["error"]) <= 4000
    assert job["finished_at"] is not None


async def test_get_job_missing_returns_none(pool):
    from app import progress

    async with pool.acquire() as conn:
        assert await progress.get_job(conn, 99999999) is None


async def test_invalid_kind_raises(pool):
    from app import progress

    async with pool.acquire() as conn:
        with pytest.raises(ValueError):
            await progress.create_job(conn, kind="bogus")
