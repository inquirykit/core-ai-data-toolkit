"""/jobs: jobs, their types, schedules and webhooks."""

from typing import Any, List, Literal, Optional


from .common import Json, Out


JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
# How urgent a task is: of the queued tasks of a kind, the most urgent starts first (tasks/runner.py).
Priority = Literal["high", "normal", "low"]


class Job(Out):
    id: str
    # A name from GET /jobs/types
    job: str
    status: JobStatus
    priority: Priority
    params: Json
    # 0..1, or null while unknown
    progress: Optional[float]
    message: Optional[str]
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


class Jobs(Out):
    jobs: List[Job]


class JobType(Out):
    name: str
    description: str
    # "default" for the service's own interpreter, else a JOB_ENVS_DIR environment
    env: str
    # Whether that environment is set up (a job in one that is not is refused)
    env_ready: bool
    # The JSON Schema of its params
    params: Json
    # The JSON Schema of its result, or null
    result: Optional[Json]


class JobLimits(Out):
    max_running_jobs: int
    num_cpus: int
    device: str
    max_upload_bytes: int


class JobTypes(Out):
    jobs: List[JobType]
    limits: JobLimits


class Schedule(Out):
    id: str
    name: str
    job: str
    params: Json
    # A 5-field cron expression (server time), or null when interval_s is set
    cron: Optional[str]
    interval_s: Optional[float]
    enabled: bool
    webhook_url: Optional[str]
    # The jobs it submits wait by it
    priority: Priority
    # The retries and timeout of the jobs it submits
    retries: int
    retry_delay_s: float
    timeout_s: Optional[float]
    created: float
    updated: float
    next_run: Optional[float]
    last_run: Optional[float]
    last_job_id: Optional[str]
    last_status: Optional[str]


class Schedules(Out):
    schedules: List[Schedule]


class WebhookDelivery(Out):
    """One delivery of an event to one URL (tasks/webhooks.py): a task, retried with backoff."""
    id: str
    event: Optional[str]
    # The job's or run's id the event tells of
    about: Optional[str]
    url: Optional[str]
    status: JobStatus
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
