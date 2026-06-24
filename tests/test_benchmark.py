"""Benchmark tests (app.benchmark): baseline-vs-publisher deltas from the matview.

Offline: scan_results rows are seeded directly. The benchmark_metric_stats matview
is refreshed before comparing. Skipped unless AI_DATABASE_URL is set.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest

from app import benchmark, datasets
from app.normalize import normalize_url

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def pool():
    if not os.environ.get("AI_DATABASE_URL"):
        pytest.skip("set AI_DATABASE_URL to a throwaway test DB to run integration tests")
    try:
        from app.db import init_pool
        p = await init_pool()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"no postgres available: {e}")
    async with p.acquire() as conn:
        await conn.execute(
            "TRUNCATE datasets, dataset_rows, job_runs, scan_results "
            "RESTART IDENTITY CASCADE"
        )
    yield p
    from app.db import close_pool
    await close_pool()


async def _seed(conn, *, dataset_id, url, integrity, metrics):
    norm = normalize_url(url)
    await conn.execute(
        "INSERT INTO dataset_rows (dataset_id, url, url_hash, domain) "
        "VALUES ($1,$2,$3,$4) ON CONFLICT (dataset_id, url_hash) DO NOTHING",
        dataset_id, norm.url, norm.url_hash, norm.domain,
    )
    await conn.execute(
        """
        INSERT INTO scan_results
            (scan_id, url_hash, url, domain, scan_tier, confidence,
             metrics, sub_scores, integrity_score, scanner_version, scoring_version)
        VALUES ($1, $2, $3, $4, 'static', 0.9, $5::jsonb, '{}'::jsonb, $6, '0', '0')
        """,
        uuid.uuid4(), norm.url_hash, norm.url, norm.domain,
        json.dumps(metrics), integrity,
    )


async def test_compare_deltas(pool):
    async with pool.acquire() as conn:
        base_id = await datasets.create_dataset(conn, name="baseline", kind="baseline")
        pub_id = await datasets.create_dataset(conn, name="acme", kind="publisher")

        # Baseline: ad_slot_count mean = 2; integrity mean = 20.
        await _seed(conn, dataset_id=base_id, url="https://base.com/1",
                    integrity=10.0, metrics={"ad_slot_count": 1})
        await _seed(conn, dataset_id=base_id, url="https://base.com/2",
                    integrity=30.0, metrics={"ad_slot_count": 3})
        # Publisher: ad_slot_count mean = 6; integrity mean = 90.
        await _seed(conn, dataset_id=pub_id, url="https://acme.com/1",
                    integrity=90.0, metrics={"ad_slot_count": 6})

        await benchmark.refresh_benchmark(conn)
        result = await benchmark.compare(conn, publisher_dataset_id=pub_id)

    assert result["baseline"]["name"] == "baseline"
    assert result["publisher"]["name"] == "acme"

    by_metric = {m["metric"]: m for m in result["metrics"]}

    slot = by_metric["ad_slot_count"]
    assert slot["baseline_mean"] == pytest.approx(2.0)
    assert slot["publisher_mean"] == pytest.approx(6.0)
    assert slot["delta_mean"] == pytest.approx(4.0)
    assert slot["pct_of_baseline"] == pytest.approx(300.0)

    isc = by_metric["integrity_score"]
    assert isc["baseline_mean"] == pytest.approx(20.0)
    assert isc["publisher_mean"] == pytest.approx(90.0)
    assert isc["delta_mean"] == pytest.approx(70.0)

    # Live integrity distributions are present and bucketed.
    dist = result["distributions"]["integrity_score"]
    base_hist = {h["bucket"]: h["n"] for h in dist["baseline"]}
    pub_hist = {h["bucket"]: h["n"] for h in dist["publisher"]}
    assert base_hist["0-20"] == 1 and base_hist["20-40"] == 1
    assert pub_hist["80-100"] == 1
    assert sum(pub_hist.values()) == 1


async def test_compare_includes_monetization_metrics(pool):
    # Monetization deltas come from live per-row aggregation (NOT the matview) and
    # span all rows including unscanned ones.
    async with pool.acquire() as conn:
        base_id = await datasets.create_dataset(conn, name="baseline", kind="baseline")
        pub_id = await datasets.create_dataset(conn, name="acme", kind="publisher")

        # Baseline rows carry mon_revenue 1.0 and 3.0 (mean 2.0); one is unscanned.
        await _seed(conn, dataset_id=base_id, url="https://base.com/1",
                    integrity=10.0, metrics={})
        nb = normalize_url("https://base.com/2")
        await conn.execute(
            "INSERT INTO dataset_rows (dataset_id, url, url_hash, domain, mon_revenue) "
            "VALUES ($1,$2,$3,$4,$5)",
            base_id, nb.url, nb.url_hash, nb.domain, 3.0,
        )
        await conn.execute(
            "UPDATE dataset_rows SET mon_revenue = 1.0 "
            "WHERE dataset_id = $1 AND url = $2",
            base_id, normalize_url("https://base.com/1").url,
        )
        # Publisher row: mon_revenue 8.0 (mean 8.0).
        np = normalize_url("https://acme.com/1")
        await conn.execute(
            "INSERT INTO dataset_rows (dataset_id, url, url_hash, domain, mon_revenue) "
            "VALUES ($1,$2,$3,$4,$5)",
            pub_id, np.url, np.url_hash, np.domain, 8.0,
        )

        await benchmark.refresh_benchmark(conn)
        result = await benchmark.compare(conn, publisher_dataset_id=pub_id)

    by_metric = {m["metric"]: m for m in result["metrics"]}
    assert "mon_revenue" in by_metric
    rev = by_metric["mon_revenue"]
    assert rev["baseline_mean"] == pytest.approx(2.0)   # over both baseline rows
    assert rev["publisher_mean"] == pytest.approx(8.0)
    assert rev["delta_mean"] == pytest.approx(6.0)
    assert rev["pct_of_baseline"] == pytest.approx(400.0)


async def test_compare_missing_baseline_raises(pool):
    async with pool.acquire() as conn:
        pub_id = await datasets.create_dataset(conn, name="solo", kind="publisher")
        with pytest.raises(ValueError):
            await benchmark.compare(conn, publisher_dataset_id=pub_id,
                                    baseline_name="does_not_exist")


async def test_compare_missing_publisher_raises(pool):
    async with pool.acquire() as conn:
        await datasets.create_dataset(conn, name="baseline", kind="baseline")
        with pytest.raises(ValueError):
            await benchmark.compare(conn, publisher_dataset_id=9999999)
