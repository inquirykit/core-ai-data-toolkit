"""
Train an AutoGluon TabularPredictor: predict one column of a table from the others.

The predictor is served afterwards as model ID "autogluon:<job id>" for the
tabular-prediction task (see models/autogluon/predictor.py).
"""

import time
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from jobs import DataFile, Job, JobContext, JobParams, register_job

from ._common import (
    PREDICTOR_DIR, EvalMetric, ProblemType, TimeLimit, TimedProgress, TrainResult, gpus, number, plain, read_table,
    records, require_columns, write_info,
)


class TabularParams(JobParams):
    train_file: DataFile
    label: str = Field(..., min_length=1, max_length=256, description="The column to predict")
    tuning_file: Optional[DataFile] = Field(None, description="Validation rows; unset: held out from train_file")
    test_file: Optional[DataFile] = Field(None, description="Rows to score the result on (test_score, leaderboard)")
    problem_type: ProblemType = Field(None, description="Unset: inferred from the label")
    eval_metric: Optional[str] = EvalMetric
    presets: Literal[
        "medium_quality", "good_quality", "high_quality", "best_quality", "extreme_quality",
        "optimize_for_deployment", "interpretable",
    ] = Field("medium_quality", description="Accuracy against time; extreme_quality wants a GPU")
    time_limit: int = TimeLimit
    ignored_columns: List[str] = Field(default_factory=list, max_length=1000, description="Columns not to learn from (IDs)")
    quantile_levels: Optional[List[float]] = Field(None, description="For problem_type quantile")
    hyperparameters: Optional[Dict[str, Any]] = Field(
        None, description="AutoGluon's hyperparameters argument (models and their settings); unset: the presets' choice"
    )


class TabularResult(TrainResult):
    problem_type: str
    features: List[str]
    classes: Optional[List[Any]] = None


@register_job("autogluon-tabular", env="autogluon")
class TrainTabular(Job):
    """Train an AutoGluon TabularPredictor on a table (classification or regression)"""

    Params = TabularParams
    Result = TabularResult

    def run(self, ctx: JobContext, p: TabularParams) -> TabularResult:
        from autogluon.tabular import TabularPredictor

        ctx.progress(0.0, "reading data")
        train = read_table(p.train_file)
        require_columns(train, [p.label, *p.ignored_columns], "train_file")
        tuning = read_table(p.tuning_file) if p.tuning_file else None
        test = read_table(p.test_file) if p.test_file else None
        if test is not None:
            require_columns(test, [p.label], "test_file")

        predictor = TabularPredictor(
            label=p.label,
            problem_type=p.problem_type,
            eval_metric=p.eval_metric,
            path=str(ctx.path(PREDICTOR_DIR)),
            quantile_levels=p.quantile_levels,
            # Only when given: AutoGluon does not take learner_kwargs=None.
            **({"learner_kwargs": {"ignored_columns": p.ignored_columns}} if p.ignored_columns else {}),
        )
        extra = {"hyperparameters": p.hyperparameters} if p.hyperparameters else {}
        started = time.monotonic()
        with TimedProgress(ctx, p.time_limit, f"training ({p.presets})"):
            predictor.fit(
                train, tuning_data=tuning, presets=p.presets, time_limit=p.time_limit,
                num_cpus=ctx.num_cpus, num_gpus=gpus(ctx), **extra,
            )
        fit_seconds = time.monotonic() - started

        ctx.progress(0.92, "scoring")
        board = predictor.leaderboard(test, silent=True)
        board.to_csv(ctx.path("leaderboard.csv"), index=False)
        best = predictor.model_best
        metric = predictor.eval_metric.name
        test_score = number(predictor.evaluate(test, silent=True).get(metric)) if test is not None else None
        validation = board.loc[board["model"] == best, "score_val"]
        classes = plain(predictor.class_labels)

        ctx.progress(0.98, "saving")
        model_id = write_info(
            ctx, "tabular", "tabular-prediction",
            label=p.label, problem_type=predictor.problem_type, features=predictor.features(),
            classes=classes, eval_metric=metric,
        )
        return TabularResult(
            model_id=model_id,
            task="tabular-prediction",
            problem_type=predictor.problem_type,
            eval_metric=metric,
            best_model=best,
            validation_score=number(validation.iloc[0]) if len(validation) else None,
            test_score=test_score,
            leaderboard=records(board, 20),
            features=predictor.features(),
            classes=classes,
            train_rows=len(train),
            fit_seconds=round(fit_seconds, 1),
        )
