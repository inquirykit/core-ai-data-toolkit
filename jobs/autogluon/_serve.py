"""
Serves one trained predictor, in the "autogluon" environment:

    python -m jobs.autogluon._serve <job workdir>

models/autogluon/predictor.py starts it when the model is loaded and stops
it when the cache evicts it. It reads predictor.json, loads the predictor,
writes {"ready": true}, then answers one JSON request per stdin line with one
JSON line on stdout: {"result": ...} or {"error": ..., "client": bool}
("client": the request did not fit the predictor, a 400).
"""

import json
import sys
import traceback
from pathlib import Path
from typing import Any, Callable, Dict

from jobs.worker import emitter, exit_with_parent, open_channel

from ._common import INFO_FILE, records, without_nvml_on_macos

# Up to this many rows, multimodal predictions skip Lightning's trainer and
# data loaders, whose worker processes take seconds to start on every request.
REALTIME_ROWS = 256


def tabular(predictor: Any, info: Dict[str, Any], realtime: bool = False) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    import pandas as pd

    def predict(request: Dict[str, Any]) -> Dict[str, Any]:
        data = pd.DataFrame(request["rows"])
        options = {"realtime": True} if realtime and len(data) <= REALTIME_ROWS else {}
        out: Dict[str, Any] = {"predictions": json.loads(predictor.predict(data, **options).to_json(orient="values"))}
        if request.get("probabilities") and info.get("classes") is not None:
            proba = predictor.predict_proba(data, **options)
            proba.columns = [str(c) for c in proba.columns]
            out["probabilities"] = records(proba)
        return out

    return predict


def timeseries(predictor: Any, info: Dict[str, Any]) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    import pandas as pd
    from autogluon.timeseries import TimeSeriesDataFrame

    id_column, timestamp_column = info["id_column"], info["timestamp_column"]

    def frame(rows):
        df = pd.DataFrame(rows)
        df[timestamp_column] = pd.to_datetime(df[timestamp_column])
        return TimeSeriesDataFrame.from_data_frame(df, id_column=id_column, timestamp_column=timestamp_column)

    def predict(request: Dict[str, Any]) -> Dict[str, Any]:
        data = frame(request["rows"])
        known = frame(request["known_covariates"]) if request.get("known_covariates") else None
        forecast = predictor.predict(data, known_covariates=known).reset_index()
        forecast = forecast.rename(columns={"item_id": id_column, "timestamp": timestamp_column})
        forecast.columns = [str(c) for c in forecast.columns]
        return {"forecast": records(forecast)}

    return predict


def load(workdir: Path) -> Callable[[Dict[str, Any]], Dict[str, Any]]:
    info = json.loads((workdir / INFO_FILE).read_text())
    path = str(workdir / info["path"])
    if info["kind"] == "tabular":
        from autogluon.tabular import TabularPredictor

        return tabular(TabularPredictor.load(path, require_py_version_match=False), info)
    if info["kind"] == "multimodal":
        from autogluon.multimodal import MultiModalPredictor

        without_nvml_on_macos()
        # Same row-in, prediction-out shape as tabular.
        return tabular(MultiModalPredictor.load(path), info, realtime=True)
    if info["kind"] == "timeseries":
        from autogluon.timeseries import TimeSeriesPredictor

        return timeseries(TimeSeriesPredictor.load(path, require_version_match=False), info)
    raise ValueError(f"Unknown predictor kind {info['kind']!r}")


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
        except (KeyError, ValueError, TypeError) as e:
            # Missing or mistyped columns: the request does not fit the predictor.
            traceback.print_exc()
            emit(error=f"{type(e).__name__}: {e}"[:2000], client=True)
        except Exception as e:
            traceback.print_exc()
            emit(error=f"{type(e).__name__}: {e}"[:2000], client=False)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
