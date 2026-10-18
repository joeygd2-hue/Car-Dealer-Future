"""FastAPI application: one app for all phases (see docs/architecture/stack-and-interfaces.md)."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.core.errors import AppError
from app.db import TenantViolation

STATIC = Path(__file__).parent / "web" / "static"


def create_app() -> FastAPI:
    app = FastAPI(title="Car Dealer Future", version="0.2.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in settings.cors_origins.split(",")],
                           allow_methods=["*"], allow_headers=["Authorization", "Content-Type"])

    @app.exception_handler(AppError)
    async def app_error(_: Request, exc: AppError):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message, "details": exc.details}}, status_code=exc.status)

    @app.exception_handler(TenantViolation)
    async def tenant_error(_: Request, exc: TenantViolation):
        return JSONResponse({"error": {"code": "forbidden", "message": "Cross-dealership access denied"}}, status_code=403)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError):
        return JSONResponse({"error": {"code": "validation_error", "message": "Invalid request",
                                       "details": [{"loc": e.get("loc"), "msg": e.get("msg")} for e in exc.errors()]}},
                            status_code=422)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        if not request.url.path.startswith("/api/docs"):
            resp.headers.setdefault("Content-Security-Policy",
                                    "default-src 'self'; img-src 'self' https: data:; style-src 'self' 'unsafe-inline'; "
                                    "script-src 'self'; connect-src 'self'")
        return resp

    from app.api import auth, inventory, listings, outreach, system
    for r in (auth.router, system.router, inventory.router, listings.router, outreach.router):
        app.include_router(r)

    if STATIC.exists():
        app.mount("/static", StaticFiles(directory=STATIC), name="static")

        @app.get("/favicon.ico", include_in_schema=False)
        def favicon():
            return FileResponse(STATIC / "favicon.svg", media_type="image/svg+xml")

        @app.get("/", include_in_schema=False)
        def index():
            return FileResponse(STATIC / "index.html")

    return app


app = create_app()
