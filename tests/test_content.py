from app.content import analyze, assess_suitability, classify_category
from app.scoring import brand_suitability_score, mfa_score


def test_classify_category_picks_dominant_topic():
    cat, conf = classify_category(
        "The team won the game in the final season playoff after the coach's call.",
        title="Sports recap",
    )
    assert cat == "Sports"
    assert conf > 0


def test_classify_category_unknown_when_no_signal():
    cat, conf = classify_category("lorem ipsum dolor sit amet", title=None)
    assert cat == "Unknown"
    assert conf == 0.0


def test_word_boundary_avoids_false_positive():
    # "arms" lexicon term must not fire on "pharmacy".
    s = assess_suitability("the pharmacy on the corner sells vitamins")
    assert "arms_ammunition" not in s["flagged_categories"]
    assert s["risk_tier"] == "low"


def test_suitability_flags_risky_content():
    s = assess_suitability("graphic violence and an isis terrorist bomb attack massacre")
    assert s["risk_tier"] in ("high", "floor")
    assert "terrorism" in s["flagged_categories"]


def test_analyze_marks_heuristic():
    a = analyze("a recipe for cooking dinner with fresh ingredients", title="Recipe")
    assert a["heuristic"] is True
    assert a["category"] == "Food & Drink"


def test_mfa_thin_ad_heavy_scores_worse_than_rich_clean():
    thin_ad_heavy = {
        "fetch": {"ok": True},
        "render": {"ok": True, "gpt": {"a2cr": 0.35, "slot_count": 12,
                                       "above_fold_count": 4, "refreshing": True,
                                       "min_refresh_seconds": 8}},
        "page": {"content": {"word_count": 80,
                             "quality": {"link_to_text_ratio": 0.4}}},
    }
    rich_clean = {
        "fetch": {"ok": True},
        "render": {"ok": True, "gpt": {"a2cr": 0.05, "slot_count": 2,
                                       "above_fold_count": 1}},
        "page": {"content": {"word_count": 1500,
                             "quality": {"link_to_text_ratio": 0.02}}},
    }
    assert mfa_score(thin_ad_heavy) < 30
    assert mfa_score(rich_clean) > 85


def test_brand_suitability_tier_maps_to_score():
    floor = {"page": {"content": {"suitability": {"risk_tier": "floor"}}}}
    low = {"page": {"content": {"suitability": {"risk_tier": "low"}}}}
    assert brand_suitability_score(floor) == 10.0
    assert brand_suitability_score(low) == 100.0
    assert brand_suitability_score({"page": {"content": {}}}) is None
