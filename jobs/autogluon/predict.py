"""
Predict a whole file with a predictor an AutoGluon job trained: the pages' "Predict a file" and "Forecast from newer
data". It is a job (submitted at high priority) since a file can be any size, where POST /inference takes a request's
rows.

The file is read as training read its own (``read_table``), so its columns get the same types. A tabular predictor
gives predictions.csv, the file's rows with a prediction column and, for classification, a probability column per
class. A forecaster gives forecast.csv, as its training job wrote it, from the series' history in the file and, if it
has known covariates, their future values in ``known_covariates_file``. Its result names the file; it has no
model_id, so it is not listed as a model.
"""

import json
from typing import Optional

from pydantic import Field

from jobs import DataFile, Job, JobContext, JobParams, JobResult, register_job

from ._common import FORECAST_FILE, PREDICTOR_DIR, forecast_table, free_column, read_table, series_frame

PREDICTIONS_FILE = "predictions.csv"


class PredictParams(JobParams):
    predictor: str = Field(..., pattern=r"^[0-9a-f]{8,32}$", description="The ID of the job that trained the predictor")
    data_file: DataFile = Field(..., description="Rows to predict, or the series' history to forecast from")
    probabilities: bool = Field(True, description="Classification: a probability column per class too")
    known_covariates_file: Optional[DataFile] = Field(
        None, description="A forecaster with known covariates: their values over the next steps",
    )


class PredictResult(JobResult):
    file: str = Field(..., description="The predictions or forecast, in this job's folder")
    rows: int


@register_job("autogluon-predict", env="autogluon")
class PredictFile(Job):
    """Predict a file with a trained AutoGluon predictor, or forecast from newer data"""

    Params = PredictParams
    Result = PredictResult
    ref_param = "predictor"  # a predictor's page lists its own (GET /jobs?ref=<its job id>)

    def run(self, ctx: JobContext, p: PredictParams) -> PredictResult:
        trained = ctx.workdir.parent / p.predictor
        try:
            task = json.loads((trained / "result.json").read_text())["task"]
            params = json.loads((trained / "params.json").read_text())
        except (OSError, ValueError, KeyError):
            raise ValueError(f"Job {p.predictor} trained no predictor (it failed, or was deleted)") from None
        path = str(trained / PREDICTOR_DIR)
        ctx.progress(0.1, "reading data")
        data = read_table(p.data_file)

        if task == "tabular-prediction":
            from autogluon.tabular import TabularPredictor

            predictor = TabularPredictor.load(path, require_py_version_match=False)
            ctx.progress(0.3, "predicting")
            out = data.copy()
            out[free_column(out, "prediction")] = predictor.predict(data).values
            if p.probabilities and predictor.class_labels is not None:
                proba = predictor.predict_proba(data)
                for label in proba.columns:
                    out[free_column(out, f"probability_{label}")] = proba[label].values
            out.to_csv(ctx.path(PREDICTIONS_FILE), index=False)
            return PredictResult(file=PREDICTIONS_FILE, rows=len(out))

        if task == "timeseries-forecast":
            from autogluon.timeseries import TimeSeriesPredictor

            predictor = TimeSeriesPredictor.load(path, require_version_match=False)
            id_column, timestamp_column = params.get("id_column"), params.get("timestamp_column") or "timestamp"
            history = series_frame(data, id_column, timestamp_column, "data_file")
            known = None
            if p.known_covariates_file:
                known = series_frame(read_table(p.known_covariates_file), id_column, timestamp_column,
                                     "known_covariates_file")
            ctx.progress(0.3, "forecasting")
            forecast = forecast_table(predictor.predict(history, known_covariates=known), id_column, timestamp_column)
            forecast.to_csv(ctx.path(FORECAST_FILE), index=False)
            return PredictResult(file=FORECAST_FILE, rows=len(forecast))

        raise ValueError(f"Job {p.predictor} trained a {task} model, not an AutoGluon predictor")
