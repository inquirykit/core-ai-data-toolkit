"""
Install or update a job environment, JOB_ENVS_DIR/<name>, from requirements-<name>.txt, with
scripts/setup_job_env.sh (its output is the job's log).

The Set up button of /admin queues it, so an environment is installed only when an admin asks: AutoGluon's is about
1.8 GB. It runs in the service's own environment, which has uv (a dependency of the service, found with
``uv.find_uv_bin()``, since the service's bin/ may not be on PATH). Cancelling it leaves the environment not ready:
the script writes the ready marker last (core/procs.py, ``env_python``).
"""

import os
import subprocess
from pathlib import Path

from pydantic import Field, field_validator

from jobs import Job, JobContext, JobParams, JobResult, register_job

ROOT = Path(__file__).resolve().parents[2]


def requirements(name: str) -> Path:
    return ROOT / f"requirements-{name}.txt"


class SetupParams(JobParams):
    name: str = Field(..., pattern=r"^[a-z0-9-]{1,64}$", description="The environment: requirements-<name>.txt names its packages")

    @field_validator("name")
    @classmethod
    def has_requirements(cls, name: str) -> str:
        if not requirements(name).is_file():
            known = sorted(p.name[len("requirements-"):-len(".txt")] for p in ROOT.glob("requirements-*.txt"))
            raise ValueError(f"No requirements-{name}.txt; the environments are {', '.join(known) or 'none'}")
        return name


class SetupResult(JobResult):
    env: str
    python: str = Field(..., description="Its interpreter, which the jobs of that environment run in")


@register_job("setup-env")
class SetupEnv(Job):
    """Install or update a job environment (AutoGluon's, say) from its requirements file"""

    Params = SetupParams
    Result = SetupResult

    def run(self, ctx: JobContext, p: SetupParams) -> SetupResult:
        from uv import find_uv_bin

        ctx.progress(None, f"installing the {p.name} environment")
        subprocess.run(["sh", str(ROOT / "scripts" / "setup_job_env.sh"), p.name],
                       env={**os.environ, "UV": find_uv_bin()}, check=True)
        envs = Path(os.environ.get("JOB_ENVS_DIR") or ROOT / "envs")
        return SetupResult(env=p.name, python=str(envs / p.name / "bin" / "python"))
