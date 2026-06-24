"""Application settings, loaded from environment / .env (prefix AI_)."""
from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AI_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    database_url: str = "postgresql://localhost:5432/ad_integrity"
    db_pool_min: int = 2
    db_pool_max: int = 40  # >= static + render concurrency + headroom (tuned for M4/32GB)

    page_ttl_seconds: int = 7 * 24 * 3600
    domain_ttl_seconds: int = 24 * 3600

    # sellers.json cross-resolution (global per-ad-system cache).
    sellers_json_ttl_seconds: int = 7 * 24 * 3600     # they change slowly
    sellers_json_neg_ttl_seconds: int = 24 * 3600     # cache misses/failures
    sellers_json_max_bytes: int = 15_000_000          # streaming cap (Google's is bigger)
    sellers_json_map_max: int = 100_000               # store the id->seller map only if smaller
    supply_resolve_max_systems: int = 30              # ad systems resolved per publisher scan

    strip_tracking_params: bool = True

    # Baseline datasets are meant to represent content pages, so home pages
    # (root-path URLs like https://example.com/) are dropped at ingest — they are
    # aggregate/landing pages that skew page-level benchmarks. Applies to
    # kind='baseline' only; publisher datasets keep every row.
    baseline_drop_home_pages: bool = True

    # SSRF allowlist (comma-separated hosts) — permits otherwise-blocked hosts
    # such as 127.0.0.1 for the local accuracy test-suite. Empty in production.
    ssrf_allow_hosts: str = ""

    # User-agent for fetch + render. A realistic Chrome UA is used so bot-protected
    # inventory (Cloudflare etc.) serves the real page — standard for "synthetic
    # user" ad-environment measurement. Set a self-identifying UA to be transparent
    # at the cost of being blocked by much premium inventory.
    user_agent: str = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36")

    # Content category / brand-suitability classifier:
    # "auto" = embedding model if available else keyword; "ml"; "keyword".
    content_classifier: str = "auto"

    static_worker_batch: int = 32
    static_worker_concurrency: int = 24  # jobs processed in parallel (I/O-bound; tuned for M4)
    static_worker_poll_ms: int = 250
    max_attempts: int = 3          # claims per job before it is parked as 'error'

    # Render tier (Playwright). sample_rate is the fraction of static scans that
    # also get a render. Default 1.0 for local dev; production would use ~0.05-0.1.
    render_enabled: bool = True
    render_sample_rate: float = 1.0
    render_concurrency: int = 6   # TOTAL parallel browser contexts across all browsers
    # Number of separate Chromium processes the render pool launches. Contexts are
    # spread across them (least-loaded), so each browser serves ~concurrency/browsers
    # contexts. One browser bottlenecks on a single CDP connection beyond ~4 contexts
    # (request interception funnels through it); more browser processes give real
    # parallelism. Scale this up with render_concurrency on bigger/cloud hardware.
    render_browsers: int = 1
    render_dwell_ms: int = 8000   # longer dwell improves refresh + viewability capture
    render_samples: int = 1       # >1 = render N times, take median CLS (N× cost)
    # page.goto cap: slow-first-byte sites fail fast instead of holding a slot.
    render_nav_timeout_ms: int = 15000
    # Hard cap on a single render job (goto+dwell+collect). A render exceeding this
    # is cancelled — the browser context is closed and the concurrency slot freed —
    # so one wedged page can't starve the pool. Caught as a job failure (requeued).
    render_timeout_seconds: int = 30
    # Resource types aborted during render. Default keeps images (accurate page
    # weight); add 'image' to cut bandwidth at the cost of weight accuracy.
    render_block_resources: str = "font,media"
    render_worker_batch: int = 8  # >= render_concurrency so a poll can fill the pool

    # Maintenance worker (Phase 4 hardening).
    visibility_timeout_seconds: int = 300    # processing jobs older than this are reaped
    queue_retention_seconds: int = 86400     # done/error queue rows pruned after this
    maintenance_interval_seconds: int = 60
    rescan_enabled: bool = False             # re-enqueue URLs whose page TTL expired
    rescan_batch: int = 100

    # Datasets / ingestion / scan-batch / benchmark.
    ingest_batch_size: int = 500            # dataset_rows inserted per executemany batch
    scan_batch_throttle_ms: int = 50        # default delay between submit_scan calls in a dataset scan
    scan_batch_concurrency: int = 1         # parallel submit_scan calls per dataset scan (1 = sequential)
    benchmark_refresh_concurrently: bool = True  # use REFRESH MATERIALIZED VIEW CONCURRENTLY when populated
    dataset_rows_page_size: int = 200       # default page size for GET /datasets/{id}/rows

    log_level: str = "INFO"

    scanner_version: str = "0.1.0"
    scoring_version: str = "0.1.0"


@lru_cache
def get_settings() -> Settings:
    return Settings()
