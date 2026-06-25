"""Ingest source files into datasets+dataset_rows, list/resolve datasets, and
kick off scans for a dataset via service.submit_scan (with job_runs progress).

url_hash is ALWAYS computed via app.normalize.normalize_url so dataset_rows JOIN
scan_results on the same canonical hash. Long loops report progress through
app.progress against a job_runs row the UI polls.
"""
from __future__ import annotations

import asyncio
import csv
import io
from dataclasses import dataclass

import asyncpg

from app import progress, service
from app.config import get_settings
from app.db import get_pool
from app.logging_config import get_logger, kv
from app.normalize import normalize_url

log = get_logger("datasets")

# Header names (case-insensitive) accepted as the url column, in priority order.
_URL_COLUMN_CANDIDATES = ("url", "page_url", "link", "address")

# Publisher-supplied ad-monetization columns -> typed dataset_rows columns.
# Header match is case-insensitive. Anything not listed here stays in `extra` JSONB.
# Counts are integer-valued; rates/money are floats. The file's `domain` column is
# captured as source_domain (raw, unreliable) — the canonical domain is derived
# from the URL via normalize_url().
_MON_INT_COLUMNS = {
    "prebid_count": "mon_prebid_count",
    "bid_count": "mon_bid_count",
    "request_count": "mon_request_count",
    "impressions": "mon_impressions",
}
_MON_FLOAT_COLUMNS = {
    "bid_rate": "mon_bid_rate",
    "revenue": "mon_revenue",
    "cpm": "mon_cpm",
    "cpm_variance": "mon_cpm_variance",
}
# Typed monetization columns in a STABLE order (matches the ingest INSERT below).
_MON_COLUMNS = (
    "mon_prebid_count", "mon_bid_count", "mon_request_count", "mon_impressions",
    "mon_bid_rate", "mon_revenue", "mon_cpm", "mon_cpm_variance",
)


def _to_number(value, *, integer: bool):
    """Coerce a csv/xlsx cell to int|float|None. Blank/garbage -> None.

    Tolerates already-numeric cells (xlsx), thousands separators, and stray
    whitespace; an integer column with a fractional value is rounded.
    """
    if value is None:
        return None
    if isinstance(value, bool):  # guard: bool is an int subclass
        return None
    if isinstance(value, (int, float)):
        return int(round(value)) if integer else float(value)
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    try:
        f = float(s)
    except ValueError:
        return None
    return int(round(f)) if integer else f


def _is_home_page(url: str) -> bool:
    """True if a canonical URL is a site home/landing page: root path, no query.

    normalize_url collapses an empty path to '/' and drops fragments + tracking
    params, so 'https://example.com/' and 'https://example.com/?utm_source=x' both
    count as home pages, while 'https://example.com/article' does not.
    """
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    return parts.path in ("", "/") and not parts.query


def _source_domain_matches_page(source_domain: str | None, page_domain: str) -> bool:
    """True if source_domain resolves to the same registrable domain as the page.

    Used to spot publisher 'rollup' rows: an aggregate keyed at a home-page URL but
    tagged with a non-web source domain (an app bundle id like
    'br.com.golmobile.nypost', a numeric placement id, or blank) instead of the
    page's own domain. A legitimate home page would carry its own domain and so
    is NOT treated as a rollup.
    """
    if not source_domain:
        return False
    try:
        return normalize_url(source_domain).domain == page_domain
    except ValueError:
        return False


def _split_monetization(extra: dict) -> tuple[dict, str | None, dict]:
    """Split a raw column dict into (typed monetization values, source_domain, leftover extra).

    Keys are matched case-insensitively. Recognized monetization headers are
    coerced to int/float and keyed by their mon_* column name; a `domain` column
    becomes source_domain; everything else is kept (as-is) in the leftover extra.
    """
    typed: dict = {}
    source_domain: str | None = None
    leftover: dict = {}
    for key, value in extra.items():
        low = (key or "").strip().lower()
        if low in _MON_INT_COLUMNS:
            typed[_MON_INT_COLUMNS[low]] = _to_number(value, integer=True)
        elif low in _MON_FLOAT_COLUMNS:
            typed[_MON_FLOAT_COLUMNS[low]] = _to_number(value, integer=False)
        elif low == "domain":
            sd = "" if value is None else str(value).strip()
            source_domain = sd or None
        else:
            leftover[key] = value
    return typed, source_domain, leftover


