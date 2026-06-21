# Metrics Dictionary

Every metric the scanner produces, with its definition, **type** (RAW = measured
directly / DERIVED = computed), **tier** (static HTTP vs headless render), and —
for derived metrics — the **raw inputs + formula** behind it. The same raw inputs
are exposed per-record in `score_breakdown` (via `GET /scan/{id}`) and flattened
into `metrics` / the CSV export, so no derived number is opaque.

Sub-scores are **0–100, higher = better integrity**. The composite
`integrity_score` is the weight-normalized average over whichever sub-scores are
present (weights below). Confidence: 0.4 static-only → 0.85 render-backed.

---

## Composite

| Metric | Type | Definition |
|---|---|---|
| `integrity_score` | DERIVED | Weighted average of present sub-scores, renormalized by present weight. Inputs: all sub-scores + their weights. |
| `confidence` | DERIVED | 0.4 if only static signals, 0.85 if render-backed, 0.1 if fetch failed. |

**Sub-score weights:** supply_chain .20 · ad_experience .22 · mfa .18 · performance .12 · video .10 · brand_suitability .10 · privacy .08.

---

## 1. Supply-chain transparency  *(static, domain-level, cached 24h)*

**`supply_chain` (DERIVED, 0–100)** — how transparently the site's ad inventory is sold. *Not* an ad-quality measure.
Weighted average of available components (renormalized so the sellers.json terms only count when resolution succeeded): `ads_txt_present` (35), `direct_ratio` (25), `ownership_declared` (15), `https` (10), `seller_resolution` (10), `seller_transparency = 1−confidential_ratio` (5). Raw inputs:

| Raw metric | Definition |
|---|---|
| `ads_txt_present` | ads.txt found + parseable at the domain root |
| `ads_txt_direct_count` / `ads_txt_reseller_count` | # DIRECT vs RESELLER authorized-seller lines |
| `ads_txt_direct_ratio` | DIRECT / (DIRECT + RESELLER) — higher = less reseller arbitrage |
| `ads_txt_distinct_ad_systems` | # distinct exchanges/SSPs authorized |
| `ads_txt_distinct_accounts` | # distinct (ad system, account) pairs |
| `ownership_declared` | OWNERDOMAIN or MANAGERDOMAIN present |
| `https` | final URL served over https |

**sellers.json cross-resolution** — each ad system's `sellers.json` is fetched once into a global 7-day cache, and the publisher's ads.txt account IDs are looked up in it:

| Raw / derived | Definition |
|---|---|
| `total_supply_paths` | # authorized-seller lines in ads.txt |
| `ad_systems_with_sellers_json` | # ad systems whose sellers.json resolved (within the top-30 cap) |
| `resolved_sellers` / `unresolved_accounts` | account IDs found vs not found in the matching sellers.json |
| `seller_resolution_rate` | resolved / resolvable — higher = more verifiable supply path |
| `intermediary_count` / `publisher_seller_count` / `intermediary_ratio` | seller_type breakdown; high intermediary ratio = more reseller hops |
| `confidential_sellers` / `confidential_ratio` | sellers marked `is_confidential` (opaque) |
| `direct_domain_match` | DIRECT lines whose sellers.json `domain` matches the publisher |

Also captured: app-ads.txt presence/counts, robots.txt (sitemap, disallow count).

## 2. Ad experience / clutter  *(render)*

**`ad_experience` (DERIVED, 0–100)** — penalizes layout clutter. `100 − penalties`, where penalties come from A2CR (≤40), above-fold stacking (≤20), total slot count (≤15), sticky ads (≤10), first-screen ad coverage (≤15), cramped spacing (10 if median gap <200px with >3 ads), refresh (25 if <15s, else 12). Raw inputs:

