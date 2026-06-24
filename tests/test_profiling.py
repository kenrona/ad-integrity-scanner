"""Profiling tests (app.profiling) over a tiny seeded dataset.

Offline: scan_results rows are seeded directly (no scanner/network). dataset_rows
are created with url_hash from normalize_url so the dataset_metrics LATERAL JOIN
picks up the seeded scan. Skipped unless AI_DATABASE_URL is set.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest

from app import datasets
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


async def _seed_row(conn, *, dataset_id, url, integrity, metrics, sub_scores=None):
    """Insert a dataset_row + a matching scan_results row (same url_hash)."""
    norm = normalize_url(url)
    await conn.execute(
        """
        INSERT INTO dataset_rows (dataset_id, url, url_hash, domain)
        VALUES ($1, $2, $3, $4)
        ON CONFLICT (dataset_id, url_hash) DO NOTHING
        """,
        dataset_id, norm.url, norm.url_hash, norm.domain,
    )
    await conn.execute(
        """
        INSERT INTO scan_results
            (scan_id, url_hash, url, domain, scan_tier, confidence,
             metrics, sub_scores, integrity_score, scanner_version, scoring_version)
        VALUES ($1, $2, $3, $4, 'static', 0.9, $5::jsonb, $6::jsonb, $7, '0', '0')
        """,
        uuid.uuid4(), norm.url_hash, norm.url, norm.domain,
        json.dumps(metrics), json.dumps(sub_scores or {}), integrity,
    )


async def test_profile_dataset_shape_and_stats(pool):
    async with pool.acquire() as conn:
        dataset_id = await datasets.create_dataset(conn, name="bench", kind="baseline")
        # Three scanned rows + one unscanned row (dataset_row only).
        await _seed_row(conn, dataset_id=dataset_id, url="https://a.com/1",
                        integrity=10.0,
                        metrics={"ad_slot_count": 2, "https": True, "content_category": "News"},
                        sub_scores={"mfa": 80.0})
        await _seed_row(conn, dataset_id=dataset_id, url="https://a.com/2",
                        integrity=50.0,
                        metrics={"ad_slot_count": 4, "https": True, "content_category": "News"},
                        sub_scores={"mfa": 60.0})
        await _seed_row(conn, dataset_id=dataset_id, url="https://a.com/3",
                        integrity=90.0,
                        metrics={"ad_slot_count": 6, "https": False, "content_category": "Sports"},
                        sub_scores={"mfa": 40.0})
        # Unscanned row (no scan_results).
        n = normalize_url("https://a.com/4")
        await conn.execute(
            "INSERT INTO dataset_rows (dataset_id, url, url_hash, domain) VALUES ($1,$2,$3,$4)",
            dataset_id, n.url, n.url_hash, n.domain,
        )

        from app import profiling
        prof = await profiling.profile_dataset(conn, dataset_id)

    assert prof["dataset_id"] == dataset_id
    assert prof["row_count"] == 4
    assert prof["scanned_count"] == 3

    by_metric = {m["metric"]: m for m in prof["numeric"]}
    # integrity_score present with correct aggregates.
    isc = by_metric["integrity_score"]
    assert isc["count"] == 3
    assert isc["min"] == 10.0
    assert isc["max"] == 90.0
    assert isc["mean"] == pytest.approx(50.0)
    assert isc["median"] == pytest.approx(50.0)

    # numeric JSONB metric.
    slot = by_metric["ad_slot_count"]
    assert slot["count"] == 3
    assert slot["min"] == 2.0 and slot["max"] == 6.0
    assert slot["mean"] == pytest.approx(4.0)
    assert slot["null_pct"] == 0.0

    # boolean metric normalized to 0/1: https True,True,False -> mean 2/3.
    https = by_metric["https"]
    assert https["count"] == 3
    assert https["mean"] == pytest.approx(2.0 / 3.0)

    # sub-score pulled from sub_scores JSONB.
    mfa = by_metric["mfa"]
    assert mfa["count"] == 3
    assert mfa["mean"] == pytest.approx(60.0)

    # categorical distribution for content_category.
    cats = {c["metric"]: c for c in prof["categorical"]}
    cc = cats["content_category"]
    dist = {d["value"]: d["n"] for d in cc["distribution"]}
    assert dist.get("News") == 2
    assert dist.get("Sports") == 1

    # integrity histogram: 10 -> 0-20, 50 -> 40-60, 90 -> 80-100.
    hist = {h["bucket"]: h["n"] for h in prof["integrity_histogram"]}
    assert hist["0-20"] == 1
    assert hist["40-60"] == 1
    assert hist["80-100"] == 1
    assert sum(hist.values()) == 3


async def test_profile_monetization_over_all_rows(pool):
    # Monetization metrics are loaded with the URL and must be profiled over ALL
    # rows (scanned or not), unlike scanner metrics which are scanned-rows-only.
    async with pool.acquire() as conn:
        dataset_id = await datasets.create_dataset(conn, name="mon", kind="publisher")
        # Two rows WITH a scan, one row WITHOUT — all three carry monetization data.
        await _seed_row(conn, dataset_id=dataset_id, url="https://m.com/1",
                        integrity=40.0, metrics={"ad_slot_count": 3})
        await _seed_row(conn, dataset_id=dataset_id, url="https://m.com/2",
                        integrity=60.0, metrics={"ad_slot_count": 5})
        n = normalize_url("https://m.com/3")
        await conn.execute(
            "INSERT INTO dataset_rows (dataset_id, url, url_hash, domain) VALUES ($1,$2,$3,$4)",
            dataset_id, n.url, n.url_hash, n.domain,
        )
        # Stamp mon_revenue on all three rows (10, 20, 30); mon_cpm only on two.
        for url, rev, cpm in [("https://m.com/1", 10.0, 100.0),
                              ("https://m.com/2", 20.0, 200.0),
                              ("https://m.com/3", 30.0, None)]:
            h = normalize_url(url).url_hash
            await conn.execute(
                "UPDATE dataset_rows SET mon_revenue = $2, mon_cpm = $3 "
                "WHERE dataset_id = $1 AND url_hash = $4",
                dataset_id, rev, cpm, h,
            )

        from app import profiling
        prof = await profiling.profile_dataset(conn, dataset_id)

    assert prof["row_count"] == 3
    assert prof["scanned_count"] == 2
    by_metric = {m["metric"]: m for m in prof["numeric"]}

    # mon_revenue counts ALL three rows (incl. the unscanned one).
    rev = by_metric["mon_revenue"]
    assert rev["count"] == 3
    assert rev["mean"] == pytest.approx(20.0)
    assert rev["median"] == pytest.approx(20.0)
    assert rev["min"] == 10.0 and rev["max"] == 30.0
    assert rev["null_pct"] == 0.0

    # mon_cpm present on 2 of 3 rows -> null_pct relative to row_count (loaded).
    cpm = by_metric["mon_cpm"]
    assert cpm["count"] == 2
    assert cpm["null_pct"] == pytest.approx(round(100.0 * 1 / 3, 2))


async def test_profile_empty_dataset(pool):
    async with pool.acquire() as conn:
        dataset_id = await datasets.create_dataset(conn, name="empty", kind="publisher")
        from app import profiling
        prof = await profiling.profile_dataset(conn, dataset_id)
    assert prof["row_count"] == 0
    assert prof["scanned_count"] == 0
    # Shape still complete; null_pct defaults to 0.0 when nothing scanned.
    isc = next(m for m in prof["numeric"] if m["metric"] == "integrity_score")
    assert isc["count"] == 0
    assert isc["null_pct"] == 0.0
