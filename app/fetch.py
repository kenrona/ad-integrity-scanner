"""Async HTTP fetching for the static tier.

A fetch failure is *data*, not a crash: callers get a FetchResult with ok=False
and an error string, and the scan still produces a (low-confidence) record.

Security/efficiency posture:
  * SSRF — every hop (initial + each redirect) is host-validated (see app.ssrf).
    Redirects are followed manually with `follow_redirects=False` so an attacker
    cannot 302 us to an internal address.
  * Memory — the body is streamed and aborted once `max_bytes` is exceeded, so a
    hostile multi-GB ads.txt / page can never exhaust worker memory.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlsplit

import httpx

from app.config import get_settings
from app.ssrf import SSRFError, assert_public_host

MAX_HTML_BYTES = 3_000_000
MAX_TEXT_BYTES = 1_000_000  # ads.txt / robots.txt are small; cap defensively
_REDIRECT_STATUS = {301, 302, 303, 307, 308}

_DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=10.0, write=5.0, pool=5.0)
_DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20)

_KEEP_HEADERS = (
    "server", "x-powered-by", "content-security-policy",
    "strict-transport-security", "x-frame-options", "content-type",
)


@dataclass
class FetchResult:
    url: str
    ok: bool
    status: int | None = None
    final_url: str | None = None
    https: bool = False
    content_type: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    text: str = ""
    truncated: bool = False
    elapsed_ms: int | None = None
    error: str | None = None


def make_client() -> httpx.AsyncClient:
    # follow_redirects=False: we follow manually so each hop is SSRF-checked.
    return httpx.AsyncClient(
        headers={"User-Agent": get_settings().user_agent},
        timeout=_DEFAULT_TIMEOUT,
        limits=_DEFAULT_LIMITS,
        follow_redirects=False,
    )


async def _read_capped(resp: httpx.Response, max_bytes: int) -> tuple[bytes, bool]:
    chunks: list[bytes] = []
    total = 0
    truncated = False
    async for chunk in resp.aiter_bytes():
        chunks.append(chunk)
        total += len(chunk)
        if total >= max_bytes:
            truncated = True
            break  # abort download — connection closes on context exit
    return b"".join(chunks)[:max_bytes], truncated


async def fetch(
    client: httpx.AsyncClient, url: str, *,
    max_bytes: int = MAX_HTML_BYTES, max_redirects: int = 5,
) -> FetchResult:
    started = time.monotonic()
    current = url
    try:
        for _ in range(max_redirects + 1):
            parts = urlsplit(current)
            port = parts.port or (443 if parts.scheme == "https" else 80)
            await assert_public_host(parts.hostname, port)

            req = client.build_request("GET", current)
            resp = await client.send(req, stream=True, follow_redirects=False)
            try:
                loc = resp.headers.get("location")
                if resp.status_code in _REDIRECT_STATUS and loc:
                    current = urljoin(current, loc)
                    continue
                body, truncated = await _read_capped(resp, max_bytes)
            finally:
                await resp.aclose()

            text = body.decode(resp.encoding or "utf-8", errors="replace")
            final = str(resp.url)
            kept = {k: v for k, v in resp.headers.items() if k.lower() in _KEEP_HEADERS}
            return FetchResult(
                url=url, ok=resp.is_success, status=resp.status_code,
                final_url=final, https=final.startswith("https://"),
                content_type=resp.headers.get("content-type"),
                headers=kept, text=text, truncated=truncated,
                elapsed_ms=int((time.monotonic() - started) * 1000),
                error=None if resp.is_success else f"HTTP {resp.status_code}",
            )
        return FetchResult(url=url, ok=False, error="too many redirects")
    except SSRFError as e:
        return FetchResult(url=url, ok=False, error=f"SSRFError: {e}")
    except httpx.HTTPError as e:
        return FetchResult(url=url, ok=False, error=f"{type(e).__name__}: {e}")
    except Exception as e:  # noqa: BLE001
        return FetchResult(url=url, ok=False, error=f"{type(e).__name__}: {e}")
