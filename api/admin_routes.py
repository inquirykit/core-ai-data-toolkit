"""
The /admin dashboard: a page (see api/pages.py) plus the JSON it polls.

The page itself holds no data and needs no key; every /admin/* data route
needs the admin key (see deps.require_admin_key). Loading, unloading and
clearing models use the existing /models routes, which need the same key.
PUT /admin/limits changes the cache's memory budget and model count live and
saves them to the .env file.

Live state (loaded models, memory) is this process's. Request metrics and
model events come from the monitoring tables in the ArcadeDB database, so
they survive restarts.
"""

import logging
import time
from typing import Literal, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from config import Config
from monitoring import summarize, system
from errors import Refused
from . import deps
from . import responses
from .setup_routes import write_settings

# The largest integer the database takes (a Java long): a query integer past it would otherwise be a 500.
INT64_MAX = 2**63 - 1

router = APIRouter(prefix="/admin", dependencies=deps.ADMIN)

WINDOWS = {"1h": 3600, "24h": 86400, "7d": 7 * 86400}
# A timeline covers at most this long, and at this many points at most.
MAX_SPAN_S = 90 * 86400
MAX_BUCKETS = 240


@router.get("/stats", **responses.answering(responses.Stats))
async def stats(store: deps.Monitoring, manager: deps.Manager, window: Literal["1h", "24h", "7d"] = "1h"):
    """Process and cache state now, plus request metrics over ``window``."""
    until = time.time()
    since = until - WINDOWS[window]
    if store is None:
        metrics = summarize([], since, until)
    else:
        metrics = await store.summary(since, until)
    return {
        "window": window,
        "monitoring": {
            "enabled": store is not None,
            "dropped": store.dropped if store is not None else 0,
            "retention_days": Config.MONITORING_RETENTION_DAYS,
        },
        "process": {"device": manager.device, **system.snapshot()},
        "cache": manager.get_cache_status(),
        **metrics,
    }


@router.get("/timeline", **responses.answering(responses.Timeline))
async def timeline(
    store: deps.Monitoring,
    since: float = Query(..., description="Unix time the range starts at"),
    until: Optional[float] = Query(None, description="Unix time the range ends at (now when left out)"),
    buckets: int = Query(120, ge=1, le=MAX_BUCKETS),
):
    """Request metrics over any range: totals, per-model stats and a timeline of ``buckets`` points.

    The Activity & logs page zooms with this: a narrower range gets the same
    number of points, so each covers less time.
    """
    until = time.time() if until is None else until
    if until <= since:
        raise Refused("until must be after since")
    if until - since > MAX_SPAN_S:
        raise Refused(f"The range may cover at most {MAX_SPAN_S // 86400} days")
    if store is None:
        return summarize([], since, until, buckets)
    return await store.summary(since, until, buckets)


@router.get("/events", **responses.answering(responses.ModelEvents))
async def events(
    store: deps.Monitoring,
    after_id: int = Query(0, ge=0, le=INT64_MAX),
    limit: int = Query(100, ge=1, le=500),
    since: Optional[float] = Query(None, description="Only events at or after this Unix time"),
    until: Optional[float] = Query(None, description="Only events at or before this Unix time"),
):
    """Model loads, failed loads, unloads and evictions, newest first."""
    if store is None:
        return {"events": []}
    return {"events": await store.events(after_id, limit, since, until)}


@router.get("/requests", **responses.answering(responses.RequestRows))
async def recent_requests(
    store: deps.Monitoring,
    limit: int = Query(50, ge=1, le=500),
    since: Optional[float] = Query(None, description="Only requests at or after this Unix time"),
    until: Optional[float] = Query(None, description="Only requests at or before this Unix time"),
    status: Optional[Literal["ok", "client_error", "error"]] = Query(None),
    model: Optional[str] = Query(None, max_length=200, description="Part of the model ID or task"),
):
    """The latest requests' metadata, newest first; each one's input and output are GET /admin/traces/{trace_id}."""
    if store is None:
        return {"requests": []}
    return {"requests": await store.recent_requests(limit, since, until, status, model)}


@router.get("/logs", **responses.answering(responses.Logs))
async def logs(
    buffer: deps.Logs,
    after_id: int = Query(0, ge=0, le=INT64_MAX),
    limit: int = Query(500, ge=1, le=2000),
    level: Optional[Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]] = Query(None),
):
    """This process's latest log lines, oldest first, from an in-memory buffer (see monitoring/logs.py)."""
    return {
        "lines": buffer.lines(after_id, limit, level),
        "capacity": buffer.capacity,
        "level": logging.getLevelName(logging.getLogger().getEffectiveLevel()),
    }


@router.get("/config", **responses.answering(responses.AdminConfig))
async def config():
    """Every setting's effective value, with keys and secrets reduced to whether they are set."""
    return {"settings": Config.described(), "warnings": Config.warnings()}


class LimitsRequest(BaseModel):
    model_config = {"extra": "forbid"}

    max_memory_gb: Optional[float] = Field(None, ge=0, le=4096, description="Budget for loaded models; 0: none")
    max_models: Optional[int] = Field(None, ge=1, le=64)


@router.put("/limits", **responses.answering(responses.LimitsResult))
async def set_limits(store: deps.Monitoring, manager: deps.Manager, body: LimitsRequest):
    """Change MAX_MEMORY_GB and MAX_MODELS now, and save them to the .env file.

    Lower limits evict idle models at once; models busy with a request stay
    until it finishes.
    """
    changes = body.model_dump(exclude_none=True)
    if not changes:
        raise Refused("Give max_memory_gb, max_models or both")
    values = {name.upper(): str(value) for name, value in changes.items()}
    # Before Config.apply, which makes them look like they came from the file.
    notes = [
        f"{name} is also set in the environment, which wins over the saved value when the server restarts."
        for name in values if Config.set_by_environment(name)
    ]
    await write_settings(values)
    evicted = await manager.set_limits(body.max_models, body.max_memory_gb)
    Config.apply(values)

    if store is not None:
        store.record_event(kind="limits", model_key="cache", detail=", ".join(f"{k}={v}" for k, v in values.items()))

    if Config.WORKERS > 1:
        notes.append("Only this worker uses the new limits until the server restarts.")
    if manager.cache.over_limits():
        notes.append("Busy models keep the cache over the new limits until their requests finish.")
    status = manager.get_cache_status()
    return {
        "max_memory_gb": status["max_memory_gb"],
        "max_models": status["max_models"],
        "evicted": evicted,
        "env_file": str(Config.ENV_FILE),
        "notes": notes,
    }
