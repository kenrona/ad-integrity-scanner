"""Scoring from collected signals.

Every derived sub-score is computed by a `_<name>` function that returns
``(score, inputs)`` — `inputs` is the dict of RAW values the score was computed
from, so nothing is opaque. `assemble()` exposes these as `score_breakdown`
({name: {score, weight, inputs}}) and the raw values are also flattened into
`metrics`. See METRICS.md for definitions.

Sub-scores (0-100, higher = better integrity):
  supply_chain   [static]  ads.txt transparency (DIRECT vs reseller, ownership)
  ad_experience  [render]  clutter geometry: A2CR, fold split, sticky, gaps, refresh
  mfa            [render]  on-page ad load + thin content + link density
  video          [render]  OLV behavior (only when video present)
  privacy        [render]  consent framework presence (TCF/GPP/USP/GPC)
  performance    [render]  Core Web Vitals (LCP/CLS) + page weight
  brand_suitability [static] GARM-style content risk tier

Composite `integrity_score` = weight-normalized average over present sub-scores.
Confidence: static-only 0.4, render-backed 0.85.
"""
from __future__ import annotations

from typing import Any

WEIGHTS = {
    "supply_chain": 0.20,
    "ad_experience": 0.22,
    "mfa": 0.18,
    "video": 0.10,
    "privacy": 0.08,
    "performance": 0.12,
    "brand_suitability": 0.10,
}

_SUITABILITY_TIER_SCORE = {"low": 100.0, "medium": 70.0, "high": 40.0, "floor": 10.0}


def _clamp(x: float) -> float:
    return round(max(0.0, min(100.0, x)), 2)


# --- supply chain -----------------------------------------------------------
def _supply_chain(signals: dict[str, Any]) -> tuple[float, dict]:
    domain = signals.get("domain") or {}
    ads = domain.get("ads_txt") or {}
    sp = domain.get("supply_paths") or {}
    https = bool((signals.get("fetch") or {}).get("https"))
    present = bool(ads.get("present"))
    direct_ratio = float(ads.get("direct_ratio") or 0.0)
    ownership = bool(ads.get("has_owner_domain") or ads.get("has_manager_domain"))

    # (value 0-100, weight, available). Score = weighted avg over available
    # components, renormalized — so sellers.json terms only count when resolved.
    comps: list[tuple[str, float, float, bool]] = [
        ("ads_txt_present", 100.0 if present else 0.0, 35.0, True),
        ("direct_ratio", direct_ratio * 100.0, 25.0, present),
        ("ownership_declared", 100.0 if ownership else 0.0, 15.0, present),
        ("https", 100.0 if https else 0.0, 10.0, True),
    ]
    res_rate = sp.get("resolution_rate")
    conf_ratio = sp.get("confidential_ratio")
    if sp.get("attempted") and res_rate is not None:
        comps.append(("seller_resolution", res_rate * 100.0, 10.0, True))
    if sp.get("attempted") and conf_ratio is not None:
        comps.append(("seller_transparency", (1.0 - conf_ratio) * 100.0, 5.0, True))

    avail = [(v, w) for _, v, w, ok in comps if ok]
    wsum = sum(w for _, w in avail)
    score = sum(v * w for v, w in avail) / wsum if wsum else 0.0

    inputs = {
        "ads_txt_present": present, "direct_ratio": direct_ratio,
        "direct_count": ads.get("direct_count"), "reseller_count": ads.get("reseller_count"),
        "distinct_accounts": ads.get("distinct_accounts"),
        "distinct_ad_systems": ads.get("distinct_ad_systems"),
        "ownership_declared": ownership, "https": https,
        "total_supply_paths": sp.get("authorized_paths"),
        "resolved_sellers": sp.get("resolved_sellers"),
        "seller_resolution_rate": res_rate,
        "intermediary_ratio": sp.get("intermediary_ratio"),
        "confidential_sellers": sp.get("confidential_sellers"),
    }
    return _clamp(score), inputs


