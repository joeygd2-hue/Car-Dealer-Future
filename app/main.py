"""FastAPI application factory."""
from __future__ import annotations

import logging
import time
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from sqlalchemy import text

from app.config import get_settings
from app.db import SessionLocal
from app.security.rate_limit import limiter
from app.security.sensitive import RedactingFilter

log = logging.getLogger("app")


def configure_logging() -> None:
    s = get_settings()
    root = logging.getLogger()
    if not any(isinstance(f, RedactingFilter) for h in root.handlers for f in h.filters):
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
        handler.addFilter(RedactingFilter())
        root.handlers = [handler]
    root.setLevel(s.log_level)


def create_app() -> FastAPI:
    s = get_settings()
    if s.is_production:
        problems = s.production_problems()
        if problems:
            raise RuntimeError("Refusing to start in production: " + "; ".join(problems))
    configure_logging()
    app = FastAPI(
        title="Dealer Growth Platform",
        docs_url=None if s.is_production else "/api/docs",
        redoc_url=None,
        openapi_url=None if s.is_production else "/api/openapi.json",
    )
    app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    @app.middleware("http")
    async def request_context(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
        request.state.request_id = rid
        ip = request.client.host if request.client else "unknown"
        path = request.url.path
        if path.startswith("/api/") and not limiter.allow(f"api:{ip}", s.api_rate_limit_per_minute):
            return JSONResponse({"detail": "Rate limit exceeded"}, status_code=429, headers={"Retry-After": "60"})
        if path == "/login" and request.method == "POST" and not limiter.allow(f"login:{ip}", s.login_rate_limit_per_minute):
            return JSONResponse({"detail": "Too many login attempts. Try again in a minute."}, status_code=429, headers={"Retry-After": "60"})
        start = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = rid
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; form-action 'self'; base-uri 'none'"
        )
        if s.is_production:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        if path.startswith(("/app", "/portal", "/api/")):
            response.headers["Cache-Control"] = "no-store"
        log.info("%s %s %s %dms rid=%s", request.method, path, response.status_code, int((time.monotonic() - start) * 1000), rid)
        return response

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        if exc.status_code == 303 and exc.headers and "Location" in exc.headers:
            return RedirectResponse(exc.headers["Location"], status_code=303)
        if request.url.path.startswith("/api/") or "text/html" not in request.headers.get("accept", ""):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=exc.headers)
        from app.web.common import TEMPLATES

        return TEMPLATES.TemplateResponse(request, "error.html", {"status": exc.status_code, "detail": exc.detail, "user": None}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        errors = [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]
        return JSONResponse({"detail": "Invalid request", "errors": errors}, status_code=422)

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", "-")
        log.exception("Unhandled error rid=%s: %s", rid, type(exc).__name__)
        return JSONResponse({"detail": "Internal server error", "request_id": rid}, status_code=500)

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    def readyz():
        from app.models.core import WorkerHeartbeat
        from app.models.base import utcnow
        from datetime import timedelta

        db = SessionLocal()
        try:
            db.execute(text("SELECT 1"))
            hb = db.query(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc()).first()
            worker_ok = bool(hb and hb.last_seen_at > utcnow() - timedelta(minutes=2))
            return {"status": "ready", "database": "ok", "worker": "ok" if worker_ok else "no recent heartbeat"}
        except Exception:  # noqa: BLE001
            return JSONResponse({"status": "not ready", "database": "error"}, status_code=503)
        finally:
            db.close()

    from app.web import api, auth_routes, pages, portal, public

    app.include_router(auth_routes.router)
    app.include_router(public.router)
    app.include_router(api.router)
    app.include_router(pages.router)
    app.include_router(portal.router)
    _register_job_handlers()
    return app


def _register_job_handlers() -> None:
    # Importing these modules registers their @handler functions.
    import app.services.agent.runner  # noqa: F401
    import app.services.maintenance  # noqa: F401
    import app.services.seo.audit  # noqa: F401
