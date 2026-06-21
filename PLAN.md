# Ad Environment Integrity Scoring — Build Plan

**Goal:** A local service that accepts a complete URL at an endpoint, scans the page + its domain, computes a robust set of "integrity" metrics about the ad environment that display **and OLV/video** ads would run in, and persists `{url → metrics + scores}` to a local database. Built to run **on your machine**: ~10k URLs/day for testing, scaling to **~1M URLs/day**. No cloud, no paid vendors.

**Confirmed decisions (2026-06-19):**
- **Runs locally.** Single machine. 10k/day test → 1M/day target (~12 URLs/sec avg). Architecture maps cleanly to AWS later, but zero cloud dependency now.
- **Hybrid scan** — cheap static fetch on *every* URL; headless render (Playwright) on a *sampled/prioritized* subset.
- **Async fire-and-forget** endpoint — `POST {url}` → `202 Accepted {scan_id}` → local queue → workers → DB.
- **Input is just the complete URL.** No other metadata supplied.
- **In-house only, no vendors.** Open data/specs only (IAB ads.txt/sellers.json, Prebid catalog, IAB Content Taxonomy, Core Web Vitals).
- **URLs are page-level** (specific articles/pages). Domain-level signals are cached and shared across pages of a domain.
- **MFA = on-page ad load.** Detected purely from how many ads are on the page + density/clutter/refresh. **No traffic-source / arbitrage data** (explicitly out).
- **Tiered TTL.** Domain signals cached ~24h; page scores cached longer (~7d). Repeat sends inside TTL return stored result; older → rescan.
- **Reads are batch/analytics.** No low-latency read API in v1; consumers query the DB directly.
- **Display and video weighted equally.** Full instrumentation of both.

---

## 1. Strategic grounding (from competitive research)

Three camps in this market; we borrow from all three:

| Camp | Players | What we take |
|---|---|---|
| **Metadata / media-telemetry scanners** | Sincera (→ The Trade Desk, Jan 2025; OpenSincera free API, ~400k publishers) | The metric suite: **ads-to-content ratio (A2CR)**, **ads-in-view**, **ad refresh rate**, **page weight**, prebid/supply-path counts. A transparent suite, not one black-box score. |
| **MFA classifiers** | Jounce Media, DeepSee.io, ANA studies | Ad **density + refresh** as MFA signals. *(We deliberately use the ad-load axis only — not the paid-traffic axis those vendors emphasize, per your decision.)* |
| **Verification vendors** | IAS, DoubleVerify, HUMAN | Signal *categories* (viewability, brand suitability) as reference definitions. |

**Useful reference points:**
- Scope3 flags a "problematic placement" as **ad refresh < 15s** or **rendering out of view** — concrete thresholds we can adopt.
- Sincera **A2CR**: compares creative dims vs ad-slot dims and takes the **larger** (captures reserved ad real estate regardless of fill).
- HTTP Archive 2024 medians as page-weight baselines: **~2.6 MB**, **71 requests**, **27–66 third-party domains** (desktop).
- Standards to target: ads.txt **1.1** / sellers.json / SupplyChain object; IAB Content Taxonomy **3.1**; Core Web Vitals (**INP** replaced FID; LCP ≤2.5s / INP ≤200ms / CLS ≤0.1); TCF **v2.3** / GPP / GPC.

---

## 2. Metric taxonomy (signal → tier)

**[S]** = static HTTP (every URL). **[R]** = headless render (sampled subset). Domain-level **[S]** signals computed once per domain and cached (24h TTL).

### A. Supply-chain transparency  *(domain-level, [S], cached)*
- ads.txt / app-ads.txt present? parseable? line count.
- DIRECT vs RESELLER ratio; reseller depth; # distinct supply paths; # authorized sellers.
- sellers.json resolution: `seller_type` PUBLISHER / INTERMEDIARY / BOTH; `is_confidential` rate.
- SupplyChain object hygiene where observable (schain depth, `complete=1`).

### B. Ad clutter / density / experience  *(page-level)* — **drives MFA**
- **[S] precursors:** hardcoded GPT/Prebid tags in HTML; declared ad-network script srcs; # ad-related third-party domains.
- **[R] truth:** rendered **ad-slot count**; **A2CR** (ad pixels vs content pixels, larger of slot/creative); above-the-fold ad count; **ads-in-view**; sticky/anchored/interstitial detection; **ad refresh rate** (dwell-window observation; flag <15s).

