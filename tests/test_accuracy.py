"""Accuracy suite.

Two layers:
  * test_truth_manifest_* — fast, no render: validates the fixture generator's
    ground-truth logic (runs in normal `pytest`).
  * test_scanner_accuracy — full render of the fixtures vs ground truth. Slow +
    needs Chromium + the SSRF allowlist, so it's gated behind AI_ACCURACY=1:
        AI_ACCURACY=1 AI_SSRF_ALLOW_HOSTS=127.0.0.1 pytest tests/test_accuracy.py
    (or run directly: python -m tests.accuracy.run)
"""
import os

import pytest

from tests.accuracy.generate import Ad, Spec, build_page, make_suite


def test_suite_size_and_uniqueness():
    suite = make_suite()
    assert len(suite) >= 150            # "a couple hundred" target
    assert len({f["name"] for f in suite}) == len(suite)


def test_truth_givt_and_fold_logic():
    # 1 normal ATF, 1 hidden, 1 offscreen, 1 below-fold, 1 empty placeholder.
    spec = Spec("t", ads=[
        Ad(300, 250, 60),                 # ATF, filled
        Ad(300, 250, 60, "hidden"),       # filled but hidden -> excluded from fold
        Ad(300, 250, 300, "offscreen"),   # filled but offscreen -> excluded from fold
        Ad(300, 250, 2000),               # below fold
        Ad(0, 0, 80, "empty"),            # empty placeholder
    ])
    _, t = build_page(spec)
    assert t["ad_slot_count"] == 5
    assert t["filled_slot_count"] == 4    # all but the empty
    assert t["empty_slot_count"] == 1
    assert t["hidden_ad_count"] == 1
    assert t["offscreen_ad_count"] == 1
    assert t["above_fold_ads"] == 1       # only the visible ATF ad
    assert t["below_fold_ads"] == 1       # the top=2000 ad (hidden/offscreen excluded)


def test_truth_sizes_and_a2cr():
    _, t = build_page(Spec("s", ads=[Ad(728, 90, 60), Ad(728, 90, 900), Ad(300, 250, 1800)]))
    assert t["ad_sizes"] == {"728x90": 2, "300x250": 1}
    assert 0 < t["a2cr"] < 1


@pytest.mark.skipif(os.environ.get("AI_ACCURACY") != "1",
                    reason="set AI_ACCURACY=1 (+AI_SSRF_ALLOW_HOSTS=127.0.0.1) to run the render accuracy suite")
def test_scanner_accuracy():
    import asyncio
    from tests.accuracy.run import run, _DETERMINISTIC, _PASS_THRESHOLD
    score = asyncio.run(run())
    for k, (p, tot) in score["passes"].items():
        if k in _DETERMINISTIC:
            assert p / tot >= _PASS_THRESHOLD, f"{k}: {p}/{tot} below {_PASS_THRESHOLD}"
