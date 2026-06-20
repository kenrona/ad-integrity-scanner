"""sellers.json parser (IAB sellers.json spec).

A sellers.json (hosted by an ad system at `/sellers.json`) lists the sellers it
represents. Each seller: `seller_id, name, domain, seller_type
(PUBLISHER|INTERMEDIARY|BOTH), is_confidential, is_passthrough`.

Returns aggregates + a compact `seller_id -> {t,d,c}` map for cross-resolution
against a publisher's ads.txt account IDs.
"""
from __future__ import annotations

import json
from typing import Any

_TYPES = ("PUBLISHER", "INTERMEDIARY", "BOTH", "OTHER")


def parse_sellers_json(text: str) -> dict[str, Any]:
    data = json.loads(text)  # raises ValueError on malformed JSON
    sellers = data.get("sellers", []) if isinstance(data, dict) else []
    type_counts = {t: 0 for t in _TYPES}
    confidential = passthrough = 0
    smap: dict[str, dict] = {}
    for s in sellers:
        if not isinstance(s, dict):
            continue
        st = str(s.get("seller_type", "")).upper()
        key = st if st in type_counts else "OTHER"
        type_counts[key] += 1
        is_conf = 1 if s.get("is_confidential") in (1, True) else 0
        confidential += is_conf
        if s.get("is_passthrough") in (1, True):
            passthrough += 1
        sid = str(s.get("seller_id", "")).strip()
        if sid:
            smap[sid] = {"t": key, "d": s.get("domain"), "c": is_conf}
    return {
        "seller_count": len(sellers),
        "type_counts": type_counts,
        "confidential_count": confidential,
        "passthrough_count": passthrough,
        "sellers": smap,
    }
