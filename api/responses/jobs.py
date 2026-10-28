"""/jobs: jobs, their types, schedules and webhooks."""

from typing import Any, List, Literal, Optional


from .common import Json, Out


JobStatus = Literal["queued", "running", "succeeded", "failed", "cancelled"]
# How urgent a task is: of the queued tasks of a kind, the most urgent starts first (jobs/tasks.py).
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
    created: float
    updated: float
    next_run: Optional[float]
    last_run: Optional[float]
    last_job_id: Optional[str]
    last_status: Optional[str]


class Schedules(Out):
    schedules: List[Schedule]


class Webhooks(Out):
    urls: List[str]
    secret_set: bool
    events: List[str]


class WebhookTestResult(Out):
    ok: bool
    status: Optional[int]
    error: Optional[str]