### C. Video / OLV  *(page-level, [R])* — **equal weight to display**
- Video player presence + framework (e.g. JW Player, Video.js, Brightcove, native `<video>`).
- Player size/geometry; above-the-fold?
- **Autoplay / muted-autoplay** behavior.
- **Instream vs outstream** (content video vs ad-only floating/in-feed player).
- **VAST / VPAID** ad tags observed in network calls; # video demand partners.
- Video ad slot count; floating/sticky video.

### D. MFA / ad-load risk  *(composite, [R])*
- Built **only** from ad density + A2CR + above-fold ad count + refresh + (video ad-load) + templated/thin-content + slideshow/gallery pagination + infinite scroll. **No traffic-source signals.**

### E. Page quality & performance  *(page-level)*
- **[R]:** real page weight & request count; **LCP, CLS** (web-vitals lib); **TBT** as INP proxy; # third-party + ad-tech domains; main-thread load.
- **[R] content:** visible word count, original-vs-aggregated heuristics, **IAB Content Taxonomy 3.1** classification, brand-suitability (GARM-style 11 categories / 4 risk tiers).

### F. Privacy / consent  *(page-level, [R])*
- CMP via `window.__tcfapi` (TCF v2.3) / `window.__gpp` (GPP); consent-string validity; **GPC**.

### G. Ad-tech footprint  *(page-level, [R])*
- `window.pbjs` bidders (`getBidResponses()`/`getEvents()` → bidderCode); `window.googletag.getSlots()`; resolve bidder codes → SSPs via Prebid catalog; # demand partners; header-bidding wrapper fingerprint.

---

## 3. Scoring model

Transparent **per-category sub-scores (0–100) + a config-weighted composite Integrity Score** — never a single opaque number. Each sub-score ships with the raw signals behind it.

| Sub-score | Drivers | Phase |
|---|---|---|
| Supply-chain transparency | ads.txt/sellers.json hygiene, reseller depth | 1 (static) |
| Ad experience | A2CR, ads-in-view, refresh, sticky/interstitial | 2 (render) |
| Video / OLV | player, autoplay, instream/outstream, VAST, video ad-load | 2 (render) |
| MFA / ad-load risk | density + refresh + content templating | 3 |
| Page quality & performance | CWV, weight, 3p footprint, content quality | 2–3 |
| Privacy/consent | CMP/TCF/GPP/GPC | 2 |
| Brand suitability | content category vs risk tiers | 3 |

- **Composite** = configurable weighted blend (weights in config, tunable without redeploy). Display and video equally weighted.
- **Versioned:** every record stamps `scoring_version` + `scanner_version`.
- **Confidence field:** static-only records carry lower confidence than render-backed ones; surfaced explicitly.

---

## 4. Architecture (local-first)

```
  POST /scan {url}            ┌───────────────────────────────────────────┐
        │                     │ API server (FastAPI / uvicorn)            │
        ▼                     │  • validate + normalize URL               │
   202 {scan_id}  ◄───────────│  • dedup vs scan_ledger (tiered TTL)      │
                              │  • enqueue → Redis queue (static)         │
                              └───────────────┬───────────────────────────┘
                                              │
                       ┌──────────────────────▼───────────────────────┐
                       │ Static workers (async httpx pool)             │  high concurrency
                       │  • fetch ads.txt/app-ads.txt/sellers.json/    │
                       │    robots/headers/raw HTML                    │
                       │  • domain cache (Postgres, 24h TTL)           │
                       │  • compute [S] signals                        │
                       │  • render needed? (sampling/priority policy)  │──► Redis queue (render)
                       └──────────────────────┬────────────────────────┘
                                              │ write signals
                       ┌──────────────────────▼────────────────────────┐
                       │ Render workers (persistent Playwright pool)    │
                       │  • Chromium, N contexts, block img/font/css    │
                       │  • intercept network (VAST/exchange calls)     │
                       │  • read pbjs/googletag/__tcfapi/__gpp          │
                       │  • ad slots / A2CR / refresh / video / CWV     │
                       └──────────────────────┬─────────────────────────┘
                                              │ write signals
                       ┌──────────────────────▼─────────────────────────┐
                       │ Scoring worker → computes sub-scores+composite, │
                       │ writes final record                              │
                       └──────────────────────┬─────────────────────────┘
                                              ▼
                       ┌────────────────────────────────────────────────┐
                       │ PostgreSQL (local, via Docker)                  │
                       │  • scan_results  • domain_signals  • scan_ledger│
                       │  + optional parquet/DuckDB export for analytics │
                       └────────────────────────────────────────────────┘
```

