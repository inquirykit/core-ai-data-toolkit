"""
Predictors trained by the autogluon-* jobs (jobs/autogluon/), served as
model ID "autogluon:<job id>" (models/trained.py finds them).

AutoGluon cannot be installed next to the service's own models (see
requirements-autogluon.txt), so a loaded predictor is a process in the
"autogluon" environment (jobs/autogluon/_serve.py) that this class talks to
over its stdin and stdout. To the cache it is a model like any other: loaded
on first use, counted by its size on disk, evicted when idle, and dropped if
the process dies.

It also defines the two tasks such predictors serve.
"""

import asyncio
import json
import signal
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import Field

from core import procs
from core.config import Config
from core import PROJECT_ROOT
from core.errors import Refused, Unavailable
from models import BaseModel, ModelCard, register, trained
from models.base import TryNextBackend
from models.contracts import TaskInput, TaskOutput, define_task

# The model ID the AutoGluon jobs write in their result (jobs/autogluon/_common.py; tests/test_trained_models.py ties
# the two).
MODEL_ID_PREFIX = "autogluon:"
ENV = "autogluon"
MAX_ROWS = 10_000
MAX_HISTORY_ROWS = 1_000_000
# What a predictor process holds beyond its files: Python, pandas, torch (measured: 60 to 230 MB).
PROCESS_OVERHEAD_BYTES = 256 * 1024 * 1024
# The predictor's own folder in its job's.
PREDICTOR_DIR = "predictor"

Rows = Field(..., min_length=1, max_length=MAX_ROWS, description="One object per row, keyed by column name")


# ------------------------------------------------------------------- tasks
class TabularPredictionInput(TaskInput):
    rows: List[Dict[str, Any]] = Rows
    probabilities: bool = Field(False, description="Also return every class's probability (classification only)")


class PredictionOutput(TaskOutput):
    predictions: List[Any] = Field(..., description="One prediction per row, in order")
    probabilities: Optional[List[Dict[str, Optional[float]]]] = Field(
        None, description="Per row, every class's probability, if asked for"
    )


class ForecastInput(TaskInput):
    rows: List[Dict[str, Any]] = Field(
        ..., min_length=1, max_length=MAX_HISTORY_ROWS,
        description="The series' history: rows with the ID, timestamp and target columns the predictor was trained on",
    )
    known_covariates: Optional[List[Dict[str, Any]]] = Field(
        None, max_length=MAX_HISTORY_ROWS, description="Future values of the known covariates, if it has any",
    )


class ForecastOutput(TaskOutput):
    forecast: List[Dict[str, Any]] = Field(
        ..., description="Per series and future time step: the ID, timestamp, mean and one column per quantile"
    )


define_task("tabular-prediction", TabularPredictionInput, PredictionOutput,
            "Predict a column of a table with a predictor trained by the autogluon-tabular job",
            category="prediction", label="Tabular prediction")
define_task("timeseries-forecast", ForecastInput, ForecastOutput,
            "Forecast series with a predictor trained by the autogluon-timeseries job",
            category="prediction", label="Time-series forecast")


# ------------------------------------------------------------------- model
def _size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


