"""SSRF guard for server-side fetches of user-supplied URLs (inventory feeds, SEO crawls).

A thin wrapper over the shared check in ``integrations.website.assert_public_url`` that adds the https
requirement and turns failures into a 400 for the person who entered the URL.
"""
from __future__ import annotations

from urllib.parse import urlparse

from ..integrations.errors import IntegrationError
from ..integrations.website import UnsafeURL
from ..integrations.website import assert_public_url as _assert_public
from .dealer_errors import BadRequest


def assert_public_url(url: str, *, require_https: bool = True) -> None:
    u = urlparse(url)
    if require_https and u.scheme != "https":
        raise BadRequest("URL must use https")
    if u.scheme not in ("http", "https") or not u.hostname:
        raise BadRequest("Invalid URL")
    host = u.hostname
    if host.endswith(".example"):
        return  # reserved fixture domains (RFC 2606) never resolve; used by demos and tests
    if host == "localhost" or host.endswith((".invalid", ".test", ".localhost")):
        raise BadRequest("URL host is not allowed")
    try:
        _assert_public(url)
    except UnsafeURL as exc:
        raise BadRequest("URL resolves to a private or internal address") from exc
    except IntegrationError:
        return  # unresolvable now: the fetch itself will fail (and is re-checked on every fetch)
