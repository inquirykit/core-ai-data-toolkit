"""
Train an AutoGluon TimeSeriesPredictor: forecast the next values of one or
many series (statistical models, tree models, deep models and Chronos).

The table is long: one row per series and time step, with an ID column, a
timestamp column and the target. The predictor is served afterwards as
model ID "autogluon:<job id>" for the timeseries-forecast task.
"""

import time
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from jobs import DataFile, Job, JobContext, JobParams, register_job

from ._common import (
    PREDICTOR_DIR, EvalMetric, TimeLimit, TimedProgress, TrainResult, number, read_table, records,
    require_columns, write_info,
)


class TimeSeriesParams(JobParams):
    train_file: DataFile
    prediction_length: int = Field(..., ge=1, le=10_000, description="Time steps to forecast")
    target: str = Field("target", min_length=1, max_length=256)
    id_column: str = Field("item_id", min_length=1, max_length=256, description="Which series a row belongs to")
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
    presets: Literal[
        "medium_quality", "high_quality", "best_quality", "chronos2", "chronos2_small", "chronos2_ensemble",
        "bolt_tiny", "bolt_mini", "bolt_small", "bolt_base",
    ] = Field("medium_quality", description="chronos2*/bolt_*: pretrained Chronos models, little or no training")
    time_limit: int = TimeLimit
    hyperparameters: Optional[Dict[str, Any]] = Field(None, description="AutoGluon's hyperparameters argument")


class TimeSeriesResult(TrainResult):
    prediction_length: int
    freq: Optional[str] = None
    series: int


@register_job("autogluon-timeseries", env="autogluon")
class TrainTimeSeries(Job):
    """Train an AutoGluon TimeSeriesPredictor to forecast one or many series"""

    Params = TimeSeriesParams
    Result = TimeSeriesResult

    def run(self, ctx: JobContext, p: TimeSeriesParams) -> TimeSeriesResult:
        import torch
        from autogluon.timeseries import TimeSeriesDataFrame, TimeSeriesPredictor

        torch.set_num_threads(ctx.num_cpus)
        columns = [p.id_column, p.timestamp_column, p.target, *p.known_covariates]

        def load(path: str, what: str):
            df = read_table(path)
            require_columns(df, columns, what)
            return TimeSeriesDataFrame.from_data_frame(df, id_column=p.id_column, timestamp_column=p.timestamp_column)

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

        ctx.progress(0.92, "scoring")
        board = predictor.leaderboard(test, silent=True)
        board.to_csv(ctx.path("leaderboard.csv"), index=False)
        best = predictor.model_best
        metric = predictor.eval_metric.name
        test_score = number(predictor.evaluate(test).get(metric)) if test is not None else None
        validation = board.loc[board["model"] == best, "score_val"]

        ctx.progress(0.98, "saving")
        model_id = write_info(
            ctx, "timeseries", "timeseries-forecast",
            target=p.target, id_column=p.id_column, timestamp_column=p.timestamp_column,
            prediction_length=p.prediction_length, freq=predictor.freq, known_covariates=p.known_covariates,
            quantile_levels=list(predictor.quantile_levels), eval_metric=metric,
        )
        return TimeSeriesResult(
            model_id=model_id,
            task="timeseries-forecast",
            eval_metric=metric,
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
