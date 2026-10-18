"""SSRF guard for server-side fetches of user-supplied URLs (inventory feeds, prospect websites)."""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse

from .dealer_errors import BadRequest


def assert_public_url(url: str, *, require_https: bool = True) -> None:
    u = urlparse(url)
    if require_https and u.scheme != "https":
        raise BadRequest("URL must use https")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise BadRequest("Invalid URL")
    host = u.hostname
    if host.endswith((".example", ".invalid", ".test", ".localhost")) or host == "localhost":
        if host.endswith(".example"):
            return  # reserved fixture domains are served from local fixtures, never fetched
        raise BadRequest("URL host is not allowed")
    try:
        infos = socket.getaddrinfo(host, u.port or (443 if u.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror:
        return  # unresolvable: the fetch itself will fail
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            raise BadRequest("URL resolves to a private or internal address")
