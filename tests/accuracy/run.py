"""Accuracy harness: serve ground-truth fixtures, scan them, score measured vs truth.

    AI_SSRF_ALLOW_HOSTS=127.0.0.1 python -m tests.accuracy.run [N]

Serves the generated fixtures on a local HTTP server (allowlisted past the SSRF
guard), renders each through the real render path (+ content backfill + scoring),
and prints a per-metric scorecard. Exits non-zero if deterministic metrics miss
the pass threshold.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import threading
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# Allowlist localhost BEFORE settings are first read.
os.environ.setdefault("AI_SSRF_ALLOW_HOSTS", "127.0.0.1,localhost")

from app.config import get_settings  # noqa: E402
get_settings.cache_clear()

from app.render.browser import RenderPool  # noqa: E402
from app.render.collect import render_page  # noqa: E402
from app.scoring import assemble  # noqa: E402
from app.workers.render_worker import _backfill_content  # noqa: E402
from tests.accuracy.generate import make_suite  # noqa: E402

# Metrics asserted exactly (deterministic from the DOM we authored).
_EXACT = ["ad_slot_count", "filled_slot_count", "empty_slot_count", "above_fold_ads",
          "below_fold_ads", "sticky_ad_count", "hidden_ad_count", "tiny_ad_count",
          "offscreen_ad_count", "interstitial", "cmp_present"]
_DETERMINISTIC = set(_EXACT) | {"ad_sizes"}
_PASS_THRESHOLD = 0.95


def _serve(fixtures: dict[str, str]) -> ThreadingHTTPServer:
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # quiet
            pass

        def do_GET(self):
            name = self.path.lstrip("/").split("?")[0]
            html = fixtures.get(name)
            if html is None:
                self.send_response(404); self.end_headers(); return
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _compare(measured: dict, truth: dict) -> dict[str, bool]:
    res = {}
    for k, exp in truth.items():
        if exp is None and k not in ("content_category",):
            continue
        m = measured.get(k)
        if k == "a2cr":
            res[k] = m is not None and abs(m - exp) <= 0.03
        elif k == "ad_sizes":
            res[k] = (m or {}) == exp
        elif k == "suitability_tier":
            res[k] = (m in exp) if isinstance(exp, tuple) else (m == exp)
        elif k == "cmp_vendor":
            res[k] = (m == exp)
        elif k == "content_category":
            res[k] = (m == exp)
        elif k == "video_autoplay":
            res[k] = (m == exp)
        else:
            res[k] = (m == exp)
    return res


async def run(limit: int | None = None) -> dict:
    suite = make_suite()
    if limit:
        suite = suite[:limit]
    # URL-safe slug per fixture (names contain spaces / '&').
    for i, f in enumerate(suite):
        f["slug"] = f"{i}-" + re.sub(r"[^a-z0-9]+", "-", f["name"].lower()).strip("-")
    fixtures = {f["slug"]: f["html"] for f in suite}
    srv = _serve(fixtures)
    port = srv.server_address[1]
    base = f"http://127.0.0.1:{port}"

    rp = RenderPool(concurrency=4, blocked_types={"font", "media"})
    await rp.start()

    async def scan(fx):
        rd = await render_page(rp, f"{base}/{fx['slug']}", dwell_ms=3500)
        signals = {"render": rd, "fetch": {"ok": False}}
        if rd.get("ok"):
            _backfill_content(signals, rd)
        return fx, assemble(signals)["metrics"]

    try:
        results = await asyncio.gather(*(scan(fx) for fx in suite))
    finally:
        await rp.stop()
        srv.shutdown()

    passes = defaultdict(lambda: [0, 0])  # metric -> [passed, total]
    failures = []
    for fx, metrics in results:
        cmp = _compare(metrics, fx["truth"])
        for k, ok in cmp.items():
            passes[k][0] += int(ok); passes[k][1] += 1
            if not ok and k in _DETERMINISTIC:
                failures.append((fx["name"], k, fx["truth"].get(k), metrics.get(k)))
    return {"passes": dict(passes), "failures": failures, "n": len(results)}


def _print(score: dict) -> bool:
    print(f"\n=== ACCURACY SCORECARD ({score['n']} fixtures) ===")
    det_ok = True
    for k in sorted(score["passes"]):
        p, t = score["passes"][k]
        rate = p / t if t else 1.0
        tag = "exact " if k in _DETERMINISTIC else "soft  "
        flag = "" if (k not in _DETERMINISTIC or rate >= _PASS_THRESHOLD) else "  <-- BELOW THRESHOLD"
        print(f"  [{tag}] {k:22} {p:3}/{t:<3} {rate*100:5.1f}%{flag}")
        if k in _DETERMINISTIC and rate < _PASS_THRESHOLD:
            det_ok = False
    if score["failures"]:
        print("\n  deterministic failures (fixture | metric | expected | measured):")
        for name, k, exp, got in score["failures"][:25]:
            print(f"    {name:28} {k:18} exp={exp} got={got}")
    print(f"\n  deterministic metrics {'PASS' if det_ok else 'FAIL'}")
    return det_ok


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else None
    ok = _print(asyncio.run(run(n)))
    sys.exit(0 if ok else 1)
