"""Ground-truth fixture generator for the accuracy suite.

Each fixture is a self-contained HTML page whose ad layout we fully control, so
we KNOW the true values. A googletag stub drives the scanner's primary GPT
detection path (getSlots + slotRenderEnded), and ads are placed at known sizes /
positions with kind-specific styling (sticky, hidden, tiny, off-screen,
interstitial, empty). Decoys and dormant CMPs probe for over-/under-counting.

`make_suite()` returns a list of {name, html, truth}. `truth` keys mirror the
scanner's flattened `metrics` so the harness can compare directly.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

VW, VH = 1366, 768
PAGE_H = 6000  # fixed body height so A2CR is deterministic

# Category -> unambiguous body text (zero-shot classifier target).
CATEGORY_TEXT = {
    "Sports": "The football team won the championship game in overtime. Their coach "
              "praised the quarterback and the defense after a thrilling playoff season "
              "of matches, tournaments, and league standings across the division.",
    "Food & Drink": "This easy weeknight pasta recipe simmers garlic and olive oil with "
              "fresh basil. A great dinner; for dessert bake a lemon meringue pie. Cooking "
              "tips, ingredients, and restaurant-quality meals you can make at home.",
    "Technology": "The new smartphone ships with a faster chip, better software, and an "
              "improved app ecosystem. Startups and gadget reviewers cover the latest "
              "computing devices, internet trends, and artificial intelligence features.",
    "Travel": "Discover the best beaches and hotels for your next vacation. This travel "
              "guide covers flights, destinations, itineraries, and tourism tips for an "
              "unforgettable trip abroad with sightseeing and local cuisine.",
    "Health": "Doctors explain the symptoms and treatment of the disease. Wellness, diet, "
              "fitness, and medicine advice to improve your health and manage chronic "
              "conditions with evidence-based medical guidance.",
}
# Suitability probes: text -> expected tier ('low' for benign / topical mentions).
SUITABILITY_TEXT = {
    "low_clean": (CATEGORY_TEXT["Food & Drink"], "low"),
    "low_topical": ("A documentary about the history of online advertising and the adult "
                    "industry's early adoption of banner ads and pop-ups in the 1990s web.", "low"),
    "flagged_adult": ("Explicit hardcore pornographic XXX adult videos and nude webcam "
                      "shows streaming uncensored content now.", ("high", "floor")),
}


@dataclass
class Ad:
    w: int
    h: int
    top: int
    kind: str = "normal"   # normal|sticky|hidden|tiny|offscreen|interstitial|empty|refresh
    left: int = 40


@dataclass
class Spec:
    name: str
    ads: list[Ad] = field(default_factory=list)
    cmp: str | None = None          # None|'tcf_live'|'tcf_locator'|'gpp_locator'|vendor name
    category: str | None = None
    suitability_key: str | None = None
    video: str | None = None        # None|'autoplay_muted'|'autoplay_unmuted'|'static'
    decoys: int = 0


_VENDOR_URL = {
    "onetrust": "https://cdn.cookielaw.org/scripttemplates/otSDKStub.js",
    "google-funding-choices": "https://fundingchoicesmessages.google.com/i/pub-0000?ers=1",
    "sourcepoint": "https://cdn.sp-prod.net/wrapperMessagingWithoutDetection.js",
    "didomi": "https://sdk.privacy-center.org/loader.js",
    "quantcast/iab": "https://quantcast.mgr.consensu.org/cmp.js",
}


def _ad_div(i: int, ad: Ad) -> str:
    base = f"position:absolute;left:{ad.left}px;top:{ad.top}px;background:#ccd;"
    if ad.kind == "empty":
        style = "position:absolute;width:0;height:0;overflow:hidden;"
        wh = ""
    elif ad.kind == "hidden":
        style = base + f"width:{ad.w}px;height:{ad.h}px;visibility:hidden;"
        wh = ""
    elif ad.kind == "offscreen":
        style = f"position:absolute;left:-9999px;top:{ad.top}px;width:{ad.w}px;height:{ad.h}px;"
        wh = ""
    elif ad.kind == "tiny":
        style = f"position:absolute;left:{ad.left}px;top:{ad.top}px;width:1px;height:1px;"
        wh = ""
    elif ad.kind == "sticky":
        style = f"position:fixed;left:{ad.left}px;bottom:0;width:{ad.w}px;height:{ad.h}px;background:#ccd;"
        wh = ""
    elif ad.kind == "interstitial":
        style = f"position:fixed;left:0;top:0;width:{VW}px;height:{VH}px;z-index:9999;background:#ccd;"
        wh = ""
    else:  # normal / refresh
        style = base + f"width:{ad.w}px;height:{ad.h}px;"
        wh = ""
    return f'<div id="div-gpt-ad-{i}" style="{style}">ad{i}{wh}</div>'


def build_page(spec: Spec) -> tuple[str, dict]:
    slot_ids = [f"div-gpt-ad-{i}" for i in range(len(spec.ads))]
    ad_divs = "\n".join(_ad_div(i, a) for i, a in enumerate(spec.ads))
    decoys = "\n".join(f'<div class="advertisement ad-slot">promoted</div>' for _ in range(spec.decoys))

    refresh_js = "".join(
        f"setTimeout(function(){{fire('div-gpt-ad-{i}');}},2500);"
        for i, a in enumerate(spec.ads) if a.kind == "refresh"
    )
    gpt_stub = f"""
