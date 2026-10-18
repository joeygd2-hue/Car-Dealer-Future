"""Aggregates the JSON API routers under /api/v1."""
from fastapi import APIRouter

from app.web import api_agent, api_core, api_finance, api_portal, api_seo_reports

router = APIRouter()
for module in (api_core, api_finance, api_agent, api_seo_reports, api_portal):
    router.include_router(module.router)
