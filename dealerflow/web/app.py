"""Application factory."""

from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.sessions import SessionMiddleware

from ..config import get_settings
from ..logging_setup import configure_logging
from ..services.dealer_errors import DealerError
from ..tenancy import TenantViolation
from .deps import DealerHome, NotAuthenticated, back_to, flash, templates

log = logging.getLogger("dealerflow.web")


def create_app() -> FastAPI:
    s = get_settings()
    configure_logging(s.log_level)
    from .. import handlers  # noqa: F401  (job handlers must be registered for enqueue-and-run paths)

    app = FastAPI(title="dealerflow", docs_url="/api/docs" if s.env != "production" else None, redoc_url=None)
    app.add_middleware(SessionMiddleware, secret_key=s.require_secret_key(), session_cookie="df_session",
                       max_age=s.session_max_age_seconds, same_site="lax", https_only=s.env == "production")

    @app.middleware("http")
    async def request_log(request: Request, call_next):
        rid = uuid.uuid4().hex[:12]
        start = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")
        if s.env == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if request.url.path not in ("/healthz",) and not request.url.path.startswith("/static"):
            response.headers["Cache-Control"] = "no-store"
        if not request.url.path.startswith("/static"):
            path = request.url.path
            if path.startswith("/u/"):
                path = "/u/[token]"  # unsubscribe tokens carry the recipient's email
            log.info("request", extra={"rid": rid, "method": request.method, "path": path,
                                       "status": response.status_code, "ms": int((time.monotonic() - start) * 1000)})
        return response

    @app.exception_handler(NotAuthenticated)
    async def _login_redirect(request: Request, exc: NotAuthenticated):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.exception_handler(DealerHome)
    async def _dealer_home(request: Request, exc: DealerHome):
        return RedirectResponse(f"/dealers/{exc.client_id}", status_code=303)

    @app.exception_handler(DealerError)
    async def _dealer_error(request: Request, exc: DealerError):
        if request.url.path.startswith("/api/") or request.method == "GET":
            if request.url.path.startswith("/api/"):
                return JSONResponse({"detail": exc.message, "errors": exc.details}, status_code=exc.status)
            return templates.TemplateResponse(request, "error.html", {"status": exc.status, "detail": exc.message, "user": None,
                                                                      "csrf": "", "flash": None}, status_code=exc.status)
        details = exc.details or {}
        extra = details.get("blockers") or details.get("problems") or details.get("validation_errors") if isinstance(details, dict) else None
        flash(request, exc.message + (": " + "; ".join(extra) if extra else ""), "error")
        return RedirectResponse(back_to(request, "/dealers"), status_code=303)

    @app.exception_handler(TenantViolation)
    async def _tenant_violation(request: Request, exc: TenantViolation):
        log.warning("tenant violation", extra={"path": request.url.path, "error": str(exc)})
        return JSONResponse({"detail": "Cross-dealership access denied"}, status_code=403)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith(("/api/", "/webhooks/", "/healthz")) or "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(request, "error.html", {"status": exc.status_code, "detail": exc.detail,
                                                                  "user": None, "csrf": "", "flash": None},
                                          status_code=exc.status_code)

    app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parents[1] / "static")), name="static")
    from . import (routes_agent, routes_api, routes_auth, routes_crm, routes_dealer_api, routes_dealers, routes_outreach,
                   routes_public, routes_settings)

    for mod in (routes_auth, routes_public, routes_crm, routes_outreach, routes_agent, routes_settings, routes_api,
                routes_dealers, routes_dealer_api):
        app.include_router(mod.router)
    return app
