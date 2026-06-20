from app.parsers.sellersjson import parse_sellers_json
from app.scoring import supply_chain_score

SELLERS = """
{"version":"1.0","sellers":[
  {"seller_id":"100","name":"Pub A","domain":"pub-a.com","seller_type":"PUBLISHER"},
  {"seller_id":"200","name":"Intermediary B","domain":"ssp.com","seller_type":"INTERMEDIARY"},
  {"seller_id":"300","seller_type":"INTERMEDIARY","is_confidential":1},
  {"seller_id":"400","domain":"both.com","seller_type":"BOTH","is_passthrough":1}
]}
"""


def test_parse_sellers_json_aggregates_and_map():
    r = parse_sellers_json(SELLERS)
    assert r["seller_count"] == 4
    assert r["type_counts"] == {"PUBLISHER": 1, "INTERMEDIARY": 2, "BOTH": 1, "OTHER": 0}
    assert r["confidential_count"] == 1
    assert r["passthrough_count"] == 1
    assert r["sellers"]["200"]["t"] == "INTERMEDIARY"
    assert r["sellers"]["200"]["d"] == "ssp.com"
    assert r["sellers"]["300"]["c"] == 1


def test_parse_sellers_json_malformed_raises():
    import pytest
    with pytest.raises(ValueError):
        parse_sellers_json("{not json")


def test_supply_chain_folds_in_resolution():
    base = {
        "fetch": {"https": True},
        "domain": {"ads_txt": {"present": True, "direct_ratio": 0.8,
                               "distinct_accounts": 50, "has_owner_domain": True}},
    }
    # Same publisher, but with poor supply-path transparency resolved.
    opaque = {**base}
    opaque["domain"] = {**base["domain"], "supply_paths": {
        "attempted": True, "resolution_rate": 0.2, "confidential_ratio": 0.9}}
    transparent = {**base}
    transparent["domain"] = {**base["domain"], "supply_paths": {
        "attempted": True, "resolution_rate": 0.95, "confidential_ratio": 0.05}}

    s_none = supply_chain_score(base)            # resolution not attempted
    s_opaque = supply_chain_score(opaque)
    s_transparent = supply_chain_score(transparent)
    assert s_transparent > s_none > s_opaque     # resolution moves the score both ways