@dataclass(frozen=True)
class ParsedSource:
    urls: list[str]                 # raw url strings, in file order
    extras: list[dict]              # parallel list; {} for .txt, column map for csv/xlsx


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------
def _resolve_url_column(headers: list[str], url_column: str | None) -> tuple[str, list[str]]:
    """Return (url_header, other_headers). Raise ValueError if unresolvable."""
    if not headers:
        raise ValueError("source has no header row")
    if url_column:
        match = next((h for h in headers if h is not None and h.lower() == url_column.lower()), None)
        if match is None:
            raise ValueError(f"url_column {url_column!r} not found in headers {headers!r}")
        url_header = match
    else:
        lowered = {h.lower(): h for h in headers if h is not None}
        url_header = next((lowered[c] for c in _URL_COLUMN_CANDIDATES if c in lowered), None)
        if url_header is None:
            url_header = headers[0]
    others = [h for h in headers if h != url_header]
    return url_header, others


def parse_txt(text: str) -> ParsedSource:
    """One URL per line; strip; ignore blank lines and lines starting with '#'.

    extras are all {}.
    """
    urls: list[str] = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return ParsedSource(urls=urls, extras=[{} for _ in urls])


def parse_csv(data: bytes, *, url_column: str | None = None) -> ParsedSource:
    """Parse CSV via stdlib csv.DictReader.

    url_column defaults to the first header matching (case-insensitive) one of
    'url','page_url','link','address'; else the first column. Remaining columns
    -> extra dict (string values). Raises ValueError if no url column resolvable.
    """
    text = data.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    headers = reader.fieldnames or []
    url_header, others = _resolve_url_column(list(headers), url_column)

    urls: list[str] = []
    extras: list[dict] = []
    for row in reader:
        raw = (row.get(url_header) or "").strip()
        if not raw:
            continue
        urls.append(raw)
        extras.append({h: row.get(h) for h in others})
    return ParsedSource(urls=urls, extras=extras)


def parse_xlsx(data: bytes, *, url_column: str | None = None) -> ParsedSource:
    """Parse the first worksheet of an .xlsx via openpyxl (read_only, data_only).

    First row = headers. url_column resolution identical to parse_csv. Remaining
    columns -> extra dict. Raises ValueError if no url column.
    """
    import openpyxl  # local import: optional dependency, only needed for xlsx

    wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        rows_iter = ws.iter_rows(values_only=True)
        try:
            header_row = next(rows_iter)
        except StopIteration:
            raise ValueError("xlsx worksheet is empty")
        headers = [("" if h is None else str(h)) for h in header_row]
        url_header, _others = _resolve_url_column(headers, url_column)
        url_idx = headers.index(url_header)
        # Map non-url columns BY POSITION (enumerate), not by headers.index(name):
        # the latter collapses duplicate header names to the first occurrence, so a
        # second same-named column would be read from the wrong cell for every row.
        other_positions = [(i, h) for i, h in enumerate(headers) if i != url_idx]

        urls: list[str] = []
        extras: list[dict] = []
        for row in rows_iter:
            cell = row[url_idx] if url_idx < len(row) else None
            raw = ("" if cell is None else str(cell)).strip()
            if not raw:
                continue
            urls.append(raw)
            extra: dict = {}
            for idx, h in other_positions:
                v = row[idx] if idx < len(row) else None
                extra[h] = None if v is None else (v if isinstance(v, (int, float, bool)) else str(v))
            extras.append(extra)
        return ParsedSource(urls=urls, extras=extras)
    finally:
        wb.close()


