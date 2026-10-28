"""
GET /metrics: the service in Prometheus' text format, for a scraper.

Request counts, latency and token counts come from monitoring.Metrics (this
process, since it started); the gauges read the model cache and the host at
scrape time. The client key (or the admin key) opens it: a scrape config's
bearer token.
"""

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

from monitoring import system
from . import deps

router = APIRouter(tags=["monitoring"], dependencies=deps.CLIENT)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request, counts: deps.Metrics, manager: deps.Manager):
    """Request counts, latency histogram, token counts, model and memory gauges, in Prometheus format."""
    cache = {"device": manager.device, **manager.get_cache_status()}
    text = counts.render(cache, system.snapshot(), version=request.app.version)
    return PlainTextResponse(text, media_type=CONTENT_TYPE)
