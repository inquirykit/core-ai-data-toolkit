"""/jobs: jobs, their types, their input files and their schedules."""

from typing import List, Optional

from pydantic import ConfigDict

from tasks.schedule import ScheduleSpec
from .common import OPEN, Json, Out
from .tasks import Task


class Jobs(Out):
    jobs: List[Task]
    next: Optional[str] = None


class FileEntry(Out):
    """A file in DATA_DIR/datasets or in a job's folder: its path there, and its size."""
    path: str
    bytes: int


class Files(Out):
    files: List[FileEntry]


class ColumnSummary(Out):
    """One column of a data file, as DuckDB reads it."""
    name: str
    # DuckDB's type: BIGINT, DOUBLE, VARCHAR, DATE, TIMESTAMP, ...
    type: str
    min: Optional[str]
    max: Optional[str]
    # Approximate, so within a few percent of the true count
    distinct: Optional[int]
    # Numbers only
    mean: Optional[float]
    # The share of rows without a value, 0 to 1
    missing: float


class FileSummary(Out):
    rows: int
    columns: List[ColumnSummary]
    # The first rows, each column by name
    head: List[Json]


class JobType(Out):
    name: str
    description: str
    # "default" for the service's own interpreter, else a JOB_ENVS_DIR environment
    env: str
    # Whether that environment is set up (a job in one that is not is refused); the setup-env job sets it up
    env_ready: bool
    # Whether it was set up from its requirements file as it is now; false: the setup-env job updates it
    env_current: bool
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


class Schedule(ScheduleSpec):
    """A schedule: its spec, as POST and PUT take it, with its kind and id, when it was made and changed, its next and
    last run, and the last task's id and status."""
    model_config = ConfigDict(**OPEN, json_schema_serialization_defaults_required=True)

    id: str
    kind: str
    created: float
    updated: float
    next_run: Optional[float]
    last_run: Optional[float]
    last_task_id: Optional[str]
    last_status: Optional[str]


class Schedules(Out):
    schedules: List[Schedule]
