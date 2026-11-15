"""
/jobs: long-running scripts (AutoGluon training, say) run in their own process.

Submitting returns at once with the job's ID (202); the job runs in the
background (see jobs/runner.py). Poll GET /jobs/{id}, or follow
GET /jobs/{id}/events (server-sent events: status changes and log text).
A job's outputs are files in its directory (GET /jobs/{id}/files).

Input files go in DATA_DIR/datasets, uploaded with POST /jobs/data; job params
name them by their path there.

Every endpoint needs the admin key: a job can run for hours on every core.
"""

import asyncio
import codecs
import json
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, File, Path as PathParam, Query, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, StreamingResponse
from pydantic import ConfigDict, Field

from jobs import registry
from jobs.runner import JobRunner
from tasks.runner import FINISHED, Retries, public
from tasks.schedule import ScheduleSpec
from core.errors import NotFound
from . import deps
from . import responses

router = APIRouter(prefix="/jobs", tags=["jobs"], dependencies=deps.ADMIN)

JobId = PathParam(..., pattern=r"^[0-9a-f]{8,32}$")
EVENTS_POLL_S = 0.5
# What an upload reads of its body at a time.
UPLOAD_CHUNK = 1024 * 1024


class SubmitJob(Retries):
    model_config = ConfigDict(extra="forbid")

    job: str = Field(..., min_length=1, max_length=128, description="A name from GET /jobs/types")
    params: Dict[str, Any] = Field(default_factory=dict, description="The job's params (see its schema)")
    webhook_url: Optional[str] = Field(None, max_length=2000, description="POSTed to when the job finishes, besides WEBHOOK_URLS")
    priority: responses.Priority = Field("normal", description="Queued jobs start the most urgent first, then the oldest")


async def _existing_job(runner: deps.Jobs, id: str = JobId) -> JobRunner:
    """The runner, once job ``id`` is known to exist (404 otherwise)."""
    await runner.get(id)
    return runner


ExistingJob = Annotated[JobRunner, Depends(_existing_job)]


# ------------------------------------------------------------------- types
@router.get("/types", **responses.answering(responses.JobTypes))
async def job_types(runner: deps.Jobs):
    """Every job: its environment (whether it is set up, and from its requirements file as it is now), and the JSON
    Schema of its params and result."""
    return {
        "jobs": [{**job.describe(), "env_ready": runner.env_ready(job.env), "env_current": runner.env_current(job.env)}
                 for job in registry.all_types()],
        "limits": {
            "max_running_jobs": runner.max_running,
            "num_cpus": runner.num_cpus,
            "device": runner.device,
            "max_upload_bytes": runner.data.max_bytes,
        },
    }


# -------------------------------------------------------------- input data
@router.get("/data", **responses.answering(responses.Files))
async def list_data(runner: deps.Jobs):
    """The files in DATA_DIR/datasets, which job params name by these paths."""
    return {"files": await runner.data.list()}


@router.get("/data/{path:path}/summary", **responses.answering(responses.FileSummary))
async def summarize_data(runner: deps.Jobs, path: str):
    """A CSV, TSV, Parquet or JSON Lines file's row count, each column's type, min, max, approximate distinct count,
    mean and share missing, and its first 20 rows: what choosing a column to train on needs. 400 for a file that cannot
    be read as a table."""
    return await runner.data.summary(path)


@router.post("/data", status_code=201, **responses.answering(responses.FileEntry))
async def upload_data(runner: deps.Jobs, file: UploadFile = File(...)):
    """Upload a file (CSV, Parquet, ...) to DATA_DIR/datasets under its own name; a file of that name is replaced."""

    async def chunks():
        while chunk := await file.read(UPLOAD_CHUNK):
            yield chunk

    return await runner.data.save(file.filename or "", chunks())


@router.delete("/data/{path:path}", **responses.answering(responses.Deleted))
async def delete_data(runner: deps.Jobs, path: str):
    """Delete an input file from DATA_DIR/datasets."""
    await runner.data.delete(path)
    return {"deleted": path}


# -------------------------------------------------------------------- jobs
@router.post("", **responses.answering(responses.Task), status_code=202)
async def submit_job(runner: deps.Jobs, body: SubmitJob):
    """Queue a job. 400 for an unknown job, params its schema rejects, or an environment not set up."""
    return public(await runner.submit(body.job, body.params, webhook_url=body.webhook_url, priority=body.priority,
                                      retries=body.retries, retry_delay_s=body.retry_delay_s, timeout_s=body.timeout_s))


# --------------------------------------------------------------- schedules
ScheduleId = JobId  # the same form of id


async def _existing_schedule(runner: deps.Jobs, id: str = ScheduleId) -> JobRunner:
    """The runner, once schedule ``id`` is known to exist and to be a job's (404 otherwise: a workflow's trigger is
    its workflow's)."""
    await runner.scheduler.get(id, kind="job")
    return runner


ExistingSchedule = Annotated[JobRunner, Depends(_existing_schedule)]


