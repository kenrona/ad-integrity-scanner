"""sellers.json cross-resolution.

Resolves a publisher's ads.txt authorized-seller lines against each ad system's
sellers.json (fetched once per ad system into a GLOBAL 7-day cache), producing
supply-path transparency metrics: how many authorized sellers actually resolve,
how many are intermediaries (reseller hops) vs publishers, and how many are
confidential (opaque).
"""
from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from typing import Any

import asyncpg
import httpx

from app import fetch
from app.config import Settings
from app.parsers.sellersjson import parse_sellers_json


async def _get_cached(conn: asyncpg.Connection, ad_system: str) -> dict | None:
    row = await conn.fetchrow(
        """
        SELECT present, too_large, seller_count, sellers
        FROM sellers_json_cache
        WHERE ad_system = $1 AND expires_at IS NOT NULL AND expires_at > now()
        """,
        ad_system,
    )
    if not row:
        return None
    return {
        "present": row["present"], "too_large": row["too_large"],
        "seller_count": row["seller_count"],
        "sellers": json.loads(row["sellers"]) if row["sellers"] else None,
    }


async def _store(conn: asyncpg.Connection, ad_system: str, rec: dict, ttl: int) -> None:
    expires = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=ttl)
    await conn.execute(
        """
        INSERT INTO sellers_json_cache
            (ad_system, present, too_large, seller_count, type_counts,
             confidential_count, passthrough_count, sellers, fetched_at, expires_at)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8, now(), $9)
        ON CONFLICT (ad_system) DO UPDATE SET
            present=EXCLUDED.present, too_large=EXCLUDED.too_large,
            seller_count=EXCLUDED.seller_count, type_counts=EXCLUDED.type_counts,
            confidential_count=EXCLUDED.confidential_count,
            passthrough_count=EXCLUDED.passthrough_count, sellers=EXCLUDED.sellers,
            fetched_at=now(), expires_at=EXCLUDED.expires_at
        """,
        ad_system, rec["present"], rec["too_large"], rec.get("seller_count"),
        json.dumps(rec.get("type_counts")) if rec.get("type_counts") else None,
        rec.get("confidential_count"), rec.get("passthrough_count"),
        json.dumps(rec["sellers"]) if rec.get("sellers") is not None else None,
        expires,
    )


async def _get_or_fetch(
    conn: asyncpg.Connection, client: httpx.AsyncClient, ad_system: str, s: Settings
) -> dict:
    cached = await _get_cached(conn, ad_system)
    if cached is not None:
        return cached

    res = await fetch.fetch(client, f"https://{ad_system}/sellers.json",
                            max_bytes=s.sellers_json_max_bytes)
    if not res.ok:
        rec = {"present": False, "too_large": False, "sellers": None}
        await _store(conn, ad_system, rec, s.sellers_json_neg_ttl_seconds)
        return rec
    if res.truncated:
        rec = {"present": True, "too_large": True, "sellers": None}
        await _store(conn, ad_system, rec, s.sellers_json_ttl_seconds)
        return rec
    try:
        parsed = parse_sellers_json(res.text)
    except (ValueError, TypeError):
        rec = {"present": True, "too_large": False, "sellers": None, "seller_count": 0}
        await _store(conn, ad_system, rec, s.sellers_json_neg_ttl_seconds)
        return rec

    store_map = parsed["seller_count"] <= s.sellers_json_map_max
    rec = {
        "present": True, "too_large": False,
        "seller_count": parsed["seller_count"], "type_counts": parsed["type_counts"],
        "confidential_count": parsed["confidential_count"],
        "passthrough_count": parsed["passthrough_count"],
        "sellers": parsed["sellers"] if store_map else None,
    }
    await _store(conn, ad_system, rec, s.sellers_json_ttl_seconds)
    return rec


async def resolve(
    pool: asyncpg.Pool, client: httpx.AsyncClient,
    ads_txt: dict, publisher_domain: str, settings: Settings,
) -> dict[str, Any]:
    """Resolve a parsed ads.txt against sellers.json caches -> supply-path metrics."""
    records = ads_txt.get("records") or []
    if not records:
        return {"attempted": False}

    # Rank ad systems by # of authorized lines, resolve the top N.
    by_system = Counter(r["ad_system"] for r in records)
    top_systems = {s for s, _ in by_system.most_common(settings.supply_resolve_max_systems)}

    caches: dict[str, dict] = {}
    for asys in top_systems:
        try:
            async with pool.acquire() as conn:
                caches[asys] = await _get_or_fetch(conn, client, asys, settings)
        except Exception:  # noqa: BLE001 — resolution is best-effort
            caches[asys] = {"present": False, "sellers": None}

    with_map = resolved = unresolved = 0
    intermediary = publisher = both = confidential = 0
    direct_total = direct_domain_match = 0
    for r in records:
        cache = caches.get(r["ad_system"])
        if not cache or cache.get("sellers") is None:
            continue
        with_map += 1
        entry = cache["sellers"].get(str(r["account_id"]).strip())
        if entry is None:
            unresolved += 1
            continue
        resolved += 1
        t = entry.get("t")
        intermediary += t == "INTERMEDIARY"
        publisher += t == "PUBLISHER"
        both += t == "BOTH"
        confidential += int(entry.get("c") or 0)
        if r["relationship"] == "DIRECT":
            direct_total += 1
            dom = (entry.get("d") or "").lower()
            if dom and (dom == publisher_domain or dom.endswith("." + publisher_domain)
                        or publisher_domain.endswith("." + dom)):
                direct_domain_match += 1

    return {
        "attempted": True,
        "ad_systems_total": len(by_system),
        "ad_systems_with_sellers_json": sum(1 for c in caches.values() if c.get("sellers") is not None),
        "authorized_paths": len(records),
        "resolvable_paths": with_map,
        "resolved_sellers": resolved,
        "unresolved_accounts": unresolved,
        "resolution_rate": round(resolved / with_map, 4) if with_map else None,
        "intermediary_count": intermediary,
        "publisher_count": publisher,
        "both_count": both,
        "intermediary_ratio": round(intermediary / resolved, 4) if resolved else None,
        "confidential_sellers": confidential,
        "confidential_ratio": round(confidential / resolved, 4) if resolved else None,
        "direct_domain_match": direct_domain_match,
        "direct_domain_total": direct_total,
    }
