# Ad Integrity Scanner

Local service that accepts a URL, scans the ad environment (display + OLV/video),
computes integrity metrics, and stores `{url → metrics + scores}` in Postgres.

See [`PLAN.md`](./PLAN.md) for the full design and roadmap, and
[`METRICS.md`](./METRICS.md) for the **metrics dictionary** — every metric with
its definition, raw vs derived type, and (for derived scores) the raw inputs +
formula behind it. Each record also carries a `score_breakdown` pairing every
sub-score with the raw data it was computed from (`GET /scan/{id}`).

## Status: Phase 4 complete (hardening + analytics + GUI)

Implemented:
- `POST /scan` — async fire-and-forget intake (returns `202` + `scan_id`).
- URL normalization + dedup via a tiered-TTL **scan ledger**.
- **Postgres-backed work queue** (`FOR UPDATE SKIP LOCKED`) — no Redis/Docker needed. Failed jobs requeue up to `AI_MAX_ATTEMPTS`, then park as `error`.
- **Static tier:** fetches `ads.txt` / `app-ads.txt` / `robots.txt` / `sellers.json` (per-domain, 24h cache) + page HTML; parses supply-chain transparency, ad-tech footprint, content, video hints.
- **Render tier (Playwright/Chromium):** persistent browser pool renders a sampled subset (`AI_RENDER_SAMPLE_RATE`); measures GPT ad slots / A2CR / above-fold / refresh, Prebid bidders, CMP (TCF/GPP/USP/GPC), video/OLV (autoplay), and Core Web Vitals (LCP/CLS) + page weight. Merges with static signals and re-scores the same `scan_id`.
- **MFA / ad-load risk** (on-page ad load: A2CR + slots + above-fold + refresh, plus thin-content/link-density), **content category** + **brand-suitability tier** (in-house keyword classifier, flagged heuristic).
- **Sub-scores** — supply_chain, ad_experience, mfa, performance, video (when present), brand_suitability, privacy — plus a weight-normalized composite `integrity_score` and confidence (0.4 static-only → 0.85 render-backed).
- `GET /scan/{scan_id}` — status / result lookup. `GET /stats` — queue depth + result counts.
- Structured `key=value` logging (`ai.*` loggers).

- **Maintenance worker** — reaps stuck `processing` jobs (crash recovery), prunes old terminal queue rows (bounds growth), and optionally re-enqueues TTL-expired pages (`AI_RESCAN_ENABLED`).
- **Analytics export** — flatten `scan_results` → Parquet, queryable with DuckDB (batch/analytics read path).
- **Browser GUI** at `/` — pick a file of URLs and submit them one at a time, with live progress + backend queue stats.

> **Note on content category / brand suitability:** these come from a dependency-free
> keyword classifier, not ML or a vendor. Word-matching can't distinguish "an article
> *about* drugs" from drug-marketplace content, so thresholds are biased against
> flagging and outputs are marked `heuristic`. Treat them as advisory hints.

## Why Postgres for the queue (not Redis)

This machine has Postgres 17 running already and no Docker/Redis. A Postgres job
table with `SKIP LOCKED` gives the same claim-once semantics with zero extra
infrastructure and easily handles 1M/day (~12/sec). Swap to Redis/arq later
behind `app/queue.py` if you scale out to multiple machines.

## Setup

Requires the running Homebrew `postgresql@17` and Python 3.12.

```bash
cd "ad integrity"

# 1. Create the database (one time)
createdb ad_integrity

# 2. Virtualenv + deps
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m playwright install chromium   # render tier browser (~130 MB)
# The content classifier downloads a ~30 MB embedding model (model2vec) on first
# use; if offline it falls back to the keyword classifier automatically.

# 3. Config
cp .env.example .env        # edit AI_DATABASE_URL if your PG user/db differ
```

## Run

```bash
source .venv/bin/activate

# API (applies schema on startup)
uvicorn app.main:app --reload --port 8000

# Static worker (separate terminal) — scans + enqueues render jobs
python -m app.workers.static_worker

# Render worker (separate terminal) — Playwright; needs `playwright install chromium`
python -m app.workers.render_worker

# Maintenance worker (optional, separate terminal) — reaper + queue pruning + re-scan
python -m app.workers.maintenance
```

Then open the **GUI** at <http://localhost:8000/> to pick a URL file (e.g.
`sample_urls.txt`) and submit it one URL at a time.

### Analytics

```bash
# Export scan_results to Parquet, then summarize with DuckDB
python -m app.analytics export scan_results.parquet
python -m app.analytics summary scan_results.parquet
```

## Try it

```bash
# Submit a URL -> 202 + scan_id
curl -s -X POST localhost:8000/scan \
  -H 'content-type: application/json' \
  -d '{"url":"https://www.example.com/some/article?utm_source=x"}' | jq

# Check status / result
curl -s localhost:8000/scan/<scan_id> | jq

curl -s localhost:8000/healthz | jq

# Queue depth + result counts
curl -s localhost:8000/stats | jq
```

## Tests

```bash
source .venv/bin/activate

# Unit tests (no DB needed)
pytest tests/test_normalize.py -q

# Integration tests (writes to a throwaway DB — do NOT point at prod data)
createdb ad_integrity_test
AI_DATABASE_URL="postgresql://$USER@localhost:5432/ad_integrity_test" \
  pytest tests/test_ledger.py -q
```

## Layout

```
app/
  config.py            settings (env prefix AI_)
  normalize.py         URL canonicalization + hashing + domain extraction
  db.py                asyncpg pool + schema bootstrap
  queue.py             Postgres work queue (enqueue / claim / done / error)
  ledger.py            dedup + tiered-TTL freshness
  service.py           submit / status orchestration
  models.py            API schemas
  main.py              FastAPI app (+ GUI route at /)
  analytics.py         scan_results -> Parquet export + DuckDB summary (CLI)
  ssrf.py              SSRF guard (private/metadata IP blocking)
  static/index.html    browser GUI (file picker + sequential submit)
  fetch.py             async httpx fetcher (static tier)
  signals_static.py    static signal collection (domain files + page HTML)
  scoring.py           sub-scores + weighted composite + confidence
  results.py           shared scan_results persistence
  domain_cache.py      per-domain signal cache (24h TTL)
  supply_resolve.py    sellers.json cross-resolution (global per-ad-system cache)
  content.py           keyword content/brand-suitability classifier (fallback)
  content_ml.py        zero-shot embedding classifier (model2vec)
  datadict.py          generates DATA_DICTIONARY.md
  parsers/             ads.txt + sellers.json + HTML parsers
  render/              Playwright pool (browser.py), collector (collect.py),
                       in-page JS (instrument.py)
  workers/
    static_worker.py   static tier; enqueues render jobs per sampling gate
    render_worker.py   render tier; merges + re-scores the same scan_id
    maintenance.py     reaper + queue pruning + TTL re-scan loop
sql/schema.sql         tables: scan_queue, scan_ledger, domain_signals, scan_results
tests/                 unit (normalize/parser/scoring/content/ssrf/analytics)
                       + integration (ledger/queue/maintenance)
```
