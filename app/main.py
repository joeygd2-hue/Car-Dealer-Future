"""FastAPI application factory — the single application shared by all four phases."""

from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.security.auth import LoginRequired


def create_app() -> FastAPI:
    import app.worker as worker_mod

    worker_mod.register_all_handlers()  # jobs enqueued by web requests must have known types
    settings = get_settings()
    application = FastAPI(title=settings.app_name, docs_url=None if settings.is_production else "/docs", redoc_url=None)
    application.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")

    from app.web import admin, auth_routes, portal, public

    application.include_router(auth_routes.router)
    application.include_router(public.router)
    application.include_router(portal.router)
    application.include_router(admin.router)

    @application.exception_handler(LoginRequired)
    async def _login_redirect(request: Request, exc: LoginRequired):
        return RedirectResponse(f"/login?next={request.url.path}", status_code=303)

    @application.middleware("http")
    async def _security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        if not request.url.path.startswith("/api/chat") and not request.url.path.startswith("/chat/"):
            resp.headers.setdefault("X-Frame-Options", "DENY")
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
