"""
Which job classes exist.

Every job file registers its class with ``@register_job``. On first use,
``discover()`` imports every ``.py`` file in the subfolders of jobs/ so those
decorators run; nothing else has to be edited to add a job. Files and folders
whose names start with "_" or "." are skipped.

Job files are imported by the service, whose interpreter may lack the job's
libraries, so they import those inside run() (see jobs/base.py).
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from discovery import import_files
from errors import Refused

logger = logging.getLogger(__name__)

JOBS_PACKAGE_DIR = Path(__file__).resolve().parent


class UnknownJob(Refused):
    """No job is registered under this name (a client error)."""


@dataclass(frozen=True)
class JobType:
    name: str
    cls: type
    env: str
    description: str

    @property
    def target(self) -> List[str]:
        """Module and class name, how the worker finds the class."""
        return [self.cls.__module__, self.cls.__qualname__]

    def describe(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "env": self.env,
            "description": self.description,
            "params": self.cls.Params.model_json_schema(),
            "result": self.cls.Result.model_json_schema() if self.cls.Result is not None else None,
        }


_types: Dict[str, JobType] = {}
_discovered = False


def register_job(name: str, *, env: str = "default", description: str = ""):
    """Class decorator that makes a Job runnable as ``name``.

    env: the environment it runs in. "default" is the service's own
         interpreter; any other name is JOB_ENVS_DIR/<env>, made by
         scripts/setup_job_env.sh <env> from requirements-<env>.txt.
    """

    def decorate(cls):
        cls.name, cls.env = name, env
        cls.description = description or (cls.__doc__ or "").strip().split("\n")[0]
        _types[name] = JobType(name, cls, env, cls.description)
        return cls

    return decorate


def discover() -> None:
    """Import every job file once (discovery.import_files), so its @register_job runs."""
    global _discovered
    if _discovered:
        return
    _discovered = True
    import_files(JOBS_PACKAGE_DIR, __package__, "job")
    logger.info("Registered %d jobs", len(_types))


def get(name: str) -> JobType:
    discover()
    try:
        return _types[name]
    except KeyError:
        raise UnknownJob(f"Unknown job {name!r}. Known jobs: {', '.join(sorted(_types)) or 'none'}") from None


def all_types() -> List[JobType]:
    discover()
    return [_types[name] for name in sorted(_types)]
