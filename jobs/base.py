"""
What a job file writes: a Job subclass with its Params, Result and run().

This module is imported twice: by the service, which reads each job's name
and schemas, and by the worker process that runs a job in its environment's
interpreter (see jobs/worker.py). That environment may be another Python with
other packages (AutoGluon's, say), so this module needs nothing but pydantic,
and job files import their heavy libraries inside run().
"""

import os
from pathlib import Path
from typing import Annotated, Any, Callable, Optional

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

# Where job input files live. The service and the worker both read it from
# the environment (the runner passes the service's value to the worker).
DATA_DIR_ENV = "JOB_DATA_DIR"


def data_dir() -> Path:
    return Path(os.environ.get(DATA_DIR_ENV) or "data/datasets").resolve()


def _inside_data_dir(value: str) -> str:
    base = data_dir()
    path = (base / value).resolve()
    if not path.is_relative_to(base) or not path.exists():
        raise ValueError(f"{value!r} must be an existing file or folder inside JOB_DATA_DIR")
    return str(path)


# A job parameter naming an input file: a path relative to JOB_DATA_DIR
# (POST /jobs/data uploads there). It is checked when the job is submitted
# and reaches run() as an absolute path.
DataFile = Annotated[
    str,
    Field(min_length=1, max_length=1024, description="A file inside JOB_DATA_DIR (see GET /jobs/data)"),
    AfterValidator(_inside_data_dir),
]


class JobParams(BaseModel):
    """Base for a job's parameters: unknown fields are an error."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class JobResult(BaseModel):
    """Base for a job's result: fields beyond it are dropped."""

    model_config = ConfigDict(extra="ignore", protected_namespaces=())


class JobContext:
    """What run() gets besides its params. Built by the worker.

    id:       the job's ID.
    workdir:  the job's own directory; write outputs here (``path()``).
              GET /jobs/{id}/files lists and serves what is in it.
    num_cpus: CPU cores the job may use (JOB_NUM_CPUS; the service keeps one).
    device:   "cpu", "cuda", "mps" or "auto" (JOB_DEVICE). With "cpu", CUDA
              is already hidden from the process.
    """

    def __init__(self, id: str, workdir: Path, num_cpus: int, device: str,
                 emit: Callable[..., None] = lambda **_: None):
        self.id = id
        self.workdir = Path(workdir)
        self.num_cpus = num_cpus
        self.device = device
        self._emit = emit

    def path(self, *parts: str) -> Path:
        """A path inside the job's directory."""
        return self.workdir.joinpath(*parts)

    def progress(self, fraction: Optional[float] = None, message: str = "") -> None:
        """Report progress: ``fraction`` done (0 to 1, None if unknown) and what is happening.

        Anything the job prints or logs goes to its log (GET /jobs/{id}/log);
        this is the short status line next to it.
        """
        if fraction is not None:
            fraction = min(max(float(fraction), 0.0), 1.0)
        self._emit(type="progress", fraction=fraction, message=message[:500])


class Job:
    """Base class for every job.

    Subclasses set ``Params`` (a JobParams subclass: what POST /jobs takes),
    optionally ``Result`` (a JobResult subclass: what run() returns, checked),
    and write ``run(ctx, params)``: plain blocking code, run in its own
    process. Register the class with ``@register_job`` (jobs/registry.py).
    """

    Params: type = JobParams
    Result: Optional[type] = None

    # Set by @register_job.
    name: str = ""
    env: str = "default"
    description: str = ""

    def run(self, ctx: JobContext, params: Any) -> Any:
        raise NotImplementedError
