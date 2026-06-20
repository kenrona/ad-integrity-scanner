"""Embedding classifier tests. Skipped when the model can't load (offline)."""
import pytest

from app import content_ml

pytestmark = pytest.mark.skipif(
    not content_ml.is_available(), reason="embedding model unavailable")


def test_embedding_category_is_semantic():
    r = content_ml.try_analyze(
        "The team clinched the championship game in overtime behind their quarterback.", None)
    assert r["method"] == "embedding"
    assert r["category"] == "Sports"


def test_no_topical_false_positive():
    # An article ABOUT advertising that mentions the adult industry must NOT be
    # flagged adult — the exact case the keyword classifier got wrong.
    r = content_ml.try_analyze(
        "Online advertising history: display banners, search ads, and the adult "
        "industry's early adoption of pop-up ads and programmatic buying.", None)
    assert r["suitability"]["risk_tier"] == "low"
    assert "adult_explicit" not in r["suitability"]["flagged_categories"]


def test_flags_genuinely_extreme_content():
    r = content_ml.try_analyze(
        "Explicit hardcore pornographic XXX adult videos and nude webcam shows.", None)
    assert r["suitability"]["risk_tier"] in ("high", "floor")
