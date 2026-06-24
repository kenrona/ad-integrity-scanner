"""Generate DATA_DICTIONARY.md — a field-level reference for the scan output.

Derived from the code so it cannot drift: the export/CSV columns come from
`analytics._SCHEMA`; definitions live in the FIELD_DEFS map below (the single
source of truth for one-line field definitions). Run:

    python -m app.datadict            # write DATA_DICTIONARY.md
    python -m app.datadict --print    # print to stdout
"""
from __future__ import annotations

import sys

from app.analytics import _SCHEMA
from app.scoring import WEIGHTS

# field -> (definition, "raw" | "derived")
FIELD_DEFS: dict[str, tuple[str, str]] = {
    # identity / meta
    "url": ("Canonical (normalized) URL scanned.", "raw"),
    "domain": ("Registrable domain of the URL.", "raw"),
    "scan_tier": ("'static' (HTTP only) or 'render' (headless browser).", "raw"),
    "integrity_score": ("Composite 0-100; weight-normalized avg of present sub-scores.", "derived"),
    "confidence": ("0.4 static-only, 0.85 render-backed, 0.1 fetch failed.", "derived"),
    "scanned_at": ("Timestamp the record was written.", "raw"),
    # sub-scores
    "supply_chain": ("Sub-score: ad-inventory selling transparency (ads.txt + sellers.json).", "derived"),
    "ad_experience": ("Sub-score: ad-layout clutter (A2CR, fold, sticky, gaps, refresh).", "derived"),
    "mfa": ("Sub-score: Made-For-Advertising risk from on-page ad load + thin content.", "derived"),
    "video": ("Sub-score: OLV behavior (autoplay); null when no video present.", "derived"),
    "privacy": ("Sub-score: consent-framework presence (TCF/GPP/USP/GPC).", "derived"),
    "performance": ("Sub-score: Core Web Vitals (LCP/CLS) + page weight.", "derived"),
    "brand_suitability": ("Sub-score: GARM-style content risk tier (heuristic).", "derived"),
    # supply chain raw
    "ads_txt_present": ("ads.txt found + parseable at the domain root.", "raw"),
    "ads_txt_direct_ratio": ("DIRECT / (DIRECT+RESELLER) authorized-seller lines.", "derived"),
    "ads_txt_distinct_ad_systems": ("# distinct exchanges/SSPs authorized in ads.txt.", "raw"),
    "https": ("Final URL served over HTTPS.", "raw"),
    "total_supply_paths": ("# authorized-seller lines in ads.txt.", "raw"),
    "resolved_sellers": ("ads.txt accounts found in the matching sellers.json.", "raw"),
    "seller_resolution_rate": ("resolved / resolvable accounts (supply-path verifiability).", "derived"),
    "intermediary_ratio": ("INTERMEDIARY / resolved sellers (reseller-hop density).", "derived"),
    "confidential_sellers": ("Resolved sellers marked is_confidential (opaque).", "raw"),
    # ad layout geometry
    "ad_slot_count": ("Ad slots detected (GPT runtime + ad-host iframes + markers).", "raw"),
    "filled_slot_count": ("Ad slots that rendered with area > 0.", "raw"),
    "ads_detected_via_gpt": ("How many slots came from the GPT runtime vs iframe/marker detection.", "raw"),
    "above_fold_ads": ("Filled ads whose top is within the opening viewport.", "raw"),
    "below_fold_ads": ("Filled ads below the opening viewport.", "raw"),
    "ads_in_view": ("Ads that reached >=50% viewport visibility.", "raw"),
    "ads_viewable_1s": ("Ads that held >=50% visibility for >=1s (MRC display viewability).", "derived"),
    "ads_per_1000px": ("Filled ad count per 1000px of page scroll height.", "derived"),
    "sticky_ad_count": ("Ads with CSS position fixed/sticky (anchored).", "raw"),
    "interstitial": ("A filled ad covering >=90% of the viewport.", "raw"),
    "hidden_ad_count": ("Ads in display:none/visibility:hidden/opacity:0 slots (GIVT).", "raw"),
    "tiny_ad_count": ("Ads in <=2x2px (1x1 pixel) slots (GIVT).", "raw"),
    "offscreen_ad_count": ("Ads positioned off-canvas (GIVT).", "raw"),
    "stacked_ad_count": ("Ads overlapping another ad by >50% (ad stacking, GIVT).", "raw"),
    "suspicious_ad_count": ("Union of hidden/tiny/offscreen/stacked ads (served-but-not-viewable).", "derived"),
    "ad_sizes": ("Histogram of rendered ad sizes, e.g. '300x250:4;728x90:2'.", "raw"),
    "a2cr": ("Ad-to-content ratio = total ad pixel area / total page pixel area.", "derived"),
    "first_screen_ad_coverage": ("Ad pixels in the opening viewport / viewport area.", "derived"),
    "first_screen_whitespace": ("Fraction of opening viewport that is empty ('unused space', approx).", "derived"),
    "first_ad_offset_px": ("Vertical distance (px) from page top to the first ad.", "raw"),
    "ad_gap_median_px": ("Median vertical gap between consecutive ad units.", "derived"),
    "ad_refreshing": ("Whether any slot re-rendered during the dwell.", "raw"),
    "min_refresh_seconds": ("Shortest observed refresh interval (s).", "raw"),
    "ad_cls_share": ("Fraction of total CLS attributable to ad nodes.", "derived"),
    "ad_load_avg_ms": ("Mean ad load time (ms from navigation start) across detected ads.", "derived"),
    "ad_load_median_ms": ("Median ad load time (ms from navigation start).", "derived"),
    "ad_load_max_ms": ("Slowest ad load time (ms from navigation start).", "derived"),
    "ad_load_samples": ("# ads contributing a load time (GPT slot renders, else ad-host resource timings).", "raw"),
    "ad_load_source": ("Timing source for ad load speed: 'gpt_slot_render' or 'ad_host_resource_timing'.", "raw"),
    # performance / footprint
    "lcp_ms": ("Largest Contentful Paint (ms, lab).", "raw"),
    "cls": ("Cumulative Layout Shift (lab; median-of-N when AI_RENDER_SAMPLES>1).", "raw"),
    "inp_ms": ("Synthetic INP proxy from a scripted interaction (lab, not field RUM).", "derived"),
    "tracker_entity_count": ("Distinct tracker OWNERS (Disconnect dataset, owner-collapsed).", "derived"),
    "schain_present": ("Prebid declared a SupplyChain object.", "raw"),
    "schain_valid": ("schain complete and every hop's asi is authorized in ads.txt.", "derived"),
    "video_viewable_2s": ("Videos that held >=50% visibility for >=2s (MRC video viewability).", "derived"),
    "page_weight_bytes": ("Total encoded bytes over the network (CDP-authoritative).", "raw"),
    "request_count": ("# network requests (CDP).", "raw"),
    "third_party_host_count": ("Distinct non-first-party registrable domains (CDP).", "raw"),
    "tracker_domain_count": ("Third-party domains matching the tracker list.", "raw"),
    "cpu_task_duration_s": ("Main-thread task time in seconds (CDP Performance).", "raw"),
    "dom_node_count": ("Total DOM elements.", "raw"),
    # consent
    "cmp_present": ("A consent-management platform is deployed (live API, locator iframe, or known vendor).", "derived"),
    "cmp_vendor": ("Detected CMP vendor (OneTrust, Sourcepoint, Google Funding Choices, ...).", "raw"),
    "cmp_tcf": ("IAB TCF detected (__tcfapi or __tcfapiLocator iframe).", "raw"),
    "cmp_gpp": ("IAB Global Privacy Platform detected (__gpp or __gppLocator).", "raw"),
    "gpc": ("Global Privacy Control signalled by the browser.", "raw"),
    "cookie_count": ("Total cookies set (CDP; includes HttpOnly).", "raw"),
    "third_party_cookie_count": ("Cookies whose domain != first party.", "raw"),
    # ad-tech / content
    "ssp_count": ("Distinct SSP/exchange vendors detected in markup.", "raw"),
    "prebid_bidder_count": ("Distinct Prebid bidders observed.", "raw"),
    "has_video": ("Any video player/element present.", "raw"),
    "word_count": ("Visible body text word count.", "raw"),
    "content_category": ("IAB-style category via zero-shot embeddings (model2vec), keyword fallback.", "derived"),
    "content_source": ("Whether content was classified from 'static' HTML or the 'render' DOM (bot-blocked sites).", "raw"),
    "suitability_tier": ("GARM-style risk tier low/medium/high/floor via embeddings (heuristic).", "derived"),
}

