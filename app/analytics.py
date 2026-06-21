"""Analytics export: flatten scan_results -> Parquet, queryable with DuckDB.

Matches the project's "batch/analytics reads" model: Postgres stays the hot
operational store; this offloads a flat, columnar copy (metrics + scores, not the
full raw signal tree) for cheap bulk analysis without touching the live DB.

CLI:
    python -m app.analytics export scan_results.parquet   # Parquet
    python -m app.analytics export scan_results.csv        # CSV (for Excel)
    python -m app.analytics summary scan_results.parquet
"""
from __future__ import annotations

import asyncio
import sys

import duckdb

from app.db import close_pool, init_pool

# (column name, DuckDB type, extractor) — extractor pulls from a scan_results row,
# reaching into the sub_scores / metrics JSON where needed.
_SUB = ("supply_chain", "ad_experience", "mfa", "video", "privacy",
        "performance", "brand_suitability")
# (column, DuckDB type). `ad_sizes` is special-cased to a compact string.
_METRIC_COLS = [
    ("ads_txt_present", "BOOLEAN"), ("ads_txt_direct_ratio", "DOUBLE"),
    ("ads_txt_distinct_ad_systems", "INTEGER"), ("https", "BOOLEAN"),
    # sellers.json supply-path resolution
    ("total_supply_paths", "INTEGER"), ("resolved_sellers", "INTEGER"),
    ("seller_resolution_rate", "DOUBLE"), ("intermediary_ratio", "DOUBLE"),
    ("confidential_sellers", "INTEGER"),
    # ad layout geometry
    ("ad_slot_count", "INTEGER"), ("filled_slot_count", "INTEGER"),
    ("ads_detected_via_gpt", "INTEGER"),
    ("above_fold_ads", "INTEGER"), ("below_fold_ads", "INTEGER"),
    ("ads_in_view", "INTEGER"), ("ads_viewable_1s", "INTEGER"),
    ("ads_per_1000px", "DOUBLE"), ("sticky_ad_count", "INTEGER"),
    ("interstitial", "BOOLEAN"),
    ("hidden_ad_count", "INTEGER"), ("tiny_ad_count", "INTEGER"),
    ("offscreen_ad_count", "INTEGER"), ("stacked_ad_count", "INTEGER"),
    ("suspicious_ad_count", "INTEGER"), ("ad_sizes", "VARCHAR"),
    ("a2cr", "DOUBLE"), ("first_screen_ad_coverage", "DOUBLE"),
    ("first_screen_whitespace", "DOUBLE"), ("first_ad_offset_px", "INTEGER"),
    ("ad_gap_median_px", "INTEGER"), ("ad_refreshing", "BOOLEAN"),
    ("min_refresh_seconds", "DOUBLE"), ("ad_cls_share", "DOUBLE"),
    # performance / footprint (bytes/requests/cpu are CDP-authoritative)
    ("lcp_ms", "INTEGER"), ("cls", "DOUBLE"), ("inp_ms", "INTEGER"),
    ("page_weight_bytes", "BIGINT"),
    ("request_count", "INTEGER"), ("third_party_host_count", "INTEGER"),
    ("tracker_domain_count", "INTEGER"), ("tracker_entity_count", "INTEGER"),
    ("cpu_task_duration_s", "DOUBLE"), ("dom_node_count", "INTEGER"),
    ("schain_present", "BOOLEAN"), ("schain_valid", "BOOLEAN"),
    ("video_viewable_2s", "INTEGER"),
    # consent
    ("cmp_present", "BOOLEAN"), ("cmp_vendor", "VARCHAR"),
    ("cmp_tcf", "BOOLEAN"), ("cmp_gpp", "BOOLEAN"), ("gpc", "BOOLEAN"),
    ("cookie_count", "INTEGER"), ("third_party_cookie_count", "INTEGER"),
    # ad-tech / content
    ("ssp_count", "INTEGER"), ("prebid_bidder_count", "INTEGER"),
    ("has_video", "BOOLEAN"), ("word_count", "INTEGER"),
    ("content_category", "VARCHAR"), ("content_source", "VARCHAR"),
    ("suitability_tier", "VARCHAR"),
]

_SCHEMA = (
    [("url", "VARCHAR"), ("domain", "VARCHAR"), ("scan_tier", "VARCHAR"),
     ("integrity_score", "DOUBLE"), ("confidence", "DOUBLE")]
    + [(s, "DOUBLE") for s in _SUB]
    + _METRIC_COLS
    + [("scanned_at", "TIMESTAMP")]
)


