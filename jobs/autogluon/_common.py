"""
What the AutoGluon jobs share: reading tables, building a time series from
one, progress while fit() runs, the model ID a trained predictor is served as,
and report.json. The job's result.json and params.json tell
models/families/autogluon/predictor.py and _serve.py how to serve it.

The result stays small: it is kept on the job's row, returned in every page of
/jobs and posted to the webhooks. What the pages draw (column types, feature
importance, the classification report, a forecast per series) goes in
report.json beside it, which GET /jobs/{id}/files/report.json serves.

Runs in the "autogluon" environment; pandas is imported inside functions so
the service can still import the job files.
"""

import json
import math
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from jobs import JobContext, JobResult

# A trained predictor is served as model ID "autogluon:<job id>", for its task, from its folder by
# models/families/autogluon/predictor.py, which has its own copy of these, as neither side imports the other:
# tests/test_trained_models.py ties them.
MODEL_ID_PREFIX = "autogluon:"
TABULAR_TASK, TIMESERIES_TASK = "tabular-prediction", "timeseries-forecast"
PREDICTOR_DIR = "predictor"
REPORT_FILE = "report.json"
FORECAST_FILE = "forecast.csv"
# AutoGluon's own names for a series' ID and time step, in its frames.
SERIES_INDEX, TIME_INDEX = "item_id", "timestamp"

TimeLimit = Field(600, ge=10, le=7 * 86400, description="Seconds fit() may take")
EvalMetric = Field(None, max_length=64, description="AutoGluon's eval_metric name; unset: chosen by problem type")
Name = Field(None, max_length=200, description="What the predictor is called in the pages and the model lists; "
                                               "unset: its training file's name")
ProblemType = Optional[Literal["binary", "multiclass", "regression", "quantile"]]


class TrainResult(JobResult):
    model_id: str = Field(..., description="Serve the predictor with POST /inference under this model ID")
    task: str = Field(..., description="... and this task")
    eval_metric: str
    best_model: str
    validation_score: Optional[float] = Field(None, description="Higher is better (AutoGluon flips error metrics)")
    test_score: Optional[float] = Field(None, description="On rows it did not train on, if any; higher is better")
    lower_is_better: bool = Field(
        False, description="The metric is an error (RMSE, log loss, WQL): the scores are it negated, so show them positive",
    )
    leaderboard: List[Dict[str, Any]] = Field(default_factory=list, description="The best models, best first")
    train_rows: int
    fit_seconds: float


def read_table(path: str):
    """A CSV, TSV, Parquet or JSON Lines file as a DataFrame."""
    import pandas as pd

    suffix = Path(path).suffix.lower()
    if suffix in (".parquet", ".pq"):
        return pd.read_parquet(path)
    if suffix in (".jsonl", ".ndjson"):
        return pd.read_json(path, lines=True)
    if suffix == ".json":
        return pd.read_json(path)
    # Its delimiter and decimal mark as the page's summary of the file reads them (jobs/data.py).
    from core.csvfiles import dialect

    delimiter, decimal = dialect(path, "\t" if suffix == ".tsv" else ",")
    return pd.read_csv(path, sep=delimiter, decimal=decimal)


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


def model_id(ctx: JobContext) -> str:
    """The model ID the predictor this job trains is served as."""
    return MODEL_ID_PREFIX + ctx.id


def gpus(ctx: JobContext) -> Any:
    """fit()'s num_gpus: none on JOB_DEVICE=cpu, else AutoGluon's choice."""
    return 0 if ctx.device == "cpu" else "auto"


def lower_is_better(metric: Any) -> bool:
    """Whether AutoGluon's ``metric`` is an error. Its ``greater_is_better`` is true for every metric, since it
    reports errors negated; ``greater_is_better_internal`` says what the metric itself is."""
    return not metric.greater_is_better_internal


def free_column(df, name: str) -> str:
    """``name``, or ``name`` with underscores, whichever no column of ``df`` has."""
    while name in df.columns:
        name = f"_{name}_"
    return name


def series_frame(df, id_column: Optional[str], timestamp_column: str, what: str):
    """A long table (a row per series and time step) as AutoGluon's TimeSeriesDataFrame.

    Without ``id_column`` the whole table is one series, under an ID column
    added here (``forecast_table`` drops it again); a table whose timestamps
    repeat is then several series, refused with what to do, since AutoGluon's
    own error speaks of a frequency it cannot infer.
    """
    import pandas as pd
    from autogluon.timeseries import TimeSeriesDataFrame

    df = df.copy()
    df[timestamp_column] = pd.to_datetime(df[timestamp_column])
    if id_column is None:
        if df[timestamp_column].duplicated().any():
            raise ValueError(f"{what}: several series share these timestamps: pick the series ID column")
        id_column = free_column(df, "series")
        df[id_column] = "series"
    return TimeSeriesDataFrame.from_data_frame(df, id_column=id_column, timestamp_column=timestamp_column)


def forecast_table(forecast, id_column: Optional[str], timestamp_column: str):
    """A forecast as a flat table, keyed as the training file was: its ID column (none for one series), its timestamp
    column, then the mean and a column per quantile."""
    out = forecast.reset_index()
    out.columns = [str(c) for c in out.columns]
    out = out.drop(columns=[SERIES_INDEX]) if id_column is None else out.rename(columns={SERIES_INDEX: id_column})
    return out.rename(columns={TIME_INDEX: timestamp_column})


def clean(value: Any) -> Any:
    """``value`` with NaN and infinities as null, numpy scalars as Python's: JSON a browser parses."""
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if type(value).__module__ == "numpy" and hasattr(value, "item"):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def write_report(ctx: JobContext, report: Dict[str, Any]) -> None:
    """Write report.json, what the pages draw of the predictor."""
    ctx.path(REPORT_FILE).write_text(json.dumps(clean(report), default=str, allow_nan=False))


def noted(report: Dict[str, Any], what: str, error: BaseException) -> None:
    """Note in the report that a part of it failed: a report never fails the job, whose predictor is trained."""
    report.setdefault("notes", []).append(f"{what}: {type(error).__name__}: {error}"[:500])