def parse_source(filename: str, data: bytes, *, url_column: str | None = None) -> ParsedSource:
    """Dispatch on extension. Raises ValueError on unknown extension."""
    lower = filename.lower()
    if lower.endswith(".txt"):
        return parse_txt(data.decode("utf-8-sig"))
    if lower.endswith(".csv"):
        return parse_csv(data, url_column=url_column)
    if lower.endswith(".xlsx"):
        return parse_xlsx(data, url_column=url_column)
    raise ValueError(f"unsupported source extension: {filename!r}")


# ---------------------------------------------------------------------------
# Dataset registry
# ---------------------------------------------------------------------------
async def create_dataset(conn: asyncpg.Connection, *, name: str, kind: str,
                         source_file: str | None = None, notes: str | None = None) -> int:
    """Insert a datasets row; return id.

    kind must be 'baseline' or 'publisher'. Raises asyncpg.UniqueViolationError
    on duplicate name.
    """
    if kind not in ("baseline", "publisher"):
        raise ValueError(f"invalid dataset kind: {kind!r}")
    return await conn.fetchval(
        """
        INSERT INTO datasets (name, kind, source_file, notes)
        VALUES ($1, $2, $3, $4)
        RETURNING id
        """,
        name, kind, source_file, notes,
    )


async def list_datasets(conn: asyncpg.Connection) -> list[dict]:
    """All datasets with row_count and scanned_count, ordered by created_at desc."""
    rows = await conn.fetch(
        """
        SELECT d.id, d.name, d.kind, d.source_file, d.notes, d.created_at,
               COALESCE(rc.row_count, 0)     AS row_count,
               COALESCE(rc.scanned_count, 0) AS scanned_count
        FROM datasets d
        LEFT JOIN (
            SELECT dr.dataset_id,
                   count(*)                                          AS row_count,
                   count(*) FILTER (WHERE sr.url_hash IS NOT NULL)   AS scanned_count
            FROM dataset_rows dr
            LEFT JOIN LATERAL (
                SELECT 1 AS url_hash FROM scan_results s
                WHERE s.url_hash = dr.url_hash LIMIT 1
            ) sr ON true
            GROUP BY dr.dataset_id
        ) rc ON rc.dataset_id = d.id
        ORDER BY d.created_at DESC
        """
    )
    return [dict(r) for r in rows]


async def get_dataset_rows(conn: asyncpg.Connection, dataset_id: int, *,
                           limit: int = 200, offset: int = 0) -> list[dict]:
    """Paginated rows of dataset_metrics for the UI rows table.

    The typed monetization columns + source_domain are folded back into the row's
    `extra` dict for display, so the response shape (and the UI) is unchanged.
    """
    import json

    rows = await conn.fetch(
        """
        SELECT url, domain, integrity_score, scan_tier, scanned_at, extra,
               source_domain, mon_prebid_count, mon_bid_count, mon_request_count,
               mon_impressions, mon_bid_rate, mon_revenue, mon_cpm, mon_cpm_variance
        FROM dataset_metrics
        WHERE dataset_id = $1
        ORDER BY dataset_row_id
        LIMIT $2 OFFSET $3
        """,
        dataset_id, limit, offset,
    )
    out: list[dict] = []
    for r in rows:
        d = dict(r)
        extra = d.pop("extra", None)
        extra = json.loads(extra) if isinstance(extra, str) else (extra or {})
        # Merge typed monetization values + source_domain into extra for display.
        for col in ("source_domain", *_MON_COLUMNS):
            val = d.pop(col, None)
            if val is not None:
                extra[col] = val
        d["extra"] = extra
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# Rollup pruning
# ---------------------------------------------------------------------------
async def delete_rollups(conn: asyncpg.Connection, *,
                         dataset_id: int | None = None) -> dict:
    """Delete aggregate 'rollup' rows from publisher datasets.

    A rollup is a home-page (root-path) row carrying a PRESENT source_domain that
    is NOT the page's own web domain — e.g. nypost.com/ tagged
    'br.com.golmobile.nypost', which aggregates app inventory and is orders of
    magnitude larger than the page-level rows around it. The present-but-mismatched
    source_domain is the positive signal; a home page with no source_domain is left
    alone (we can't confirm it's an aggregate). Baseline datasets are never touched
    (their home pages are already dropped at ingest). Pass dataset_id to scope to
    one dataset; None scans every publisher dataset.

    Returns {'deleted': int, 'rows': [{'dataset','url','source_domain'}...]}.
    """
    candidates = await conn.fetch(
        """
        SELECT dr.id, dr.url, dr.domain, dr.source_domain, d.name AS dataset
        FROM dataset_rows dr
        JOIN datasets d ON d.id = dr.dataset_id
        WHERE d.kind = 'publisher'
          AND ($1::bigint IS NULL OR dr.dataset_id = $1)
          AND dr.url ~ '^https?://[^/]+/?$'
          AND dr.source_domain IS NOT NULL AND dr.source_domain <> ''
        """,
        dataset_id,
    )
    victims = [r for r in candidates
               if not _source_domain_matches_page(r["source_domain"], r["domain"])]
    if victims:
        await conn.execute(
            "DELETE FROM dataset_rows WHERE id = ANY($1::bigint[])",
            [r["id"] for r in victims],
        )
    return {
        "deleted": len(victims),
        "rows": [{"dataset": r["dataset"], "url": r["url"],
                  "source_domain": r["source_domain"]} for r in victims],
    }


