"""
/queue: every kind of background work on the one task queue (tasks/runner.py), in one list: jobs, eval runs and
judging, webhook deliveries, bucket copies and workflow runs, each a task of its kind.

    GET  /queue?kind=&status=&ref=&limit=&cursor=   {tasks, next}: newest first
    POST /queue/{id}/cancel                         cancel a queued task, or stop a running one
    GET  /queue/webhooks                            the webhook URLs and their latest deliveries
    POST /queue/webhooks/test                       send a test event to a URL

A kind's own routes say more of its tasks (a job's log and files, a run's scores, a workflow run's counts); here they
are the one record (Task). All of it needs the admin key.
"""

from typing import List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Field

from core.errors import Refused
from tasks.runner import public
from . import audit, deps
from . import responses

router = APIRouter(prefix="/queue", tags=["queue"], dependencies=deps.ADMIN)


@router.get("", **responses.answering(responses.Queue))
async def list_tasks(
    tasks: deps.Tasks,
    kind: Optional[str] = Query(None, max_length=32, description="Only tasks of this kind (job, run, workflow...)"),
    status: Optional[List[responses.TaskStatus]] = Query(None),
    ref: Optional[str] = Query(None, max_length=500, description="Only tasks working on this (a run's id, a bucket)"),
    limit: int = Query(50, ge=1, le=500),
    cursor: deps.Cursor = None,
):
    """The tasks of every kind, newest first, a page at a time; ``status`` given more than once takes any of them
    (``?status=queued&status=running``: the work not done yet). Ended ones are kept as their kind says (a job until
    it is deleted, a webhook delivery for days)."""
    rows, after = await tasks.page(limit, cursor, kind=kind, statuses=status or (), ref=ref)
    return responses.page("tasks", [public(row) for row in rows], after)


@router.post("/{id}/cancel", **responses.answering(responses.Task))
async def cancel_task(tasks: deps.Tasks, id: str):
    """Cancel a queued task, or stop a running one and wait for it to end; one that ended is left as it is. A kind's
    own cancel may do more (an eval run's stops its judging too). A webhook delivery under way is let finish, and a
    bucket's copy is asked for again at the next upload or hourly check while its files wait."""
    return public(await tasks.cancel(id))


# ---------------------------------------------------------------- webhooks
class WebhookTest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1, max_length=2000)


@router.get("/webhooks", **responses.answering(responses.Webhooks))
async def webhooks(hooks: deps.Webhooks):
    """The configured webhook URLs (WEBHOOK_URLS), whether a signing secret is set, the event names, and the latest
    deliveries with how each went (those still retried are queued, with the last try's error). Finished jobs and eval
    runs are posted to them, each delivery a task of its own."""
    return {**hooks.describe(), "deliveries": await hooks.deliveries()}


@router.post("/webhooks/test", **responses.answering(responses.WebhookTestResult))
@audit.reads
async def test_webhook(hooks: deps.Webhooks, body: WebhookTest):
    """Send a ``test`` event to the URL now and report what it answered."""
    if not body.url.startswith(("http://", "https://")):
        raise Refused("url must start with http:// or https://")
    return await hooks.test(body.url)