@router.get("/schedules", **responses.answering(responses.Schedules))
async def list_schedules(runner: deps.Jobs):
    """Every job's schedule with its next and last run (and the last job's status)."""
    return {"schedules": await runner.scheduler.list(kind="job")}


@router.post("/schedules", **responses.answering(responses.Schedule), status_code=201)
async def create_schedule(runner: deps.Jobs, body: ScheduleSpec):
    """Add a schedule. 400 for an unknown job, params its schema rejects, or a cadence that is not cron / interval_s."""
    return await runner.scheduler.create(body)


@router.get("/schedules/{id}", **responses.answering(responses.Schedule))
async def get_schedule(runner: deps.Jobs, id: str = ScheduleId):
    """One job's schedule."""
    return await runner.scheduler.get(id, kind="job")


@router.put("/schedules/{id}", **responses.answering(responses.Schedule))
async def update_schedule(runner: ExistingSchedule, body: ScheduleSpec, id: str = ScheduleId):
    """Replace a schedule's fields; its next run is recomputed from now when its cadence changed or it was switched
    on or off, and stays as planned otherwise."""
    return await runner.scheduler.update(id, body)


@router.delete("/schedules/{id}", status_code=204, response_class=PlainTextResponse)
async def delete_schedule(runner: ExistingSchedule, id: str = ScheduleId):
    """Delete a schedule."""
    await runner.scheduler.delete(id)
    return PlainTextResponse(status_code=204)


@router.post("/schedules/{id}/run", **responses.answering(responses.Task), status_code=202)
async def run_schedule(runner: ExistingSchedule, id: str = ScheduleId):
    """Submit the schedule's job now; the planned next run is unchanged."""
    return public(await runner.scheduler.run(id))


@router.get("", **responses.answering(responses.Jobs))
async def list_jobs(
    runner: deps.Jobs,
    limit: int = Query(50, ge=1, le=500),
    status: Optional[responses.TaskStatus] = None,
    job: List[str] = Query([], max_length=50, description="Only these jobs (a name from GET /jobs/types), e.g. "
                                                          "job=autogluon-tabular&job=autogluon-timeseries"),
    ref: Optional[str] = Query(None, max_length=256, description="Only the jobs working on this: the value of the "
                                                                "param a job names as its ref (autogluon-predict's "
                                                                "predictor)"),
    cursor: deps.Cursor = None,
):
    """Jobs, newest first, a page at a time. Jobs are kept until deleted, so the list only grows."""
    rows, after = await runner.tasks.page(limit, cursor, kind="job", statuses=[status] if status else (), jobs=job,
                                          ref=ref)
    return responses.page("jobs", [public(row) for row in rows], after)


@router.get("/{id}", **responses.answering(responses.Task))
async def get_job(runner: deps.Jobs, id: str = JobId):
    """The job: its status, progress, message, result and error."""
    return public(await runner.get(id))


@router.post("/{id}/cancel", **responses.answering(responses.Task))
async def cancel_job(runner: ExistingJob, id: str = JobId):
    """Cancel a queued job, or stop a running one (its whole process group). A finished job is left as is."""
    return public(await runner.cancel(id))


@router.delete("/{id}", **responses.answering(responses.Deleted))
async def delete_job(runner: ExistingJob, manager: deps.Manager, id: str = JobId):
    """Delete a finished job and its files. A model it trained goes too, and is unloaded if it was loaded."""
    row = await runner.delete(id)
    # After its files: a request arriving in between finds nothing to load again.
    result = row.get("result") or {}
    if isinstance(result, dict) and result.get("model_id") and result.get("task"):
        try:
            await manager.unload_model(result["model_id"], result["task"])
        except NotFound:
            pass  # not loaded
    return {"deleted": id}


@router.get("/{id}/log", response_class=PlainTextResponse)
async def job_log(runner: ExistingJob, id: str = JobId, tail: int = Query(200_000, ge=1, le=10_000_000)):
    """The end of the job's log (what it and its libraries printed): the last ``tail`` bytes."""
    return await runner.log_tail(id, tail)


@router.get("/{id}/events")
async def job_events(request: Request, runner: ExistingJob, id: str = JobId):
    """Server-sent events until the job ends: ``status`` (the job, on every change) and ``log`` (new log text)."""

    def event(name: str, data: Any) -> str:
        return f"event: {name}\ndata: {json.dumps(data)}\n\n"

    async def stream():
        offset, last = 0, None
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            row = await runner.tasks.lookup(id)
            if row is None:
                return
            data, offset = await runner.log_from(id, offset)
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


@router.get("/{id}/files", **responses.answering(responses.Files))
async def job_files(runner: ExistingJob, id: str = JobId):
    """The files in the job's directory: params, log, result and its outputs."""
    return {"files": await runner.files(id)}


@router.get("/{id}/files/{path:path}")
async def job_file(runner: ExistingJob, path: str, id: str = JobId):
    """One file of the job's directory."""
    target = runner.file(id, path)
    return FileResponse(target, filename=target.name)
