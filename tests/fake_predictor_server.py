"""Stands in for jobs/autogluon/_serve.py: the same protocol, no AutoGluon.

Predicts the "x" column doubled; a row without "x" is a client error, and a
row with "crash" ends the process.
"""

import json
import os
import sys

from jobs.worker import emitter, open_channel


def main(argv):
    emit = emitter(open_channel())
    info = json.loads(open(os.path.join(argv[0], "predictor.json")).read())
    emit(ready=True)
    for line in sys.stdin:
        request = json.loads(line)
        rows = request["rows"]
        if any("crash" in row for row in rows):
            os._exit(3)
        if info["kind"] == "timeseries":
            emit(result={"forecast": [{"item_id": rows[0]["item_id"], "mean": 1.0}]})
            continue
        if any("x" not in row for row in rows):
            emit(error="KeyError: 'x'", client=True)
            continue
        result = {"predictions": [row["x"] * 2 for row in rows]}
        if request.get("probabilities"):
            result["probabilities"] = [{"a": 0.5, "b": 0.5} for _ in rows]
        result["rows_seen"] = rows  # beyond the contract: dropped
        emit(result=result)


if __name__ == "__main__":
    main(sys.argv[1:])
