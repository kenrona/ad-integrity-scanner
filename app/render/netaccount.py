"""CDP-based network accounting.

The in-page Resource Timing API zeroes `transferSize` for cross-origin responses
without Timing-Allow-Origin and for cache hits — exactly where ad/tracker bytes
live — so it undercounts page weight badly. The Chrome DevTools Protocol
`Network` domain reports the true `encodedDataLength`, so we account bytes there.
"""
from __future__ import annotations

import functools
import json
import pathlib
import re
from typing import Any

import tldextract

_extract = tldextract.TLDExtract(suffix_list_urls=())


@functools.lru_cache(maxsize=1)
def _tracker_db() -> dict:
    """Disconnect tracking-protection dataset: registrable domain -> {e:entity, c:category}."""
    p = pathlib.Path(__file__).resolve().parents[1] / "data" / "disconnect_trackers.json"
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 — fall back to the small curated set
        return {}

_AD_HOST_RE = re.compile(
    r"(googlesyndication|doubleclick|amazon-adsystem|adnxs|criteo|rubiconproject"
    r"|pubmatic|adsrvr|3lift|sharethrough|smartadserver|teads|adform|openx"
    r"|casalemedia|33across|gampad|adsystem)", re.I)
# Curated tracker domains (registrable). A drop-in for DuckDuckGo Tracker Radar /
# Disconnect services.json later; conservative but covers the common set.
_TRACKER_DOMAINS = {
    "google-analytics.com", "googletagmanager.com", "scorecardresearch.com",
    "quantserve.com", "quantcount.com", "chartbeat.com", "segment.com",
    "segment.io", "mixpanel.com", "hotjar.com", "facebook.net", "facebook.com",
    "doubleclick.net", "adnxs.com", "krxd.net", "crwdcntrl.net", "demdex.net",
    "bluekai.com", "rlcdn.com", "adsrvr.org", "amazon-adsystem.com",
    "criteo.com", "pubmatic.com", "rubiconproject.com", "casalemedia.com",
    "bidswitch.net", "agkn.com", "mathtag.com", "sharethrough.com",
    "newrelic.com", "nr-data.net", "branch.io", "amplitude.com", "fullstory.com",
    "cloudflareinsights.com", "tiktok.com", "snapchat.com", "bing.com",
}
_TYPE_KEYS = ("Document", "Script", "Stylesheet", "Image", "Font", "Media",
              "XHR", "Fetch", "Other")
_MAX_REQUESTS = 8000  # defensive cap against a hostile request flood


def registrable(host: str | None) -> str | None:
    if not host:
        return None
    return _extract(host.split(":")[0]).registered_domain or host


class NetworkAccountant:
    def __init__(self) -> None:
        self._meta: dict[str, dict] = {}      # requestId -> {url, type}
        self._bytes: dict[str, int] = {}      # requestId -> encodedDataLength
        self._overflow = False

    # CDP event handlers (sync callbacks receiving the event params dict).
    def on_response(self, params: dict) -> None:
        if len(self._meta) >= _MAX_REQUESTS:
            self._overflow = True
            return
        rid = params.get("requestId")
        resp = params.get("response") or {}
        if rid:
            self._meta[rid] = {"url": resp.get("url", ""), "type": params.get("type", "Other")}

    def on_finished(self, params: dict) -> None:
        rid = params.get("requestId")
        if rid:
            self._bytes[rid] = int(params.get("encodedDataLength") or 0)

    def summary(self, page_url: str) -> dict[str, Any]:
        page_reg = registrable((page_url.split("//", 1)[-1].split("/", 1)[0]))
        total = sum(self._bytes.values())
        by_type = {k: 0 for k in _TYPE_KEYS}
        third_party: set[str] = set()
        trackers: set[str] = set()
        tracker_entities: set[str] = set()
        tracker_categories: dict[str, int] = {}
        ad_requests = 0
        hosts: set[str] = set()
        db = _tracker_db()
        for rid, meta in self._meta.items():
            b = self._bytes.get(rid, 0)
            t = meta["type"] if meta["type"] in by_type else "Other"
            by_type[t] += b
            url = meta["url"]
            try:
                host = url.split("//", 1)[1].split("/", 1)[0]
            except IndexError:
                continue
            reg = registrable(host)
            if reg:
                hosts.add(reg)
                if reg != page_reg:
                    third_party.add(reg)
                hit = db.get(reg) or db.get(host)
                if hit:
                    trackers.add(reg)
                    tracker_entities.add(hit.get("e") or reg)   # owner-collapse
                    cat = hit.get("c") or "Other"
                    tracker_categories[cat] = tracker_categories.get(cat, 0) + 1
                elif not db and reg in _TRACKER_DOMAINS:         # fallback set
                    trackers.add(reg)
                    tracker_entities.add(reg)
            if _AD_HOST_RE.search(url):
                ad_requests += 1
        return {
            "request_count": len(self._meta),
            "page_weight_bytes": total,
            "bytes_by_type": by_type,
            "distinct_host_count": len(hosts),
            "third_party_host_count": len(third_party),
            "tracker_domain_count": len(trackers),
            "tracker_entity_count": len(tracker_entities),   # distinct owners
            "tracker_entities": sorted(tracker_entities)[:40],
            "tracker_categories": tracker_categories,
            "ad_request_count": ad_requests,
            "source": "cdp",
            "request_overflow": self._overflow,
        }


def count_third_party_cookies(cookies: list[dict], page_url: str) -> int:
    page_reg = registrable((page_url.split("//", 1)[-1].split("/", 1)[0]))
    n = 0
    for c in cookies:
        if registrable((c.get("domain") or "").lstrip(".")) != page_reg:
            n += 1
    return n