# ---------------------------------------------------------------------------
# Ingestion
# ---------------------------------------------------------------------------
async def ingest(pool: asyncpg.Pool, *, dataset_id: int, parsed: ParsedSource,
                 job_id: int, batch_size: int) -> dict:
    """Normalize each url and bulk-insert dataset_rows in batches.

    Uses executemany INSERT ... ON CONFLICT (dataset_id, url_hash) DO NOTHING.
    Rows whose normalize_url raises ValueError are skipped (counted). For
    kind='baseline' datasets, home pages (root-path URLs) are dropped when
    settings.baseline_drop_home_pages is set (counted separately as 'home_pages').
    Updates job_runs via progress.advance per batch. Sets total at start; finishes
    (or fails) the job. Returns {'inserted', 'skipped', 'duplicates', 'home_pages'}.
    """
    import json

    settings = get_settings()
    strip = settings.strip_tracking_params

    inserted = 0
    skipped = 0
    duplicates = 0
    home_pages = 0
    try:
        async with pool.acquire() as conn:
            await progress.set_total(conn, job_id, len(parsed.urls))
            kind = await conn.fetchval("SELECT kind FROM datasets WHERE id = $1", dataset_id)
        drop_home = settings.baseline_drop_home_pages and kind == "baseline"

        # tuple layout: (dataset_id, url, url_hash, domain, extra_json, source_domain,
        #                *mon columns in _MON_COLUMNS order)
        batch: list[tuple] = []
        processed_in_batch = 0

        async def flush() -> None:
            nonlocal inserted, duplicates, batch, processed_in_batch
            if not batch:
                return
            # INSERT ... SELECT unnest(...) ... RETURNING counts exactly THIS batch's
            # inserts in one round-trip — no whole-table COUNT(*) (which was O(n^2)
            # over the growing dataset and wrong under concurrent same-dataset ingest).
            cols = list(zip(*batch))  # transpose 14-tuples into 14 column arrays
            async with pool.acquire() as conn:
                returned = await conn.fetch(
                    """
                    INSERT INTO dataset_rows (
                        dataset_id, url, url_hash, domain, extra, source_domain,
                        mon_prebid_count, mon_bid_count, mon_request_count,
                        mon_impressions, mon_bid_rate, mon_revenue, mon_cpm,
                        mon_cpm_variance
                    )
                    SELECT * FROM unnest(
                        $1::bigint[], $2::text[], $3::text[], $4::text[],
                        $5::jsonb[], $6::text[],
                        $7::bigint[], $8::bigint[], $9::bigint[], $10::bigint[],
                        $11::double precision[], $12::double precision[],
                        $13::double precision[], $14::double precision[]
                    )
                    ON CONFLICT (dataset_id, url_hash) DO NOTHING
                    RETURNING 1
                    """,
                    *cols,
                )
                ins = len(returned)
                inserted += ins
                duplicates += len(batch) - ins
                await progress.advance(
                    conn, job_id, done_delta=processed_in_batch,
                    message=f"ingested {inserted} rows ({skipped} skipped)",
                )
            batch = []
            processed_in_batch = 0

        for raw, extra in zip(parsed.urls, parsed.extras):
            processed_in_batch += 1
            try:
                norm = normalize_url(raw, strip_tracking=strip)
            except ValueError:
                skipped += 1
                if len(batch) == 0 and processed_in_batch >= batch_size:
                    # Only-skips batch: still advance the job so progress moves.
                    async with pool.acquire() as conn:
                        await progress.advance(conn, job_id, done_delta=processed_in_batch)
                    processed_in_batch = 0
                continue
            if drop_home and _is_home_page(norm.url):
                home_pages += 1
                if len(batch) == 0 and processed_in_batch >= batch_size:
                    async with pool.acquire() as conn:
                        await progress.advance(conn, job_id, done_delta=processed_in_batch)
                    processed_in_batch = 0
                continue
            typed, source_domain, leftover = _split_monetization(extra or {})
            batch.append((
                dataset_id, norm.url, norm.url_hash, norm.domain,
                json.dumps(leftover), source_domain,
                *(typed.get(col) for col in _MON_COLUMNS),
            ))
            if processed_in_batch >= batch_size:
                await flush()

        await flush()

        # Publisher datasets: drop any aggregate rollup row(s) once loaded.
        rollups_deleted = 0
        if kind == "publisher":
            async with pool.acquire() as conn:
                rollups_deleted = (await delete_rollups(conn, dataset_id=dataset_id))["deleted"]

        async with pool.acquire() as conn:
            await progress.finish(
                conn, job_id,
                message=(f"done: {inserted} inserted, {duplicates} duplicates, "
                         f"{skipped} skipped, {home_pages} home pages dropped, "
                         f"{rollups_deleted} rollups deleted"),
            )
    except Exception as e:  # noqa: BLE001 — record failure on the job row
        async with pool.acquire() as conn:
            await progress.fail(conn, job_id, repr(e))
        log.warning("ingest failed %s err=%r", kv(dataset_id=dataset_id, job_id=job_id), e)
        raise

    return {"inserted": inserted, "skipped": skipped, "duplicates": duplicates,
            "home_pages": home_pages, "rollups_deleted": rollups_deleted}


