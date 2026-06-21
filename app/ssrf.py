"""SSRF protection.

The service fetches arbitrary user-supplied URLs, so every request target — the
initial URL AND every redirect hop — must be validated against private / loopback
/ link-local / reserved IP space (incl. the cloud metadata address 169.254.169.254).

Residual risk: a resolver that returns a public IP at check time and a private IP
at connect time (DNS rebinding) is not fully closed here — production should pin
the resolved IP for the connection or enforce egress filtering at the network.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket

from app.config import get_settings

_BLOCKED_HOSTNAMES = {"metadata.google.internal", "localhost"}


class SSRFError(ValueError):
    """Raised when a target host resolves to disallowed address space."""


def _allowlisted(host: str) -> bool:
    raw = get_settings().ssrf_allow_hosts
    if not raw:
        return False
    allow = {h.strip().lower() for h in raw.split(",") if h.strip()}
    return host.lower().strip("[]") in allow


def _ip_blocked(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return False
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def literal_host_blocked(host: str | None) -> bool:
    """Cheap, no-DNS check: blocked hostname or a literal private/reserved IP.

    Used by the render tier's request router on every subresource, where doing a
    DNS lookup per request would be prohibitively expensive.
    """
    if not host:
        return True
    h = host.lower().strip("[]")
    if _allowlisted(h):
        return False
    if h in _BLOCKED_HOSTNAMES:
        return True
    return _ip_blocked(h)


async def assert_public_host(host: str | None, port: int = 443) -> None:
    """Resolve `host` and raise SSRFError if any resolved address is disallowed."""
    if not host:
        raise SSRFError("missing host")
    h = host.lower().strip("[]")
    if _allowlisted(h):
        return
    if h in _BLOCKED_HOSTNAMES:
        raise SSRFError(f"blocked host: {h}")

    # Literal IP — check directly, no DNS.
    try:
        ipaddress.ip_address(h)
        if _ip_blocked(h):
            raise SSRFError(f"blocked ip: {h}")
        return
    except ValueError:
        pass

    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(h, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise SSRFError(f"dns resolution failed for {h}: {e}") from e
    if not infos:
        raise SSRFError(f"no addresses for {h}")
    for info in infos:
        ip = info[4][0]
        if _ip_blocked(ip):
            raise SSRFError(f"{h} resolves to blocked address {ip}")