# --- ad experience (clutter geometry) ---------------------------------------
def _ad_experience(render: dict[str, Any]) -> tuple[float | None, dict]:
    gpt = render.get("gpt") or {}
    res = render.get("resources") or {}
    if not gpt.get("present") and not res.get("ad_request_count"):
        return 90.0, {"observable_ads": False}

    a2cr = gpt.get("a2cr") or 0.0
    afc = gpt.get("above_fold_count") or 0
    filled = gpt.get("filled_count") or 0
    sticky = gpt.get("sticky_count") or 0
    fsc = gpt.get("first_screen_ad_coverage") or 0.0
    gap_med = gpt.get("gap_median_px")
    refreshing = bool(gpt.get("refreshing"))
    mrs = gpt.get("min_refresh_seconds")
    suspicious = gpt.get("suspicious_ad_count") or 0   # GIVT: hidden/tiny/offscreen/stacked

    p = 0.0
    p += min(40.0, a2cr * 125.0)                         # ad-to-content area
    p += min(20.0, max(0, afc - 1) * 8.0)                # above-fold stacking
    p += min(15.0, max(0, filled - 5) * 1.5)             # overall slot count
    p += min(10.0, sticky * 5.0)                         # sticky/anchored ads
    p += min(15.0, fsc * 60.0)                           # first-screen ad coverage
    p += min(20.0, suspicious * 7.0)                     # served-but-not-viewable (GIVT)
    if gap_med is not None and gap_med < 200 and filled > 3:
        p += 10.0                                        # cramped spacing
    if refreshing:
        p += 25.0 if (mrs is not None and mrs < 15) else 12.0
    inputs = {
        "a2cr": a2cr, "above_fold_count": afc, "below_fold_count": gpt.get("below_fold_count"),
        "filled_count": filled, "sticky_count": sticky, "first_screen_ad_coverage": fsc,
        "gap_median_px": gap_med, "sizes": gpt.get("sizes"),
        "suspicious_ad_count": suspicious,
        "hidden_ad_count": gpt.get("hidden_ad_count"), "tiny_ad_count": gpt.get("tiny_ad_count"),
        "offscreen_ad_count": gpt.get("offscreen_ad_count"), "stacked_ad_count": gpt.get("stacked_ad_count"),
        "refreshing": refreshing, "min_refresh_seconds": mrs,
    }
    return _clamp(100.0 - p), inputs


# --- video ------------------------------------------------------------------
def _video(render: dict[str, Any]) -> tuple[float | None, dict]:
    v = render.get("video") or {}
    if not v.get("has_video"):
        return None, {"has_video": False}
    autoplay = v.get("autoplay_count") or 0
    muted = v.get("muted_autoplay_count") or 0
    score = 100.0
    if autoplay - muted > 0:
        score -= 40.0
    elif muted > 0:
        score -= 15.0
    inputs = {"has_video": True, "autoplay_count": autoplay,
              "muted_autoplay_count": muted, "players": v.get("players"),
              "video_tag_count": v.get("video_tag_count")}
    return _clamp(score), inputs


# --- privacy ----------------------------------------------------------------
def _privacy(render: dict[str, Any]) -> tuple[float, dict]:
    cmp = render.get("cmp") or {}
    res = render.get("resources") or {}
    trackers = res.get("tracker_domain_count") or 0
    score = 0.0
    if cmp.get("tcf"):
        score += 50.0
    if cmp.get("gpp"):
        score += 30.0
    if cmp.get("usp"):
        score += 10.0
    if score == 0.0:
        score = 10.0 if trackers > 10 else 20.0          # trackers, no consent mgmt
    inputs = {"tcf": cmp.get("tcf"), "gpp": cmp.get("gpp"), "usp": cmp.get("usp"),
              "gpc": cmp.get("gpc"), "cookie_count": cmp.get("cookie_count"),
              "tracker_domain_count": trackers}
    return _clamp(score), inputs


