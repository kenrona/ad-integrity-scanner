-- Ad Integrity Scanner — Phase 0 schema.
-- Idempotent: safe to run on every startup.

-- ---------------------------------------------------------------------------
-- scan_queue: Postgres-backed work queue (claimed via FOR UPDATE SKIP LOCKED).
-- Replaces Redis for the single-machine deploy; same semantics, zero extra infra.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scan_queue (
    id           BIGSERIAL PRIMARY KEY,
    scan_id      UUID        NOT NULL,
    url_hash     TEXT        NOT NULL,
    url          TEXT        NOT NULL,
    domain       TEXT        NOT NULL,
    tier         TEXT        NOT NULL DEFAULT 'static',   -- 'static' | 'render'
    status       TEXT        NOT NULL DEFAULT 'queued',   -- queued|processing|done|error
    attempts     INT         NOT NULL DEFAULT 0,
    enqueued_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    claimed_at   TIMESTAMPTZ,
    last_error   TEXT
);

-- Partial index keeps the "claimable" scan fast as the table grows.
CREATE INDEX IF NOT EXISTS idx_scan_queue_claimable
    ON scan_queue (tier, enqueued_at)
    WHERE status = 'queued';

CREATE INDEX IF NOT EXISTS idx_scan_queue_scan_id ON scan_queue (scan_id);

-- Reaper: find stuck 'processing' jobs by claim age.
CREATE INDEX IF NOT EXISTS idx_scan_queue_processing
    ON scan_queue (claimed_at)
    WHERE status = 'processing';

-- Pruning: find old terminal rows by enqueue time.
CREATE INDEX IF NOT EXISTS idx_scan_queue_terminal
    ON scan_queue (enqueued_at)
    WHERE status IN ('done', 'error');