<script>
(function(){{
  var SLOTS={slot_ids!r};
  var slots=SLOTS.map(function(id){{return {{getSlotElementId:function(){{return id;}}}};}});
  var L={{}};
  var pubads={{getSlots:function(){{return slots;}},
    addEventListener:function(ev,cb){{(L[ev]=L[ev]||[]).push(cb);}}, refresh:function(){{}}}};
  var gt=window.googletag=window.googletag||{{cmd:[]}};
  gt.pubads=function(){{return pubads;}};
  var q=(gt.cmd&&gt.cmd.length)?gt.cmd.slice():[];
  gt.cmd={{push:function(fn){{try{{fn();}}catch(e){{}}}}}};
  q.forEach(function(fn){{try{{fn();}}catch(e){{}}}});
  function fire(id){{(L['slotRenderEnded']||[]).forEach(function(cb){{cb({{slot:{{getSlotElementId:function(){{return id;}}}}}});}});}}
  SLOTS.forEach(fire);
  {refresh_js}
}})();
</script>"""

    cmp_html = ""
    if spec.cmp == "tcf_live":
        cmp_html = "<script>window.__tcfapi=function(){};</script>"
    elif spec.cmp == "tcf_locator":
        cmp_html = '<iframe name="__tcfapiLocator" style="display:none"></iframe>'
    elif spec.cmp == "gpp_locator":
        cmp_html = '<iframe name="__gppLocator" style="display:none"></iframe>'
    elif spec.cmp in _VENDOR_URL:
        cmp_html = f'<script src="{_VENDOR_URL[spec.cmp]}"></script>'

    video_html = ""
    if spec.video == "autoplay_muted":
        video_html = '<video autoplay muted src="x.mp4" width="640" height="360"></video>'
    elif spec.video == "autoplay_unmuted":
        video_html = '<video autoplay src="x.mp4" width="640" height="360"></video>'
    elif spec.video == "static":
        video_html = '<video src="x.mp4" width="640" height="360"></video>'

    text = ""
    if spec.suitability_key:
        text = SUITABILITY_TEXT[spec.suitability_key][0]
    elif spec.category:
        text = CATEGORY_TEXT[spec.category]
    body_text = f'<div style="position:absolute;top:50px;left:500px;width:700px">{text}</div>' if text else ""

    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>{spec.name}</title><meta name="viewport" content="width=device-width">
{cmp_html}</head>
<body style="margin:0">
<div style="height:{PAGE_H}px;position:relative">
{ad_divs}
{decoys}
{body_text}
{video_html}
</div>
{gpt_stub}
</body></html>"""

    return html, _truth(spec)


def _truth(spec: Spec) -> dict:
    filled = [a for a in spec.ads if a.kind not in ("empty",)]  # area>0 (incl hidden/tiny/offscreen/sticky/interstitial)
    visible_for_area = [a for a in spec.ads if a.kind in ("normal", "refresh", "sticky", "interstitial", "hidden", "tiny", "offscreen")]
    def area(a): return (1 if a.kind == "tiny" else a.w) * (1 if a.kind == "tiny" else a.h) if a.kind != "interstitial" else VW * VH
    ad_area = sum(area(a) for a in visible_for_area)
    sizes: dict[str, int] = {}
    for a in filled:
        if a.kind == "tiny":
            key = "1x1"
        elif a.kind == "interstitial":
            key = f"{VW}x{VH}"
        else:
            key = f"{a.w}x{a.h}"
        sizes[key] = sizes.get(key, 0) + 1
    # Fold counts exclude hidden/off-screen (not part of the visible fold).
    placed = [a for a in filled if a.kind not in ("hidden", "offscreen")]
    def is_above(a):
        if a.kind in ("sticky", "interstitial"):
            return True
        return a.top < VH
    above = sum(1 for a in placed if is_above(a))
    truth = {
        "ad_slot_count": len(spec.ads),
        "filled_slot_count": len(filled),
        "empty_slot_count": sum(1 for a in spec.ads if a.kind == "empty"),
        "above_fold_ads": above,
        "below_fold_ads": len(placed) - above,
        "sticky_ad_count": sum(1 for a in spec.ads if a.kind == "sticky"),
        "hidden_ad_count": sum(1 for a in spec.ads if a.kind == "hidden"),
        "tiny_ad_count": sum(1 for a in spec.ads if a.kind == "tiny"),
        "offscreen_ad_count": sum(1 for a in spec.ads if a.kind == "offscreen"),
        "interstitial": any(a.kind == "interstitial" for a in spec.ads),
        "ad_sizes": sizes,
        "a2cr": round(ad_area / (VW * PAGE_H), 3),
        "ad_refreshing": any(a.kind == "refresh" for a in spec.ads),
        "cmp_present": spec.cmp is not None,
        "cmp_vendor": spec.cmp if spec.cmp in _VENDOR_URL else None,
        "content_category": spec.category,
        "suitability_tier": (SUITABILITY_TEXT[spec.suitability_key][1] if spec.suitability_key else None),
        "video_autoplay": 1 if spec.video in ("autoplay_muted", "autoplay_unmuted") else (0 if spec.video else None),
    }
    return truth


