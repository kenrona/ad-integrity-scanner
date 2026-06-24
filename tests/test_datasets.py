"""Dataset ingestion tests (app.datasets): parsing + ingest + url_hash join key.

Skipped automatically unless a Postgres test DB is reachable. Point
AI_DATABASE_URL at a throwaway DB (e.g. ad_integrity_test) before running.
The parser tests need no DB and run regardless.
"""
from __future__ import annotations

import os

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
            "TRUNCATE datasets, dataset_rows, job_runs RESTART IDENTITY CASCADE"
        )
    yield p
    from app.db import close_pool
    await close_pool()


# ---------------------------------------------------------------------------
# Parsing (no DB)
# ---------------------------------------------------------------------------
def test_parse_txt_ignores_blanks_and_comments():
    text = "https://a.com/1\n\n# a comment\n  https://b.com/2  \n"
    parsed = datasets.parse_txt(text)
    assert parsed.urls == ["https://a.com/1", "https://b.com/2"]
    assert parsed.extras == [{}, {}]


def test_parse_csv_resolves_url_column_and_keeps_extras():
    data = b"page_url,tier,note\nhttps://a.com/1,gold,hi\nhttps://b.com/2,silver,yo\n"
    parsed = datasets.parse_csv(data)
    assert parsed.urls == ["https://a.com/1", "https://b.com/2"]
    assert parsed.extras[0] == {"tier": "gold", "note": "hi"}
    assert parsed.extras[1] == {"tier": "silver", "note": "yo"}


def test_parse_csv_explicit_url_column():
    data = b"link,other\nhttps://a.com/1,x\n"
    parsed = datasets.parse_csv(data, url_column="link")
    assert parsed.urls == ["https://a.com/1"]
    assert parsed.extras[0] == {"other": "x"}


def test_parse_csv_no_url_column_falls_back_to_first():
    # No header matches the candidates -> first column is used as URL.
    data = b"col_a,col_b\nhttps://a.com/1,x\n"
    parsed = datasets.parse_csv(data)
    assert parsed.urls == ["https://a.com/1"]
    assert parsed.extras[0] == {"col_b": "x"}


def test_parse_source_unknown_extension_raises():
    with pytest.raises(ValueError):
        datasets.parse_source("data.json", b"{}")


def test_parse_xlsx_roundtrip():
    openpyxl = pytest.importorskip("openpyxl")
    import io

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["url", "tier"])
    ws.append(["https://a.com/1", "gold"])
    ws.append(["https://b.com/2", "silver"])
    buf = io.BytesIO()
    wb.save(buf)
    parsed = datasets.parse_source("d.xlsx", buf.getvalue())
    assert parsed.urls == ["https://a.com/1", "https://b.com/2"]
    assert parsed.extras[0] == {"tier": "gold"}


# ---------------------------------------------------------------------------
# Ingestion (DB)
# ---------------------------------------------------------------------------
async def test_ingest_txt_url_hash_matches_normalize(pool):
    from app import progress

    async with pool.acquire() as conn:
        dataset_id = await datasets.create_dataset(
            conn, name="baseline", kind="baseline", source_file="b.txt"
        )
        job_id = await progress.create_job(conn, kind="ingest", dataset_id=dataset_id)

    parsed = datasets.parse_txt(
        "https://a.com/article?utm_source=x\nhttps://b.com/2\nnot a valid url with spaces\n"
    )
    result = await datasets.ingest(
        pool, dataset_id=dataset_id, parsed=parsed, job_id=job_id, batch_size=10
    )
    # 2 valid URLs inserted; the spaces line is unparseable -> skipped.
    assert result["inserted"] == 2
    assert result["skipped"] == 1
    assert result["duplicates"] == 0

    # url_hash stored MUST equal normalize_url() so it JOINs scan_results.
    expected = normalize_url("https://a.com/article?utm_source=x")
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT url, url_hash, domain FROM dataset_rows WHERE url_hash = $1",
            expected.url_hash,
        )
        job = await progress.get_job(conn, job_id)
    assert row is not None
    assert row["url"] == expected.url
    assert row["domain"] == expected.domain
    # Job lifecycle: total set to all parsed urls, done counts every processed url,
    # finished.
    assert job["status"] == "done"
    assert job["total"] == 3
    assert job["done"] == 3