# --- performance ------------------------------------------------------------
def _performance(render: dict[str, Any]) -> tuple[float | None, dict]:
    cwv = render.get("cwv") or {}
    res = render.get("resources") or {}
    lcp, cls = cwv.get("lcp_ms"), cwv.get("cls")
    weight = res.get("page_weight_bytes")
    parts: list[tuple[float, float]] = []
    if lcp is not None:
        parts.append((_clamp(100.0 - max(0.0, (lcp - 2500) / 3500) * 100.0), 0.5))
    if cls is not None:
        parts.append((_clamp(100.0 - max(0.0, (cls - 0.1) / 0.4) * 100.0), 0.3))
    if weight is not None:
        parts.append((_clamp(100.0 - max(0.0, (weight/1e6 - 2) / 6) * 100.0), 0.2))
    inputs = {"lcp_ms": lcp, "cls": cls, "page_weight_bytes": weight,
              "request_count": res.get("request_count"),
              "third_party_host_count": res.get("third_party_host_count")}
    if not parts:
        return None, inputs
    wsum = sum(w for _, w in parts)
    return _clamp(sum(s * w for s, w in parts) / wsum), inputs


# --- MFA / ad-load ----------------------------------------------------------
def _mfa(signals: dict[str, Any]) -> tuple[float, dict]:
    render = signals.get("render") or {}
    gpt = render.get("gpt") or {}
    content = (signals.get("page") or {}).get("content") or {}
    ad_tech = (signals.get("page") or {}).get("ad_tech") or {}
    quality = content.get("quality") or {}
    word_count = content.get("word_count") or 0
    ltr = quality.get("link_to_text_ratio") or 0.0

    risk = 0.0
    if render.get("ok"):
        risk += min(45.0, (gpt.get("a2cr") or 0.0) * 150.0)
        risk += min(20.0, max(0, (gpt.get("filled_count") or 0) - 4) * 2.0)
        risk += min(10.0, max(0, (gpt.get("above_fold_count") or 0) - 1) * 5.0)
        if gpt.get("refreshing") and (gpt.get("min_refresh_seconds") or 99) < 15:
            risk += 15.0
    else:
        if ad_tech.get("has_gpt"):
            risk += 10.0
        risk += min(20.0, (content.get("ad_related_domain_count") or 0) * 5.0)
    if word_count < 200:
        risk += 25.0
    elif word_count < 500:
        risk += 12.0
    if ltr > 0.2:
        risk += 15.0
    elif ltr > 0.1:
        risk += 7.0
    inputs = {"a2cr": gpt.get("a2cr"), "filled_count": gpt.get("filled_count"),
              "above_fold_count": gpt.get("above_fold_count"),
              "refreshing": gpt.get("refreshing"),
              "min_refresh_seconds": gpt.get("min_refresh_seconds"),
              "word_count": word_count, "link_to_text_ratio": ltr,
              "rendered": bool(render.get("ok"))}
    return _clamp(100.0 - risk), inputs


# --- brand suitability ------------------------------------------------------
def _brand_suitability(signals: dict[str, Any]) -> tuple[float | None, dict]:
    content = (signals.get("page") or {}).get("content") or {}
    suit = content.get("suitability")
    if not suit:
        return None, {}
    tier = suit.get("risk_tier", "low")
    inputs = {"risk_tier": tier, "flagged_categories": suit.get("flagged_categories"),
              "risk_weight": suit.get("risk_weight"), "heuristic": True}
    return _SUITABILITY_TIER_SCORE.get(tier, 100.0), inputs


# Backward-compatible scalar wrappers (used by tests / callers).
def supply_chain_score(signals): return _supply_chain(signals)[0]
def ad_experience_score(render): return _ad_experience(render)[0]
def video_score(signals): return _video(signals.get("render") or {})[0]
def privacy_score(render): return _privacy(render)[0]
def performance_score(render): return _performance(render)[0]
def mfa_score(signals): return _mfa(signals)[0]
def brand_suitability_score(signals): return _brand_suitability(signals)[0]