| Raw metric | Type | Definition |
|---|---|---|
| `ad_slot_count` | RAW | ad slots detected — GPT runtime **+** ad-host iframes + `google_ads_iframe_`/`adsbygoogle`/`data-google-query-id` markers (not GPT-only) |
| `ads_detected_via_gpt` | RAW | how many of those came from the GPT runtime (vs iframe/marker detection) |
| `filled_slot_count` / `empty_slot_count` | RAW | slots that rendered with area > 0 vs collapsed/unfilled |
| `ads_in_view` | RAW | ad slots that reached ≥50% viewport visibility (IntersectionObserver) |
| `ads_viewable_1s` | DERIVED | ads that held ≥50% visibility for ≥1 continuous second (MRC display viewability). Inputs: per-slot `viewable_ms` |
| `ads_per_screen` / `ads_per_1000px` | DERIVED | ad density normalized to scroll depth. Inputs: `filled_count`, page height, viewport height |
| `interstitial` | RAW | a filled ad covering ≥90% of the viewport |
| `ad_cls_share` | DERIVED | fraction of total CLS attributable to ad nodes (layout-shift sources matched to tagged ad elements) |
| `hidden_ad_count` / `tiny_ad_count` / `offscreen_ad_count` / `stacked_ad_count` | RAW | **GIVT-style validity** — filled ads rendered into hidden (display:none/visibility/opacity), 1×1-pixel, off-canvas, or overlapping (stacked) slots; classic served-but-not-viewable signals |
| `suspicious_ad_count` | DERIVED | union of the four GIVT counts; small penalty applied to `ad_experience` |
| `above_fold_ads` / `below_fold_ads` | RAW | filled slots whose top is within / below the opening viewport |
| `ad_sizes` | RAW | histogram of rendered ad sizes (e.g. `300x250:4;728x90:2`), IAB-named where standard |
| `a2cr` | DERIVED | ad-to-content ratio = total ad pixel area / total page pixel area. Inputs: `total_ad_area_px`, `page_area_px` |
| `first_screen_ad_coverage` | DERIVED | ad pixels visible in the opening viewport / viewport area |
| `first_screen_whitespace` | DERIVED | fraction of the opening viewport that is empty (grid-sampled `elementFromPoint`; **approximate**) — "unused space" |
| `first_ad_offset_px` | RAW | vertical distance (px) from page top to the first ad |
| `ad_gap_min_px` / `ad_gap_median_px` / `ad_gap_max_px` | DERIVED | vertical gap between consecutive ad units (sorted by top). Inputs: per-slot `top`+`height` |
| `sticky_ad_count` | RAW | slots with CSS position fixed/sticky (anchored ads) |
| `ad_refreshing` / `ad_refresh_events` / `min_refresh_seconds` | RAW | observed slot re-renders during the dwell + shortest interval seen |
| `total_ad_area_px` / `page_area_px` | RAW | summed ad pixel area; full scroll-height page area |

## 3. MFA / ad-load risk  *(render; static proxy fallback)*

**`mfa` (DERIVED, 0–100; higher = less MFA-like)** — per project decision, driven by **on-page ad load only** (no traffic-source data). `100 − risk`, risk from A2CR (≤45), filled slot count (≤20), above-fold stacking (≤10), sub-15s refresh (15), thin content (<200 words +25, <500 +12), link-density (link/text >0.2 +15, >0.1 +7). Raw inputs: `a2cr`, `filled_count`, `above_fold_count`, `refreshing`, `min_refresh_seconds`, `word_count`, `link_to_text_ratio`, `rendered`.

## 4. Performance  *(render)*

**`performance` (DERIVED, 0–100)** — weighted: LCP 0.5, CLS 0.3, page weight 0.2. LCP scored 100 at ≤2.5s → 0 at ≥6s; CLS 100 at ≤0.1 → 0 at ≥0.5; weight 100 at ≤2MB → 0 at ≥8MB. Raw inputs:

| Raw metric | Definition |
|---|---|
| `lcp_ms` | Largest Contentful Paint (lab, this render) |
| `cls` | Cumulative Layout Shift (lab; varies between renders) |
| `page_weight_bytes` | **CDP-authoritative** total encoded bytes (`Network.loadingFinished`), not the under-reporting Resource Timing API. Fonts/media are blocked by default (`AI_RENDER_BLOCK_RESOURCES`); images are kept so weight is accurate |
| `bytes_by_type` | byte breakdown Document/Script/Stylesheet/Image/Font/Media/XHR/Fetch |
| `request_count` | # network requests (CDP) |
| `third_party_host_count` | distinct non-first-party registrable domains (CDP) |
| `cpu_task_duration_s` | main-thread task time (CDP Performance) |

## 5. Privacy / consent  *(render)*

