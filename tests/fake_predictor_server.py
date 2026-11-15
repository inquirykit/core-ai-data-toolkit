"""Stands in for jobs/autogluon/_serve.py: the same protocol, no AutoGluon.

Predicts the "x" column doubled; a row without "x" is a client error, a
row with "crash" ends the process, and one with "sleep" waits that many
seconds before answering. It writes its pid to serve.pid, and waits the
params' "start_sleep" seconds before it is ready.
"""

import json
import os
import sys
import time

from jobs.worker import emitter, open_channel


def main(argv):
    emit = emitter(open_channel())
    task = json.loads(open(os.path.join(argv[0], "result.json")).read())["task"]
    params = json.loads(open(os.path.join(argv[0], "params.json")).read())
    id_column = params.get("id_column")
    with open(os.path.join(argv[0], "serve.pid"), "w") as f:
        f.write(str(os.getpid()))
    time.sleep(params.get("start_sleep", 0))
    emit(ready=True)
    for line in sys.stdin:
        request = json.loads(line)
        rows = request["rows"]
        if any("crash" in row for row in rows):
            os._exit(3)
        time.sleep(max((row.get("sleep", 0) for row in rows), default=0))
        if task == "timeseries-forecast":
            # Keyed as the training file was: by its ID column, or by time alone for one series.
            key = {id_column: rows[0][id_column]} if id_column else {}
            emit(result={"forecast": [{**key, "timestamp": "2026-01-02", "mean": 1.0}]})
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
