"""
/hub: the Hugging Face Hub from the admin dashboard (see models/hub.py).

Search the Hub, look a repo up, download its weights ahead of time (so the
first request for a model does not wait for them), and list or delete the
repos on disk. All of it needs the admin key: downloads fetch whatever repo is
named, and deleting weights evicts nothing but costs a re-download.
"""

import asyncio
import logging
import re
from typing import Literal, Optional

from fastapi import APIRouter, Path, Query, Response
from pydantic import BaseModel, Field

from models import ModelManager, hub, memory, registry, tasks
from config import Config
from errors import Conflict, Forbidden, NotFound, Refused, ServiceError, Unavailable, describe
from . import deps
from . import responses

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/hub", tags=["hub"], dependencies=deps.ADMIN)

# The tasks the Hub search can filter by: those with a pipeline tag (models/tasks.py).
Task = Literal[tuple(tasks.hub_tags())]
REPO_ID = re.compile(r"^[\w.-]+(/[\w.-]+)?$")


def _repo_id(value: str) -> str:
    if not REPO_ID.match(value) or ".." in value:
        raise Refused("repo_id must be like org/name")
    return value


def _online() -> None:
    if hub.offline():
        raise Unavailable("The Hub is not reachable: HF_HUB_OFFLINE is set")


def _hub_error(exc: Exception) -> ServiceError:
    """The Hub's errors as HTTP statuses: unknown repo 404, gated 403, anything else 502."""
    from huggingface_hub import errors

    if isinstance(exc, errors.GatedRepoError):
        return Forbidden("The repo is gated: set HF_TOKEN to a token whose account accepted its terms")
    if isinstance(exc, (errors.RepositoryNotFoundError, errors.RevisionNotFoundError)):
        return NotFound("No such repo on the Hub")
    logger.exception("The Hub did not answer")
    return ServiceError(describe(exc, "The Hub did not answer", Config.DEBUG), status=502)


def _loaded_tasks(manager: ModelManager, repo_id: str) -> list:
    details = manager.get_cache_status()["model_details"].values()
    return sorted({d["task"] for d in details if d["model_id"] == repo_id})


@router.get("/search", **responses.answering(responses.HubSearch))
async def search(
    q: str = Query("", max_length=200, description="Words of the model ID or card"),
    task: Optional[Task] = Query(None, description="Only models for this task (by the Hub's pipeline tag)"),
    limit: int = Query(30, ge=1, le=100),
):
    """Models on the Hub, most downloaded first, each with whether its weights are already on disk."""
    _online()
    try:
        results = await asyncio.to_thread(hub.search, q, task, limit)
    except Exception as e:
        raise _hub_error(e)
    on_disk = await asyncio.to_thread(memory.cached_weights)
    for result in results:
        result["on_disk"] = result["id"] in on_disk
    return {"query": q, "task": task, "results": results}


@router.get("/repos/{repo_id:path}", **responses.answering(responses.HubRepo))
async def repo(repo_id: str = Path(..., min_length=1, max_length=256)):
    """A repo's files with their sizes, its total and weight bytes, license and whether it is gated."""
    _online()
    repo_id = _repo_id(repo_id)
    try:
        info = await asyncio.to_thread(hub.repo_info, repo_id)
    except Exception as e:
        raise _hub_error(e)
    info["on_disk"] = repo_id in await asyncio.to_thread(memory.cached_weights)
    return info


@router.get("/downloads", **responses.answering(responses.HubDownloads))
async def downloads(hub_downloads: deps.HubDownloads):
    """Downloads queued, running and finished (until dismissed), newest first."""
    return {"downloads": await hub_downloads.list()}


class DownloadRequest(BaseModel):
    repo_id: str = Field(..., min_length=1, max_length=256)
    priority: responses.Priority = Field("normal", description="Queued downloads start the most urgent first, then the oldest")


@router.post("/downloads", **responses.answering(responses.HubDownload), status_code=202)
async def start_download(hub_downloads: deps.HubDownloads, body: DownloadRequest):
    """Download a repo's files into the Hub cache in the background; 409 if it is already being downloaded."""
    _online()
    repo_id = _repo_id(body.repo_id)
    return await hub_downloads.start(repo_id, body.priority)


@router.delete("/downloads/{download_id}", status_code=204, response_class=Response)
async def cancel_download(hub_downloads: deps.HubDownloads, download_id: str = Path(..., max_length=32)):
    """Cancel a queued or running download (the partial files stay and resume next time), or dismiss a finished one."""
    await hub_downloads.cancel(download_id)
    return Response(status_code=204)


@router.get("/local", **responses.answering(responses.HubLocal))
async def local(manager: deps.Manager):
    """Model repos in the Hub cache, with their size, whether a model loaded from them now, and the tasks that claim them."""
    repos = await asyncio.to_thread(hub.local_repos)
    for repo in repos:
        repo["loaded_tasks"] = _loaded_tasks(manager, repo["id"])
        repo["loaded"] = bool(repo["loaded_tasks"])
        repo["tasks"] = registry.claimed_tasks(repo["id"])
    return {"repos": repos, "total_bytes": sum(r["size_bytes"] for r in repos)}


@router.delete("/local/{repo_id:path}", status_code=204, response_class=Response)
async def delete_local(hub_downloads: deps.HubDownloads, manager: deps.Manager, repo_id: str = Path(..., min_length=1, max_length=256)):
    """Delete a repo's files from the Hub cache; 409 while a model is loaded from it or it is being downloaded."""
    repo_id = _repo_id(repo_id)
    if _loaded_tasks(manager, repo_id):
        raise Conflict(f"{repo_id} is loaded: unload it first")
    if await hub_downloads.active_for(repo_id) is not None:
        raise Conflict(f"{repo_id} is being downloaded")
    await asyncio.to_thread(hub.delete_repo, repo_id)
    return Response(status_code=204)