def _flatten_metrics(signals: dict[str, Any]) -> dict[str, Any]:
    domain_sig = signals.get("domain") or {}
    ads = domain_sig.get("ads_txt") or {}
    sp = domain_sig.get("supply_paths") or {}
    page = signals.get("page") or {}
    content = page.get("content") or {}
    ad_tech = page.get("ad_tech") or {}
    fetch_ = signals.get("fetch") or {}
    render = signals.get("render") or {}
    gpt = render.get("gpt") or {}
    cwv = render.get("cwv") or {}
    res = render.get("resources") or {}
    prebid = render.get("prebid") or {}
    cmp = render.get("cmp") or {}
    video = render.get("video") or {}
    layout = render.get("layout") or {}
    cpu = render.get("cpu") or {}
    suit = content.get("suitability") or {}
    m = {
        "https": bool(fetch_.get("https")),
        "ads_txt_present": bool(ads.get("present")),
        "ads_txt_direct_ratio": ads.get("direct_ratio"),
        "ads_txt_direct_count": ads.get("direct_count"),
        "ads_txt_reseller_count": ads.get("reseller_count"),
        "ads_txt_distinct_ad_systems": ads.get("distinct_ad_systems"),
        "ads_txt_distinct_accounts": ads.get("distinct_accounts"),
        # sellers.json cross-resolution (supply-path transparency)
        "total_supply_paths": sp.get("authorized_paths"),
        "ad_systems_with_sellers_json": sp.get("ad_systems_with_sellers_json"),
        "resolved_sellers": sp.get("resolved_sellers"),
        "unresolved_accounts": sp.get("unresolved_accounts"),
        "seller_resolution_rate": sp.get("resolution_rate"),
        "intermediary_count": sp.get("intermediary_count"),
        "publisher_seller_count": sp.get("publisher_count"),
        "intermediary_ratio": sp.get("intermediary_ratio"),
        "confidential_sellers": sp.get("confidential_sellers"),
        "confidential_ratio": sp.get("confidential_ratio"),
        "direct_domain_match": sp.get("direct_domain_match"),
        "word_count": content.get("word_count"),
        "paragraph_count": (content.get("quality") or {}).get("paragraph_count"),
        "link_count": (content.get("quality") or {}).get("link_count"),
        "link_to_text_ratio": (content.get("quality") or {}).get("link_to_text_ratio"),
        "content_category": content.get("category"),
        "content_source": content.get("content_source", "static") if content else None,
        "suitability_tier": suit.get("risk_tier"),
        "suitability_flags": suit.get("flagged_categories"),
        "has_gpt": ad_tech.get("has_gpt"),
        "has_prebid": ad_tech.get("has_prebid"),
        "ssp_count": ad_tech.get("ssp_count"),
        "native_widgets": ad_tech.get("native_widgets"),
    }
    if render.get("ok"):
        m.update({
            "rendered": True,
            # ad layout geometry
            "ad_slot_count": gpt.get("slot_count"),
            "filled_slot_count": gpt.get("filled_count"),
            "empty_slot_count": gpt.get("empty_count"),
            "ads_detected_via_gpt": gpt.get("detected_via_gpt"),
            "above_fold_ads": gpt.get("above_fold_count"),
            "below_fold_ads": gpt.get("below_fold_count"),
            "sticky_ad_count": gpt.get("sticky_count"),
            "interstitial": gpt.get("interstitial"),
            # GIVT-style validity (served-but-not-viewable)
            "hidden_ad_count": gpt.get("hidden_ad_count"),
            "tiny_ad_count": gpt.get("tiny_ad_count"),
            "offscreen_ad_count": gpt.get("offscreen_ad_count"),
            "stacked_ad_count": gpt.get("stacked_ad_count"),
            "suspicious_ad_count": gpt.get("suspicious_ad_count"),
            "ads_in_view": gpt.get("ads_in_view"),
            "ads_viewable_1s": gpt.get("ads_viewable_1s"),
            "ads_per_screen": gpt.get("ads_per_screen"),
            "ads_per_1000px": gpt.get("ads_per_1000px"),
            "ad_sizes": gpt.get("sizes"),
            "a2cr": gpt.get("a2cr"),
            "total_ad_area_px": gpt.get("total_ad_area"),
            "page_area_px": gpt.get("page_area"),
            "first_screen_ad_coverage": gpt.get("first_screen_ad_coverage"),
            "first_ad_offset_px": gpt.get("first_ad_offset_px"),
            "ad_gap_min_px": gpt.get("gap_min_px"),
            "ad_gap_median_px": gpt.get("gap_median_px"),
            "ad_gap_max_px": gpt.get("gap_max_px"),
            "ad_refreshing": gpt.get("refreshing"),
            "ad_refresh_events": gpt.get("refresh_events"),
            "min_refresh_seconds": gpt.get("min_refresh_seconds"),
            "first_screen_whitespace": layout.get("first_screen_whitespace"),
            "dom_node_count": layout.get("dom_node_count"),
            # ad-tech
            "prebid_bidder_count": prebid.get("bidder_count"),
            "prebid_version": prebid.get("version"),
            # performance (bytes/requests/cpu are CDP-authoritative)
            "lcp_ms": cwv.get("lcp_ms"),
            "cls": cwv.get("cls"),
            "ad_cls_share": cwv.get("ad_cls_share"),
            "page_weight_bytes": res.get("page_weight_bytes"),
            "bytes_by_type": res.get("bytes_by_type"),
            "request_count": res.get("request_count"),
            "third_party_host_count": res.get("third_party_host_count"),
            "tracker_domain_count": res.get("tracker_domain_count"),
            "ad_request_count": res.get("ad_request_count"),
            "cpu_task_duration_s": cpu.get("task_duration_s"),
            # privacy
            "cmp_tcf": cmp.get("tcf"),
            "cmp_gpp": cmp.get("gpp"),
            "cmp_usp": cmp.get("usp"),
            "gpc": cmp.get("gpc"),
            "cookie_count": cmp.get("cookie_count"),
            "third_party_cookie_count": cmp.get("third_party_cookie_count"),
            # video
            "has_video": video.get("has_video"),
            "video_autoplay": video.get("autoplay_count"),
            "video_muted_autoplay": video.get("muted_autoplay_count"),
            "max_player_area_px": video.get("max_player_area_px"),
            "large_player": video.get("large_player"),
        })
    else:
        m["rendered"] = False
    return m


