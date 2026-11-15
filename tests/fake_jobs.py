"""Jobs for tests: run for real, by jobs.worker in a subprocess, with the test interpreter."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

from pydantic import Field

from jobs import DataFile, Job, JobContext, JobParams, JobResult, register_job


class EchoParams(JobParams):
    text: str = Field(..., min_length=1)
    steps: int = Field(3, ge=1, le=10)
    data: Optional[DataFile] = None


class EchoResult(JobResult):
    echo: str
    data_bytes: Optional[int] = None


@register_job("test-echo")
class Echo(Job):
    """Echo the text, reporting progress and writing an output file"""

    Params = EchoParams
    Result = EchoResult
    ref_param = "text"

    def run(self, ctx: JobContext, p: EchoParams) -> EchoResult:
        for step in range(p.steps):
            print(f"step {step}", flush=True)
            ctx.progress((step + 1) / p.steps, f"step {step}")
        ctx.path("out.txt").write_text(p.text)
        size = Path(p.data).stat().st_size if p.data else None
        return EchoResult(echo=p.text, data_bytes=size, extra="dropped")


@register_job("test-fail")
class Fail(Job):
    def run(self, ctx, params):
        raise ValueError("boom")


@register_job("test-crash")
class Crash(Job):
    def run(self, ctx, params):
        os.kill(os.getpid(), signal.SIGKILL)


class SleepParams(JobParams):
    seconds: float = 60


@register_job("test-sleep")
class Sleep(Job):
    """Sleep, with a child process, so cancelling must stop the whole group"""

    Params = SleepParams

    def run(self, ctx, p: SleepParams):
        child = subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({p.seconds})"])
        ctx.path("child.pid").write_text(str(child.pid))
        ctx.progress(None, "sleeping")
        time.sleep(p.seconds)
        return {}


class NeedsField(JobResult):
    value: int


@register_job("test-bad-result")
class BadResult(Job):
    Result = NeedsField

    def run(self, ctx, params):
        return {"other": 1}


@register_job("test-missing-env", env="no-such-env")
class MissingEnv(Job):
    def run(self, ctx, params):
        return {}


class EnvParams(JobParams):
    name: str = Field(..., min_length=1)


class EnvResult(JobResult):
    present: bool


@register_job("test-env")
class Env(Job):
    """Say whether the job's environment has a variable: the service's secrets must not reach it"""

    Params = EnvParams
    Result = EnvResult

    def run(self, ctx, p: EnvParams) -> EnvResult:
        return EnvResult(present=p.name in os.environ)
