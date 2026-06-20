"""Static-tier signal collection: domain-level (cached) + page-level.

DB connections are held only for the brief cache get/put — never during network
I/O — so many scans can run concurrently without exhausting the pool.
"""
from __future__ import annotations

import asyncio
from typing import Any

import asyncpg
import httpx

from app import domain_cache, fetch, supply_resolve
from app.config import Settings
from app.parsers.adstxt import parse_ads_txt
from app.parsers.html import parse_html


def _parse_robots(text: str) -> dict[str, Any]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    sitemaps = [ln for ln in lines if ln.lower().startswith("sitemap:")]
    disallows = [ln for ln in lines if ln.lower().startswith("disallow:")]
    disallow_all = any(ln.split(":", 1)[1].strip() == "/" for ln in disallows)
    return {
        "present": bool(lines),
        "has_sitemap": bool(sitemaps),
        "disallow_count": len(disallows),
        "disallow_all": disallow_all,
    }


async def _collect_domain(
    client: httpx.AsyncClient, pool: asyncpg.Pool, domain: str, settings: Settings
) -> dict[str, Any]:
    # ads.txt / app-ads.txt / robots.txt are all on the same host -> fetch in parallel.
    base = f"https://{domain}"
    ads, app_ads, robots = await asyncio.gather(
        fetch.fetch(client, f"{base}/ads.txt", max_bytes=fetch.MAX_TEXT_BYTES),
        fetch.fetch(client, f"{base}/app-ads.txt", max_bytes=fetch.MAX_TEXT_BYTES),
        fetch.fetch(client, f"{base}/robots.txt", max_bytes=fetch.MAX_TEXT_BYTES),
    )
    ads_txt = parse_ads_txt(ads.text) if ads.ok else {"present": False}

    # Cross-resolve authorized sellers against each ad system's sellers.json
    # (global cache). Done on the domain-cache miss path only, so it's amortized.
    supply_paths = await supply_resolve.resolve(pool, client, ads_txt, domain, settings)
    ads_txt.pop("records", None)  # don't persist the bulky raw line list

    app_ads_txt = parse_ads_txt(app_ads.text) if app_ads.ok else {"present": False}
    app_ads_txt.pop("records", None)

    return {
        "ads_txt": ads_txt,
        "app_ads_txt": app_ads_txt,
        "robots_txt": _parse_robots(robots.text) if robots.ok else {"present": False},
        "supply_paths": supply_paths,
    }


async def collect(
    client: httpx.AsyncClient,
    pool: asyncpg.Pool,
    *,
    url: str,
    domain: str,
    settings: Settings,
) -> dict[str, Any]:
    """Gather all static signals for a URL. Domain files come from cache when fresh."""
    async with pool.acquire() as conn:
        domain_signals = await domain_cache.get(conn, domain)
    domain_cache_hit = domain_signals is not None

    if not domain_cache_hit:
        domain_signals = await _collect_domain(client, pool, domain, settings)  # network
        async with pool.acquire() as conn:
            await domain_cache.put(conn, domain, domain_signals, settings.domain_ttl_seconds)

    page = await fetch.fetch(client, url)  # network, no DB conn held
    page_signals = parse_html(page.text, page_domain=domain) if page.ok else {}

    return {
        "fetch": {
            "ok": page.ok,
            "status": page.status,
            "final_url": page.final_url,
            "https": page.https,
            "content_type": page.content_type,
            "elapsed_ms": page.elapsed_ms,
            "truncated": page.truncated,
            "error": page.error,
            "headers": page.headers,
            "domain_cache_hit": domain_cache_hit,
        },
        "domain": domain_signals,
        "page": page_signals,
    }