def _fmt_sizes(sizes) -> str | None:
    """Render the ad_sizes histogram as a compact string for a CSV cell."""
    if not isinstance(sizes, dict) or not sizes:
        return None
    return ";".join(f"{k}:{v}" for k, v in sorted(sizes.items(), key=lambda kv: -kv[1]))


def _row_to_tuple(r: dict, sub: dict, metrics: dict) -> tuple:
    vals = [r["url"], r["domain"], r["scan_tier"], r["integrity_score"], r["confidence"]]
    vals += [sub.get(s) for s in _SUB]
    for name, _ in _METRIC_COLS:
        vals.append(_fmt_sizes(metrics.get("ad_sizes")) if name == "ad_sizes"
                    else metrics.get(name))
    vals.append(r["scanned_at"])
    return tuple(vals)


async def _fetch_rows(pool) -> list[tuple]:
    import json
    async with pool.acquire() as conn:
        records = await conn.fetch(
            """
            SELECT url, domain, scan_tier, integrity_score, confidence,
                   sub_scores, metrics, scanned_at
            FROM scan_results
            """
        )
    out = []
    for r in records:
        sub = json.loads(r["sub_scores"]) if r["sub_scores"] else {}
        metrics = json.loads(r["metrics"]) if r["metrics"] else {}
        out.append(_row_to_tuple(dict(r), sub, metrics))
    return out


def _write_table(rows: list[tuple], out_path: str, copy_opts: str) -> int:
    con = duckdb.connect()
    try:
        cols = ", ".join(f"{name} {typ}" for name, typ in _SCHEMA)
        con.execute(f"CREATE TABLE r ({cols})")
        if rows:
            placeholders = ", ".join("?" for _ in _SCHEMA)
            con.executemany(f"INSERT INTO r VALUES ({placeholders})", rows)
        con.execute(f"COPY r TO '{out_path}' {copy_opts}")
    finally:
        con.close()
    return len(rows)


def write_parquet(rows: list[tuple], out_path: str) -> int:
    return _write_table(rows, out_path, "(FORMAT parquet)")


def write_csv(rows: list[tuple], out_path: str) -> int:
    return _write_table(rows, out_path, "(FORMAT csv, HEADER)")


async def export(pool, out_path: str) -> int:
    """Export scan_results to Parquet or CSV, chosen by the file extension."""
    rows = await _fetch_rows(pool)
    if out_path.lower().endswith(".csv"):
        return write_csv(rows, out_path)
    return write_parquet(rows, out_path)


def summarize(path: str) -> None:
    con = duckdb.connect()
    try:
        reader = "read_csv_auto" if path.lower().endswith(".csv") else "read_parquet"
        rel = f"{reader}('{path}')"
        total = con.execute(f"SELECT count(*) FROM {rel}").fetchone()[0]
        print(f"rows: {total}")
        if not total:
            return
        print("\nby tier / avg integrity:")
        for tier, n, avg in con.execute(
            f"SELECT scan_tier, count(*), round(avg(integrity_score),1) "
            f"FROM {rel} GROUP BY scan_tier ORDER BY 2 DESC"
        ).fetchall():
            print(f"  {tier:8} n={n:<6} avg_integrity={avg}")
        print("\nMFA risk buckets (lower mfa = more MFA-like):")
        for label, lo, hi in [("high-risk <40", -1, 40), ("mid 40-70", 40, 70), ("low-risk >70", 70, 101)]:
            n = con.execute(
                f"SELECT count(*) FROM {rel} WHERE mfa > {lo} AND mfa <= {hi}"
            ).fetchone()[0]
            print(f"  {label:14} {n}")
        print("\nlowest-integrity domains:")
        for dom, score in con.execute(
            f"SELECT domain, round(avg(integrity_score),1) s FROM {rel} "
            f"WHERE integrity_score IS NOT NULL GROUP BY domain ORDER BY s LIMIT 10"
        ).fetchall():
            print(f"  {score:6}  {dom}")
    finally:
        con.close()


async def _amain(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in ("export", "summary"):
        print(__doc__)
        return 2
    cmd, path = argv[0], argv[1]
    if cmd == "export":
        pool = await init_pool()
        try:
            n = await export(pool, path)
        finally:
            await close_pool()
        print(f"exported {n} rows -> {path}")
    else:
        summarize(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain(sys.argv[1:])))
