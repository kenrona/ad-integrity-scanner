"""URL normalization, hashing, and domain extraction.

Normalization is deterministic so that the same logical page maps to the same
``url_hash`` (the dedup/ledger key) regardless of cosmetic differences:
scheme/host casing, default ports, trailing slashes, fragment, tracking
parameters, and query-parameter ordering.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import tldextract

# Exact tracking keys + prefixes that never identify a distinct page.
_TRACKING_EXACT = {
    "gclid", "gclsrc", "dclid", "gbraid", "wbraid", "fbclid", "msclkid",
    "mc_eid", "mc_cid", "igshid", "vero_id", "yclid", "_hsenc", "_hsmi",
    "ref", "ref_src", "spm", "scid",
}
_TRACKING_PREFIXES = ("utm_", "pk_", "piwik_", "matomo_", "hsa_")

_DEFAULT_PORTS = {"http": "80", "https": "443"}

# Permissive but real hostname/IP guard: rejects whitespace and other junk that
# urlsplit otherwise passes through as a "host" (e.g. "not a url with spaces").
_HOST_RE = re.compile(r"^(?:\[[0-9a-fA-F:]+\]|[A-Za-z0-9](?:[A-Za-z0-9.\-]*[A-Za-z0-9])?)$")

# tldextract with no live PSL fetch — uses the bundled snapshot (offline, fast).
_extract = tldextract.TLDExtract(suffix_list_urls=())


@dataclass(frozen=True)
class NormalizedURL:
    url: str          # canonical URL string (the value we store / hash)
    url_hash: str     # sha256 hex of `url`
    host: str         # full hostname, lowercased (e.g. news.example.co.uk)
    domain: str       # registrable domain (e.g. example.co.uk), or host if none


def _is_tracking_param(key: str) -> bool:
    k = key.lower()
    return k in _TRACKING_EXACT or any(k.startswith(p) for p in _TRACKING_PREFIXES)


def normalize_url(raw: str, *, strip_tracking: bool = True) -> NormalizedURL:
    """Canonicalize a URL. Raises ValueError if it is not a usable http(s) URL."""
    if not raw or not raw.strip():
        raise ValueError("empty url")
    raw = raw.strip()

    # Default to https:// when no scheme is supplied.
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://", raw):
        raw = "https://" + raw

    parts = urlsplit(raw)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported scheme: {parts.scheme!r}")
    if not parts.hostname:
        raise ValueError("url has no host")

    host = parts.hostname.lower()
    if not _HOST_RE.match(host):
        raise ValueError(f"invalid host: {host!r}")

    # Drop default ports; keep non-default ports.
    netloc = host
    if parts.port and _DEFAULT_PORTS.get(scheme) != str(parts.port):
        netloc = f"{host}:{parts.port}"

    # Normalize path: collapse empty path to "/", strip a single trailing slash
    # on non-root paths so /a/ and /a are treated as the same page.
    path = parts.path or "/"
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/") or "/"

    # Filter + sort query params for a stable canonical form.
    query_pairs = parse_qsl(parts.query, keep_blank_values=True)
    if strip_tracking:
        query_pairs = [(k, v) for k, v in query_pairs if not _is_tracking_param(k)]
    query_pairs.sort()
    query = urlencode(query_pairs)

    # Fragment is always dropped — never identifies a distinct page server-side.
    canonical = urlunsplit((scheme, netloc, path, query, ""))

    ext = _extract(host)
    domain = ext.registered_domain or host

    url_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return NormalizedURL(url=canonical, url_hash=url_hash, host=host, domain=domain)