def make_suite() -> list[dict]:
    """A few hundred tricky fixtures across the parameter matrix."""
    specs: list[Spec] = []
    sizes = [(300, 250), (728, 90), (300, 600), (970, 250), (320, 50), (160, 600)]
    cmps = [None, "tcf_live", "tcf_locator", "gpp_locator", "onetrust",
            "google-funding-choices", "sourcepoint", "didomi"]
    cats = list(CATEGORY_TEXT)

    n = 0
    # Core matrix: ad count x size x category, cmp rotating (~180 fixtures).
    for count in (1, 2, 3, 4, 5, 6):
        for (w, h) in sizes:
            for cat in cats:
                cmp = cmps[n % len(cmps)]
                ads = [Ad(w, h, top=60 + i * 900) for i in range(count)]  # spread down page
                specs.append(Spec(f"core_{count}x{w}x{h}_{cmp}_{cat}_{n}", ads=ads,
                                  cmp=cmp, category=cat, decoys=n % 3))
                n += 1

    # Tricky / GIVT / edge cases.
    tricky = [
        Spec("givt_hidden", ads=[Ad(300, 250, 60), Ad(300, 250, 60, "hidden")], cmp="onetrust", category="Technology"),
        Spec("givt_tiny_pixel", ads=[Ad(728, 90, 60), Ad(1, 1, 200, "tiny")], category="News" if False else "Sports"),
        Spec("givt_offscreen", ads=[Ad(300, 250, 60), Ad(300, 250, 300, "offscreen")], cmp="gpp_locator", category="Travel"),
        Spec("givt_stacked", ads=[Ad(300, 250, 100, left=40), Ad(300, 250, 110, left=60)], category="Health"),
        Spec("sticky_anchor", ads=[Ad(728, 90, 60), Ad(320, 50, 0, "sticky")], cmp="sourcepoint", category="Food & Drink"),
        Spec("interstitial_overlay", ads=[Ad(VW, VH, 0, "interstitial"), Ad(300, 250, 60)], category="Technology"),
        Spec("all_empty_placeholders", ads=[Ad(0, 0, 60, "empty"), Ad(0, 0, 120, "empty")], category="Travel"),
        Spec("decoys_only", ads=[Ad(300, 250, 60)], decoys=6, category="Sports"),
        Spec("below_fold_only", ads=[Ad(300, 250, 2000), Ad(300, 250, 3000)], category="Health"),
        Spec("above_and_below", ads=[Ad(728, 90, 60), Ad(300, 250, 400), Ad(300, 250, 2500)], cmp="didomi", category="Technology"),
        Spec("refresh_slot", ads=[Ad(300, 250, 60, "refresh"), Ad(728, 90, 500)], category="Food & Drink"),
        Spec("heavy_clutter", ads=[Ad(300, 250, 60 + i * 350) for i in range(12)], cmp="onetrust", category="Technology"),
        Spec("suit_clean", ads=[Ad(300, 250, 60)], suitability_key="low_clean"),
        Spec("suit_topical_mention", ads=[Ad(300, 250, 60)], suitability_key="low_topical"),
        Spec("suit_flagged_adult", ads=[Ad(300, 250, 60)], suitability_key="flagged_adult"),
        Spec("video_autoplay_unmuted", ads=[Ad(300, 250, 60)], video="autoplay_unmuted", category="Entertainment" if False else "Technology"),
        Spec("video_autoplay_muted", ads=[Ad(728, 90, 60)], video="autoplay_muted", category="Travel"),
        Spec("no_ads_clean_article", ads=[], cmp="onetrust", category="Health"),
    ]
    specs.extend(tricky)

    out = []
    for s in specs:
        html, truth = build_page(s)
        out.append({"name": s.name, "html": html, "truth": truth})
    return out
