"""Persistent Playwright browser pool (multi-browser).

Launches `browsers` Chromium processes for the worker's lifetime and spreads up
to `concurrency` total isolated contexts across them (least-loaded), so each
browser serves ~concurrency/browsers contexts. A single browser bottlenecks
beyond ~4 concurrent contexts because every render's request interception funnels
through its one CDP connection; multiple browser processes give real parallelism.
A global semaphore caps total concurrent contexts so memory stays bounded.
Fonts/media are blocked by default to cut bandwidth+RAM while preserving CSS/JS
(needed for layout geometry and ad execution); images are kept for page-weight.
"""
from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

from urllib.parse import urlsplit

from playwright.async_api import Browser, Page, Playwright, async_playwright

from app.config import get_settings
from app.render.instrument import INIT_JS
from app.ssrf import literal_host_blocked

_VIEWPORT = {"width": 1366, "height": 768}


def _make_route_handler(blocked_types: set[str], ssrf_intercept: bool):
    async def _route_handler(route) -> None:
        # SSRF: block any subresource/redirect to a literal private/metadata host
        # (cheap, no DNS) — only when in-app SSRF interception is enabled. Then drop
        # configured heavy resource types. Note: intercepting + continuing every
        # request is the dominant render cost on request-heavy pages, so this route
        # is only installed when there's something to do (see RenderPool.page).
        if ssrf_intercept and literal_host_blocked(urlsplit(route.request.url).hostname):
            await route.abort()
        elif route.request.resource_type in blocked_types:
            await route.abort()
        else:
            await route.continue_()
    return _route_handler


def _least_loaded(inflight: list[int]) -> int:
    """Index of the browser with the fewest in-flight contexts (ties -> lowest)."""
    best = 0
    for i in range(1, len(inflight)):
        if inflight[i] < inflight[best]:
            best = i
    return best


class RenderPool:
    def __init__(self, concurrency: int = 2, blocked_types: set[str] | None = None,
                 browsers: int = 1, ssrf_intercept: bool = True) -> None:
        self._concurrency = max(1, concurrency)
        self._n_browsers = max(1, browsers)
        # Default blocks fonts/media only — images are kept so page-weight stays
        # accurate (a headline metric). Pass {'image','font','media'} to trade
        # accuracy for lower bandwidth.
        self._blocked = blocked_types if blocked_types is not None else {"font", "media"}
        self._ssrf_intercept = ssrf_intercept
        self._route = _make_route_handler(self._blocked, ssrf_intercept)
        # Only intercept requests if there's actually something to do — otherwise
        # skip context.route entirely to avoid a CDP round-trip per request (the
        # dominant cost on 1000+ request pages). Egress-hardened deploys can run
        # with ssrf_intercept=false + no blocked types for a big render speedup.
        self._needs_route = ssrf_intercept or bool(self._blocked)
        self._pw: Playwright | None = None
        self._browsers: list[Browser] = []
        self._inflight: list[int] = []      # per-browser active-context counts
        self._sem: asyncio.Semaphore | None = None

    async def start(self) -> None:
        self._pw = await async_playwright().start()
        # Keep Chromium's sandbox ON — we render hostile pages, so --no-sandbox
        # would remove the last barrier between a browser exploit and the host.
        for _ in range(self._n_browsers):
            self._browsers.append(await self._pw.chromium.launch(
                headless=True,
                args=["--disable-dev-shm-usage"],
            ))
        self._inflight = [0] * len(self._browsers)
        self._sem = asyncio.Semaphore(self._concurrency)

    async def stop(self) -> None:
        for b in self._browsers:
            try:
                await b.close()
            except Exception:  # noqa: BLE001 — best-effort teardown
                pass
        if self._pw:
            await self._pw.stop()
        self._browsers = []
        self._inflight = []
        self._pw = None

    @contextlib.asynccontextmanager
    async def page(self) -> AsyncIterator[Page]:
        if not self._browsers or not self._sem:
            raise RuntimeError("RenderPool not started")
        # Global cap on total contexts; pick the least-loaded browser so contexts
        # spread evenly (~concurrency/browsers each). Index select + increment is
        # atomic in single-threaded asyncio (no await between them).
        async with self._sem:
            idx = _least_loaded(self._inflight)
            self._inflight[idx] += 1
            # Outer try guarantees the inflight decrement even if new_context()
            # raises or is cancelled (e.g. the render-timeout wait_for fires while
            # the context is being created) — otherwise the per-browser count
            # drifts up forever and _least_loaded stops balancing.
            try:
                browser = self._browsers[idx]
                context = await browser.new_context(
                    user_agent=get_settings().user_agent,
                    viewport=_VIEWPORT,
                    java_script_enabled=True,
                )
                try:
                    if self._needs_route:
                        await context.route("**/*", self._route)
                    page = await context.new_page()
                    await page.add_init_script(INIT_JS)
                    yield page
                finally:
                    await context.close()
            finally:
                self._inflight[idx] -= 1