**`privacy` (DERIVED, 0–100)** — whether a consent-management platform is deployed. `50·TCF + 30·GPP + 10·USP`; if no live framework but a **CMP vendor is detected** (deployed but dormant — e.g. an EU TCF prompt that doesn't fire for our non-EU vantage point), 40; if none, 10 (>10 trackers) or 20. Detection uses the live API **and** the spec locator iframes (`__tcfapiLocator` etc.) **and** known vendor scripts (OneTrust, Sourcepoint, Google Funding Choices, Didomi, …) so region-gated CMPs aren't missed. Measures whether consent capture is *implemented*, **not** whether it's honored. Raw inputs: `cmp_present`, `cmp_vendor`, `cmp_tcf`, `cmp_gpp`, `cmp_usp`, `cmp_tcf_api_live`, `gpc`, `cookie_count`, `tracker_domain_count`.

## 6. Video / OLV  *(render; only when video present)*

**`video` (DERIVED, 0–100)** — `100 − 40` if unmuted autoplay, `−15` if muted autoplay. Excluded from composite when no video. Raw inputs: `has_video`, `video_autoplay`, `video_muted_autoplay`, players (jwplayer/videojs), `video_tag_count`.

## 7. Brand suitability  *(static — heuristic)*

**`brand_suitability` (DERIVED, 0–100)** — maps a GARM-style risk tier to a score (low 100 / medium 70 / high 40 / floor 10). Raw inputs: `suitability_tier`, `suitability_flags`, `risk_weight`. Classified by **zero-shot embeddings** (model2vec `potion-base-8M`): page text is compared to short risk-class prototypes vs benign anchors, flagging only matches that clearly beat the benign baseline. Falls back to the keyword lexicon if the model can't load (`method` field records which). Still **`heuristic`**: embeddings judge topic, so they can flag content that is strongly *about* violence/crime even when reporting on it (they cluster related risk concepts); thin/diluted real pages land milder than keyword-dense text. Advisory only.

## 8. Content & ad-tech footprint  *(informational, not scored directly)*

`content_category` (+confidence) — **zero-shot embedding** classification (model2vec) into IAB-style tier-1 categories, with keyword fallback. Classified from the static HTML when available, else from the **rendered DOM** (the render tier loads bot-protected pages the static fetch is blocked from) — `content_source` records `static` vs `render`. `word_count`, `paragraph_count`, `link_count`, `link_to_text_ratio`, `has_gpt`, `has_prebid`, `ssp_count`, `prebid_bidder_count`, `prebid_version`, `native_widgets`, `ad_request_count`, `dom_node_count`.

---

## Roadmap to commercial parity

Grounded in the union of what Sincera/OpenSincera, Scope3, IAS, DoubleVerify, Jounce, DeepSee, MRC and IAB actually measure. Status vs that target:

### ✅ Done (metrics v2)
- **Page weight / bytes via CDP** — authoritative `Network.loadingFinished.encodedDataLength`, with `bytes_by_type`. (Was ~2× under-reported by Resource Timing.)
- **Cookies via CDP** — `getAllCookies` incl. HttpOnly; 1st/3rd-party split.
- **Multi-source ad detection** — GPT runtime + ad-host iframes + GPT/adsense markers, nesting de-duped (no longer GPT-only).
- **MRC time-weighted viewability** — `ads_in_view` + `ads_viewable_1s` (≥50% for ≥1s) via IntersectionObserver over the dwell.
- **Refresh** — GPT events + MutationObserver iframe-swap counting; dwell raised to 8s.
- **Ad density** per screen / per 1000px; **interstitial detection**; **ad-attributable CLS**.
- **CPU seconds** + **CDP request / third-party-host counts**; **video player area + large-player flag**.

### Still missing (render)
- **CLS median-of-N renders** (varies run-to-run); **CWV INP** (needs real interaction).
- **Behavioral sticky confirmation** (re-read rect after scroll) and **instream-vs-outstream** classification; **video viewability** (≥50% ≥2s).
- **Tracker classification via the full DuckDuckGo Tracker Radar / Disconnect dataset** (we ship a curated list + entity-owner collapse as a drop-in point).

### ✅ Done (static) — sellers.json cross-resolution
- ads.txt accounts resolved against each ad system's `sellers.json` (global 7-day cache, streaming-capped, top-30 systems): `total_supply_paths`, `resolved_sellers`, `seller_resolution_rate`, `intermediary_ratio`, `confidential_sellers`, `direct_domain_match`; folded into the `supply_chain` score. *(Huge sellers.json files — e.g. Google's — exceed the 15MB cap and are recorded as present-but-too-large rather than resolved; raise `AI_SELLERS_JSON_MAX_BYTES` to include them.)*

### ✅ Done — GIVT-style geometric validity
- Served-but-not-viewable detection on filled ad slots: `hidden_ad_count`, `tiny_ad_count` (1×1), `offscreen_ad_count`, `stacked_ad_count`, `suspicious_ad_count` (folded into `ad_experience`). *(The traffic-source side of GIVT — data-center IPs, known bots, prefetch — needs request-log/IP data and is out of scope in-house.)*

### ✅ Done — embedding content classifier
- `content_category` + brand-suitability now use **zero-shot static embeddings** (model2vec) with keyword fallback. Fixes the keyword classifier's topical false-positives (e.g. an article *about* advertising no longer flags "adult"). Limitation: embeddings judge topic, so crime/violence *reporting* can still land at elevated suitability tiers; a fine-tuned/larger model would separate report-vs-promote.

### Still missing (static)
- **SupplyChain (schain) validation** — ads.txt account == sellers.json seller_id == schain sid (needs bid-stream schain object).

### Out of scope in-house (need bid-stream / external data)
- **ID absorption rate**, **GPID adoption** — require RTB bid-stream access.
- **Paid-traffic dependence** (Jounce/DeepSee primary MFA axis) — needs traffic-source data; **excluded by project decision** (our MFA is on-page ad-load only).
- **SIVT/fraud, full attention models** (IAS/DV proprietary), **carbon/gCO2PM** (Scope3 model).

### Architecture
Two render planes: **in-page** (`page.evaluate` — geometry, viewability, consent APIs, prebid, video) and **CDP** (`Network`/`Performance` — authoritative bytes, requests, cookies, CPU). Consent APIs (`__tcfapi`/`__gpp`) are in-page only; bytes/cookies are CDP only.
