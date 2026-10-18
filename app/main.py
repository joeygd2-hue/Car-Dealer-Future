"""FastAPI application factory — the single application shared by all four phases."""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import HTTPException as FastAPIHTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings, validate_settings
from app.db import get_db
from app.security.auth import LoginRequired, MfaEnrollmentRequired, MfaRequired

log = logging.getLogger("app")

CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "connect-src 'self'; form-action 'self'; base-uri 'self'; frame-ancestors 'none'; object-src 'none'")


def create_app() -> FastAPI:
    import app.worker as worker_mod

    settings = get_settings()
    for warning in validate_settings(settings):  # raises ConfigError on unsafe configuration
        log.warning("config: %s", warning)
    worker_mod.register_all_handlers()  # jobs enqueued by web requests must have known types
    application = FastAPI(title=settings.app_name, docs_url=None if settings.is_production else "/docs", redoc_url=None,
                          openapi_url=None if settings.is_production else "/openapi.json")
    application.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    from app.web import admin, auth_routes, pilot, portal, public

    application.include_router(auth_routes.router)
    application.include_router(public.router)
    application.include_router(portal.router)
    application.include_router(admin.router)
    application.include_router(pilot.router)

    @application.exception_handler(LoginRequired)
    async def _login_redirect(request: Request, exc: LoginRequired):
        return RedirectResponse("/login", status_code=303)

    @application.exception_handler(MfaRequired)
    async def _mfa_redirect(request: Request, exc: MfaRequired):
        return RedirectResponse("/login/mfa", status_code=303)

    @application.exception_handler(MfaEnrollmentRequired)
    async def _mfa_enroll_redirect(request: Request, exc: MfaEnrollmentRequired):
        return RedirectResponse("/account/security", status_code=303)

    @application.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        # Never leak stack traces, SQL or secrets to the client; log with a reference id instead.
        if isinstance(exc, FastAPIHTTPException):
            raise exc
        ref = uuid.uuid4().hex[:12]
        log.exception("unhandled error ref=%s path=%s", ref, request.url.path)
        if request.url.path.startswith(("/api/", "/webhooks/")):
            return JSONResponse({"error": "internal error", "reference": ref}, status_code=500)
        from app.web.deps import TEMPLATES

        return TEMPLATES.TemplateResponse(request, "error.html", {"request_id": ref, "user": None, "settings": settings,
                                                                  "platform_status": None, "csrf_token": ""},
                                          status_code=500)

    @application.middleware("http")
    async def _security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        path = request.url.path
        if not path.startswith(("/api/chat", "/static/")):
            resp.headers.setdefault("Content-Security-Policy", CSP)
            resp.headers.setdefault("X-Frame-Options", "DENY")
        if path.startswith(("/portal", "/admin", "/account", "/login", "/password")):
            resp.headers.setdefault("Cache-Control", "no-store")
        if settings.is_production:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        return resp

    @application.get("/")
    def root():
        return RedirectResponse("/portal", status_code=303)

    @application.get("/healthz")
    def healthz(db: Session = Depends(get_db)):
        db.execute(text("SELECT 1"))
        return {"ok": True}

    @application.get("/healthz/worker")
    def healthz_worker(db: Session = Depends(get_db)):
        health = worker_mod.worker_health(db)
        return JSONResponse(health, status_code=200 if health["healthy"] else 503)

    return application


app = create_app()
