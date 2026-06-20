"""Persistent Playwright browser pool.

One Chromium process is launched for the worker's lifetime; each render gets a
fresh (isolated) context. A semaphore caps concurrent contexts so memory stays
bounded. Images/fonts/media are blocked to cut bandwidth + RAM while preserving
CSS/JS (needed for layout geometry and ad execution).
"""
from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator

from urllib.parse import urlsplit

from playwright.async_api import Browser, Page, Playwright, async_playwright

from app.config import get_settings
from app.render.instrument import INIT_JS
from app.ssrf import literal_host_blocked

_VIEWPORT = {"width": 1366, "height": 768}


def _make_route_handler(blocked_types: set[str]):
    async def _route_handler(route) -> None:
        # SSRF: block any subresource/redirect to a literal private/metadata host
        # (cheap, no DNS). Then drop configured heavy resource types.
        if literal_host_blocked(urlsplit(route.request.url).hostname):
            await route.abort()
        elif route.request.resource_type in blocked_types:
            await route.abort()
        else:
            await route.continue_()
    return _route_handler


class RenderPool:
    def __init__(self, concurrency: int = 2, blocked_types: set[str] | None = None) -> None:
        self._concurrency = concurrency
        # Default blocks fonts/media only — images are kept so page-weight stays
        # accurate (a headline metric). Pass {'image','font','media'} to trade
        # accuracy for lower bandwidth.
        self._blocked = blocked_types if blocked_types is not None else {"font", "media"}
        self._route = _make_route_handler(self._blocked)
        self._pw: Playwright | None = None
        self._browser: Browser | None = None
        self._sem: contextlib.AbstractAsyncContextManager | None = None

    async def start(self) -> None:
        import asyncio
        self._pw = await async_playwright().start()
        # Keep Chromium's sandbox ON — we render hostile pages, so --no-sandbox
        # would remove the last barrier between a browser exploit and the host.
        self._browser = await self._pw.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
        )
        self._sem = asyncio.Semaphore(self._concurrency)

    async def stop(self) -> None:
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()
        self._browser = self._pw = None

    @contextlib.asynccontextmanager
    async def page(self) -> AsyncIterator[Page]:
        if not self._browser or not self._sem:
            raise RuntimeError("RenderPool not started")
        async with self._sem:
            context = await self._browser.new_context(
                user_agent=get_settings().user_agent,
                viewport=_VIEWPORT,
                java_script_enabled=True,
            )
            try:
                await context.route("**/*", self._route)
                page = await context.new_page()
                await page.add_init_script(INIT_JS)
                yield page
            finally:
                await context.close()
