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
from .deps import NotAuthenticated, templates

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
        if not request.url.path.startswith("/static"):
            log.info("request", extra={"rid": rid, "method": request.method, "path": request.url.path,
                                       "status": response.status_code, "ms": int((time.monotonic() - start) * 1000)})
        return response

    @app.exception_handler(NotAuthenticated)
    async def _login_redirect(request: Request, exc: NotAuthenticated):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": "Not authenticated"}, status_code=401)
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, exc: StarletteHTTPException):
        if request.url.path.startswith(("/api/", "/webhooks/", "/healthz")) or "application/json" in request.headers.get("accept", ""):
            return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
        return templates.TemplateResponse(request, "error.html", {"status": exc.status_code, "detail": exc.detail,
                                                                  "user": None, "csrf": "", "flash": None},
                                          status_code=exc.status_code)

    app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parents[1] / "static")), name="static")
    from . import routes_agent, routes_api, routes_auth, routes_crm, routes_outreach, routes_public, routes_settings

    for mod in (routes_auth, routes_public, routes_crm, routes_outreach, routes_agent, routes_settings, routes_api):
        app.include_router(mod.router)
    return app