_BUILDERS = {
    "supply_chain": lambda s: _supply_chain(s),
    "mfa": lambda s: _mfa(s),
    "brand_suitability": lambda s: _brand_suitability(s),
    "ad_experience": lambda s: _ad_experience(s.get("render") or {}),
    "video": lambda s: _video(s.get("render") or {}),
    "privacy": lambda s: _privacy(s.get("render") or {}),
    "performance": lambda s: _performance(s.get("render") or {}),
}


def assemble(signals: dict[str, Any]) -> dict[str, Any]:
    """Compute sub-scores, per-score raw-input breakdown, composite, confidence."""
    fetch_ok = bool((signals.get("fetch") or {}).get("ok"))
    render_ok = bool((signals.get("render") or {}).get("ok"))

    # Which sub-scores are computable given the tier.
    names = ["supply_chain"]
    if fetch_ok or render_ok:
        names += ["mfa", "brand_suitability"]
    if render_ok:
        names += ["ad_experience", "video", "privacy", "performance"]

    subs: dict[str, float] = {}
    breakdown: dict[str, dict] = {}
    for name in names:
        score, inputs = _BUILDERS[name](signals)
        if score is None:
            continue
        subs[name] = score
        breakdown[name] = {"score": score, "weight": WEIGHTS[name], "inputs": inputs}

    if not fetch_ok and not render_ok:
        integrity = None
    else:
        wsum = sum(WEIGHTS[k] for k in subs)
        integrity = round(sum(subs[k] * WEIGHTS[k] for k in subs) / wsum, 2) if wsum else None

    confidence = 0.85 if render_ok else (0.4 if fetch_ok else 0.1)
    return {
        "scan_tier": "render" if render_ok else "static",
        "signals": signals,
        "metrics": _flatten_metrics(signals),
        "sub_scores": subs,
        "score_breakdown": breakdown,
        "integrity_score": integrity,
        "confidence": confidence,
    }


def score_static(signals: dict[str, Any]) -> dict[str, Any]:
    """Backward-compatible entry for the static worker (no render signals)."""
    return assemble(signals)
