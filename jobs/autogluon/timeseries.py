"""
Train an AutoGluon TimeSeriesPredictor: forecast the next values of one or
many series (statistical models, tree models, deep models and Chronos).

The table is long: one row per series and time step, with a timestamp column,
the target and, when it holds several series, an ID column. Every preset,
medium_quality included, uses small pretrained forecasters (Chronos-2, Toto-2)
beside the statistical ones, which the first training downloads from the
Hugging Face Hub.

The score is AutoGluon's validation score, on the last steps of each series,
which the models kept were not fitted on (it does not refit on them unless
asked); the best model was chosen on it too. A test_file scores on data the
predictor never saw. After training the job forecasts the next steps of every
series into forecast.csv, and report.json holds up to 20 series' recent
history with their forecast, for the pages' chart; a predictor with known
covariates cannot forecast without their future values, so it skips that.

The predictor is served afterwards as model ID "autogluon:<job id>" for the
timeseries-forecast task.
"""

import time
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from jobs import DataFile, Job, JobContext, JobParams, register_job

from ._common import (
    FORECAST_FILE, PREDICTOR_DIR, TIMESERIES_TASK, EvalMetric, Name, TimeLimit, TimedProgress, TrainResult, forecast_table,
    lower_is_better, model_id, noted, number, read_table, records, require_columns, series_frame, write_report,
)

# The series report.json draws, and how much of each one's history (in forecasts' lengths).
REPORT_SERIES = 20
HISTORY_WINDOWS = 3


class TimeSeriesParams(JobParams):
    train_file: DataFile
    prediction_length: int = Field(..., ge=1, le=10_000, description="Time steps to forecast")
    target: str = Field("target", min_length=1, max_length=256)
    name: Optional[str] = Name
    id_column: Optional[str] = Field(
        None, min_length=1, max_length=256, description="Which series a row belongs to; unset: the file is one series",
    )
    timestamp_column: str = Field("timestamp", min_length=1, max_length=256)
    freq: Optional[str] = Field(None, max_length=16, description="pandas frequency, e.g. D, h, W; unset: inferred")
    known_covariates: List[str] = Field(
        default_factory=list, max_length=256,
        description="Columns known in advance (holidays, prices); forecasts then need their future values",
    )
    test_file: Optional[DataFile] = Field(
        None, description="Series that continue train_file; its last prediction_length steps are scored"
    )
    eval_metric: Optional[str] = EvalMetric
    quantile_levels: Optional[List[float]] = Field(None, description="Unset: 0.1 to 0.9")
    presets: Literal["medium_quality", "high_quality", "best_quality"] = Field(
        "medium_quality", description="Accuracy against time; best_quality validates on several windows",
    )
    time_limit: int = TimeLimit
    hyperparameters: Optional[Dict[str, Any]] = Field(None, description="AutoGluon's hyperparameters argument")


class TimeSeriesResult(TrainResult):
    prediction_length: int
    freq: Optional[str] = None
    series: int


def band(levels: List[float]) -> List[float]:
    """The quantiles the chart shades between: 10% and 90%, or the widest the predictor has."""
    return [0.1, 0.9] if {0.1, 0.9} <= set(levels) else [min(levels), max(levels)]


def series_report(predictor: Any, train, forecast, p: TimeSeriesParams) -> Dict[str, Any]:
    """Up to REPORT_SERIES series: the end of each one's history, then its forecast's mean and band."""
    low, high = band(list(predictor.quantile_levels))
    shown = []
    for item in list(train.item_ids)[:REPORT_SERIES]:
        history = train.loc[item][p.target].tail(HISTORY_WINDOWS * p.prediction_length)
        ahead = forecast.loc[item]
        shown.append({
            "id": None if p.id_column is None else str(item),
            "history": [{"t": t.isoformat(), "y": number(y)} for t, y in history.items()],
            "forecast": [{"t": t.isoformat(), "mean": number(row["mean"]), "low": number(row[str(low)]),
                          "high": number(row[str(high)])} for t, row in ahead.iterrows()],
        })
    return {"series": shown, "series_count": train.num_items, "band": [low, high]}


@register_job("autogluon-timeseries", env="autogluon")
class TrainTimeSeries(Job):
    """Train an AutoGluon TimeSeriesPredictor to forecast one or many series"""

    Params = TimeSeriesParams
    Result = TimeSeriesResult

    def run(self, ctx: JobContext, p: TimeSeriesParams) -> TimeSeriesResult:
        import torch
        from autogluon.timeseries import TimeSeriesPredictor

        torch.set_num_threads(ctx.num_cpus)
        columns = [*([p.id_column] if p.id_column else []), p.timestamp_column, p.target, *p.known_covariates]

        def load(path: str, what: str):
            df = read_table(path)
            require_columns(df, columns, what)
            return series_frame(df, p.id_column, p.timestamp_column, what)

        ctx.progress(0.0, "reading data")
        train = load(p.train_file, "train_file")
        test = load(p.test_file, "test_file") if p.test_file else None

        predictor = TimeSeriesPredictor(
            target=p.target,
            prediction_length=p.prediction_length,
            freq=p.freq,
            eval_metric=p.eval_metric,
            known_covariates_names=p.known_covariates or None,
            quantile_levels=p.quantile_levels,
            path=str(ctx.path(PREDICTOR_DIR)),
        )
        extra = {"hyperparameters": p.hyperparameters} if p.hyperparameters else {}
        started = time.monotonic()
        with TimedProgress(ctx, p.time_limit, f"training ({p.presets})"):
            predictor.fit(train, presets=p.presets, time_limit=p.time_limit, **extra)
        fit_seconds = time.monotonic() - started

        ctx.progress(0.9, "scoring")
        metric = predictor.eval_metric.name
        report: Dict[str, Any] = {"kind": "timeseries", "target": p.target, "prediction_length": p.prediction_length}
        try:
            board = predictor.leaderboard(test, silent=True)
            test_score = number(predictor.evaluate(test).get(metric)) if test is not None else None
        except Exception as e:  # a test_file it cannot score: the predictor is trained all the same
            if test is None:
                raise
            noted(report, "scoring the test file", e)
            board, test_score = predictor.leaderboard(silent=True), None
        board.to_csv(ctx.path("leaderboard.csv"), index=False)
        best = predictor.model_best
        validation = board.loc[board["model"] == best, "score_val"]

        ctx.progress(0.95, "forecasting")
        if p.known_covariates:
            report["forecast_skipped"] = "It has known covariates: a forecast needs their future values"
        else:
            try:
                forecast = predictor.predict(train)
                forecast_table(forecast, p.id_column, p.timestamp_column).to_csv(ctx.path(FORECAST_FILE), index=False)
                report.update(forecast_file=FORECAST_FILE, **series_report(predictor, train, forecast, p))
            except Exception as e:  # the predictor is trained: the job still succeeds
                noted(report, "forecast", e)
        write_report(ctx, report)
        return TimeSeriesResult(
            model_id=model_id(ctx),
            task=TIMESERIES_TASK,
            eval_metric=metric,
            lower_is_better=lower_is_better(predictor.eval_metric),
            best_model=best,
            validation_score=number(validation.iloc[0]) if len(validation) else None,
            test_score=test_score,
            leaderboard=records(board, 20),
            prediction_length=p.prediction_length,
            freq=predictor.freq,
            series=train.num_items,
            train_rows=len(train),
            fit_seconds=round(fit_seconds, 1),
        )
