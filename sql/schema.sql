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
