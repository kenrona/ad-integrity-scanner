from app.parsers.adstxt import parse_ads_txt
from app.parsers.html import parse_html
from app.scoring import (
    ad_experience_score,
    assemble,
    privacy_score,
    score_static,
    supply_chain_score,
)


def test_privacy_credits_deployed_cmp_without_live_api():
    # CMP vendor detected but no live API (e.g. EU TCF dormant for a US visitor).
    vendor_only = privacy_score({"cmp": {"vendor": "onetrust"},
                                 "resources": {"tracker_domain_count": 20}})
    none_trackers = privacy_score({"cmp": {}, "resources": {"tracker_domain_count": 20}})
    tcf = privacy_score({"cmp": {"tcf": True}, "resources": {}})
    assert none_trackers == 10.0          # trackers, no consent mgmt
    assert vendor_only == 40.0            # deployed CMP gets credit
    assert tcf >= 50.0
    assert tcf > vendor_only > none_trackers

ADS_TXT = """\
# sample ads.txt
google.com, pub-0000000000000000, DIRECT, f08c47fec0942fa0
google.com, pub-0000000000000000, RESELLER, f08c47fec0942fa0
rubiconproject.com, 12345, RESELLER, 0bfd66d529a55807
openx.com, 540000, DIRECT
OWNERDOMAIN=example.com
MANAGERDOMAIN=manager.example,US
CONTACT=ads@example.com
SUBDOMAIN=sports.example.com
not,enough
"""


def test_parse_ads_txt_counts_and_variables():
    r = parse_ads_txt(ADS_TXT)
    assert r["present"] is True
    assert r["total_records"] == 4
    assert r["direct_count"] == 2
    assert r["reseller_count"] == 2
    assert r["direct_ratio"] == 0.5
    assert r["distinct_ad_systems"] == 3          # google, rubicon, openx
    assert r["distinct_accounts"] == 3            # (google,pub..) counted once
    assert r["has_owner_domain"] is True
    assert r["has_manager_domain"] is True        # value-with-comma handled
    assert r["subdomain_referrals"] == ["sports.example.com"]
    assert r["malformed_lines"] == 1              # "not,enough"


def test_parse_ads_txt_empty():
    r = parse_ads_txt("# just a comment\n\n")
    assert r["present"] is False
    assert r["direct_ratio"] == 0.0


HTML = """\
<html lang="en"><head>
<title>Test Page</title>
<meta name="description" content="d">
<meta name="viewport" content="width=device-width">
<link rel="canonical" href="https://example.com/x">
<script src="https://securepubads.g.doubleclick.net/tag/js/gpt.js"></script>
<script src="https://cdn.example.com/prebid.js"></script>
<script src="https://widgets.outbrain.com/outbrain.js"></script>
</head><body>
<p>Hello world this is some content text.</p>
<video src="movie.mp4"></video>
<iframe src="https://www.youtube.com/embed/abc"></iframe>
<script>var x = jwplayer('p');</script>
</body></html>
"""


def test_parse_html_signals():
    r = parse_html(HTML, page_domain="example.com")
    c, a, v = r["content"], r["ad_tech"], r["video"]
    assert c["title"] == "Test Page"
    assert c["meta_description_present"] and c["has_viewport"] and c["canonical_present"]
    assert c["lang"] == "en"
    assert c["word_count"] >= 6
    assert a["has_gpt"] and a["has_prebid"]
    assert "outbrain" in a["native_widgets"]
    assert "doubleclick.net" in c["external_script_domains"]
    assert c["ad_related_domain_count"] >= 1
    assert v["has_video"] and v["native_video_tags"] == 1
    assert "jwplayer" in v["players"] and "youtube" in v["embedded"]


def test_supply_chain_score_rewards_transparency():
    good = {
        "fetch": {"https": True, "ok": True},
        "domain": {"ads_txt": parse_ads_txt(ADS_TXT)},
    }
    bare = {"fetch": {"https": False, "ok": True}, "domain": {"ads_txt": {"present": False}}}
    s_good = supply_chain_score(good)
    s_bare = supply_chain_score(bare)
    assert s_good > s_bare
    assert s_bare == 0.0
    # Renormalized components (no sellers.json resolution here):
    # (100·35 + 50·25 + 100·15 + 100·10) / 85 = 85.29
    assert s_good == 85.29


def test_score_static_shape_and_confidence():
    ok = score_static({"fetch": {"https": True, "ok": True},
                       "domain": {"ads_txt": parse_ads_txt(ADS_TXT)}, "page": {}})
    assert ok["scan_tier"] == "static"
    # Static tier now yields supply_chain + mfa (content-derived) sub-scores.
    assert set(ok["sub_scores"]) == {"supply_chain", "mfa"}
    lo, hi = sorted(ok["sub_scores"].values())
    assert lo <= ok["integrity_score"] <= hi
    assert ok["confidence"] == 0.4

    failed = score_static({"fetch": {"ok": False}, "domain": {}, "page": {}})
    assert failed["integrity_score"] is None
    assert failed["confidence"] == 0.1


def test_ad_experience_penalizes_clutter_and_fast_refresh():
    clean = ad_experience_score({"gpt": {"present": True, "slot_count": 2,
                                          "above_fold_count": 1, "a2cr": 0.05}})
    cluttered = ad_experience_score({"gpt": {
        "present": True, "slot_count": 14, "above_fold_count": 4, "a2cr": 0.45,
        "refreshing": True, "min_refresh_seconds": 8}})
    assert clean > cluttered
    assert cluttered < 30  # heavy clutter + sub-15s refresh tanks the score
    # No observable ads -> "clean" default, not None.
    assert ad_experience_score({"gpt": {"present": False}, "resources": {}}) == 90.0


def test_assemble_with_render_raises_confidence_and_blends():
    signals = {
        "fetch": {"ok": True, "https": True},
        "domain": {"ads_txt": parse_ads_txt(ADS_TXT)},
        "page": {},
        "render": {
            "ok": True,
            "gpt": {"present": True, "slot_count": 3, "above_fold_count": 1, "a2cr": 0.1},
            "cwv": {"lcp_ms": 2000, "cls": 0.05},
            "resources": {"page_weight_bytes": 1_500_000, "request_count": 60,
                          "ad_request_count": 5},
            "cmp": {"tcf": True, "gpp": False, "usp": False, "gpc": False},
            "video": {"has_video": False},
            "prebid": {"present": True, "bidder_count": 4},
        },
    }
    r = assemble(signals)
    assert r["scan_tier"] == "render"
    assert r["confidence"] == 0.85
    subs = r["sub_scores"]
    # video excluded (no video); mfa present (content-derived).
    assert set(subs) == {"supply_chain", "mfa", "ad_experience", "privacy", "performance"}
    assert "video" not in subs
    assert r["metrics"]["rendered"] is True
    assert 0 <= r["integrity_score"] <= 100
