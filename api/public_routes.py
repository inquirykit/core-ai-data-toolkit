"""
The routes anyone may call, without a key: the service's name, the health
check (the container's healthcheck uses it), and the browser pages, which ask
for a key themselves. The first-run wizard (api/setup_routes.py), which runs
before there is a key and checks its setup code, is the only other router
without one; tests/test_access.py holds every other route to a key.
"""

from fastapi import APIRouter
from fastapi.responses import RedirectResponse

from . import deps
from .pages import page

router = APIRouter()


@router.get("/")
async def root():
    return {"message": "Core AI & Data toolkit", "status": "running"}


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # What a browser asks for on a page that names no icon (/, /health); the pages link the built one themselves.
    return RedirectResponse("/admin/static/favicon.svg")


@router.get("/health")
async def health_check(manager: deps.Manager):
    return {
        "status": "healthy",
        "device": manager.device,
        "cache_status": manager.get_cache_status(),
    }


@router.get("/admin", include_in_schema=False)
async def dashboard():
    return page("admin.html")


@router.get("/playground", include_in_schema=False)
async def playground():
    return page("playground.html")
