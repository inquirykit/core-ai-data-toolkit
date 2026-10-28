"""
Predictors trained by the autogluon-* jobs (jobs/autogluon/), served as
model ID "autogluon:<job id>".

AutoGluon cannot be installed next to the service's own models (see
requirements-autogluon.txt), so a loaded predictor is a process in the
"autogluon" environment (jobs/autogluon/_serve.py) that this class talks to
over its stdin and stdout. To the cache it is a model like any other: loaded
on first use, counted by its size on disk, evicted when idle, and dropped if
the process dies.

It also defines the three tasks such predictors serve.
"""

import asyncio
import json
import re
import signal
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import Field

import procs
from config import Config
from procs import PROJECT_ROOT
from models import BaseModel, register
from models.tasks import TaskInput, TaskOutput, define_task

MODEL_ID_PREFIX = "autogluon:"
ENV = "autogluon"
MAX_ROWS = 10_000
MAX_HISTORY_ROWS = 1_000_000
# What a predictor process holds beyond its files: Python, pandas, torch (measured: 60 to 230 MB).
PROCESS_OVERHEAD_BYTES = 256 * 1024 * 1024
_JOB_ID = re.compile(r"^[0-9a-f]{8,32}$")

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


class MultimodalPredictionInput(TabularPredictionInput):
    """Image columns hold image paths inside IMAGE_DIR."""


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
            "Predict a column of a table with a predictor trained by the autogluon-tabular job")
define_task("multimodal-prediction", MultimodalPredictionInput, PredictionOutput,
            "Predict from text, image and tabular columns with a predictor trained by the autogluon-multimodal job")
define_task("timeseries-forecast", ForecastInput, ForecastOutput,
            "Forecast series with a predictor trained by the autogluon-timeseries job")


# ------------------------------------------------------------------- model
def workdir_for(model_id: str) -> Optional[Path]:
    """The job directory of ``autogluon:<job id>``, or None if the ID is not one."""
    if not Config.JOBS_DIR or not model_id.startswith(MODEL_ID_PREFIX):
        return None
    job_id = model_id[len(MODEL_ID_PREFIX):]
    if not _JOB_ID.match(job_id):
        return None
    return Path(Config.JOBS_DIR).resolve() / job_id


def predictor_info(model_id: str) -> Optional[Dict[str, Any]]:
    """What the training job wrote about the predictor, or None if there is no trained predictor."""
    workdir = workdir_for(model_id)
    if workdir is None:
        return None
    try:
        return json.loads((workdir / "predictor.json").read_text())
    except (OSError, ValueError):
        return None


def trained(model_id: str) -> bool:
    """Whether ``model_id`` names a predictor a job trained here (allowed whatever ALLOWED_MODELS says)."""
    return predictor_info(model_id) is not None


def _size(path: Path) -> int:
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())


@register(task="tabular-prediction", match="autogluon:*")
@register(task="multimodal-prediction", match="autogluon:*")
@register(task="timeseries-forecast", match="autogluon:*")
class AutoGluonPredictor(BaseModel):
    """A trained AutoGluon predictor, answered by a process in the autogluon environment."""

    kind = "server"
    serve_module = "jobs.autogluon._serve"
    startup_timeout = 600.0
    request_timeout = 600.0

    @classmethod
    def weights_source(cls, model_id: str) -> Optional[str]:
        return None  # nothing from the Hub

    @property
    def alive(self) -> bool:
        process = getattr(self, "process", None)
        return process is None or process.returncode is None

    async def estimate_bytes(self) -> Optional[int]:
        workdir = workdir_for(self.model_id)
        if workdir is None or not workdir.is_dir():
            return None
        return PROCESS_OVERHEAD_BYTES + await asyncio.to_thread(_size, workdir / "predictor")

    def python(self) -> Path:
        return Config.JOB_ENVS_DIR / ENV / "bin" / "python"

    async def load(self) -> None:
        info = predictor_info(self.model_id)
        if info is None:
            raise ValueError(f"{self.model_id}: no trained predictor; an autogluon-* job's result gives the model ID")
        if info["task"] != self.task:
            raise ValueError(f"{self.model_id} is a {info['task']} predictor, not {self.task}")
        python = self.python()
        if not python.exists():
            raise RuntimeError(f"The {ENV} environment is not set up: run scripts/setup_job_env.sh {ENV}")
        self.info = info
        # Its stderr (AutoGluon's log) goes to this service's log.
        self.process = procs.track(await asyncio.create_subprocess_exec(
            str(python), "-m", self.serve_module, str(workdir_for(self.model_id)),
            cwd=str(PROJECT_ROOT), env=procs.python_env(Config.JOB_DEVICE == "cpu"),
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            start_new_session=True, limit=256 * 1024 * 1024,
        ))
        ready = await self._read(self.startup_timeout, "loading")
        if not ready.get("ready"):
            raise RuntimeError(f"{self.model_id}: unexpected first message {ready!r}")

    async def predict(self, request: Any) -> Any:
        payload = request.model_dump(mode="json")
        if self.task == "multimodal-prediction":
            payload["rows"] = self._images_inside_image_dir(payload["rows"])
        self.process.stdin.write(json.dumps(payload).encode() + b"\n")
        await self.process.stdin.drain()
        reply = await self._read(self.request_timeout, "predicting")
        if "error" in reply:
            raise (ValueError if reply.get("client") else RuntimeError)(reply["error"])
        return reply["result"]

    async def unload(self) -> None:
        process = getattr(self, "process", None)
        if process is None or process.returncode is not None:
            return
        process.stdin.close()
        await procs.stop(process, 10)

    async def _read(self, timeout: float, doing: str) -> Dict[str, Any]:
        try:
            line = await asyncio.wait_for(self.process.stdout.readline(), timeout)
        except asyncio.TimeoutError:
            # Its answer would come late and out of step with the next request.
            procs.signal_group(self.process.pid, signal.SIGKILL)
            raise RuntimeError(f"{self.model_id}: no answer after {timeout:.0f}s {doing}") from None
        if not line:
            await self.process.wait()
            raise RuntimeError(
                f"{self.model_id}: predictor process exited with code {self.process.returncode} while {doing} "
                "(its output is in this service's log)"
            )
        return json.loads(line)

    def _images_inside_image_dir(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Image paths resolved inside IMAGE_DIR, the only place requests may point to."""
        base = Config.IMAGE_DIR
        for row in rows:
            for column in self.info.get("image_columns", []):
                value = row.get(column)
                if not value:
                    continue
                path = (base / str(value)).resolve()
                if not path.is_relative_to(base) or not path.is_file():
                    raise ValueError(f"{column}: {value!r} must be an existing file inside IMAGE_DIR")
                row[column] = str(path)
        return rows
