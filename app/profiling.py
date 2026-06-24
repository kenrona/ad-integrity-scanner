"""Profile a dataset: per-metric numeric stats + category distributions + the
integrity-score histogram, computed in SQL over the dataset_metrics view.

The metric set is the canonical app.analytics._METRIC_COLS + _SUB list, so a
profile covers exactly the metrics the DuckDB export flattens (no divergence).

Postgres-only: every aggregate is computed in-DB over the dataset_metrics view
(dataset row LEFT JOINed to its latest scan_results). asyncpg, no DuckDB here.
"""
from __future__ import annotations

import asyncpg

from app.analytics import _METRIC_COLS, _SUB

# Split the canonical metric set into numeric vs categorical by DuckDB type.
# BOOLEAN/INTEGER/BIGINT/DOUBLE are treated as numeric (booleans cast to 0/1 in
# SQL below); VARCHAR metrics are categorical (value-count distributions).
NUMERIC_METRICS: list[str] = [n for n, t in _METRIC_COLS if t != "VARCHAR"]
CATEGORICAL_METRICS: list[str] = [n for n, t in _METRIC_COLS if t == "VARCHAR"]
SUB_SCORES: tuple[str, ...] = _SUB

# Publisher-supplied monetization columns (typed columns on dataset_rows, NOT in
# the scanner metrics JSONB). These are profiled over ALL loaded rows — they exist
# independent of whether the URL was scanned — so their null_pct is relative to
# row_count, not scanned_count.
MONETIZATION_METRICS: list[str] = [
    "mon_prebid_count", "mon_bid_count", "mon_request_count", "mon_impressions",
    "mon_bid_rate", "mon_revenue", "mon_cpm", "mon_cpm_variance",
]

# Histogram bucket edges for the integrity score (0-100), 20 wide.
_HIST_BUCKETS = ("0-20", "20-40", "40-60", "60-80", "80-100")


def _numeric_extract(metric: str) -> str:
    """SQL expression yielding a DOUBLE for `metric` out of dataset_metrics.

    Booleans are normalized to 1.0/0.0; numbers cast straight to double. We read
    the top level of the metrics JSONB (or sub_scores for sub-score names).
    """
    if metric == "integrity_score":
        src = "dm.integrity_score::double precision"
        return src
    container = "dm.sub_scores" if metric in SUB_SCORES else "dm.metrics"
    # ->> gives text; JSON `true`/`false` become 't'/'f' via ->> so handle both.
    return (
        f"CASE jsonb_typeof({container} -> '{metric}') "
        f"WHEN 'boolean' THEN (CASE WHEN ({container} -> '{metric}') = 'true'::jsonb "
        f"THEN 1.0 ELSE 0.0 END) "
        f"WHEN 'number' THEN ({container} ->> '{metric}')::double precision "
        f"ELSE NULL END"
    )


async def _numeric_stats(conn: asyncpg.Connection, dataset_id: int,
                         scanned_count: int) -> list[dict]:
    """Per-metric count/null%/min/max/mean/median/p25/p75/distinct.

    One aggregate query per metric (dataset sizes are bounded). Each query runs
    over scanned rows only; null_pct is relative to scanned_count.
    """
    # integrity_score + sub-scores + numeric JSONB metrics, in a stable order.
    names = ["integrity_score", *SUB_SCORES, *NUMERIC_METRICS]
    out: list[dict] = []
    for name in names:
        expr = _numeric_extract(name)
        row = await conn.fetchrow(
            f"""
            WITH vals AS (
                SELECT {expr} AS v
                FROM dataset_metrics dm
                WHERE dm.dataset_id = $1 AND dm.scan_id IS NOT NULL
            )
            SELECT
                count(v)                                          AS count,
                min(v)                                            AS min,
                max(v)                                            AS max,
                avg(v)                                            AS mean,
                percentile_cont(0.5)  WITHIN GROUP (ORDER BY v)   AS median,
                percentile_cont(0.25) WITHIN GROUP (ORDER BY v)   AS p25,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY v)   AS p75,
                count(DISTINCT v)                                 AS distinct
            FROM vals
            """,
            dataset_id,
        )
        count = row["count"] or 0
        null_pct = (
            round(100.0 * (scanned_count - count) / scanned_count, 2)
            if scanned_count else 0.0
        )
        out.append({
            "metric": name,
            "count": count,
            "null_pct": null_pct,
            "min": row["min"],
            "max": row["max"],
            "mean": row["mean"],
            "median": row["median"],
            "p25": row["p25"],
            "p75": row["p75"],
            "distinct": row["distinct"] or 0,
        })
    return out