**Recommended stack (all local):**
- **Language: Python.** Rich for parsing/content-analysis/scoring, first-class `playwright`, and easy analytics (DuckDB/pandas). *(Node is a viable alternative if you prefer JS-native pbjs eval — say the word.)*
- **API: FastAPI + uvicorn** — async, trivial `202` + enqueue.
- **Queue: PostgreSQL job table** (`FOR UPDATE SKIP LOCKED`), not Redis. The machine already runs `postgresql@17` and has no Docker/Redis, so a PG-backed queue gives the same claim-once semantics with zero extra infra and easily covers 1M/day (~12/sec). Jobs carry `attempts`; failures requeue up to `AI_MAX_ATTEMPTS` then park as `error`. Two logical queues via the `tier` column: `static` (high volume) and `render` (gated). **Swap to Redis/arq later** behind `app/queue.py` only if scaling out to multiple machines.
- **Static fetch: httpx/aiohttp** — async, thousands of concurrent fetches on one box.
- **Render: Playwright (Chromium), persistent browser + recycled contexts.** Block images/fonts/CSS (~40–60% memory saving). Never launch a browser per request.
- **DB: PostgreSQL** (Docker) as operational store; periodic **parquet export queried via DuckDB** for batch analytics (matches your "batch/analytics reads" answer — no read API needed).
- **Orchestration: none required** — uses the local `postgresql@17`; API + workers run as native Python processes. (No Docker/Redis dependency.)

**Why this fits 1M/day on one machine:** 1M/day ≈ 12 URLs/sec average. Static tier is I/O-bound and trivially handles that. Render at ~5–10% sampling ≈ 50–100k/day ≈ ~1/sec → 2–4 Chromium contexts suffice. Comfortable headroom on a modern laptop/desktop.

---

## 5. Data model (sketch)

**`scan_results`** (Postgres): `url_hash` (PK), `url`, `domain`, `scanned_at`, `scan_tier` (static|render), `confidence`, `signals` (JSONB), `metrics` (JSONB), `sub_scores` (JSONB), `integrity_score`, `scoring_version`, `scanner_version`.

**`domain_signals`** (Postgres): `domain` (PK), ads.txt/sellers.json parse (JSONB), supply-path stats, `expires_at` (24h TTL).

**`scan_ledger`** (Postgres): `url_hash` (PK), `last_scanned`, `expires_at` (page TTL ~7d) — powers tiered-TTL dedup.

**Analytics:** scheduled export of `scan_results` → parquet on disk → DuckDB for ad-hoc/bulk queries.

---

## 6. Scale & sizing
- **Test:** 10k/day (~0.1/sec). Single process, in-process queue fine; SQLite acceptable but start on Postgres to avoid a later migration.
- **Target:** 1M/day (~12/sec avg). Redis queue + static worker pool (async) + 2–4 Playwright contexts + Postgres. All on one machine.
- **Sampling/priority policy** (config): always render new/unknown domains + suspected high-ad-load pages + a rolling re-scan; static-only otherwise. **Log what was skipped** so static-only is never mistaken for fully measured.
- **Backpressure:** Redis queue depth + worker concurrency caps; render queue is the throttle point.

---

