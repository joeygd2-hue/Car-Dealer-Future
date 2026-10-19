"""Errors raised by Phase 2 services. Routes turn them into JSON (API) or a flash message (pages)."""

from __future__ import annotations

from typing import Any


class DealerError(Exception):
    status = 400

    def __init__(self, message: str, details: Any = None):
        super().__init__(message)
        self.message = message
        self.details = details


class BadRequest(DealerError):
    status = 400


class Forbidden(DealerError):
    status = 403


class NotFound(DealerError):
    """Also used for another dealership's ids, so they cannot be probed."""
    status = 404


class Conflict(DealerError):
    status = 409


class Unprocessable(DealerError):
    status = 422
