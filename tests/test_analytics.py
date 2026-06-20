import datetime as dt

from app.analytics import _row_to_tuple, summarize, write_csv, write_parquet


def _row(url, domain, integrity, mfa):
    # Build via the real row builder so the test tracks schema changes.
    r = {"url": url, "domain": domain, "scan_tier": "render",
         "integrity_score": integrity, "confidence": 0.85,
         "scanned_at": dt.datetime(2026, 6, 19, 12, 0, 0)}
    sub = {"supply_chain": 80.0, "ad_experience": 50.0, "mfa": mfa,
           "privacy": 30.0, "performance": 90.0, "brand_suitability": 100.0}
    metrics = {"ads_txt_present": True, "https": True, "a2cr": 0.11,
               "ad_slot_count": 14, "above_fold_ads": 3, "below_fold_ads": 11,
               "ad_sizes": {"300x250": 4, "728x90": 2}, "word_count": 1200,
               "content_category": "News", "suitability_tier": "low",
               "lcp_ms": 1800, "cls": 0.05, "page_weight_bytes": 3000000,
               "tracker_domain_count": 12}
    return _row_to_tuple(r, sub, metrics)


def test_write_and_read_parquet(tmp_path):
    out = str(tmp_path / "scan_results.parquet")
    rows = [_row("https://a.com/1", "a.com", 65.0, 50.0),
            _row("https://b.com/2", "b.com", 90.0, 95.0)]
    assert write_parquet(rows, out) == 2

    import duckdb
    con = duckdb.connect()
    total = con.execute(f"SELECT count(*) FROM read_parquet('{out}')").fetchone()[0]
    avg = con.execute(f"SELECT round(avg(integrity_score),1) FROM read_parquet('{out}')").fetchone()[0]
    sizes = con.execute(f"SELECT ad_sizes FROM read_parquet('{out}') LIMIT 1").fetchone()[0]
    con.close()
    assert total == 2 and avg == 77.5
    assert "300x250:4" in sizes


def test_write_empty_parquet(tmp_path):
    out = str(tmp_path / "empty.parquet")
    assert write_parquet([], out) == 0
    summarize(out)  # must not raise on empty


def test_write_csv_has_header_and_rows(tmp_path):
    out = str(tmp_path / "scan_results.csv")
    rows = [_row("https://a.com/1", "a.com", 65.0, 50.0),
            _row("https://b.com/2", "b.com", 90.0, 95.0)]
    assert write_csv(rows, out) == 2
    lines = open(out).read().strip().splitlines()
    assert lines[0].startswith("url,domain,scan_tier,integrity_score")
    assert "above_fold_ads" in lines[0] and "first_screen_whitespace" in lines[0]
    assert len(lines) == 3
    assert "a.com" in lines[1]