async def ingest_path(pool: asyncpg.Pool, *, dataset_id: int, path: str,
                      job_id: int, batch_size: int = 5000,
                      url_column: str | None = None) -> dict:
    """Stream a (possibly very large) local CSV into dataset_rows via COPY.

    Memory-bounded: reads the file row-by-row (never loads it whole), dedups by
    url_hash in-process, and bulk-loads with copy_records_to_table — orders of
    magnitude faster than per-row INSERT and suitable for multi-hundred-MB files
    the multipart-upload/parse_csv path cannot handle. Applies the same
    normalization, baseline home-page drop, and monetization typing as ingest(),
    then prunes publisher rollups. Updates job_runs progress as it goes.

    Assumes the dataset is empty (or has no overlapping url_hash) — COPY does not
    do ON CONFLICT, and intra-file duplicates are removed via the in-process set.
    Returns {'inserted','skipped','duplicates','home_pages','rollups_deleted'}.
    """
    import csv as _csv
    import json

    settings = get_settings()
    strip = settings.strip_tracking_params
    async with pool.acquire() as conn:
        kind = await conn.fetchval("SELECT kind FROM datasets WHERE id = $1", dataset_id)
    drop_home = settings.baseline_drop_home_pages and kind == "baseline"

    # total (for the progress bar) = data row count — a cheap line scan.
    total = 0
    with open(path, "r", encoding="utf-8-sig", newline="") as fh:
        for _ in fh:
            total += 1
    total = max(0, total - 1)
    async with pool.acquire() as conn:
        await progress.set_total(conn, job_id, total)

    columns = ["dataset_id", "url", "url_hash", "domain", "extra", "source_domain",
               *_MON_COLUMNS]
    inserted = skipped = duplicates = home_pages = processed = reported = 0
    seen: set[str] = set()
    records: list[tuple] = []

    async def flush_records() -> None:
        nonlocal inserted, records
        if not records:
            return
        async with pool.acquire() as conn:
            await conn.copy_records_to_table(
                "dataset_rows", records=records, columns=columns
            )
        inserted += len(records)
        records = []

    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as fh:
            reader = _csv.DictReader(fh)
            url_header, others = _resolve_url_column(
                list(reader.fieldnames or []), url_column
            )
            for row in reader:
                processed += 1
                raw = (row.get(url_header) or "").strip()
                if not raw:
                    skipped += 1
                else:
                    try:
                        norm = normalize_url(raw, strip_tracking=strip)
                    except ValueError:
                        norm = None
                    if norm is None:
                        skipped += 1
                    elif drop_home and _is_home_page(norm.url):
                        home_pages += 1
                    elif norm.url_hash in seen:
                        duplicates += 1
                    else:
                        seen.add(norm.url_hash)
                        typed, source_domain, leftover = _split_monetization(
                            {h: row.get(h) for h in others}
                        )
                        records.append((
                            dataset_id, norm.url, norm.url_hash, norm.domain,
                            json.dumps(leftover), source_domain,
                            *(typed.get(col) for col in _MON_COLUMNS),
                        ))
                        if len(records) >= batch_size:
                            await flush_records()
                if processed - reported >= 20000:
                    async with pool.acquire() as conn:
                        await progress.advance(
                            conn, job_id, done_delta=processed - reported,
                            message=f"ingested {inserted} rows",
                        )
                    reported = processed
            await flush_records()

        rollups_deleted = 0
        if kind == "publisher":
            async with pool.acquire() as conn:
                rollups_deleted = (await delete_rollups(conn, dataset_id=dataset_id))["deleted"]

        async with pool.acquire() as conn:
            if processed > reported:
                await progress.advance(conn, job_id, done_delta=processed - reported)
            await progress.finish(
                conn, job_id,
                message=(f"done: {inserted} inserted, {duplicates} duplicates, "
                         f"{skipped} skipped, {home_pages} home pages dropped, "
                         f"{rollups_deleted} rollups deleted"),
            )
    except Exception as e:  # noqa: BLE001 — record failure on the job row
        async with pool.acquire() as conn:
            await progress.fail(conn, job_id, repr(e))
        log.warning("ingest_path failed %s err=%r",
                    kv(dataset_id=dataset_id, job_id=job_id), e)
        raise

    return {"inserted": inserted, "skipped": skipped, "duplicates": duplicates,
            "home_pages": home_pages, "rollups_deleted": rollups_deleted}


