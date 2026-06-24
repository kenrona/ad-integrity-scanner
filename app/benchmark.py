"""Benchmark a publisher dataset against the baseline.

Per-metric baseline-vs-publisher mean/median + deltas + percentage-of-baseline,
read from the benchmark_metric_stats materialized view (defined in sql/schema.sql
by the DB layer). Also produces live integrity-score distributions for charting.

Postgres-only: the heavy aggregation lives in the matview; this module refreshes
it and joins the two dataset slices. asyncpg, no DuckDB.
"""
from __future__ import annotations

import asyncpg

from app.profiling import MONETIZATION_METRICS

# Integrity-score histogram buckets, 20 wide over [0, 100]. Mirrors
# app.profiling so baseline/publisher distributions line up bucket-for-bucket.
_HIST_BUCKETS = ("0-20", "20-40", "40-60", "60-80", "80-100")


async def refresh_benchmark(conn: asyncpg.Connection) -> None:
    """Refresh benchmark_metric_stats.

    Tries REFRESH MATERIALIZED VIEW CONCURRENTLY first (needs a unique index and
    a previously populated matview); falls back to a plain REFRESH if Postgres
    rejects the concurrent form (e.g. never populated, or no concurrent support).
    """
    try:
        await conn.execute(
            "REFRESH MATERIALIZED VIEW CONCURRENTLY benchmark_metric_stats"
        )
    except asyncpg.PostgresError as exc:
        if "CONCURRENTLY" in str(exc) or "concurrently" in str(exc).lower():
            await conn.execute("REFRESH MATERIALIZED VIEW benchmark_metric_stats")
        else:
            raise


async def _resolve_baseline(conn: asyncpg.Connection, baseline_name: str) -> dict:
    row = await conn.fetchrow(
        "SELECT id, name FROM datasets WHERE name = $1 AND kind = 'baseline'",
        baseline_name,
    )
    if row is None:
        raise ValueError(f"no baseline dataset named {baseline_name!r}")
    return {"dataset_id": row["id"], "name": row["name"]}


async def _resolve_publisher(conn: asyncpg.Connection,
                             publisher_dataset_id: int) -> dict:
    row = await conn.fetchrow(
        "SELECT id, name FROM datasets WHERE id = $1", publisher_dataset_id
    )
    if row is None:
        raise ValueError(f"no dataset with id {publisher_dataset_id}")
    return {"dataset_id": row["id"], "name": row["name"]}


async def _monetization_stats(conn: asyncpg.Connection,
                              dataset_id: int) -> dict[str, dict]:
    """mean+median per monetization metric over ALL loaded rows of a dataset.

    Monetization data is loaded with the URL (independent of scanning), so unlike
    the scanner metrics in benchmark_metric_stats these are computed over every
    row, not just scanned ones. Returns {metric: {'mean': float, 'median': float}}.
    """
    cols = ", ".join(
        f"avg(dm.{m}::double precision) AS {m}_mean, "
        f"percentile_cont(0.5) WITHIN GROUP (ORDER BY dm.{m}::double precision) AS {m}_median"
        for m in MONETIZATION_METRICS
    )
    row = await conn.fetchrow(
        f"SELECT {cols} FROM dataset_metrics dm WHERE dm.dataset_id = $1",
        dataset_id,
    )
    return {
        m: {"mean": row[f"{m}_mean"], "median": row[f"{m}_median"]}
        for m in MONETIZATION_METRICS
    }


async def _integrity_distribution(conn: asyncpg.Connection,
                                  dataset_id: int) -> list[dict]:
    """Live 20-wide integrity-score histogram for one dataset over scanned rows."""
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


async def compare(conn: asyncpg.Connection, *, publisher_dataset_id: int,
                  baseline_name: str = "baseline") -> dict:
    """Baseline-vs-publisher comparison sourced from benchmark_metric_stats.

    Returns the shape consumed by BenchmarkResponse (see app/models.py):
      {'publisher': {...}, 'baseline': {...},
       'metrics': [BenchmarkMetric...],   # one per metric in either dataset
       'distributions': {'integrity_score': {'baseline': [...], 'publisher': [...]}}}

    Raises ValueError if the named baseline (or the publisher id) is missing.
    """
    baseline = await _resolve_baseline(conn, baseline_name)
    publisher = await _resolve_publisher(conn, publisher_dataset_id)

    # FULL OUTER JOIN the two dataset slices of the matview on metric so a metric
    # present in only one side still appears (its other side is NULL).
    rows = await conn.fetch(
        """
        SELECT
            coalesce(b.metric, p.metric)  AS metric,
            b.mean   AS baseline_mean,   b.median AS baseline_median,
            p.mean   AS publisher_mean,  p.median AS publisher_median
        FROM (SELECT metric, mean, median FROM benchmark_metric_stats
              WHERE dataset_id = $1) b
        FULL OUTER JOIN (SELECT metric, mean, median FROM benchmark_metric_stats
              WHERE dataset_id = $2) p
          ON b.metric = p.metric
        ORDER BY metric
        """,
        baseline["dataset_id"], publisher["dataset_id"],
    )

    metrics: list[dict] = []
    for r in rows:
        b_mean, p_mean = r["baseline_mean"], r["publisher_mean"]
        b_med, p_med = r["baseline_median"], r["publisher_median"]
        delta_mean = (
            p_mean - b_mean if b_mean is not None and p_mean is not None else None
        )
        delta_median = (
            p_med - b_med if b_med is not None and p_med is not None else None
        )
        pct_of_baseline = (
            p_mean / b_mean * 100.0
            if b_mean not in (None, 0) and p_mean is not None else None
        )
        metrics.append({
            "metric": r["metric"],
            "baseline_mean": b_mean,
            "baseline_median": b_med,
            "publisher_mean": p_mean,
            "publisher_median": p_med,
            "delta_mean": delta_mean,
            "delta_median": delta_median,
            "pct_of_baseline": pct_of_baseline,
        })

    # Monetization metrics: computed live over ALL rows (not in the matview, which
    # is scanned-rows-only). Same delta shape as the scanner metrics above.
    b_mon = await _monetization_stats(conn, baseline["dataset_id"])
    p_mon = await _monetization_stats(conn, publisher["dataset_id"])
    for name in MONETIZATION_METRICS:
        b_mean, p_mean = b_mon[name]["mean"], p_mon[name]["mean"]
        b_med, p_med = b_mon[name]["median"], p_mon[name]["median"]
        metrics.append({
            "metric": name,
            "baseline_mean": b_mean,
            "baseline_median": b_med,
            "publisher_mean": p_mean,
            "publisher_median": p_med,
            "delta_mean": (p_mean - b_mean if b_mean is not None and p_mean is not None else None),
            "delta_median": (p_med - b_med if b_med is not None and p_med is not None else None),
            "pct_of_baseline": (
                p_mean / b_mean * 100.0
                if b_mean not in (None, 0) and p_mean is not None else None
            ),
        })

    distributions = {
        "integrity_score": {
            "baseline": await _integrity_distribution(conn, baseline["dataset_id"]),
            "publisher": await _integrity_distribution(conn, publisher["dataset_id"]),
        }
    }

    return {
        "publisher": publisher,
        "baseline": baseline,
        "metrics": metrics,
        "distributions": distributions,
    }
