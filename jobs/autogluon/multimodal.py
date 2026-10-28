"""
Train an AutoGluon MultiModalPredictor: predict a column from text, images
and tabular columns together, by fine-tuning pretrained networks.

Image columns hold image file paths, relative to the table's folder (or
absolute). The predictor is served afterwards as model ID
"autogluon:<job id>" for the multimodal-prediction task.
"""

import time
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

from pydantic import Field

from jobs import DataFile, Job, JobContext, JobParams, register_job

from ._common import (
    PREDICTOR_DIR, EvalMetric, TimeLimit, TimedProgress, TrainResult, number, plain, read_table,
    require_columns, without_nvml_on_macos, write_info,
)


class MultimodalParams(JobParams):
    train_file: DataFile
    label: str = Field(..., min_length=1, max_length=256, description="The column to predict")
    image_columns: List[str] = Field(
        default_factory=list, max_length=32,
        description="Columns of image paths (relative to the table's folder); others are typed by AutoGluon",
    )
    tuning_file: Optional[DataFile] = None
    test_file: Optional[DataFile] = Field(None, description="Rows to score the result on")
    problem_type: Optional[Literal["binary", "multiclass", "regression"]] = Field(None, description="Unset: inferred")
    eval_metric: Optional[str] = EvalMetric
    presets: Literal["medium_quality", "high_quality", "best_quality"] = "medium_quality"
    time_limit: int = TimeLimit
    hyperparameters: Optional[Dict[str, Any]] = Field(
        None, description="AutoGluon's hyperparameters, e.g. {\"optim.max_epochs\": 5}"
    )


class MultimodalResult(TrainResult):
    problem_type: str
    columns: List[str]
    classes: Optional[List[Any]] = None


def _absolute_images(df, columns: List[str], folder: Path):
    """Image paths made absolute against ``folder``, so the predictor finds them from any directory."""
    df = df.copy()
    for column in columns:
        df[column] = [str((folder / str(v)).resolve()) if isinstance(v, str) and v else v for v in df[column]]
    return df


@register_job("autogluon-multimodal", env="autogluon")
class TrainMultimodal(Job):
    """Train an AutoGluon MultiModalPredictor on text, images and tabular columns"""

    Params = MultimodalParams
    Result = MultimodalResult

    def run(self, ctx: JobContext, p: MultimodalParams) -> MultimodalResult:
        from autogluon.multimodal import MultiModalPredictor

        without_nvml_on_macos()

        def load(path: Optional[str], what: str, label: bool = True):
            if not path:
                return None
            df = read_table(path)
            require_columns(df, ([p.label] if label else []) + p.image_columns, what)
            return _absolute_images(df, p.image_columns, Path(path).parent)

        ctx.progress(0.0, "reading data")
        train = load(p.train_file, "train_file")
        tuning = load(p.tuning_file, "tuning_file")
        test = load(p.test_file, "test_file")

        hyperparameters = dict(p.hyperparameters or {})
        # Lightning's "auto" would take an Apple GPU or CUDA; JOB_DEVICE decides.
        if ctx.device == "cpu":
            hyperparameters.setdefault("env.accelerator", "cpu")
            hyperparameters.setdefault("env.num_gpus", 0)
        elif ctx.device == "mps":
            hyperparameters.setdefault("env.accelerator", "mps")
        hyperparameters.setdefault("env.num_workers", min(ctx.num_cpus, 4))
        hyperparameters.setdefault("env.num_workers_inference", min(ctx.num_cpus, 4))

        predictor = MultiModalPredictor(
            label=p.label, problem_type=p.problem_type, eval_metric=p.eval_metric, path=str(ctx.path(PREDICTOR_DIR)),
        )
        started = time.monotonic()
        with TimedProgress(ctx, p.time_limit, f"training ({p.presets})"):
            predictor.fit(
                train, tuning_data=tuning, presets=p.presets, time_limit=p.time_limit,
                hyperparameters=hyperparameters, column_types={c: "image_path" for c in p.image_columns} or None,
            )
        fit_seconds = time.monotonic() - started

        ctx.progress(0.92, "scoring")
        metric = predictor.eval_metric if isinstance(predictor.eval_metric, str) else predictor.eval_metric.name
        test_score = None
        if test is not None:
            scores = predictor.evaluate(test, metrics=[metric])
            test_score = number(scores.get(metric))
        classes = plain(predictor.class_labels) if predictor.problem_type in ("binary", "multiclass") else None
        columns = [c for c in train.columns if c != p.label]

        ctx.progress(0.98, "saving")
        predictor.save(str(ctx.path(PREDICTOR_DIR)))
        model_id = write_info(
            ctx, "multimodal", "multimodal-prediction",
            label=p.label, problem_type=predictor.problem_type, columns=columns, image_columns=p.image_columns,
            classes=classes, eval_metric=metric,
        )
        return MultimodalResult(
            model_id=model_id,
            task="multimodal-prediction",
            problem_type=predictor.problem_type,
            eval_metric=metric,
            best_model="MultiModalPredictor",
            validation_score=None,
            test_score=test_score,
            leaderboard=[],
            columns=columns,
            classes=classes,
            train_rows=len(train),
            fit_seconds=round(fit_seconds, 1),
        )
