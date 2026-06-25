"""Adaptive render-timeout controller tests (app.render_control).

Seeds terminal render rows in scan_queue with a recent terminated_at, then checks
the escalate / halt / hold policy. Skipped unless AI_DATABASE_URL is set.
"""
from __future__ import annotations

import os
import uuid

import pytest

from app import render_control
from app.config import get_settings

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
        await conn.execute("TRUNCATE scan_queue, render_control RESTART IDENTITY")
    yield p
    from app.db import close_pool
    await close_pool()


async def _seed(conn, *, done: int, errors: int) -> None:
    """Insert recent terminal render jobs: `done` succeeded, `errors` parked."""
    for status, n in (("done", done), ("error", errors)):
        for _ in range(n):
            await conn.execute(
                "INSERT INTO scan_queue (scan_id, url_hash, url, domain, tier, status, terminated_at) "
                "VALUES ($1, $2, 'u', 'd', 'render', $3, now())",
                uuid.uuid4(), uuid.uuid4().hex, status,
            )


async def test_healthy_holds_timeout(pool):
    s = get_settings()
    async with pool.acquire() as conn:
        await _seed(conn, done=100, errors=2)            # ~2% < 5%
        start = (await render_control.get_control(conn, s))["timeout_seconds"]
        ctrl = await render_control.evaluate(conn, s)
    assert not ctrl["halted"]
    assert ctrl["timeout_seconds"] == start


async def test_high_rate_escalates_timeout(pool):
    s = get_settings()
    async with pool.acquire() as conn:
        await _seed(conn, done=80, errors=20)            # 20% > 5%
        ctrl = await render_control.evaluate(conn, s)
    assert not ctrl["halted"]
    assert ctrl["timeout_seconds"] == min(
        s.render_timeout_max_seconds, s.render_timeout_seconds + s.render_timeout_step_seconds)


async def test_high_rate_at_cap_halts(pool):
    s = get_settings()
    async with pool.acquire() as conn:
        await _seed(conn, done=80, errors=20)
        await render_control.get_control(conn, s)         # create row
        await conn.execute("UPDATE render_control SET timeout_seconds = $1 WHERE id = 1",
                           s.render_timeout_max_seconds)
        ctrl = await render_control.evaluate(conn, s)
    assert ctrl["halted"]
    assert "HALTED" in (ctrl["reason"] or "")
    # resume clears the halt and resets the timeout.
    async with pool.acquire() as conn:
        await render_control.resume(conn, s)
        after = await render_control.get_control(conn, s)
    assert not after["halted"]
    assert after["timeout_seconds"] == s.render_timeout_seconds


async def test_below_min_sample_no_change(pool):
    s = get_settings()
    async with pool.acquire() as conn:
        await _seed(conn, done=3, errors=3)              # high rate but < min_sample
        start = (await render_control.get_control(conn, s))["timeout_seconds"]
        ctrl = await render_control.evaluate(conn, s)
    assert not ctrl["halted"]
    assert ctrl["timeout_seconds"] == start