-- ---------------------------------------------------------------------------
-- scan_ledger: dedup + tiered-TTL freshness for page-level results.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scan_ledger (
    url_hash      TEXT PRIMARY KEY,
    url           TEXT        NOT NULL,
    domain        TEXT        NOT NULL,
    last_scan_id  UUID,
    last_scanned  TIMESTAMPTZ,
    expires_at    TIMESTAMPTZ,                            -- page TTL boundary
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_scan_ledger_domain ON scan_ledger (domain);

-- Re-scan scheduler: find page results whose TTL has expired.
CREATE INDEX IF NOT EXISTS idx_scan_ledger_expires ON scan_ledger (expires_at);

-- ---------------------------------------------------------------------------
-- domain_signals: per-domain cached signals (ads.txt / sellers.json / ...).
-- Populated by the static tier in Phase 1; reserved here.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS domain_signals (
    domain      TEXT PRIMARY KEY,
    signals     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    fetched_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ                               -- domain TTL boundary
);

-- ---------------------------------------------------------------------------
-- sellers_json_cache: GLOBAL per-ad-system cache of parsed sellers.json (7d TTL).
-- Keyed by ad-system domain, shared across all publishers (not per-domain).
-- The id->seller map is stored only when small enough; huge files keep aggregates.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sellers_json_cache (
    ad_system          TEXT PRIMARY KEY,
    present            BOOLEAN     NOT NULL DEFAULT false,
    too_large          BOOLEAN     NOT NULL DEFAULT false,
    seller_count       INT,
    type_counts        JSONB,
    confidential_count INT,
    passthrough_count  INT,
    sellers            JSONB,                          -- id->{t,d,c} map, or null if large
    fetched_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at         TIMESTAMPTZ
);

-- ---------------------------------------------------------------------------
-- scan_results: one row per completed scan (the deliverable {url -> metrics}).
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS scan_results (
    scan_id          UUID PRIMARY KEY,
    url_hash         TEXT        NOT NULL,
    url              TEXT        NOT NULL,
    domain           TEXT        NOT NULL,
    scan_tier        TEXT        NOT NULL,                -- 'static' | 'render'
    confidence       REAL,
    signals          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    metrics          JSONB       NOT NULL DEFAULT '{}'::jsonb,
    sub_scores       JSONB       NOT NULL DEFAULT '{}'::jsonb,
    score_breakdown  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    integrity_score  REAL,
    scanner_version  TEXT        NOT NULL,
    scoring_version  TEXT        NOT NULL,
    scanned_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Idempotent migration for DBs created before score_breakdown existed.
ALTER TABLE scan_results ADD COLUMN IF NOT EXISTS score_breakdown JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_scan_results_url_hash ON scan_results (url_hash);
CREATE INDEX IF NOT EXISTS idx_scan_results_domain   ON scan_results (domain);
CREATE INDEX IF NOT EXISTS idx_scan_results_scanned  ON scan_results (scanned_at);

-- ===========================================================================
-- DATASETS layer: one-table + label + views model for baseline/publisher data.
-- ===========================================================================

-- datasets: registry of named collections of URLs.
CREATE TABLE IF NOT EXISTS datasets (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT        NOT NULL UNIQUE,           -- human label, e.g. 'baseline' or 'acme_news'
    kind        TEXT        NOT NULL,                  -- 'baseline' | 'publisher'
    source_file TEXT,                                  -- original filename (provenance)
    notes       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Exactly one baseline is the "active" baseline for benchmarking. We do not hard-
-- enforce single baseline (multiple allowed), but benchmark resolves by name.
CREATE INDEX IF NOT EXISTS idx_datasets_kind ON datasets (kind);

-- dataset_rows: one row per URL per dataset. url_hash MUST equal normalize_url().url_hash.
CREATE TABLE IF NOT EXISTS dataset_rows (
    id          BIGSERIAL PRIMARY KEY,
    dataset_id  BIGINT      NOT NULL REFERENCES datasets (id) ON DELETE CASCADE,
    url         TEXT        NOT NULL,                  -- canonical url (NormalizedURL.url)
    url_hash    TEXT        NOT NULL,                  -- sha256 hex from normalize_url(); JOIN key
    domain      TEXT        NOT NULL,
    extra       JSONB       NOT NULL DEFAULT '{}'::jsonb,  -- non-url columns from csv/xlsx
    last_scan_id UUID,                                 -- optional: scan kicked off for this row
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (dataset_id, url_hash)                      -- dedup within a dataset
);

CREATE INDEX IF NOT EXISTS idx_dataset_rows_dataset  ON dataset_rows (dataset_id);
CREATE INDEX IF NOT EXISTS idx_dataset_rows_url_hash ON dataset_rows (url_hash);

-- Publisher-supplied ad-monetization metrics (loaded WITH the URL, distinct from
-- scanner-generated integrity metrics which join in via scan_results.url_hash).
-- Both baseline and publisher sources carry the same column set. Prefixed mon_*
-- so they never collide with scanner metric keys (e.g. request_count) or the
-- canonical `domain` identity column. All nullable: blank cells ingest as NULL.
-- source_domain keeps the file's raw (often unreliable: app bundle ids / numeric
-- placement ids / blanks) domain value; the canonical `domain` is always derived
-- from the URL via normalize_url(). Revenue/CPM are USD.
ALTER TABLE dataset_rows ADD COLUMN IF NOT EXISTS source_domain     TEXT;
ALTER TABLE dataset_rows ADD COLUMN IF NOT EXISTS mon_prebid_count  BIGINT;
ALTER TABLE dataset_rows ADD COLUMN IF NOT EXISTS mon_bid_count     BIGINT;
ALTER TABLE dataset_rows ADD COLUMN IF NOT EXISTS mon_request_count BIGINT;
ALTER TABLE dataset_rows ADD COLUMN IF NOT EXISTS mon_bid_rate      DOUBLE PRECISION;
ALTER TABLE dataset_rows ADD COLUMN IF NOT EXISTS mon_impressions   BIGINT;
ALTER TABLE dataset_rows ADD COLUMN IF NOT EXISTS mon_revenue       DOUBLE PRECISION;
ALTER TABLE dataset_rows ADD COLUMN IF NOT EXISTS mon_cpm           DOUBLE PRECISION;
ALTER TABLE dataset_rows ADD COLUMN IF NOT EXISTS mon_cpm_variance  DOUBLE PRECISION;

-- job_runs: generic progress tracker polled by the UI for any >10s server op.
CREATE TABLE IF NOT EXISTS job_runs (
    id          BIGSERIAL PRIMARY KEY,
    kind        TEXT        NOT NULL,                  -- 'ingest' | 'scan_batch' | 'refresh' | 'profile'
    status      TEXT        NOT NULL DEFAULT 'running',-- 'running' | 'done' | 'error'
    total       INT         NOT NULL DEFAULT 0,
    done        INT         NOT NULL DEFAULT 0,
    message     TEXT,
    dataset_id  BIGINT,                                -- optional association
    error       TEXT,
    started_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS idx_job_runs_kind ON job_runs (kind, started_at);

-- ---------------------------------------------------------------------------
-- dataset_metrics: dataset row + its LATEST scan_results (by url_hash).
-- "Latest" = highest scanned_at for that url_hash. LEFT JOIN so unscanned rows
-- still appear (metrics NULL). This is capability B's "one row" view.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW dataset_metrics AS
SELECT
    dr.dataset_id,
    d.name        AS dataset_name,
    d.kind        AS dataset_kind,
    dr.id         AS dataset_row_id,
    dr.url,
    dr.url_hash,
    dr.domain,
    dr.extra,
    sr.scan_id,
    sr.scan_tier,
    sr.integrity_score,
    sr.confidence,
    sr.sub_scores,
    sr.metrics,
    sr.scanned_at,
    -- Publisher-supplied monetization columns (appended at END so CREATE OR
    -- REPLACE VIEW stays valid for the benchmark matview that depends on this view).
    dr.source_domain,
    dr.mon_prebid_count,
    dr.mon_bid_count,
    dr.mon_request_count,
    dr.mon_bid_rate,
    dr.mon_impressions,
    dr.mon_revenue,
    dr.mon_cpm,
    dr.mon_cpm_variance
FROM dataset_rows dr
JOIN datasets d ON d.id = dr.dataset_id
LEFT JOIN LATERAL (
    SELECT scan_id, scan_tier, integrity_score, confidence,
           sub_scores, metrics, scanned_at
    FROM scan_results s
    WHERE s.url_hash = dr.url_hash
    ORDER BY s.scanned_at DESC
    LIMIT 1
) sr ON true;

-- ---------------------------------------------------------------------------
-- baseline_metrics: convenience view = dataset_metrics restricted to baseline kind.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW baseline_metrics AS
SELECT * FROM dataset_metrics WHERE dataset_kind = 'baseline';

-- ---------------------------------------------------------------------------
-- Per-publisher resolution is done by filtering dataset_metrics on dataset_id
-- (no per-publisher DDL). No separate object needed; documented for implementers:
--   SELECT * FROM dataset_metrics WHERE dataset_id = $1;
-- ---------------------------------------------------------------------------

-- ---------------------------------------------------------------------------
-- benchmark_metric_stats: MATERIALIZED view of per-(dataset, metric) numeric
-- aggregates over scanned rows. Metric values are extracted from metrics JSONB
-- at the top level as DOUBLE. Refreshed by app/benchmark.py.
-- The metric key list is driven from app.analytics._METRIC_COLS but we cannot
-- enumerate Python here; instead this matview unrolls metrics via jsonb_each and
-- keeps only numeric-castable scalar values, plus integrity_score as a pseudo-metric.
-- ---------------------------------------------------------------------------
CREATE MATERIALIZED VIEW IF NOT EXISTS benchmark_metric_stats AS
WITH scanned AS (
    SELECT dataset_id, dataset_name, dataset_kind, integrity_score, metrics
    FROM dataset_metrics
    WHERE scan_id IS NOT NULL
),
unrolled AS (
    SELECT dataset_id, dataset_name, dataset_kind,
           kv.key AS metric,
           CASE WHEN jsonb_typeof(kv.value) = 'number'
                THEN (kv.value #>> '{}')::double precision
                WHEN jsonb_typeof(kv.value) = 'boolean'
                THEN CASE WHEN kv.value = 'true'::jsonb THEN 1.0 ELSE 0.0 END
                ELSE NULL END AS val
    FROM scanned, jsonb_each(scanned.metrics) AS kv
    UNION ALL
    SELECT dataset_id, dataset_name, dataset_kind,
           'integrity_score' AS metric,
           integrity_score::double precision AS val
    FROM scanned
)
SELECT
    dataset_id, dataset_name, dataset_kind, metric,
    count(val)                                              AS n,
    avg(val)                                                AS mean,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY val)        AS median,
    percentile_cont(0.25) WITHIN GROUP (ORDER BY val)       AS p25,
    percentile_cont(0.75) WITHIN GROUP (ORDER BY val)       AS p75,
    min(val)                                                AS min,
    max(val)                                                AS max
FROM unrolled
WHERE val IS NOT NULL
GROUP BY dataset_id, dataset_name, dataset_kind, metric;

-- Unique index required for REFRESH MATERIALIZED VIEW CONCURRENTLY.
CREATE UNIQUE INDEX IF NOT EXISTS uq_benchmark_metric_stats
    ON benchmark_metric_stats (dataset_id, metric);

-- ===========================================================================
-- Adaptive render control: terminal timestamp (for recent error-rate windows)
-- + a single-row control record the fleet reads (dynamic timeout + halt flag).
-- ===========================================================================
ALTER TABLE scan_queue ADD COLUMN IF NOT EXISTS terminated_at TIMESTAMPTZ;

-- Recent error-rate window query: terminal render rows by termination time.
CREATE INDEX IF NOT EXISTS idx_scan_queue_terminated
    ON scan_queue (tier, terminated_at)
    WHERE status IN ('done', 'error');

-- render_control: one row (id=1). The maintenance controller adjusts it; render
-- workers read it each poll. timeout_seconds escalates on high error rate up to a
-- cap; halted=true tells workers to stop claiming (auto-stop for diagnosis).
CREATE TABLE IF NOT EXISTS render_control (
    id              INT PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    timeout_seconds INT         NOT NULL,
    halted          BOOLEAN     NOT NULL DEFAULT false,
    reason          TEXT,
    error_rate      REAL,
    window_terminal INT,
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- last_error_at: stamped on EVERY failed attempt (requeue or park), so the
-- controller's error rate reflects ongoing timeout churn immediately, not just
-- jobs that exhausted all retries (which lag by max_attempts).
ALTER TABLE scan_queue ADD COLUMN IF NOT EXISTS last_error_at TIMESTAMPTZ;
CREATE INDEX IF NOT EXISTS idx_scan_queue_last_error
    ON scan_queue (tier, last_error_at)
    WHERE last_error_at IS NOT NULL;
