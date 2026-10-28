"""
What the AutoGluon jobs share: reading tables, progress while fit() runs,
and predictor.json, which tells models/autogluon/predictor.py how to serve
the predictor a job trained.

Runs in the "autogluon" environment; pandas is imported inside functions so
the service can still import the job files.
"""

import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from jobs import JobContext, JobResult

# A trained predictor is served as model ID "autogluon:<job id>".
MODEL_ID_PREFIX = "autogluon:"
PREDICTOR_DIR = "predictor"
INFO_FILE = "predictor.json"

TimeLimit = Field(600, ge=10, le=7 * 86400, description="Seconds fit() may take")
EvalMetric = Field(None, max_length=64, description="AutoGluon's eval_metric name; unset: chosen by problem type")
ProblemType = Optional[Literal["binary", "multiclass", "regression", "quantile"]]


class TrainResult(JobResult):
    model_id: str = Field(..., description="Serve the predictor with POST /inference under this model ID")
    task: str = Field(..., description="... and this task")
    eval_metric: str
    best_model: str
    validation_score: Optional[float] = Field(None, description="Higher is better (AutoGluon flips error metrics)")
    test_score: Optional[float] = Field(None, description="On test_file, if given; higher is better")
    leaderboard: List[Dict[str, Any]] = Field(default_factory=list, description="The best models, best first")
    train_rows: int
    fit_seconds: float


def read_table(path: str):
    """A CSV, TSV, Parquet, JSON Lines or Excel file as a DataFrame."""
    import pandas as pd

    suffix = Path(path).suffix.lower()
    if suffix in (".parquet", ".pq"):
        return pd.read_parquet(path)
    if suffix in (".jsonl", ".ndjson"):
        return pd.read_json(path, lines=True)
    if suffix == ".json":
        return pd.read_json(path)
    if suffix in (".xlsx", ".xls"):
        return pd.read_excel(path)
    return pd.read_csv(path, sep="\t" if suffix == ".tsv" else ",")


def require_columns(df, columns: List[str], what: str) -> None:
    missing = [c for c in columns if c not in df.columns]
    if missing:
        raise ValueError(f"{what} has no column {', '.join(map(repr, missing))}; it has {list(df.columns)[:50]}")


def records(df, limit: Optional[int] = None) -> List[Dict[str, Any]]:
    """DataFrame rows as JSON-safe dicts (NaN becomes null)."""
    if limit is not None:
        df = df.head(limit)
    return json.loads(df.to_json(orient="records", date_format="iso"))


def plain(values: Any) -> Optional[List[Any]]:
    """numpy scalars (class labels, say) as plain Python values."""
    if values is None:
        return None
    return [v.item() if hasattr(v, "item") else v for v in values]


def number(value: Any) -> Optional[float]:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value == value else None  # NaN


class TimedProgress:
    """While fit() runs: progress as the share of ``time_limit`` used, reported every few seconds.

    fit() reports nothing itself, and it ends close to its time limit, so
    elapsed time is the best estimate there is. ``start`` and ``end`` place
    that span within the job's overall progress.
    """

    def __init__(self, ctx: JobContext, time_limit: float, message: str, start: float = 0.05, end: float = 0.9,
                 every: float = 5.0):
        self.ctx, self.time_limit, self.message = ctx, time_limit, message
        self.start, self.end, self.every = start, end, every
        self._done = threading.Event()

    def __enter__(self):
        began = time.monotonic()

        def report():
            while not self._done.wait(self.every):
                share = min((time.monotonic() - began) / self.time_limit, 1.0)
                self.ctx.progress(self.start + share * (self.end - self.start), self.message)

        self.ctx.progress(self.start, self.message)
        threading.Thread(target=report, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self._done.set()


def write_info(ctx: JobContext, kind: str, task: str, **details: Any) -> str:
    """Write predictor.json next to the predictor, for serving; returns the model ID."""
    info = {"kind": kind, "task": task, "path": PREDICTOR_DIR, **details}
    ctx.path(INFO_FILE).write_text(json.dumps(info, indent=2, default=str))
    return MODEL_ID_PREFIX + ctx.id


def without_nvml_on_macos() -> None:
    """Keep AutoGluon multimodal from calling NVIDIA's NVML on a Mac.

    It counts an Apple GPU as a GPU and then, to log its memory, calls
    nvidia_smi (installed, but NVML exists only with NVIDIA drivers), which
    fails the whole fit. When nvidia_smi cannot be imported it skips that.
    """
    if sys.platform == "darwin":
        sys.modules["nvidia_smi"] = None


def gpus(ctx: JobContext) -> Any:
    """fit()'s num_gpus: none on JOB_DEVICE=cpu, else AutoGluon's choice."""
    return 0 if ctx.device == "cpu" else "auto"
