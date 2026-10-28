"""
JobRunner: jobs, the kind of task (jobs/tasks.py) that runs a script in its own process.

Every job runs as ``python -m jobs.worker`` in its environment's interpreter
(the service's own for env "default", JOB_ENVS_DIR/<env> otherwise), in a new
process group. So a job can use libraries the service cannot install, a crash
or out-of-memory kill takes down only the job, and cancelling it kills the
whole group, the processes the job started included (Ray's, for example).

Jobs are one kind of the task runner's: at most ``max_running`` run at once,
and the rest wait, the most urgent first, then the oldest. Their rows live in the JobStore (the
ArcadeDB database, which only this process opens); the log in
<workdir>/job.log. A job a stopped service left running (it ends itself when
the service goes away) is marked failed at the next start. Schedules are the
Scheduler's (jobs/schedule.py); this runner checks them every POLL_S.
"""

import asyncio
import json
import logging
import os
import shutil
import signal
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

import procs
from background import every
from errors import Conflict, Refused
from procs import PROJECT_ROOT

from . import registry
from .base import DATA_DIR_ENV
from .schedule import POLL_S, Scheduler
from .store import FINISHED, JobNotFound, public
from .tasks import Kind, TaskFailed, TaskRunner
from .webhooks import Webhooks

logger = logging.getLogger(__name__)

# Seconds between SIGTERM and SIGKILL when stopping a job.
STOP_GRACE = 10.0
# Progress is written to the store at most this often.
PROGRESS_EVERY = 0.5
# Longest line read from a worker's channel (its result).
CHANNEL_LIMIT = 64 * 1024 * 1024

__all__ = ["JobNotFound", "JobRunner"]


