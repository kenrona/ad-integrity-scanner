"""ads.txt / app-ads.txt parser (IAB ads.txt 1.1).

Data records are comma-separated: `<domain>, <publisher account id>, <DIRECT|RESELLER>[, <cert authority id>]`.
Variable lines are `KEY=VALUE` (CONTACT, SUBDOMAIN, OWNERDOMAIN, MANAGERDOMAIN, ...).
A data record never contains `=`, so the presence of `=` disambiguates the two
(and tolerates values that themselves contain commas, e.g. MANAGERDOMAIN=ex.com,US).
"""
from __future__ import annotations

from typing import Any

_VAR_KEYS = {
    "CONTACT", "SUBDOMAIN", "OWNERDOMAIN", "MANAGERDOMAIN", "INVENTORYPARTNERDOMAIN",
}


def parse_ads_txt(text: str) -> dict[str, Any]:
    records: list[dict[str, str]] = []
    variables: dict[str, list[str]] = {}
    malformed = 0

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            variables.setdefault(key.strip().upper(), []).append(value.strip())
            continue
        fields = [f.strip() for f in line.split(",")]
        if len(fields) < 3 or not fields[0] or not fields[1]:
            malformed += 1
            continue
        records.append({
            "ad_system": fields[0].lower(),
            "account_id": fields[1],
            "relationship": fields[2].upper(),
            "cert_authority": fields[3] if len(fields) >= 4 and fields[3] else None,
        })

    direct = sum(1 for r in records if r["relationship"] == "DIRECT")
    reseller = sum(1 for r in records if r["relationship"] == "RESELLER")
    rel_total = direct + reseller
    ad_systems = {r["ad_system"] for r in records}
    accounts = {(r["ad_system"], r["account_id"]) for r in records}

    return {
        "present": bool(records) or bool(variables),
        "records": records,            # raw lines, used for sellers.json resolution
        "total_records": len(records),
        "direct_count": direct,
        "reseller_count": reseller,
        # Share of authorized-direct relationships; higher = more transparent.
        "direct_ratio": round(direct / rel_total, 4) if rel_total else 0.0,
        "distinct_ad_systems": len(ad_systems),
        "ad_systems": sorted(ad_systems),     # for schain asi validation
        "distinct_accounts": len(accounts),
        "malformed_lines": malformed,
        "has_owner_domain": "OWNERDOMAIN" in variables,
        "has_manager_domain": "MANAGERDOMAIN" in variables,
        "subdomain_referrals": variables.get("SUBDOMAIN", []),
        "variable_keys": sorted(variables.keys()),
    }
