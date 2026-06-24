"""Render-tier worker.

Claims `render` jobs, loads the static signals already stored for that scan_id,
renders the page with Playwright to collect ad-slot / CWV / pbjs / CMP / video
signals, merges them, recomputes all sub-scores + composite, and updates the
same scan_results row (raising confidence to render-grade).

Run: python -m app.workers.render_worker
"""
from __future__ import annotations

import asyncio
import json
import signal

import asyncpg

from app import queue, results
from app.config import get_settings
from app.db import close_pool, init_pool, with_retry
from app.logging_config import configure_logging, get_logger, kv
from app.queue import Job
from app.render.browser import RenderPool
from app.render.collect import render_page_sampled
from app.scoring import assemble

_stop = asyncio.Event()
log = get_logger("worker.render")


async def _load_static_signals(conn: asyncpg.Connection, scan_id) -> dict:
    row = await conn.fetchrow("SELECT signals FROM scan_results WHERE scan_id = $1", scan_id)
    return json.loads(row["signals"]) if row and row["signals"] else {}


def _backfill_content(signals: dict, render_data: dict) -> None:
    """Classify from the rendered DOM when static didn't capture content.

    Bot-protected sites (Cloudflare) 403 the static fetch, so content category /
    suitability / word count are missing — but the render tier loaded the real
    page, so classify from its text instead.
    """
    from app import content as content_mod

    page = signals.setdefault("page", {})
    existing = page.get("content") or {}
    cr = render_data.get("content_render")
    if existing.get("word_count") or not cr:
        return  # static content is fine, or nothing rendered to classify
    wc = cr.get("word_count") or 0
    links = cr.get("link_count") or 0
    analysis = content_mod.analyze(cr.get("text") or "", title=cr.get("title"))
    page["content"] = {
        "title": cr.get("title"), "title_present": bool(cr.get("title")),
        "lang": cr.get("lang"), "word_count": wc,
        "quality": {
            "paragraph_count": cr.get("paragraph_count"),
            "heading_count": cr.get("heading_count"),
            "link_count": links,
            "link_to_text_ratio": round(links / wc, 4) if wc else 0.0,
        },
        "category": analysis["category"],
        "category_confidence": analysis["category_confidence"],
        "suitability": analysis["suitability"],
        "content_source": "render",      # vs static
    }


async def _scan_render(pool: asyncpg.Pool, render_pool: RenderPool, job: Job) -> dict:
    settings = get_settings()
    async with pool.acquire() as conn:
        signals = await _load_static_signals(conn, job.scan_id)
    # Hard per-render cap: a wedged page (hung goto/evaluate) is cancelled so it
    # can't hold a concurrency slot indefinitely. Cancellation propagates into
    # RenderPool.page()'s `finally`, which closes the context and frees the slot.
    render_data = await asyncio.wait_for(
        render_page_sampled(
            render_pool, job.url, dwell_ms=settings.render_dwell_ms,
            samples=settings.render_samples,
            nav_timeout_ms=settings.render_nav_timeout_ms),
        timeout=settings.render_timeout_seconds,
    )
    signals["render"] = render_data
    if render_data.get("ok"):
        _backfill_content(signals, render_data)
    return assemble(signals)


async def _process(pool: asyncpg.Pool, render_pool: RenderPool, job: Job) -> None:
    settings = get_settings()
    try:
        result = await _scan_render(pool, render_pool, job)

        # Retry persist on transient deadlock/serialization so a successful (and
        # expensive ~10s) render is not thrown away and re-done.
        async def _do_persist() -> None:
            async with pool.acquire() as conn:
                await results.persist(conn, job, result, settings)

        await with_retry(_do_persist)
        m = result["metrics"]
        log.info("rendered %s", kv(
            scan_id=job.scan_id, domain=job.domain, tier=result["scan_tier"],
            slots=m.get("ad_slot_count"), a2cr=m.get("a2cr"),
            lcp=m.get("lcp_ms"), score=result["integrity_score"]))
    except Exception as e:  # noqa: BLE001
        retry = job.attempts < settings.max_attempts
        async with pool.acquire() as conn:
            if retry:
                await queue.requeue(conn, job.id, repr(e))
            else:
                await queue.mark_error(conn, job.id, repr(e))
        log.warning("render failed (%s) %s err=%r",
                    "requeued" if retry else "parked",
                    kv(scan_id=job.scan_id, attempts=job.attempts), e)


async def _run_once(pool: asyncpg.Pool, render_pool: RenderPool, batch: int) -> int:
    async def _do_claim():
        async with pool.acquire() as conn:
            return await queue.claim(conn, tier="render", batch=batch)

    jobs = await with_retry(_do_claim)
    # Launch all claimed renders concurrently; RenderPool's semaphore caps how
    # many browser contexts actually run at once (AI_RENDER_CONCURRENCY).
    await asyncio.gather(*(_process(pool, render_pool, j) for j in jobs))
    return len(jobs)


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    pool = await init_pool(apply_schema=False)  # schema owned by the app; avoid DDL deadlocks
    blocked = {t.strip() for t in settings.render_block_resources.split(",") if t.strip()}
    render_pool = RenderPool(concurrency=settings.render_concurrency, blocked_types=blocked,
                             browsers=settings.render_browsers)
    await render_pool.start()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _stop.set)

    log.info("started %s", kv(concurrency=settings.render_concurrency,
                              browsers=settings.render_browsers,
                              dwell_ms=settings.render_dwell_ms))
    try:
        while not _stop.is_set():
            try:
                n = await _run_once(pool, render_pool, settings.render_worker_batch)
            except Exception as e:  # noqa: BLE001 — never die on a transient error
                log.warning("render loop iteration failed err=%r", e)
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
        await render_pool.stop()
        await close_pool()


if __name__ == "__main__":
    asyncio.run(main())
