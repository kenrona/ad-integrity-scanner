"""Maintenance worker (Phase 4 hardening).

Periodically:
  * reaps jobs stuck in 'processing' (crashed workers) -> requeue or park,
  * prunes old done/error queue rows to bound table growth,
  * (optional) re-enqueues page URLs whose TTL has expired.

Run: python -m app.workers.maintenance
"""
from __future__ import annotations

import asyncio
import signal

import asyncpg

from app import ledger, queue, render_control, service
from app.config import Settings, get_settings
from app.db import close_pool, init_pool
from app.logging_config import configure_logging, get_logger, kv

_stop = asyncio.Event()
log = get_logger("worker.maintenance")


async def run_once(pool: asyncpg.Pool, settings: Settings) -> dict:
    async with pool.acquire() as conn:
        reaped = await queue.reap_stale(
            conn, timeout_seconds=settings.visibility_timeout_seconds,
            max_attempts=settings.max_attempts,
        )
        pruned = await queue.prune(conn, settings.queue_retention_seconds)
        # Adaptive render-timeout controller: escalate timeout / halt on high error rate.
        ctrl = await render_control.evaluate(conn, settings)
    if ctrl.get("halted"):
        log.warning("render controller HALTED: %s", ctrl.get("reason"))

    rescanned = 0
    if settings.rescan_enabled:
        async with pool.acquire() as conn:
            due = await ledger.due_for_rescan(conn, settings.rescan_batch)
        # submit_scan dedups via inflight, so repeated cycles won't double-enqueue.
        for row in due:
            await service.submit_scan(pool, row["url"])
            rescanned += 1

    summary = {"requeued": reaped["requeued"], "parked": reaped["parked"],
               "pruned": pruned, "rescanned": rescanned}
    if any(summary.values()):
        log.info("maintenance %s", kv(**summary))
    return summary


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    pool = await init_pool(apply_schema=False)  # schema owned by the app; avoid DDL deadlocks

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop.set)

    log.info("started %s", kv(interval=settings.maintenance_interval_seconds,
                              visibility_timeout=settings.visibility_timeout_seconds,
                              rescan=settings.rescan_enabled))
    try:
        while not _stop.is_set():
            await run_once(pool, settings)
            try:
                await asyncio.wait_for(
                    _stop.wait(), timeout=settings.maintenance_interval_seconds
                )
            except asyncio.TimeoutError:
                pass
    finally:
        log.info("shutting down")
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