# ---------------------------------------------------------------------------
# Kick off scans for a dataset
# ---------------------------------------------------------------------------
async def _set_last_scan_id(pool: asyncpg.Pool, dataset_id: int,
                            url_hash: str, scan_id) -> None:
    async with pool.acquire() as conn:
        await conn.execute(
            "UPDATE dataset_rows SET last_scan_id = $1 WHERE dataset_id = $2 AND url_hash = $3",
            scan_id, dataset_id, url_hash,
        )


async def scan_dataset(pool: asyncpg.Pool, *, dataset_id: int, job_id: int,
                       throttle_ms: int, sample_rate: float = 1.0) -> dict:
    """Kick off scans for the rows of dataset_id via service.submit_scan.

    submit_scan owns its own connection/transaction, so it is called with the
    pool (not a connection), one URL at a time (or fanned out with a semaphore
    when settings.scan_batch_concurrency > 1). dataset_rows.last_scan_id is
    updated with the returned scan_id. job_runs.done advances per URL. Per-URL
    errors are caught and counted; they do not abort the batch. Sets total at
    start; finishes the job at end.

    sample_rate (0 < r <= 1) scans a DETERMINISTIC subset: rows are selected by a
    threshold on the first 24 bits of url_hash (uniformly distributed sha256), so
    the same sample is chosen on every run and is independent of insert order.
    r=1.0 scans every row.

    Returns {'queued', 'fresh', 'inflight', 'error'}.
    """
    settings = get_settings()
    concurrency = max(1, settings.scan_batch_concurrency)
    counts = {"queued": 0, "fresh": 0, "inflight": 0, "error": 0}
    # Reject <=0 explicitly: a 0/negative rate would select zero rows and finish
    # 'done' with an empty scan that looks successful. (The API model also enforces
    # gt=0, but the CLI path calls here directly.)
    if not 0 < sample_rate <= 1:
        raise ValueError(f"sample_rate must be in (0, 1], got {sample_rate}")

    try:
        async with pool.acquire() as conn:
            if sample_rate >= 1.0:
                rows = await conn.fetch(
                    "SELECT url_hash, url FROM dataset_rows WHERE dataset_id = $1 ORDER BY id",
                    dataset_id,
                )
            else:
                # 2^24 buckets; keep rows whose hash-prefix bucket is below the cut.
                threshold = int(sample_rate * (1 << 24))
                rows = await conn.fetch(
                    "SELECT url_hash, url FROM dataset_rows WHERE dataset_id = $1 "
                    "AND ('x' || substr(url_hash, 1, 6))::bit(24)::int < $2 "
                    "ORDER BY id",
                    dataset_id, threshold,
                )
            await progress.set_total(conn, job_id, len(rows))

        async def handle(url_hash: str, url: str) -> None:
            try:
                accepted = await service.submit_scan(pool, url)
                status = accepted.status
                counts[status] = counts.get(status, 0) + 1
                await _set_last_scan_id(pool, dataset_id, url_hash, accepted.scan_id)
            except Exception as e:  # noqa: BLE001 — count + continue
                counts["error"] += 1
                log.warning("scan submit failed %s err=%r",
                            kv(dataset_id=dataset_id, url=url[:80]), e)
            async with pool.acquire() as conn:
                done = sum(counts.values())
                await progress.advance(
                    conn, job_id, done_delta=1,
                    message=(f"queued={counts['queued']} fresh={counts['fresh']} "
                             f"inflight={counts['inflight']} error={counts['error']}"),
                )

        if concurrency == 1:
            for r in rows:
                await handle(r["url_hash"], r["url"])
                if throttle_ms > 0:
                    await asyncio.sleep(throttle_ms / 1000.0)
        else:
            sem = asyncio.Semaphore(concurrency)

            async def guarded(url_hash: str, url: str) -> None:
                async with sem:
                    await handle(url_hash, url)
                    if throttle_ms > 0:
                        await asyncio.sleep(throttle_ms / 1000.0)

            await asyncio.gather(*(guarded(r["url_hash"], r["url"]) for r in rows))

        async with pool.acquire() as conn:
            await progress.finish(
                conn, job_id,
                message=(f"done: queued={counts['queued']} fresh={counts['fresh']} "
                         f"inflight={counts['inflight']} error={counts['error']}"),
            )
    except Exception as e:  # noqa: BLE001 — record failure on the job row
        async with pool.acquire() as conn:
            await progress.fail(conn, job_id, repr(e))
        log.warning("scan_dataset failed %s err=%r", kv(dataset_id=dataset_id, job_id=job_id), e)
        raise

    return counts
