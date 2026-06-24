"""Unit tests for the multi-browser RenderPool distribution logic.

These exercise the pure browser-selection helper without launching Chromium, so
they run in normal pytest (no Playwright/browser needed).
"""
from __future__ import annotations

from app.render.browser import RenderPool, _least_loaded


def test_least_loaded_picks_min_ties_lowest_index():
    assert _least_loaded([0, 0, 0]) == 0      # all idle -> first
    assert _least_loaded([2, 1, 3]) == 1      # clear min
    assert _least_loaded([1, 1, 0]) == 2      # min at end
    assert _least_loaded([3, 0, 0]) == 1      # tie -> lowest index among mins
    assert _least_loaded([5]) == 0            # single browser


def test_pool_clamps_browsers_and_concurrency():
    p = RenderPool(concurrency=0, browsers=0)
    assert p._concurrency == 1
    assert p._n_browsers == 1
    p2 = RenderPool(concurrency=8, browsers=2)
    assert p2._concurrency == 8 and p2._n_browsers == 2


def test_default_blocked_types_keep_images():
    # images kept by default (page-weight accuracy); only font/media blocked.
    p = RenderPool(concurrency=4, browsers=2)
    assert "image" not in p._blocked
    assert {"font", "media"} <= p._blocked
