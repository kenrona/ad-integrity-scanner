"""Static HTML signal extraction (no browser).

Detects ad-tech / video / content signals that are observable in the raw markup.
Anything injected at runtime (real ad slots, bid responses, CMP state) is left to
the render tier (Phase 2). Detections here are therefore *declared/likely* signals.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

import tldextract
from selectolax.parser import HTMLParser

from app import content as content_mod

_extract = tldextract.TLDExtract(suffix_list_urls=())

# Substring fingerprints -> normalized vendor/tech name. Matched against the
# lowercased HTML (covers inline script, src attrs, and config blobs).
_AD_TECH = {
    "gpt.js": "google-gpt", "googletag": "google-gpt",
    "securepubads": "google-gpt", "pagead2.googlesyndication": "google-adsense",
    "prebid": "prebid", "pbjs": "prebid",
    "amazon-adsystem": "amazon-aps", "apstag": "amazon-aps",
    "criteo": "criteo", "rubiconproject": "magnite", "pubmatic": "pubmatic",
    "openx": "openx", "casalemedia": "indexexchange", "indexexchange": "indexexchange",
    "adnxs": "xandr", "33across": "33across", "sharethrough": "sharethrough",
    "smartadserver": "smart", "adform": "adform", "teads": "teads",
}
# Native-content / recommendation widgets — arbitrage precursors (MFA-relevant, Phase 3).
_NATIVE_WIDGETS = {
    "taboola": "taboola", "outbrain": "outbrain",
    "revcontent": "revcontent", "mgid": "mgid", "zergnet": "zergnet",
}
# Video player fingerprints.
_VIDEO_PLAYERS = {
    "jwplayer": "jwplayer", "jwpltx": "jwplayer",
    "videojs": "videojs", "video.js": "videojs",
    "brightcove": "brightcove", "bc.js": "brightcove",
    "hls.js": "hlsjs", "flowplayer": "flowplayer", "kaltura": "kaltura",
}


def _registrable(netloc: str) -> str | None:
    host = netloc.split("@")[-1].split(":")[0].lower()
    if not host:
        return None
    return _extract(host).registered_domain or host


def _detect(haystack: str, table: dict[str, str]) -> list[str]:
    found = {v for k, v in table.items() if k in haystack}
    return sorted(found)


def parse_html(html: str, *, page_domain: str | None = None) -> dict[str, Any]:
    tree = HTMLParser(html or "")

    title_node = tree.css_first("title")
    title = title_node.text(strip=True) if title_node else None

    def meta(name: str, attr: str = "name") -> str | None:
        node = tree.css_first(f'meta[{attr}="{name}"]')
        return node.attributes.get("content") if node else None

    html_node = tree.css_first("html")
    lang = html_node.attributes.get("lang") if html_node else None

    # Build a "code haystack" from script src/inline-text + iframe/link URLs so
    # fingerprints match technology, NOT brand names mentioned in article prose.
    code_parts: list[str] = []
    ext_domains: set[str] = set()
    script_count = 0
    for s in tree.css("script"):
        script_count += 1
        src = s.attributes.get("src")
        if src:
            code_parts.append(src)
            if "//" in src:
                d = _registrable(urlsplit(src if "://" in src else "https:" + src).netloc)
                if d and d != page_domain:
                    ext_domains.add(d)
        inline = s.text()
        if inline:
            code_parts.append(inline)
    iframe_count = 0
    embedded_video = set()
    for f in tree.css("iframe"):
        iframe_count += 1
        src = (f.attributes.get("src") or "").lower()
        code_parts.append(src)
        if "youtube" in src or "youtu.be" in src:
            embedded_video.add("youtube")
        elif "vimeo" in src:
            embedded_video.add("vimeo")
    for link in tree.css("link[href]"):
        code_parts.append(link.attributes.get("href") or "")

    code = " ".join(code_parts).lower()

    # Rough content volume: visible body text word count.
    body = tree.css_first("body")
    text = body.text(separator=" ", strip=True) if body else ""
    word_count = len(text.split())

    # Content-quality / templating signals (thin + link-dense => MFA-leaning).
    paragraph_count = len(tree.css("p"))
    heading_count = len(tree.css("h1, h2, h3"))
    link_count = len(tree.css("a"))
    link_to_text_ratio = round(link_count / word_count, 4) if word_count else 0.0
    content_analysis = content_mod.analyze(text, title=title)

    ad_tech = _detect(code, _AD_TECH)
    native_widgets = _detect(code, _NATIVE_WIDGETS)
    video_players = _detect(code, _VIDEO_PLAYERS)
    native_video_tags = len(tree.css("video"))

    ad_related_ext = sorted(
        d for d in ext_domains
        if any(k in d for k in (
            "doubleclick", "googlesyndication", "adnxs", "criteo", "pubmatic",
            "rubicon", "openx", "casalemedia", "amazon-adsystem", "taboola",
            "outbrain", "33across", "sharethrough", "adform", "smartadserver",
        ))
    )

    return {
        "content": {
            "title_present": bool(title),
            "title": title,
            "meta_description_present": bool(meta("description")),
            "lang": lang,
            "has_viewport": bool(meta("viewport")),
            "canonical_present": bool(tree.css_first('link[rel="canonical"]')),
            "word_count": word_count,
            "script_count": script_count,
            "iframe_count": iframe_count,
            "external_script_domains": sorted(ext_domains),
            "external_script_domain_count": len(ext_domains),
            "ad_related_domain_count": len(ad_related_ext),
            "quality": {
                "paragraph_count": paragraph_count,
                "heading_count": heading_count,
                "link_count": link_count,
                "link_to_text_ratio": link_to_text_ratio,
            },
            "category": content_analysis["category"],
            "category_confidence": content_analysis["category_confidence"],
            "suitability": content_analysis["suitability"],
        },
        "ad_tech": {
            "vendors": ad_tech,
            "has_gpt": "google-gpt" in ad_tech,
            "has_prebid": "prebid" in ad_tech,
            "has_amazon_aps": "amazon-aps" in ad_tech,
            "ssp_count": len([v for v in ad_tech if v not in ("google-adsense",)]),
            "native_widgets": native_widgets,
        },
        "video": {
            "players": video_players,
            "native_video_tags": native_video_tags,
            "embedded": sorted(embedded_video),
            "has_video": bool(video_players or native_video_tags or embedded_video),
        },
    }