## 7. Phased roadmap
- **Phase 0 — Foundations [DONE]:** FastAPI `/scan` returning 202, URL normalization, `scan_ledger` dedup w/ tiered TTL, Postgres-backed queue plumbing (with retry/max-attempts), static-worker skeleton, `GET /scan/{id}`, `/stats`, structured logging. (Replaced the originally-planned docker-compose Postgres+Redis with the local PG-queue decision above.)
- **Phase 1 — Static tier + first scores [DONE]:** ads.txt/app-ads.txt/robots/sellers.json + headers + HTML parsing (ad-tech footprint, content, video hints) → supply-chain sub-score + provisional composite + static page signals; per-domain cache (24h TTL). End-to-end `url → DB` verified on live publishers. *(ad-tech fingerprints match code/URLs only, not article prose, to avoid brand-mention false positives; true sellers.json cross-resolution per ad-system domain deferred.)*
- **Phase 2 — Render tier [DONE]:** persistent Playwright/Chromium pool (blocks img/font/media), pbjs/googletag/CMP via `page.evaluate`, ad-slot geometry/A2CR/above-fold/refresh, **video/OLV** (autoplay), CWV (LCP/CLS) + page weight via Resource Timing → ad-experience + video + privacy + performance sub-scores; weighted composite + 0.85 confidence. Sampling gate (`AI_RENDER_SAMPLE_RATE`) enqueues `render` jobs from the static tier; render worker merges + re-scores the same scan_id. Verified live (theverge.com, wikipedia.org). *(INP not measured — needs real interaction; deferred. Refresh detection is best-effort via GPT slotRenderEnded during the dwell.)*
- **Phase 3 — MFA & content [DONE]:** MFA / ad-load risk sub-score (A2CR + slots + above-fold + refresh from render; thin-content + link-density secondary; **no traffic-source data** per decision), content-quality/templating metrics, IAB-style content category, GARM-style brand-suitability tier → folded into a 7-way weighted composite. Verified live. *(Category + suitability are an in-house keyword classifier flagged `heuristic`; thresholds biased against flagging since word-matching can't distinguish "about X" from "is X". A future ML/embedding pass would sharpen these.)*
- **Phase 4 — Hardening & analytics [DONE]:** maintenance worker (`app/workers/maintenance.py`) with **visibility-timeout reaper** (requeue/park stuck `processing` jobs), **scan_queue pruning** (bounds growth), and an opt-in **TTL re-scan scheduler**; **Parquet/DuckDB analytics export** (`app/analytics.py`, CLI `export`/`summary`) as the batch read path; **browser GUI** (`app/static/index.html` at `/`) to submit a file of URLs one at a time with live progress + queue stats. Verified live. *(Throughput-to-1M/day is validated by design/sampling math + concurrent workers rather than a network load test against real publishers; raw-signals→lake offload is available via export, Postgres still keeps full signals hot — a hard offload/trim policy is left as a config choice.)*

**Code review (pre-Phase-4) — applied:** SSRF guard on every fetch + redirect hop and on render (blocks private/loopback/link-local/metadata; `app/ssrf.py`); streaming download size cap (no full-body OOM); Chromium sandbox kept ON; concurrent batch processing in both workers (static fan-out + real use of render concurrency; DB conns no longer held during network I/O); parallel domain-file fetches; dropped the wasteful publisher `sellers.json` fetch; URL length cap. Residual notes: DNS-rebinding not fully closed (pin IP / egress-filter in prod); `/scan` needs auth + rate-limit if exposed; stored URLs may carry PII.

---

## 8. Resolved — no open questions
All scoping decisions captured above. **Language: Python** (confirmed — the cost center is Chromium, which is language-agnostic). Phases 0–4 plus a security/efficiency review and a browser GUI are all built and verified.

### Post-Phase-4 — metrics expansion toward commercial parity (all DONE)
The per-phase caveats above were superseded by a metrics-v2 build (grounded in a competitive parity catalog):
- **CDP plane** — authoritative page weight / requests / cookies / CPU (Resource-Timing under-counted ad bytes ~2×); **multi-source ad detection** (GPT runtime + ad-host iframes + markers, not GPT-only); images kept during render so page weight is accurate.
- **Deep ad-layout geometry** — ad sizes (IAB), above/below-fold, inter-ad gaps, sticky (CSS + **behavioral**), interstitial, first-screen whitespace, ad density per screen/1000px, ad-attributable CLS.
- **MRC time-weighted viewability** (ads-in-view / ads-viewable-1s); **GIVT** validity (hidden/tiny/offscreen/stacked); accurate refresh.
- **CWV**: CLS **median-of-N** (`AI_RENDER_SAMPLES`); **synthetic INP** (lab, scripted interaction — not field RUM).
- **sellers.json cross-resolution** (global per-ad-system cache) → supply-path transparency; **schain validation** (Prebid schain `asi` vs ads.txt).
- **Tracker classification** via the bundled Disconnect dataset (owner-collapsed); **CMP detection** via live API + locator iframes + vendor scripts (region-robust); **realistic Chrome UA** (bot-protected inventory).
- **Embedding content / brand-suitability classifier** (model2vec, keyword fallback) — replaced the keyword-only classifier; bot-blocked pages classify from the **rendered DOM**.
- Every derived score ships a **`score_breakdown`** of raw inputs. Full field reference: [`DATA_DICTIONARY.md`](./DATA_DICTIONARY.md).
- **Ground-truth accuracy suite** (`tests/accuracy/`, ~200 fixtures) — 100% on deterministic metrics.

**Remaining work is external-data-dependent only** (full 3-way schain `sid` match, ID absorption, GPID, paid-traffic dependence, SIVT/fraud, attention, carbon, real-user field CWV/INP) — out of scope for an in-house scanner.

**Current composite weights** (in `app/scoring.py`, renormalized over present sub-scores): supply_chain 0.20, ad_experience 0.22, mfa 0.18, performance 0.12, video 0.10, brand_suitability 0.10, privacy 0.08.
