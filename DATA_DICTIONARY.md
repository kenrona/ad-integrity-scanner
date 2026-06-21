# Data Dictionary

Field-level reference for scan output. **Generated** by `python -m app.datadict` from `analytics._SCHEMA` + `scoring.WEIGHTS` — do not edit by hand. See [`METRICS.md`](./METRICS.md) for scoring formulas and the inputs behind each derived score.

## Composite weights

`integrity_score` = weight-normalized average of present sub-scores:

| Sub-score | Weight |
|---|---|
| ad_experience | 0.22 |
| supply_chain | 0.2 |
| mfa | 0.18 |
| performance | 0.12 |
| video | 0.1 |
| brand_suitability | 0.1 |
| privacy | 0.08 |

## Export / CSV fields

These are the columns in the CSV/Parquet export, in order.

| Column | Type | R/D | Definition |
|---|---|---|---|
| `url` | VARCHAR | raw | Canonical (normalized) URL scanned. |
| `domain` | VARCHAR | raw | Registrable domain of the URL. |
| `scan_tier` | VARCHAR | raw | 'static' (HTTP only) or 'render' (headless browser). |
| `integrity_score` | DOUBLE | derived | Composite 0-100; weight-normalized avg of present sub-scores. |
| `confidence` | DOUBLE | derived | 0.4 static-only, 0.85 render-backed, 0.1 fetch failed. |
| `supply_chain` | DOUBLE | derived | Sub-score: ad-inventory selling transparency (ads.txt + sellers.json). |
| `ad_experience` | DOUBLE | derived | Sub-score: ad-layout clutter (A2CR, fold, sticky, gaps, refresh). |
| `mfa` | DOUBLE | derived | Sub-score: Made-For-Advertising risk from on-page ad load + thin content. |
| `video` | DOUBLE | derived | Sub-score: OLV behavior (autoplay); null when no video present. |
| `privacy` | DOUBLE | derived | Sub-score: consent-framework presence (TCF/GPP/USP/GPC). |
| `performance` | DOUBLE | derived | Sub-score: Core Web Vitals (LCP/CLS) + page weight. |
| `brand_suitability` | DOUBLE | derived | Sub-score: GARM-style content risk tier (heuristic). |
| `ads_txt_present` | BOOLEAN | raw | ads.txt found + parseable at the domain root. |
| `ads_txt_direct_ratio` | DOUBLE | derived | DIRECT / (DIRECT+RESELLER) authorized-seller lines. |
| `ads_txt_distinct_ad_systems` | INTEGER | raw | # distinct exchanges/SSPs authorized in ads.txt. |
| `https` | BOOLEAN | raw | Final URL served over HTTPS. |
| `total_supply_paths` | INTEGER | raw | # authorized-seller lines in ads.txt. |
| `resolved_sellers` | INTEGER | raw | ads.txt accounts found in the matching sellers.json. |
| `seller_resolution_rate` | DOUBLE | derived | resolved / resolvable accounts (supply-path verifiability). |
| `intermediary_ratio` | DOUBLE | derived | INTERMEDIARY / resolved sellers (reseller-hop density). |
| `confidential_sellers` | INTEGER | raw | Resolved sellers marked is_confidential (opaque). |
| `ad_slot_count` | INTEGER | raw | Ad slots detected (GPT runtime + ad-host iframes + markers). |
| `filled_slot_count` | INTEGER | raw | Ad slots that rendered with area > 0. |
| `ads_detected_via_gpt` | INTEGER | raw | How many slots came from the GPT runtime vs iframe/marker detection. |
| `above_fold_ads` | INTEGER | raw | Filled ads whose top is within the opening viewport. |
| `below_fold_ads` | INTEGER | raw | Filled ads below the opening viewport. |
| `ads_in_view` | INTEGER | raw | Ads that reached >=50% viewport visibility. |
| `ads_viewable_1s` | INTEGER | derived | Ads that held >=50% visibility for >=1s (MRC display viewability). |
| `ads_per_1000px` | DOUBLE | derived | Filled ad count per 1000px of page scroll height. |
| `sticky_ad_count` | INTEGER | raw | Ads with CSS position fixed/sticky (anchored). |
| `interstitial` | BOOLEAN | raw | A filled ad covering >=90% of the viewport. |
| `hidden_ad_count` | INTEGER | raw | Ads in display:none/visibility:hidden/opacity:0 slots (GIVT). |
| `tiny_ad_count` | INTEGER | raw | Ads in <=2x2px (1x1 pixel) slots (GIVT). |
| `offscreen_ad_count` | INTEGER | raw | Ads positioned off-canvas (GIVT). |
| `stacked_ad_count` | INTEGER | raw | Ads overlapping another ad by >50% (ad stacking, GIVT). |
| `suspicious_ad_count` | INTEGER | derived | Union of hidden/tiny/offscreen/stacked ads (served-but-not-viewable). |
| `ad_sizes` | VARCHAR | raw | Histogram of rendered ad sizes, e.g. '300x250:4;728x90:2'. |
| `a2cr` | DOUBLE | derived | Ad-to-content ratio = total ad pixel area / total page pixel area. |
| `first_screen_ad_coverage` | DOUBLE | derived | Ad pixels in the opening viewport / viewport area. |
| `first_screen_whitespace` | DOUBLE | derived | Fraction of opening viewport that is empty ('unused space', approx). |
| `first_ad_offset_px` | INTEGER | raw | Vertical distance (px) from page top to the first ad. |
| `ad_gap_median_px` | INTEGER | derived | Median vertical gap between consecutive ad units. |
| `ad_refreshing` | BOOLEAN | raw | Whether any slot re-rendered during the dwell. |
| `min_refresh_seconds` | DOUBLE | raw | Shortest observed refresh interval (s). |
| `ad_cls_share` | DOUBLE | derived | Fraction of total CLS attributable to ad nodes. |
| `lcp_ms` | INTEGER | raw | Largest Contentful Paint (ms, lab). |
| `cls` | DOUBLE | raw | Cumulative Layout Shift (lab; median-of-N when AI_RENDER_SAMPLES>1). |
| `inp_ms` | INTEGER | derived | Synthetic INP proxy from a scripted interaction (lab, not field RUM). |
| `page_weight_bytes` | BIGINT | raw | Total encoded bytes over the network (CDP-authoritative). |
| `request_count` | INTEGER | raw | # network requests (CDP). |
| `third_party_host_count` | INTEGER | raw | Distinct non-first-party registrable domains (CDP). |
| `tracker_domain_count` | INTEGER | raw | Third-party domains matching the tracker list. |
| `tracker_entity_count` | INTEGER | derived | Distinct tracker OWNERS (Disconnect dataset, owner-collapsed). |
| `cpu_task_duration_s` | DOUBLE | raw | Main-thread task time in seconds (CDP Performance). |
| `dom_node_count` | INTEGER | raw | Total DOM elements. |
| `schain_present` | BOOLEAN | raw | Prebid declared a SupplyChain object. |
| `schain_valid` | BOOLEAN | derived | schain complete and every hop's asi is authorized in ads.txt. |
| `video_viewable_2s` | INTEGER | derived | Videos that held >=50% visibility for >=2s (MRC video viewability). |
| `cmp_present` | BOOLEAN | derived | A consent-management platform is deployed (live API, locator iframe, or known vendor). |
| `cmp_vendor` | VARCHAR | raw | Detected CMP vendor (OneTrust, Sourcepoint, Google Funding Choices, ...). |
| `cmp_tcf` | BOOLEAN | raw | IAB TCF detected (__tcfapi or __tcfapiLocator iframe). |
| `cmp_gpp` | BOOLEAN | raw | IAB Global Privacy Platform detected (__gpp or __gppLocator). |
| `gpc` | BOOLEAN | raw | Global Privacy Control signalled by the browser. |
| `cookie_count` | INTEGER | raw | Total cookies set (CDP; includes HttpOnly). |
| `third_party_cookie_count` | INTEGER | raw | Cookies whose domain != first party. |
| `ssp_count` | INTEGER | raw | Distinct SSP/exchange vendors detected in markup. |
| `prebid_bidder_count` | INTEGER | raw | Distinct Prebid bidders observed. |
| `has_video` | BOOLEAN | raw | Any video player/element present. |
| `word_count` | INTEGER | raw | Visible body text word count. |
| `content_category` | VARCHAR | derived | IAB-style category via zero-shot embeddings (model2vec), keyword fallback. |
| `content_source` | VARCHAR | raw | Whether content was classified from 'static' HTML or the 'render' DOM (bot-blocked sites). |
| `suitability_tier` | VARCHAR | derived | GARM-style risk tier low/medium/high/floor via embeddings (heuristic). |
| `scanned_at` | TIMESTAMP | raw | Timestamp the record was written. |

_R/D = raw (measured) or derived (computed). The full nested signal tree and the `score_breakdown` (every derived score with its raw inputs) are stored in `scan_results` and returned by `GET /scan/{id}`._

## Database tables

| Table | Purpose |
|---|---|
| `scan_results` | One row per completed scan — the deliverable {url -> metrics + scores}. JSONB columns: signals (raw tree), metrics (flattened), sub_scores, score_breakdown (per-score raw inputs). |
| `scan_queue` | Postgres-backed work queue (FOR UPDATE SKIP LOCKED). tier static|render, status queued|processing|done|error, attempts. |
| `scan_ledger` | Dedup + tiered-TTL freshness per url_hash (page TTL). |
| `domain_signals` | Per-domain cached signals (ads.txt/sellers/robots/supply_paths), 24h TTL. |
| `sellers_json_cache` | Global per-ad-system sellers.json cache (7d TTL); seller id->entry map when small enough. |
