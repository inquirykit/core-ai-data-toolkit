"""/queue: the tasks of every kind on the one queue of background work (tasks/runner.py), as the jobs, a bucket's copy
and the queue's own routes show them, and the webhooks finished jobs and eval runs are posted to."""

from typing import Any, List, Optional

from core.priorities import Priority
from tasks.runner import Status
from .common import Json, Out


TaskStatus = Status


class Task(Out):
    id: str
    # job, run, judge, webhook, copy or workflow: each kind with its own limit
    kind: str
    # What it works on: an eval run's id, a bucket (workspace/bucket), a webhook's URL, a workflow's id
    ref: Optional[str]
    # A job's name from GET /jobs/types, for the kind job
    job: Optional[str]
    # The model it needs, when it needs one: tasks on loaded models go first
    model_id: Optional[str]
    status: TaskStatus
    priority: Priority
    params: Json
    # 0..1, or null while unknown
    progress: Optional[float]
    message: Optional[str]
    # What it gave once it succeeded (a copy's: {remote, copied, deleted, skipped}, the files copied, the copies deleted
    # at the remote, the files skipped as their bytes are not on disk; and {with}, the copy that did its work)
    result: Any
    error: Optional[str]
    created: float
    started: Optional[float]
    finished: Optional[float]
    # The schedule that started it, when one did
    schedule_id: Optional[str]
    webhook_url: Optional[str]
    # Times it is queued again after failing, and the seconds before the first retry (doubled for each next)
    retries: int
    retry_delay_s: float
    # Seconds a run may take, null for no limit
    timeout_s: Optional[float]
    # Retries so far; a queued retry waits until not_before
    retried: int
    not_before: Optional[float]


class Queue(Out):
    tasks: List[Task]
    next: Optional[str] = None


class WebhookDelivery(Out):
    """One delivery of an event to one URL (tasks/webhooks.py): a task, retried with backoff."""
    id: str
    event: Optional[str]
    # The job's or run's id the event tells of
    about: Optional[str]
    url: Optional[str]
    status: TaskStatus
    # The last try's failure: still retried while queued
    error: Optional[str]
    retried: int
    retries: int
    # When a queued retry is tried
    not_before: Optional[float]
    created: float
    finished: Optional[float]


class Webhooks(Out):
    urls: List[str]
    secret_set: bool
    events: List[str]
    # The latest deliveries, newest first
    deliveries: List[WebhookDelivery]


class WebhookTestResult(Out):
    ok: bool
    status: Optional[int]
    error: Optional[str]
