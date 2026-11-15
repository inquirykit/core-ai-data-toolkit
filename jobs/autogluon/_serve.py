"""
Serves one trained predictor, in the "autogluon" environment:

    python -m jobs.autogluon._serve <job workdir>

models/families/autogluon/predictor.py starts it when the model is loaded and stops
it when the cache evicts it. It reads the job's result.json (the task) and
params.json (the columns a series is keyed by), loads the predictor, writes
{"ready": true}, then answers one JSON request per stdin line with one JSON
line on stdout: {"result": ...} or {"error": ..., "client": bool} ("client":
the request did not fit the predictor, a 400).
"""

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict

from jobs.worker import emitter, exit_with_parent, open_channel

from ._common import PREDICTOR_DIR, forecast_table, records, series_frame


def tabular(predictor: Any) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    import pandas as pd

    def predict(request: Dict[str, Any]) -> Dict[str, Any]:
        data = pd.DataFrame(request["rows"])
        out: Dict[str, Any] = {"predictions": json.loads(predictor.predict(data).to_json(orient="values"))}
        if request.get("probabilities") and predictor.class_labels is not None:
            proba = predictor.predict_proba(data)
            proba.columns = [str(c) for c in proba.columns]
            out["probabilities"] = records(proba)
        return out

    return predict


def timeseries(predictor: Any, params: Dict[str, Any]) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    import pandas as pd

    # Without an ID column it was trained on one series, and forecasts one.
    id_column, timestamp_column = params.get("id_column"), params.get("timestamp_column") or "timestamp"

    def predict(request: Dict[str, Any]) -> Dict[str, Any]:
        data = series_frame(pd.DataFrame(request["rows"]), id_column, timestamp_column, "rows")
        future = request.get("known_covariates")
        known = series_frame(pd.DataFrame(future), id_column, timestamp_column, "known_covariates") if future else None
        forecast = predictor.predict(data, known_covariates=known)
        return {"forecast": records(forecast_table(forecast, id_column, timestamp_column))}

    return predict


def load(workdir: Path) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    task = json.loads((workdir / "result.json").read_text())["task"]
    params = json.loads((workdir / "params.json").read_text())
    path = str(workdir / PREDICTOR_DIR)
    if task == "tabular-prediction":
        from autogluon.tabular import TabularPredictor

        return tabular(TabularPredictor.load(path, require_py_version_match=False))
    if task == "timeseries-forecast":
        from autogluon.timeseries import TimeSeriesPredictor

        return timeseries(TimeSeriesPredictor.load(path, require_version_match=False), params)
    raise ValueError(f"Not a predictor's task: {task!r}")


def main(argv: list) -> int:
    emit = emitter(open_channel())
    exit_with_parent()
    predict = load(Path(argv[0]))
    emit(ready=True)
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            emit(result=predict(json.loads(line)))
        except (KeyError, ValueError, TypeError, AssertionError) as e:
            # Missing or mistyped columns, or a forecast without the known covariates the predictor needs (an
            # assertion in AutoGluon): the request does not fit the predictor.
            traceback.print_exc()
            emit(error=f"{type(e).__name__}: {e}"[:2000], client=True)
        except Exception as e:
            traceback.print_exc()
            emit(error=f"{type(e).__name__}: {e}"[:2000], client=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
