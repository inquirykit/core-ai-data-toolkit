"""
/jobs: long-running scripts (AutoGluon training, say) run in their own process.

Submitting returns at once with the job's ID (202); the job runs in the
background (see jobs/runner.py). Poll GET /jobs/{id}, or follow
GET /jobs/{id}/events (server-sent events: status changes and log text).
A job's outputs are files in its directory (GET /jobs/{id}/files).

Input files go in JOB_DATA_DIR, uploaded with POST /jobs/data; job params
name them by their path there.

Every endpoint needs the admin key: a job can run for hours on every core.
"""

import asyncio
import codecs
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Annotated, Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, File, Path as PathParam, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from config import Config
from jobs import registry
from jobs.runner import JobRunner
from jobs.store import FINISHED, public
from errors import NotFound, Refused, TooLarge
from . import deps
from . import responses

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=deps.ADMIN)

JobId = PathParam(..., pattern=r"^[0-9a-f]{8,32}$")
# Uploaded file names: no folders, no hidden files.
_FILE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,200}$")
MAX_LISTED_FILES = 2000
EVENTS_POLL_S = 0.5


class SubmitJob(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job: str = Field(..., min_length=1, max_length=128, description="A name from GET /jobs/types")
    params: Dict[str, Any] = Field(default_factory=dict, description="The job's params (see its schema)")
    webhook_url: Optional[str] = Field(None, max_length=2000, description="POSTed to when the job finishes, besides WEBHOOK_URLS")
    priority: responses.Priority = Field("normal", description="Queued jobs start the most urgent first, then the oldest")


class ScheduleBody(BaseModel):
    """A schedule: a job and its params, run on a cron expression or every ``interval_s`` seconds."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field("", max_length=120, description="Shown in the dashboard; the job's name when empty")
    job: str = Field(..., min_length=1, max_length=128)
    params: Dict[str, Any] = Field(default_factory=dict)
    cron: Optional[str] = Field(None, max_length=120, description="minute hour day-of-month month day-of-week, local time")
    interval_s: Optional[float] = Field(None, ge=1, description="Seconds between runs (at least 60), instead of cron")
    enabled: bool = True
    webhook_url: Optional[str] = Field(None, max_length=2000)
    priority: responses.Priority = Field("normal", description="The priority of the jobs it submits")


class WebhookTest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1, max_length=2000)


async def _existing_job(runner: deps.Jobs, id: str = JobId) -> JobRunner:
    """The runner, once job ``id`` is known to exist (404 otherwise)."""
    await runner.get(id)
    return runner


ExistingJob = Annotated[JobRunner, Depends(_existing_job)]


def _inside(base: Path, relative: str) -> Path:
    path = (base / relative).resolve()
    if not path.is_relative_to(base.resolve()):
        raise Refused("Path must stay inside the directory")
    return path


def _listing(base: Path) -> List[Dict[str, Any]]:
    if not base.is_dir():
        return []
    files = []
    for path in sorted(base.rglob("*")):
        if path.is_file():
            files.append({"path": path.relative_to(base).as_posix(), "bytes": path.stat().st_size})
            if len(files) >= MAX_LISTED_FILES:
                break
    return files


# ------------------------------------------------------------------- types
@router.get("/types", **responses.answering(responses.JobTypes))
async def job_types(runner: deps.Jobs):
    """Every job: its environment (and whether that is set up), and the JSON Schema of its params and result."""
    return {
        "jobs": [{**job.describe(), "env_ready": runner.env_ready(job.env)} for job in registry.all_types()],
        "limits": {
            "max_running_jobs": runner.max_running,
            "num_cpus": runner.num_cpus,
            "device": runner.device,
            "max_upload_bytes": Config.JOB_MAX_UPLOAD_BYTES,
        },
    }


# -------------------------------------------------------------- input data
# These use no runner, but take it for its 503: with the jobs off, the files are no use.
@router.get("/data")
async def list_data(runner: deps.Jobs):
    """The files in JOB_DATA_DIR, which job params name by these paths."""
    return {"files": await asyncio.to_thread(_listing, Config.JOB_DATA_DIR)}


@router.post("/data", status_code=201)
async def upload_data(runner: deps.Jobs, file: UploadFile = File(...)):
    """Upload a file (CSV, Parquet, ...) to JOB_DATA_DIR under its own name; a file of that name is replaced."""
    name = file.filename or ""
    if not _FILE_NAME.match(name):
        raise Refused("File name: letters, digits, '.', '_' and '-' only")
    base = Config.JOB_DATA_DIR
    base.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=base, prefix=".upload-")
    size = 0
    try:
        with os.fdopen(fd, "wb") as out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > Config.JOB_MAX_UPLOAD_BYTES:
                    raise TooLarge("File is larger than JOB_MAX_UPLOAD_BYTES")
                out.write(chunk)
        os.replace(tmp, base / name)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return {"path": name, "bytes": size}


@router.delete("/data/{path:path}")
async def delete_data(runner: deps.Jobs, path: str):
    target = _inside(Config.JOB_DATA_DIR, path)
    if not target.is_file():
        raise NotFound("File not found")
    target.unlink()
    return {"deleted": path}


# -------------------------------------------------------------------- jobs
@router.post("", **responses.answering(responses.Job), status_code=202)
async def submit_job(runner: deps.Jobs, body: SubmitJob):
    """Queue a job. 400 for an unknown job, params its schema rejects, or an environment not set up."""
    return public(await runner.submit(body.job, body.params, webhook_url=body.webhook_url, priority=body.priority))


# --------------------------------------------------------------- schedules
ScheduleId = JobId  # the same form of id


async def _existing_schedule(runner: deps.Jobs, id: str = ScheduleId) -> JobRunner:
    """The runner, once schedule ``id`` is known to exist (404 otherwise)."""
    await runner.scheduler.get(id)
    return runner


ExistingSchedule = Annotated[JobRunner, Depends(_existing_schedule)]


@router.get("/schedules", **responses.answering(responses.Schedules))
async def list_schedules(runner: deps.Jobs):
    """Every schedule with its next and last run (and the last job's status)."""
    return {"schedules": await runner.scheduler.list()}


@router.post("/schedules", **responses.answering(responses.Schedule), status_code=201)
async def create_schedule(runner: deps.Jobs, body: ScheduleBody):
    """Add a schedule. 400 for an unknown job, params its schema rejects, or a cadence that is not cron / interval_s."""
    return await runner.scheduler.create(body.model_dump())


@router.get("/schedules/{id}", **responses.answering(responses.Schedule))
async def get_schedule(runner: deps.Jobs, id: str = ScheduleId):
    return await runner.scheduler.get(id)


@router.put("/schedules/{id}", **responses.answering(responses.Schedule))
async def update_schedule(runner: ExistingSchedule, body: ScheduleBody, id: str = ScheduleId):
    """Replace a schedule's fields; its next run is recomputed from now."""
    return await runner.scheduler.update(id, body.model_dump())


@router.delete("/schedules/{id}", status_code=204, response_class=PlainTextResponse)
async def delete_schedule(runner: ExistingSchedule, id: str = ScheduleId):
    await runner.scheduler.delete(id)
    return PlainTextResponse(status_code=204)


@router.post("/schedules/{id}/run", **responses.answering(responses.Job), status_code=202)
async def run_schedule(runner: ExistingSchedule, id: str = ScheduleId):
    """Submit the schedule's job now; the planned next run is unchanged."""
    return public(await runner.scheduler.run(id))


# ---------------------------------------------------------------- webhooks
@router.get("/webhooks", **responses.answering(responses.Webhooks))
async def webhooks(hooks: deps.Webhooks):
    """The configured webhook URLs (WEBHOOK_URLS), whether a signing secret is set, and the event names.

    Eval runs post to them too, so these work with jobs off.
    """
    return hooks.describe()


@router.post("/webhooks/test", **responses.answering(responses.WebhookTestResult))
async def test_webhook(hooks: deps.Webhooks, body: WebhookTest):
    """Send a ``test`` event to the URL now and report what it answered."""
    if not body.url.startswith(("http://", "https://")):
        raise Refused("url must start with http:// or https://")
    return await hooks.test(body.url)


@router.get("", **responses.answering(responses.Jobs))
async def list_jobs(
    runner: deps.Jobs,
    limit: int = Query(50, ge=1, le=500),
    status: Optional[Literal["queued", "running", "succeeded", "failed", "cancelled"]] = None,
):
    """Jobs, newest first."""
    return {"jobs": [public(row) for row in await runner.store.list(limit, status)]}


@router.get("/{id}", **responses.answering(responses.Job))
async def get_job(runner: deps.Jobs, id: str = JobId):
    return public(await runner.get(id))


@router.post("/{id}/cancel", **responses.answering(responses.Job))
async def cancel_job(runner: ExistingJob, id: str = JobId):
    """Cancel a queued job, or stop a running one (its whole process group). A finished job is left as is."""
    return public(await runner.cancel(id))


@router.delete("/{id}", **responses.answering(responses.Deleted))
async def delete_job(runner: ExistingJob, id: str = JobId):
    """Delete a finished job and its files (a trained predictor too)."""
    await runner.delete(id)
    return {"deleted": id}


@router.get("/{id}/log", response_class=PlainTextResponse)
async def job_log(runner: ExistingJob, id: str = JobId, tail: int = Query(200_000, ge=1, le=10_000_000)):
    """The end of the job's log (what it and its libraries printed): the last ``tail`` bytes."""
    path = runner.workdir(id) / "job.log"

    def read() -> str:
        if not path.is_file():
            return ""
        with open(path, "rb") as f:
            f.seek(max(0, path.stat().st_size - tail))
            return f.read().decode("utf-8", errors="replace")

    return await asyncio.to_thread(read)


@router.get("/{id}/events")
async def job_events(request: Request, runner: ExistingJob, id: str = JobId):
    """Server-sent events until the job ends: ``status`` (the job, on every change) and ``log`` (new log text)."""
    path = runner.workdir(id) / "job.log"

    def read_from(offset: int) -> tuple:
        if not path.is_file():
            return b"", offset
        with open(path, "rb") as f:
            f.seek(offset)
            data = f.read(1024 * 1024)
        return data, offset + len(data)

    def event(name: str, data: Any) -> str:
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"

    async def stream():
        offset, last = 0, None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            row = await runner.store.get(id)
            if row is None:
                return
            data, offset = await asyncio.to_thread(read_from, offset)
            if data:
                yield event("log", {"text": decoder.decode(data)})
            state = (row["status"], row["progress"], row["message"])
            if state != last:
                last = state
                yield event("status", public(row))
            if row["status"] in FINISHED and not data:
                yield event("end", {"status": row["status"]})
                return
            if await request.is_disconnected():
                return
            if not data:
                await asyncio.sleep(EVENTS_POLL_S)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/{id}/files")
async def job_files(runner: ExistingJob, id: str = JobId):
    """The files in the job's directory: params, log, result and its outputs."""
    return {"files": await asyncio.to_thread(_listing, runner.workdir(id))}


@router.get("/{id}/files/{path:path}")
async def job_file(runner: ExistingJob, path: str, id: str = JobId):
    target = _inside(runner.workdir(id), path)
    if not target.is_file():
        raise NotFound("File not found")
    return FileResponse(target, filename=target.name)