_TABLES = {
    "scan_results": "One row per completed scan — the deliverable {url -> metrics + scores}. JSONB columns: signals (raw tree), metrics (flattened), sub_scores, score_breakdown (per-score raw inputs).",
    "scan_queue": "Postgres-backed work queue (FOR UPDATE SKIP LOCKED). tier static|render, status queued|processing|done|error, attempts.",
    "scan_ledger": "Dedup + tiered-TTL freshness per url_hash (page TTL).",
    "domain_signals": "Per-domain cached signals (ads.txt/sellers/robots/supply_paths), 24h TTL.",
    "sellers_json_cache": "Global per-ad-system sellers.json cache (7d TTL); seller id->entry map when small enough.",
}


def build() -> str:
    lines = [
        "# Data Dictionary",
        "",
        "Field-level reference for scan output. **Generated** by `python -m app.datadict` "
        "from `analytics._SCHEMA` + `scoring.WEIGHTS` — do not edit by hand. See "
        "[`METRICS.md`](./METRICS.md) for scoring formulas and the inputs behind each derived score.",
        "",
        "## Composite weights",
        "",
        "`integrity_score` = weight-normalized average of present sub-scores:",
        "",
        "| Sub-score | Weight |", "|---|---|",
    ]
    for k, w in sorted(WEIGHTS.items(), key=lambda kv: -kv[1]):
        lines.append(f"| {k} | {w} |")

    lines += ["", "## Export / CSV fields", "",
              "These are the columns in the CSV/Parquet export, in order.", "",
              "| Column | Type | R/D | Definition |", "|---|---|---|---|"]
    for name, typ in _SCHEMA:
        defn, rd = FIELD_DEFS.get(name, ("(see METRICS.md)", ""))
        lines.append(f"| `{name}` | {typ} | {rd or '-'} | {defn} |")

    lines += ["", "_R/D = raw (measured) or derived (computed). The full nested signal "
              "tree and the `score_breakdown` (every derived score with its raw inputs) "
              "are stored in `scan_results` and returned by `GET /scan/{id}`._", "",
              "## Database tables", "", "| Table | Purpose |", "|---|---|"]
    for t, d in _TABLES.items():
        lines.append(f"| `{t}` | {d} |")
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    md = build()
    if "--print" in sys.argv[1:]:
        print(md)
    else:
        import pathlib
        out = pathlib.Path(__file__).resolve().parent.parent / "DATA_DICTIONARY.md"
        out.write_text(md, encoding="utf-8")
        print(f"wrote {out}")