async def test_ingest_csv_label_kind_extras(pool):
    from app import progress

    async with pool.acquire() as conn:
        dataset_id = await datasets.create_dataset(
            conn, name="acme_news", kind="publisher", source_file="acme.csv"
        )
        job_id = await progress.create_job(conn, kind="ingest", dataset_id=dataset_id)

    parsed = datasets.parse_csv(
        b"url,tier\nhttps://acme.com/a,gold\nhttps://acme.com/b,silver\n"
    )
    result = await datasets.ingest(
        pool, dataset_id=dataset_id, parsed=parsed, job_id=job_id, batch_size=10
    )
    assert result["inserted"] == 2

    async with pool.acquire() as conn:
        summaries = await datasets.list_datasets(conn)
        extra = await conn.fetchval(
            "SELECT extra FROM dataset_rows WHERE url = $1",
            normalize_url("https://acme.com/a").url,
        )
    me = next(s for s in summaries if s["name"] == "acme_news")
    assert me["kind"] == "publisher"
    assert me["row_count"] == 2
    assert me["scanned_count"] == 0  # nothing scanned yet
    import json
    assert json.loads(extra)["tier"] == "gold"


async def test_ingest_dedups_within_dataset(pool):
    from app import progress

    async with pool.acquire() as conn:
        dataset_id = await datasets.create_dataset(conn, name="dups", kind="publisher")
        job_id = await progress.create_job(conn, kind="ingest", dataset_id=dataset_id)

    # Same logical page twice (cosmetic differences only) -> one dataset_row.
    parsed = datasets.parse_txt("https://x.com/p\nhttps://X.com/p/#frag\n")
    result = await datasets.ingest(
        pool, dataset_id=dataset_id, parsed=parsed, job_id=job_id, batch_size=10
    )
    assert result["inserted"] == 1
    assert result["duplicates"] == 1


async def test_baseline_drops_home_pages_publisher_keeps_them(pool):
    from app import progress

    # _is_home_page is a pure predicate: root path (+ tracking-only query) -> home.
    assert datasets._is_home_page("https://example.com/")
    assert datasets._is_home_page("https://example.com")
    assert not datasets._is_home_page("https://example.com/article")
    assert not datasets._is_home_page("https://example.com/?p=1")

    urls = "example.com/\nnews.example.com/\nexample.com/article/x\nexample.com/2\n"

    # Baseline: the two root-path URLs are dropped, content pages kept.
    async with pool.acquire() as conn:
        base_id = await datasets.create_dataset(conn, name="baseline", kind="baseline")
        bjob = await progress.create_job(conn, kind="ingest", dataset_id=base_id)
    bres = await datasets.ingest(
        pool, dataset_id=base_id, parsed=datasets.parse_txt(urls), job_id=bjob, batch_size=10
    )
    assert bres["home_pages"] == 2
    assert bres["inserted"] == 2

    # Publisher: identical input keeps every row (home pages NOT dropped).
    async with pool.acquire() as conn:
        pub_id = await datasets.create_dataset(conn, name="pub", kind="publisher")
        pjob = await progress.create_job(conn, kind="ingest", dataset_id=pub_id)
    pres = await datasets.ingest(
        pool, dataset_id=pub_id, parsed=datasets.parse_txt(urls), job_id=pjob, batch_size=10
    )
    assert pres["home_pages"] == 0
    assert pres["inserted"] == 4


async def test_delete_rollups_removes_app_aggregate(pool):
    from app import progress

    async with pool.acquire() as conn:
        pub_id = await datasets.create_dataset(conn, name="nypost", kind="publisher")
        job_id = await progress.create_job(conn, kind="ingest", dataset_id=pub_id)

    # Row 1: the app-aggregate rollup (home page + bundle-id source_domain).
    # Row 2: a normal article page. Row 3: a legit home page tagged with its OWN
    # domain (must be SPARED). Row 4: a home page with a numeric placement id.
    csv = (
        b"url,domain,prebid_count,revenue\n"
        b"nypost.com/,br.com.golmobile.nypost,2315611742,398219.99\n"
        b"nypost.com/2024/01/01/some-article,nypost.com,3000,0.43\n"
        b"realhome.com/,realhome.com,10,1.0\n"
        b"aggregate.com/,410094240,9999,500.0\n"
    )
    res = await datasets.ingest(
        pool, dataset_id=pub_id, parsed=datasets.parse_csv(csv),
        job_id=job_id, batch_size=10,
    )
    assert res["inserted"] == 4
    # ingest auto-prunes publisher rollups: the bundle-id and numeric-id home
    # pages go; the article and the self-tagged home page stay.
    assert res["rollups_deleted"] == 2

    async with pool.acquire() as conn:
        remaining = await conn.fetch(
            "SELECT url FROM dataset_rows WHERE dataset_id = $1 ORDER BY url", pub_id
        )
    urls = [r["url"] for r in remaining]
    assert normalize_url("nypost.com/2024/01/01/some-article").url in urls
    assert normalize_url("realhome.com/").url in urls          # spared (own domain)
    assert normalize_url("nypost.com/").url not in urls        # rollup removed
    assert normalize_url("aggregate.com/").url not in urls     # numeric-id rollup removed

    # delete_rollups is idempotent: a second pass finds nothing.
    async with pool.acquire() as conn:
        again = await datasets.delete_rollups(conn, dataset_id=pub_id)
    assert again["deleted"] == 0


