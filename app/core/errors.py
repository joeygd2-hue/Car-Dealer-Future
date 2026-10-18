from __future__ import annotations

from typing import Any


class AppError(Exception):
    status = 400
    code = "bad_request"
    default_message = "Request failed"

    def __init__(self, message: str | None = None, details: Any = None, *, status: int | None = None, code: str | None = None):
        message = message or self.default_message
        super().__init__(message)
        self.message = message
        self.details = details
        if status:
            self.status = status
        if code:
            self.code = code


class BadRequest(AppError):
    status, code = 400, "bad_request"


class Unauthorized(AppError):
    status, code = 401, "unauthorized"
    default_message = "Authentication required"


class Forbidden(AppError):
    status, code = 403, "forbidden"
    default_message = "You do not have access to this resource"


class NotFound(AppError):
    """Also used for cross-tenant access so ids cannot be probed."""
    status, code = 404, "not_found"
    default_message = "Not found"


class Conflict(AppError):
    status, code = 409, "conflict"


class Unprocessable(AppError):
    status, code = 422, "unprocessable"


class PolicyBlocked(AppError):
    """An action was refused by a safety/compliance policy."""
    status, code = 423, "policy_blocked"
