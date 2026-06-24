"""Static-tier worker.

Claims `static` jobs, collects static signals (ads.txt / app-ads.txt / robots /
sellers.json + page HTML), scores them, and writes the scan_results row. If the
render sampling gate fires, it enqueues a `render` job (same scan_id) so the
render worker can enrich the same record.

Run: python -m app.workers.static_worker
"""
from __future__ import annotations

import asyncio
import random
import signal

import asyncpg
import httpx

from app import fetch, queue, results, signals_static
from app.config import Settings, get_settings
from app.db import close_pool, init_pool, with_retry
from app.logging_config import configure_logging, get_logger, kv
from app.queue import Job
from app.scoring import score_static

_stop = asyncio.Event()
log = get_logger("worker.static")


async def _scan_static(pool: asyncpg.Pool, client: httpx.AsyncClient, job: Job) -> dict:
    """Collect static signals (domain files + page HTML) and score them."""
    settings = get_settings()
    signals = await signals_static.collect(
        client, pool, url=job.url, domain=job.domain, settings=settings,
    )
    return score_static(signals)


def _needs_render(settings: Settings) -> bool:
    if not settings.render_enabled:
        return False
    return random.random() < settings.render_sample_rate


async def _process(pool: asyncpg.Pool, client: httpx.AsyncClient, job: Job,
                   settings: Settings) -> None:
    try:
        result = await _scan_static(pool, client, job)

        # Retry persist (+ render enqueue) on transient deadlock/serialization so
        # the completed static scan is not discarded under heavy write contention.
        async def _do_persist() -> None:
            async with pool.acquire() as conn:
                await results.persist(conn, job, result, settings)
                if _needs_render(settings):
                    await queue.enqueue(
                        conn, scan_id=job.scan_id, url_hash=job.url_hash,
                        url=job.url, domain=job.domain, tier="render",
                    )

        await with_retry(_do_persist)
        log.info("scanned %s", kv(
            scan_id=job.scan_id, domain=job.domain,
            supply=result["sub_scores"].get("supply_chain"),
            ads_txt=result["metrics"].get("ads_txt_present")))
    except Exception as e:  # noqa: BLE001 — record + continue
        retry = job.attempts < settings.max_attempts
        async with pool.acquire() as conn:
            if retry:
                await queue.requeue(conn, job.id, repr(e))
            else:
                await queue.mark_error(conn, job.id, repr(e))
        log.warning("job failed (%s) %s err=%r",
                    "requeued" if retry else "parked",
                    kv(scan_id=job.scan_id, attempts=job.attempts), e)


async def _run_once(
    pool: asyncpg.Pool, batch: int, client: httpx.AsyncClient | None = None
) -> int:
    settings = get_settings()
    own_client = client is None
    if own_client:
        client = fetch.make_client()
    async def _do_claim():
        async with pool.acquire() as conn:
            return await queue.claim(conn, tier="static", batch=batch)

    jobs = await with_retry(_do_claim)
    # Process the batch concurrently — this tier is I/O-bound, so fan-out (capped
    # by a semaphore) is the throughput win over awaiting jobs one at a time.
    sem = asyncio.Semaphore(settings.static_worker_concurrency)

    async def _guarded(job: Job) -> None:
        async with sem:
            await _process(pool, client, job, settings)

    try:
        await asyncio.gather(*(_guarded(j) for j in jobs))
    finally:
        if own_client:
            await client.aclose()
    return len(jobs)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    pool = await init_pool(apply_schema=False)  # schema owned by the app; avoid DDL deadlocks

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop.set)

    client = fetch.make_client()
    log.info("started %s", kv(batch=settings.static_worker_batch,
                              max_attempts=settings.max_attempts,
                              render_rate=settings.render_sample_rate))
    try:
        while not _stop.is_set():
            try:
                n = await _run_once(pool, settings.static_worker_batch, client)
            except Exception as e:  # noqa: BLE001 — never die on a transient error
                log.warning("static loop iteration failed err=%r", e)
                n = 0
            if n == 0:
                try:
                    await asyncio.wait_for(
                        _stop.wait(), timeout=settings.static_worker_poll_ms / 1000
                    )
                except asyncio.TimeoutError:
                    pass
    finally:
        log.info("shutting down")
        await client.aclose()
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
