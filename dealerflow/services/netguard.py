"""SSRF guard for every server-side fetch of a user-supplied URL (inventory feeds, dealer websites).

Rules (strictest of the Phase 1.5 audit and Phase 2 implementations):
- http(s) only; https required unless the caller opts out (dealer websites may be plain http).
- Reserved fixture domains (*.example) are served from local fixtures and never fetched.
- Every address the host resolves to must be globally routable (``ip.is_global``): this rejects
  private, loopback, link-local (cloud metadata), CGNAT, reserved and multicast ranges.
- DNS failure fails closed.
Callers that follow redirects must re-check every hop (see integrations.website._get).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from .dealer_errors import BadRequest


class UnsafeURL(BadRequest):
    """The URL is not allowed for a server-side fetch."""


def assert_public_url(url: str, *, require_https: bool = True) -> None:
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise UnsafeURL("Invalid URL: only http(s) URLs with a host are allowed")
    if require_https and u.scheme != "https":
        raise UnsafeURL("URL must use https")
    host = u.hostname
    if host.endswith(".example"):
        return  # reserved fixture domains are served from local fixtures, never fetched
    if host.endswith((".invalid", ".test", ".localhost")) or host == "localhost":
        raise UnsafeURL("URL host is not allowed")
    try:
        infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise UnsafeURL(f"DNS lookup failed for {host}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global or ip.is_multicast:
            raise UnsafeURL(f"{host} resolves to a non-public address ({ip})")
