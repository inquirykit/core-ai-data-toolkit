"""
Train an AutoGluon TabularPredictor: predict one column of a table from the others.

Without a test_file, ``test_share`` of the rows (a seeded random draw) are held
back from training and scored on, and feature importance and, for
classification, a classification report are measured on them for report.json.
The predictor is served afterwards as model ID "autogluon:<job id>" for the
tabular-prediction task (see models/families/autogluon/predictor.py).
"""

import time
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from jobs import DataFile, Job, JobContext, JobParams, register_job

from ._common import (
    PREDICTOR_DIR, TABULAR_TASK, EvalMetric, Name, ProblemType, TimeLimit, TimedProgress, TrainResult, gpus, lower_is_better,
    model_id, noted, number, plain, read_table, records, require_columns, write_report,
)

# The importance and the report are measured on at most this many held-out rows.
REPORT_ROWS = 5000
# The confusion matrix of more labels than this is not drawn.
MAX_CONFUSION_LABELS = 50


class TabularParams(JobParams):
    train_file: DataFile
    label: str = Field(..., min_length=1, max_length=256, description="The column to predict")
    name: Optional[str] = Name
    tuning_file: Optional[DataFile] = Field(None, description="Validation rows; unset: held out from train_file")
    test_file: Optional[DataFile] = Field(None, description="Rows to score the result on; unset: test_share of train_file")
    test_share: float = Field(
        0.2, ge=0, le=0.5, description="Without a test_file, the share of train_file's rows held back to score on; 0: none",
    )
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
    test_rows: int = Field(0, description="The rows test_score is on")


def held_out(train, share: float):
    """(train, test): ``share`` of the rows drawn at random (seeded) to score on, or none when too few rows."""
    n = int(len(train) * share)
    if n < 1 or len(train) - n < 2:
        return train, None
    test = train.sample(n=n, random_state=0)
    return train.drop(test.index), test


def classification_report(predictor: Any, test) -> Dict[str, Any]:
    """AutoGluon's detailed evaluation as the Evals section's classification report: accuracy, macro F1, each
    label's precision, recall, F1 and support, and the confusion matrix (a row per true label)."""
    detail = predictor.evaluate(test, detailed_report=True, silent=True)
    per_label, matrix = detail["classification_report"], detail["confusion_matrix"]
    matrix = matrix.reindex(index=matrix.index, columns=matrix.index, fill_value=0)
    labels = [str(label) for label in matrix.index]
    return {
        "count": len(test),
        "accuracy": number(per_label["accuracy"]),
        "macro_f1": number(per_label["macro avg"]["f1-score"]),
        "labels": [
            {"label": label, "precision": number(per_label[label]["precision"]),
             "recall": number(per_label[label]["recall"]), "f1": number(per_label[label]["f1-score"]),
             "support": int(per_label[label]["support"])}
            for label in labels if label in per_label
        ],
        "confusion": matrix.values.tolist() if len(labels) <= MAX_CONFUSION_LABELS else None,
    }


def report(ctx: JobContext, predictor: Any, p: TabularParams, train, test, notes: List[str]) -> None:
    """report.json: each input column's type, a training row to start from, and, on the held-out rows, the
    importance of each column and (classification) the classification report; with ``notes`` on what is missing."""
    types = dict(predictor.feature_metadata_in.type_map_raw)
    out: Dict[str, Any] = {"kind": "tabular", "label": p.label, "columns": types,
                           "sample": (records(train[list(types)], 1) or [None])[0], **({"notes": notes} if notes else {})}
    if test is not None:
        test = test.head(REPORT_ROWS)
        try:
            importance = predictor.feature_importance(test, time_limit=min(60, p.time_limit / 10), silent=True)
            importance.to_csv(ctx.path("feature_importance.csv"), index_label="feature")
            out["importance"] = [
                {"feature": str(feature), "importance": number(row["importance"]), "stddev": number(row["stddev"]),
                 "p_value": number(row["p_value"])}
                for feature, row in importance.iterrows()
            ]
        except Exception as e:
            noted(out, "feature importance", e)
        if predictor.problem_type in ("binary", "multiclass"):
            try:
                out["classification"] = classification_report(predictor, test)
            except Exception as e:
                noted(out, "classification report", e)
    write_report(ctx, out)


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
        if p.test_file:
            test = read_table(p.test_file)
            require_columns(test, [p.label], "test_file")
        else:
            train, test = held_out(train, p.test_share)
        if test is not None:
            test = test[test[p.label].notna()]  # rows without an answer score nothing
            test = test if len(test) else None

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

        ctx.progress(0.9, "scoring")
        metric = predictor.eval_metric.name
        notes: List[str] = []
        try:
            board = predictor.leaderboard(test, silent=True)
            test_score = number(predictor.evaluate(test, silent=True).get(metric)) if test is not None else None
        except Exception as e:
            # Rows it cannot score (log loss over a class it never saw, say): the predictor is trained all the same.
            if test is None:
                raise
            notes.append(f"scoring the held-out rows: {type(e).__name__}: {e}"[:500])
            board, test_score, test = predictor.leaderboard(silent=True), None, None
        board.to_csv(ctx.path("leaderboard.csv"), index=False)
        best = predictor.model_best
        validation = board.loc[board["model"] == best, "score_val"]

        ctx.progress(0.95, "measuring feature importance")
        try:
            report(ctx, predictor, p, train, test, notes)
        except Exception as e:  # the predictor is trained: the job still succeeds
            noted(failed := {"kind": "tabular", "label": p.label, "notes": notes}, "report", e)
            write_report(ctx, failed)
        return TabularResult(
            model_id=model_id(ctx),
            task=TABULAR_TASK,
            problem_type=predictor.problem_type,
            eval_metric=metric,
            lower_is_better=lower_is_better(predictor.eval_metric),
            best_model=best,
            validation_score=number(validation.iloc[0]) if len(validation) else None,
            test_score=test_score,
            test_rows=len(test) if test is not None else 0,
            leaderboard=records(board, 20),
            features=predictor.features(),
            classes=plain(predictor.class_labels),
            train_rows=len(train),
            fit_seconds=round(fit_seconds, 1),
        )