@register(task="tabular-prediction", match=f"{MODEL_ID_PREFIX}*")
@register(task="timeseries-forecast", match=f"{MODEL_ID_PREFIX}*")
class AutoGluonPredictor(BaseModel):
    """A trained AutoGluon predictor, answered by a process in the autogluon environment."""

    kind = "server"
    serve_module = "jobs.autogluon._serve"
    startup_timeout = 600.0
    request_timeout = 600.0

    @classmethod
    def weights_source(cls, model_id: str) -> Optional[str]:
        return None  # nothing from the Hub

    @classmethod
    def listed(cls, task: str) -> List[ModelCard]:
        return [ModelCard(t.model_id, t.name) for t in trained.listed(MODEL_ID_PREFIX, task)]

    @classmethod
    def operator_chose(cls, model_id: str) -> bool:
        return trained.find(model_id, MODEL_ID_PREFIX) is not None  # a job here trained it; only an admin starts jobs

    # Set when an answer may still be on its way to no one (see ``_kill``).
    out_of_step = False

    @property
    def alive(self) -> bool:
        process = getattr(self, "process", None)
        return not self.out_of_step and (process is None or process.returncode is None)

    async def estimate_bytes(self) -> Optional[int]:
        found = await asyncio.to_thread(trained.find, self.model_id, MODEL_ID_PREFIX)
        if found is None:
            return None
        return PROCESS_OVERHEAD_BYTES + await asyncio.to_thread(_size, found.workdir / PREDICTOR_DIR)


    async def load(self) -> None:
        found = await asyncio.to_thread(trained.find, self.model_id, MODEL_ID_PREFIX)
        if found is None:
            raise Refused(f"{self.model_id}: no trained predictor; an autogluon-* job's result gives the model ID")
        if found.task != self.task:
            raise Refused(f"{self.model_id} is a {found.task} predictor, not {self.task}")
        python = procs.env_python(Config.JOB_ENVS_DIR, ENV)
        if python is None:
            raise Unavailable(f"The {ENV} environment is not set up: set it up from /admin (the setup-env job), or run "
                              f"scripts/setup_job_env.sh {ENV}")
        # Its stderr (AutoGluon's log) goes to this service's log.
        self.process = procs.track(await asyncio.create_subprocess_exec(
            str(python), "-m", self.serve_module, str(found.workdir),
            cwd=str(PROJECT_ROOT), env=procs.python_env(Config.JOB_DEVICE == "cpu"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            start_new_session=True, limit=256 * 1024 * 1024,
        ))
        try:
            ready = await self._read(self.startup_timeout, "loading")
        except BaseException:
            # Cancelled (the request it loads for went away) or failed: no process is left running for nobody.
            self._kill()
            raise
        if not ready.get("ready"):
            raise RuntimeError(f"{self.model_id}: unexpected first message {ready!r}")

    async def predict(self, request: Any) -> Any:
        if self.out_of_step or self.process.returncode is not None:
            # Stopped while this request waited its turn (the one before it was cancelled), or crashed: the manager
            # drops it and serves this request with a new one.
            raise TryNextBackend(f"{self.model_id}: its process has stopped; starting another")
        payload = request.model_dump(mode="json")
        try:
            self.process.stdin.write(json.dumps(payload).encode() + b"\n")
            await self.process.stdin.drain()
            reply = await self._read(self.request_timeout, "predicting")
        except BaseException:
            # Cancelled (a client gone, an eval run stopped) or failed between the request and its answer.
            self._kill()
            raise
        if "error" in reply:
            raise (Refused if reply.get("client") else RuntimeError)(reply["error"])
        return reply["result"]

    async def unload(self) -> None:
        process = getattr(self, "process", None)
        if process is None:
            return
        if process.returncode is not None:
            procs.untrack(process)
            return
        process.stdin.close()
        await procs.stop(process, 10)

    async def _read(self, timeout: float, doing: str) -> Dict[str, Any]:
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
        except asyncio.TimeoutError:
            self._kill()
            raise RuntimeError(f"{self.model_id}: no answer after {timeout:.0f}s {doing}") from None
        if not line:
            await self.process.wait()
            procs.untrack(self.process)
            raise RuntimeError(
                f"{self.model_id}: predictor process exited with code {self.process.returncode} while {doing} "
                "(its output is in this service's log)"
            )
        return json.loads(line)

    def _kill(self) -> None:
        """End the process now. An answer it still owes would reach the next caller as theirs; the cache sees it is
        not alive and drops it, and the next request starts another."""
        self.out_of_step = True
        if self.process.returncode is None:
            procs.signal_group(self.process.pid, signal.SIGKILL)
        procs.untrack(self.process)