async def test_create_dataset_rejects_bad_kind(pool):
    async with pool.acquire() as conn:
        with pytest.raises(ValueError):
            await datasets.create_dataset(conn, name="x", kind="not_a_kind")


# ---------------------------------------------------------------------------
# Monetization columns (publisher/baseline ad-revenue data)
# ---------------------------------------------------------------------------
def test_split_monetization_routes_typed_columns():
    # Mirrors a real publisher row: garbage source domain, the request_count
    # name-collision, blank cells, and an unrecognized column.
    extra = {
        "domain": "br.com.golmobile.nypost",
        "prebid_count": "2,315,611,742",   # thousands separators tolerated
        "bid_count": "0",
        "request_count": "1000",           # collides with scanner metric -> mon_request_count
        "bid_rate": "0.0078",
        "impressions": "",                 # blank -> None
        "revenue": "398219.9995",
        "cpm": "93.86723227",
        "cpm_variance": "",                # blank -> None
        "tier": "gold",                    # unknown -> stays in leftover
    }
    typed, source_domain, leftover = datasets._split_monetization(extra)
    assert source_domain == "br.com.golmobile.nypost"
    assert typed["mon_prebid_count"] == 2315611742   # BIGINT range
    assert typed["mon_bid_count"] == 0
    assert typed["mon_request_count"] == 1000
    assert typed["mon_bid_rate"] == pytest.approx(0.0078)
    assert typed["mon_impressions"] is None
    assert typed["mon_revenue"] == pytest.approx(398219.9995)
    assert typed["mon_cpm"] == pytest.approx(93.86723227)
    assert typed["mon_cpm_variance"] is None
    assert leftover == {"tier": "gold"}


async def test_ingest_monetization_typed_columns(pool):
    from app import progress

    async with pool.acquire() as conn:
        dataset_id = await datasets.create_dataset(
            conn, name="nypost", kind="publisher", source_file="nypost.csv"
        )
        job_id = await progress.create_job(conn, kind="ingest", dataset_id=dataset_id)

    # Real publisher layout: scheme-less URLs, one with trailing spaces + blank
    # source domain + blank revenue/cpm cells; the second a normal row.
    csv = (
        b"url,domain,prebid_count,bid_count,request_count,bid_rate,"
        b"impressions,revenue,cpm,cpm_variance\n"
        b"nypost.com/a   ,,1000,0,1000,0,0,0,,\n"
        b"nypost.com/b,nypost.com,2000,9,2009,0.00299,6,0.4376,72.93,946.8\n"
    )
    parsed = datasets.parse_csv(csv)
    result = await datasets.ingest(
        pool, dataset_id=dataset_id, parsed=parsed, job_id=job_id, batch_size=10
    )
    assert result["inserted"] == 2

    norm_a = normalize_url("nypost.com/a")   # scheme prepended; trailing space stripped
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT domain, source_domain, extra,
                   mon_prebid_count, mon_request_count, mon_revenue, mon_cpm
            FROM dataset_rows WHERE url_hash = $1
            """,
            norm_a.url_hash,
        )
    assert row["domain"] == norm_a.domain      # canonical domain derived from URL
    assert row["domain"] == "nypost.com"
    assert row["source_domain"] is None        # blank source domain -> NULL
    assert row["mon_prebid_count"] == 1000
    assert row["mon_request_count"] == 1000    # collision-safe (own typed column)
    assert row["mon_revenue"] == pytest.approx(0.0)
    assert row["mon_cpm"] is None              # blank cell -> NULL
    # Recognized monetization columns are pulled OUT of the JSONB extra.
    import json
    assert json.loads(row["extra"]) == {}