async def _monetization_stats(conn: asyncpg.Connection, dataset_id: int,
                              row_count: int) -> list[dict]:
    """Per-metric stats for the typed monetization columns over ALL loaded rows.

    Same stat shape as _numeric_stats, but computed directly from the typed
    dataset_metrics columns (no JSONB extraction) and NOT gated on scan_id —
    monetization data is loaded with the URL and is meaningful for every row.
    null_pct is relative to row_count (loaded rows), not scanned_count.
    """
    out: list[dict] = []
    for name in MONETIZATION_METRICS:
        row = await conn.fetchrow(
            f"""
            WITH vals AS (
                SELECT dm.{name}::double precision AS v
                FROM dataset_metrics dm
                WHERE dm.dataset_id = $1
            )
            SELECT
                count(v)                                          AS count,
                min(v)                                            AS min,
                max(v)                                            AS max,
                avg(v)                                            AS mean,
                percentile_cont(0.5)  WITHIN GROUP (ORDER BY v)   AS median,
                percentile_cont(0.25) WITHIN GROUP (ORDER BY v)   AS p25,
                percentile_cont(0.75) WITHIN GROUP (ORDER BY v)   AS p75,
                count(DISTINCT v)                                 AS distinct
            FROM vals
            """,
            dataset_id,
        )
        count = row["count"] or 0
        null_pct = round(100.0 * (row_count - count) / row_count, 2) if row_count else 0.0
        out.append({
            "metric": name,
            "count": count,
            "null_pct": null_pct,
            "min": row["min"],
            "max": row["max"],
            "mean": row["mean"],
            "median": row["median"],
            "p25": row["p25"],
            "p75": row["p75"],
            "distinct": row["distinct"] or 0,
        })
    return out


async def _categorical_stats(conn: asyncpg.Connection,
                             dataset_id: int) -> list[dict]:
    """Top-20 value distribution per VARCHAR metric, over scanned rows."""
    out: list[dict] = []
    for name in CATEGORICAL_METRICS:
        rows = await conn.fetch(
            """
            SELECT (dm.metrics ->> $2) AS value, count(*) AS n
            FROM dataset_metrics dm
            WHERE dm.dataset_id = $1 AND dm.scan_id IS NOT NULL
              AND dm.metrics ? $2
            GROUP BY (dm.metrics ->> $2)
            ORDER BY n DESC, value
            LIMIT 20
            """,
            dataset_id, name,
        )
        out.append({
            "metric": name,
            "distribution": [
                {"value": r["value"], "n": r["n"]} for r in rows
            ],
        })
    return out


async def _integrity_histogram(conn: asyncpg.Connection,
                               dataset_id: int) -> list[dict]:
    """0-20/20-40/.../80-100 counts of integrity_score over scanned rows.

    The top edge (100) lands in the 80-100 bucket; values outside [0,100] are
    clamped into the end buckets via width_bucket.
    """
    rows = await conn.fetch(
        """
        WITH b AS (
            SELECT least(4, greatest(0,
                width_bucket(dm.integrity_score, 0, 100, 5) - 1)) AS bi
            FROM dataset_metrics dm
            WHERE dm.dataset_id = $1 AND dm.scan_id IS NOT NULL
              AND dm.integrity_score IS NOT NULL
        )
        SELECT bi, count(*) AS n FROM b GROUP BY bi
        """,
        dataset_id,
    )
    counts = {r["bi"]: r["n"] for r in rows}
    return [
        {"bucket": label, "n": counts.get(i, 0)}
        for i, label in enumerate(_HIST_BUCKETS)
    ]


async def profile_dataset(conn: asyncpg.Connection, dataset_id: int) -> dict:
    """Profile a single dataset: numeric stats, categorical distributions, and
    the integrity-score histogram, all computed in SQL over dataset_metrics.

    Returns the shape consumed by ProfileResponse (see app/models.py):
      {'dataset_id', 'row_count', 'scanned_count',
       'numeric': [NumericStat...], 'categorical': [CategoricalStat...],
       'integrity_histogram': [HistogramBucket...]}
    """
    counts = await conn.fetchrow(
        """
        SELECT count(*) AS row_count,
               count(*) FILTER (WHERE scan_id IS NOT NULL) AS scanned_count
        FROM dataset_metrics
        WHERE dataset_id = $1
        """,
        dataset_id,
    )
    row_count = counts["row_count"] or 0
    scanned_count = counts["scanned_count"] or 0

    numeric = await _numeric_stats(conn, dataset_id, scanned_count)
    # Monetization metrics span ALL loaded rows; append after the scanner metrics.
    numeric += await _monetization_stats(conn, dataset_id, row_count)
    categorical = await _categorical_stats(conn, dataset_id)
    integrity_histogram = await _integrity_histogram(conn, dataset_id)

    return {
        "dataset_id": dataset_id,
        "row_count": row_count,
        "scanned_count": scanned_count,
        "numeric": numeric,
        "categorical": categorical,
        "integrity_histogram": integrity_histogram,
    }