class JobRunner:
    def __init__(
        self,
        tasks: TaskRunner,
        webhooks: Webhooks,
        jobs_dir: Path,
        envs_dir: Path,
        data_dir: Path,
        max_running: int = 1,
        num_cpus: int = 0,
        device: str = "cpu",
    ):
        self.tasks = tasks
        self.store = tasks.store
        # The service's one Webhooks: api/app.py makes and closes it, and eval runs post through it too.
        self.webhooks = webhooks
        self.scheduler = Scheduler(self.store, self.submit)
        self._scheduling: Optional[asyncio.Task] = None
        self.jobs_dir = Path(jobs_dir)
        self.envs_dir = Path(envs_dir)
        self.data_dir = Path(data_dir)
        self.max_running = max(1, max_running)
        # 0: every core but one, which stays with the service.
        self.num_cpus = num_cpus or max(1, (os.cpu_count() or 2) - 1)
        self.device = device
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        tasks.add(Kind("job", run=self._run, stop=self._stop, concurrency=self.max_running, finished=self._notify))

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Start submitting the schedules' jobs; the task runner starts the jobs themselves."""
        self._scheduling = asyncio.create_task(every(POLL_S, self.scheduler.tick, "The scheduler"), name="job-scheduler")

    async def stop(self) -> None:
        """Stop submitting the schedules' jobs; the task runner stops the running ones."""
        if self._scheduling is not None:
            self._scheduling.cancel()
            await asyncio.gather(self._scheduling, return_exceptions=True)
            self._scheduling = None

    # -------------------------------------------------------------- queries
    def python_for(self, env: str) -> Optional[str]:
        """The interpreter of ``env``, or None if it has not been set up."""
        if env == "default":
            return sys.executable
        python = self.envs_dir / env / "bin" / "python"
        return str(python) if python.exists() else None

    def env_ready(self, env: str) -> bool:
        return self.python_for(env) is not None

    def workdir(self, id: str) -> Path:
        return self.jobs_dir / id

    async def get(self, id: str) -> Dict[str, Any]:
        """The job ``id``; JobNotFound for no task, or a task of another kind."""
        row = await self.tasks.get(id)
        if row["kind"] != "job":
            raise JobNotFound("Job not found")
        return row

    # ------------------------------------------------------------- commands
    async def submit(self, name: str, params: Any, schedule_id: Optional[str] = None,
                     webhook_url: Optional[str] = None, priority: str = "normal") -> Dict[str, Any]:
        """Check and queue a job. Refused for an unknown job, bad params or a missing environment."""
        job = registry.get(name)
        try:
            checked = job.cls.Params.model_validate(params or {})
        except ValueError as e:  # the params' ValidationError
            raise Refused(str(e)) from e
        if not self.env_ready(job.env):
            raise Refused(
                f"Job {name!r} runs in the {job.env!r} environment, which is not set up: "
                f"run scripts/setup_job_env.sh {job.env}"
            )
        id = uuid.uuid4().hex[:16]
        workdir = self.workdir(id)
        workdir.mkdir(parents=True)
        dumped = checked.model_dump(mode="json")
        (workdir / "params.json").write_text(checked.model_dump_json(indent=2))
        row = await self.tasks.submit("job", id=id, job=name, params=dumped, priority=priority,
                                      schedule_id=schedule_id, webhook_url=webhook_url)
        logger.info("Queued job %s (%s, %s priority)", id, name, priority)
        return row

    async def cancel(self, id: str) -> Dict[str, Any]:
        """Cancel a queued job, or stop a running one (its whole process group). A finished one is left as is."""
        await self.get(id)
        return await self.tasks.cancel(id)

    async def delete(self, id: str) -> None:
        """Remove a finished job and its files. Conflict while it is queued or running."""
        row = await self.get(id)
        if row["status"] not in FINISHED:
            raise Conflict("Cancel the job before deleting it")
        await asyncio.to_thread(shutil.rmtree, self.workdir(id), ignore_errors=True)
        await self.store.delete(id)

    # ------------------------------------------------------------ execution
    async def _run(self, row: Dict[str, Any]) -> Any:
        """The job's process, from start to exit: its result, or TaskFailed with what went wrong."""
        id = row["id"]
        try:
            job = registry.get(row["job"])
        except registry.UnknownJob as e:
            raise TaskFailed(str(e)) from e
        python = self.python_for(job.env)
        if python is None:
            raise TaskFailed(f"Environment {job.env!r} is not set up")

        workdir = self.workdir(id)
        env = procs.python_env(
            self.device == "cpu",
            JOB_ID=id,
            **{DATA_DIR_ENV: str(self.data_dir)},
            JOB_NUM_CPUS=str(self.num_cpus),
            JOB_DEVICE=self.device,
            # Libraries that size their thread pools from these stay within num_cpus.
            OMP_NUM_THREADS=str(self.num_cpus),
            MKL_NUM_THREADS=str(self.num_cpus),
        )
        process = await asyncio.create_subprocess_exec(
            python, "-m", "jobs.worker", *job.target, str(workdir),
            cwd=str(PROJECT_ROOT), env=env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, limit=CHANNEL_LIMIT,
        )
        self._processes[id] = procs.track(process)
        await self.store.set_pid(id, process.pid)
        logger.info("Started job %s (%s) as pid %d", id, job.name, process.pid)
        if (await self.get(id))["cancel"]:  # cancelled as it started: it ends as cancelled
            await procs.stop(process, STOP_GRACE)

        outcome: Dict[str, Any] = {}
        try:
            with open(workdir / "job.log", "ab") as log:
                await asyncio.gather(
                    self._read_channel(id, process.stdout, outcome, log),
                    self._copy_log(process.stderr, log),
                )
            code = await process.wait()
        except asyncio.CancelledError:
            # The service is shutting down; the task runner marks the job interrupted.
            await asyncio.shield(procs.stop(process, STOP_GRACE))
            raise
        finally:
            self._processes.pop(id, None)
            procs.untrack(process)
            # Children the job left behind (a Ray worker, say).
            procs.signal_group(process.pid, signal.SIGKILL)

        if "result" in outcome and code == 0:
            return outcome["result"]
        # A cancel killed it: the task runner sees the cancel and marks it cancelled.
        raise TaskFailed(outcome.get("error") or _exit_reason(code))

    async def _stop(self, row: Dict[str, Any]) -> None:
        """Stop a running job's process group; its run then ends and the job is marked cancelled."""
        process = self._processes.get(row["id"])
        if process is not None:
            await procs.stop(process, STOP_GRACE)

    async def _notify(self, row: Dict[str, Any]) -> None:
        """POST the finished job to the webhooks: WEBHOOK_URLS and the job's own URL."""
        self.webhooks.send("job.finished", public(row), extra_urls=[row.get("webhook_url")])

    async def _read_channel(self, id: str, stream: asyncio.StreamReader, outcome: Dict[str, Any], log) -> None:
        last_write = 0.0
        pending = None
        while True:
            line = await stream.readline()
            if not line:
                break
            try:
                message = json.loads(line)
            except ValueError:
                log.write(line)  # not a message; keep it in the log
                continue
            kind = message.get("type")
            if kind == "progress":
                pending = (message.get("fraction"), message.get("message") or "")
                if time.monotonic() - last_write >= PROGRESS_EVERY:
                    await self.store.progress(id, *pending)
                    last_write, pending = time.monotonic(), None
            elif kind == "result":
                outcome["result"] = message.get("result")
            elif kind == "error":
                outcome["error"] = message.get("error")
        if pending is not None:
            await self.store.progress(id, *pending)

    @staticmethod
    async def _copy_log(stream: asyncio.StreamReader, log) -> None:
        while True:
            chunk = await stream.read(65536)
            if not chunk:
                return
            log.write(chunk)
            log.flush()


def _exit_reason(code: Optional[int]) -> str:
    if code is not None and code < 0:
        name = signal.Signals(-code).name
        hint = " (out of memory?)" if code == -signal.SIGKILL else ""
        return f"The job's process was killed by {name}{hint}; see its log"
    return f"The job's process exited with code {code}; see its log"
