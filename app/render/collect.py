"""Render a page and collect render-tier signals.

Two planes:
  * in-page JS (page.evaluate): geometry, viewability, CWV, consent, prebid, video
  * CDP (Network/Performance): authoritative bytes, requests, cookies, CPU

Sequence: goto -> settle -> SETUP_JS (tag+observe ads) -> dwell+scroll (let ads
load/refresh and viewability accrue) -> COLLECT_JS + CDP read.
"""
from __future__ import annotations

import asyncio
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import Error as PWError
from playwright.async_api import TimeoutError as PWTimeout

import statistics

from app.render.browser import RenderPool
from app.render.instrument import COLLECT_JS, SETUP_JS, STICKY_PROBE_JS
from app.render.netaccount import NetworkAccountant, count_third_party_cookies
from app.ssrf import SSRFError, assert_public_host

_SETTLE_MS = 1500  # let initial ads load before tagging


async def _auto_scroll(page) -> None:
    try:
        await page.evaluate(
            "() => new Promise(r => { window.scrollTo(0, document.body.scrollHeight); setTimeout(r, 500); })"
        )
        await page.evaluate("() => window.scrollTo(0, 0)")
    except PWError:
        pass


def _task_duration(metrics: list[dict]) -> float | None:
    for m in metrics:
        if m.get("name") == "TaskDuration":
            return round(float(m.get("value") or 0), 3)
    return None


async def render_page(
    pool: RenderPool, url: str, *, dwell_ms: int = 8000, nav_timeout_ms: int = 25000
) -> dict[str, Any]:
    parts = urlsplit(url)
    try:
        await assert_public_host(parts.hostname, parts.port or 443)
    except SSRFError as e:
        return {"ok": False, "error": f"SSRFError: {e}"}

    async with pool.page() as page:
        page.set_default_timeout(nav_timeout_ms)
        # CDP plane for authoritative network/cookie/cpu accounting.
        net = NetworkAccountant()
        cdp = None
        try:
            cdp = await page.context.new_cdp_session(page)
            await cdp.send("Network.enable")
            await cdp.send("Performance.enable")
            cdp.on("Network.responseReceived", net.on_response)
            cdp.on("Network.loadingFinished", net.on_finished)
        except PWError:
            cdp = None

        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=nav_timeout_ms)
        except (PWTimeout, PWError) as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

        status = resp.status if resp else None
        await asyncio.sleep(_SETTLE_MS / 1000)
        try:
            await page.evaluate(SETUP_JS)          # tag ads + attach observers
        except PWError:
            pass
        await _auto_scroll(page)
        await asyncio.sleep(dwell_ms / 1000)       # observers accrue time-in-view

        # Behavioral sticky probe (scroll + re-read rects), then a synthetic
        # interaction so the Event-Timing observer yields an INP proxy.
        try:
            await page.evaluate(STICKY_PROBE_JS)
            # Keyboard only — a blind mouse click could follow a link and navigate
            # the page away mid-scan. Tab/keydown still yields an Event-Timing entry.
            await page.keyboard.press("Tab")
            await page.keyboard.press("Tab")
            await asyncio.sleep(0.25)
        except PWError:
            pass

        try:
            data = await page.evaluate(COLLECT_JS)
        except PWError as e:
            return {"ok": False, "status": status, "error": f"collect: {e}"}

        final_url = page.url
        if cdp is not None:
            data["resources"] = net.summary(final_url)
            try:
                cookies = (await cdp.send("Network.getAllCookies")).get("cookies", [])
            except PWError:
                cookies = []
            data.setdefault("cmp", {})["cookie_count"] = len(cookies)
            data["cmp"]["third_party_cookie_count"] = count_third_party_cookies(cookies, final_url)
            try:
                metrics = (await cdp.send("Performance.getMetrics")).get("metrics", [])
                data["cpu"] = {"task_duration_s": _task_duration(metrics)}
            except PWError:
                data["cpu"] = {}
        else:
            data["resources"] = data.get("resources_inpage", {})

        data["ok"] = True
        data["status"] = status
        data["final_url"] = final_url
        return data


async def render_page_sampled(
    pool: RenderPool, url: str, *, dwell_ms: int = 8000, samples: int = 1
) -> dict[str, Any]:
    """Render `samples` times and replace run-to-run-variable CLS with the median.

    CLS varies between renders (late-loading ads shift layout differently each
    time); the median of N is far more stable. Other signals come from the first
    successful render. samples<=1 is a plain single render.
    """
    base = await render_page(pool, url, dwell_ms=dwell_ms)
    if samples <= 1 or not base.get("ok"):
        return base
    cls_vals = []
    c = (base.get("cwv") or {}).get("cls")
    if c is not None:
        cls_vals.append(c)
    for _ in range(samples - 1):
        r = await render_page(pool, url, dwell_ms=dwell_ms)
        c = (r.get("cwv") or {}).get("cls") if r.get("ok") else None
        if c is not None:
            cls_vals.append(c)
    if cls_vals:
        base.setdefault("cwv", {})["cls"] = round(statistics.median(cls_vals), 3)
        base["cwv"]["cls_samples"] = cls_vals
    return base
