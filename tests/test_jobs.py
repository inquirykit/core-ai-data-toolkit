import asyncio
import json
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tests.fake_jobs  # noqa: F401  registers the test-* jobs
from api import app as app_module
from config import Config
from jobs.store import FAILED, SUCCEEDED
from models.autogluon import predictor as ag


@pytest.fixture
def client():
    with TestClient(app_module.app) as c:
        yield c


def submit(client, job, **params):
    response = client.post("/jobs", json={"job": job, "params": params})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def wait(client, id, until=("succeeded", "failed", "cancelled"), timeout=30.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = client.get(f"/jobs/{id}").json()
        if job["status"] in until:
            return job
        time.sleep(0.05)
    raise AssertionError(f"job {id} still {job['status']} after {timeout}s")


def alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


# ------------------------------------------------------------------- types
def test_types_show_schemas_and_whether_the_environment_is_ready(client):
    types = {j["name"]: j for j in client.get("/jobs/types").json()["jobs"]}
    echo = types["test-echo"]
    assert echo["env"] == "default" and echo["env_ready"] is True
    assert set(echo["params"]["properties"]) == {"text", "steps", "data"}
    assert echo["result"]["properties"]["echo"]
    assert echo["description"].startswith("Echo the text")
    assert types["test-missing-env"]["env_ready"] is False
    # The AutoGluon job files import without AutoGluon; their environment is not set up in tests.
    for name in ("autogluon-tabular", "autogluon-multimodal", "autogluon-timeseries"):
        assert types[name]["env"] == "autogluon" and types[name]["env_ready"] is False
    assert "label" in types["autogluon-tabular"]["params"]["required"]


# --------------------------------------------------------------- lifecycle
def test_a_job_runs_and_reports_progress_log_files_and_result(client):
    assert client.post("/jobs/data", files={"file": ("input.csv", b"a,b\n1,2\n")}).status_code == 201
    assert {"path": "input.csv", "bytes": 8} in client.get("/jobs/data").json()["files"]

    id = submit(client, "test-echo", text="hello", data="input.csv")
    job = wait(client, id)
    assert job["status"] == "succeeded", job
    assert job["result"] == {"echo": "hello", "data_bytes": 8}  # "extra" is beyond the Result model
    assert job["progress"] == 1.0
    assert job["params"]["data"] == str(Config.JOB_DATA_DIR / "input.csv")
    assert "step 2" in client.get(f"/jobs/{id}/log").text

    files = {f["path"] for f in client.get(f"/jobs/{id}/files").json()["files"]}
    assert {"params.json", "job.log", "result.json", "out.txt"} <= files
    assert client.get(f"/jobs/{id}/files/out.txt").text == "hello"
    assert client.get(f"/jobs/{id}/files/../jobs.db").status_code in (400, 404)
    assert id in [j["id"] for j in client.get("/jobs?status=succeeded").json()["jobs"]]


def test_events_stream_status_and_log_until_the_end(client):
    id = submit(client, "test-echo", text="streamed")
    with client.stream("GET", f"/jobs/{id}/events") as response:
        body = "".join(response.iter_text())
    assert "event: status" in body and "event: log" in body
    assert body.rstrip().endswith('data: {"status": "succeeded"}')
    assert "step 1" in body


@pytest.mark.parametrize("job, params, detail", [
    ("test-echo", {}, "text"),
    ("test-echo", {"text": "x", "nope": 1}, "nope"),
    ("test-echo", {"text": "x", "data": "../../etc/passwd"}, "JOB_DATA_DIR"),
    ("test-echo", {"text": "x", "data": "missing.csv"}, "JOB_DATA_DIR"),
    ("no-such-job", {}, "Unknown job"),
    ("test-missing-env", {}, "setup_job_env.sh no-such-env"),
    ("autogluon-tabular", {"train_file": "x"}, "label"),
])
def test_bad_submissions_are_refused(client, job, params, detail):
    response = client.post("/jobs", json={"job": job, "params": params})
    assert response.status_code == 400
    assert detail in response.text


@pytest.mark.parametrize("job, error", [
    ("test-fail", "ValueError: boom"),
    ("test-crash", "SIGKILL"),
    ("test-bad-result", "ValidationError"),
])
def test_failures_are_reported(client, job, error):
    id = submit(client, job)
    result = wait(client, id)
    assert result["status"] == "failed"
    assert error in result["error"]


def test_cancelling_a_running_job_stops_its_whole_process_group(client):
    id = submit(client, "test-sleep")
    child_pid_file = Config.JOBS_DIR and Path(Config.JOBS_DIR) / id / "child.pid"
    deadline = time.monotonic() + 20
    while not child_pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    child = int(child_pid_file.read_text())
    assert client.get(f"/jobs/{id}").json()["status"] == "running"

    assert client.post(f"/jobs/{id}/cancel").json()["status"] == "cancelled"
    deadline = time.monotonic() + 5
    while alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not alive(child)


def test_a_queued_job_can_be_cancelled_and_waits_its_turn(client):
    running = submit(client, "test-sleep")
    wait(client, running, until=("running",))
    queued = submit(client, "test-echo", text="later")
    time.sleep(0.3)
    assert client.get(f"/jobs/{queued}").json()["status"] == "queued"  # MAX_RUNNING_JOBS is 1
    assert client.post(f"/jobs/{queued}/cancel").json()["status"] == "cancelled"
    client.post(f"/jobs/{running}/cancel")
    assert wait(client, queued)["status"] == "cancelled"


def test_deleting_removes_the_job_and_its_files_but_not_while_it_runs(client):
    id = submit(client, "test-sleep")
    wait(client, id, until=("running",))
    assert client.delete(f"/jobs/{id}").status_code == 409
    client.post(f"/jobs/{id}/cancel")
    assert client.delete(f"/jobs/{id}").status_code == 200
    assert client.get(f"/jobs/{id}").status_code == 404
    assert not (Path(Config.JOBS_DIR) / id).exists()


def test_a_restart_requeues_queued_jobs_and_fails_interrupted_ones():
    runner = app_module.app.state.jobs
    queued = asyncio.run(runner.submit("test-echo", {"text": "after restart"}))["id"]
    stale = asyncio.run(runner.submit("test-echo", {"text": "was running"}))["id"]
    asyncio.run(runner.store.claim(stale))  # as a service that stopped left it

    with TestClient(app_module.app) as c:
        assert wait(c, queued)["result"] == {"echo": "after restart", "data_bytes": None}
        interrupted = c.get(f"/jobs/{stale}").json()
        assert interrupted["status"] == FAILED and "Interrupted" in interrupted["error"]


def test_uploads_keep_to_plain_names(client):
    assert client.post("/jobs/data", files={"file": ("../evil.csv", b"x")}).status_code == 400
    assert client.post("/jobs/data", files={"file": (".hidden", b"x")}).status_code == 400
    assert client.post("/jobs/data", files={"file": ("gone.csv", b"x")}).status_code == 201
    assert client.delete("/jobs/data/gone.csv").status_code == 200
    assert client.delete("/jobs/data/gone.csv").status_code == 404


def test_jobs_need_the_admin_key(client, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin")
    assert client.get("/jobs", headers={"X-API-Key": "client"}).status_code == 401
    assert client.get("/jobs", headers={"X-API-Key": "admin"}).status_code == 200


# ------------------------------------------------- serving a trained predictor
@pytest.fixture
def predictor(monkeypatch):
    """A "trained" predictor and an autogluon environment that is the test interpreter."""
    python = Config.JOB_ENVS_DIR / "autogluon" / "bin" / "python"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
    python.chmod(0o755)
    monkeypatch.setattr(ag.AutoGluonPredictor, "serve_module", "tests.fake_predictor_server")

    made = []

    def make(kind="tabular", task="tabular-prediction", **info):
        id = f"{len(made) + 1:02d}" + "ab" * 6
        workdir = Path(Config.JOBS_DIR) / id
        (workdir / "predictor").mkdir(parents=True, exist_ok=True)
        (workdir / "predictor" / "model.pkl").write_bytes(b"0" * 1000)
        (workdir / "predictor.json").write_text(json.dumps({"kind": kind, "task": task, "path": "predictor", **info}))
        made.append(workdir)
        return f"autogluon:{id}"

    yield make
    python.unlink()


def infer(client, model_id, task, **input):
    return client.post("/inference", json={"model_id": model_id, "task": task, "input": input})


def test_a_trained_predictor_is_served_through_inference(client, predictor):
    model_id = predictor()
    response = infer(client, model_id, "tabular-prediction", rows=[{"x": 1}, {"x": 2}], probabilities=True)
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    assert result == {"predictions": [2, 4], "probabilities": [{"a": 0.5, "b": 0.5}] * 2}  # rows_seen dropped
    assert response.json()["cold_start"] is True
    assert infer(client, model_id, "tabular-prediction", rows=[{"x": 5}]).json()["cold_start"] is False

    # A row the predictor cannot use is the client's mistake.
    assert infer(client, model_id, "tabular-prediction", rows=[{"y": 1}]).status_code == 400


def test_predictor_ids_and_tasks_are_checked(client, predictor):
    model_id = predictor()
    assert infer(client, model_id, "timeseries-forecast", rows=[{"item_id": "a"}]).status_code == 400
    assert infer(client, "autogluon:00000000ffff", "tabular-prediction", rows=[{"x": 1}]).status_code == 400
    assert infer(client, model_id, "tabular-prediction", rows=[]).status_code == 422


def test_trained_predictors_pass_the_model_allow_list(client, predictor, monkeypatch):
    model_id = predictor()
    monkeypatch.setattr(Config, "ALLOWED_MODELS", ["gpt2"])
    assert infer(client, model_id, "tabular-prediction", rows=[{"x": 1}]).status_code == 200
    assert infer(client, "autogluon:00000000ffff", "tabular-prediction", rows=[{"x": 1}]).status_code == 403


def test_a_crashed_predictor_is_dropped_and_restarted(client, predictor):
    model_id = predictor()
    assert infer(client, model_id, "tabular-prediction", rows=[{"x": 1}]).status_code == 200
    assert infer(client, model_id, "tabular-prediction", rows=[{"x": 1, "crash": True}]).status_code == 500
    response = infer(client, model_id, "tabular-prediction", rows=[{"x": 3}])
    assert response.status_code == 200 and response.json()["cold_start"] is True


def test_multimodal_images_must_be_inside_image_dir(client, predictor):
    model_id = predictor("multimodal", "multimodal-prediction", image_columns=["img"])
    image = Config.IMAGE_DIR / "job-test.png"
    image.write_bytes(b"png")
    try:
        ok = infer(client, model_id, "multimodal-prediction", rows=[{"x": 1, "img": "job-test.png"}])
        assert ok.status_code == 200, ok.text
        outside = infer(client, model_id, "multimodal-prediction", rows=[{"x": 1, "img": "../conftest.py"}])
        assert outside.status_code == 400
    finally:
        image.unlink()


def test_forecasts_follow_their_contract(client, predictor):
    model_id = predictor("timeseries", "timeseries-forecast", id_column="item_id", timestamp_column="timestamp")
    response = infer(client, model_id, "timeseries-forecast", rows=[{"item_id": "a", "timestamp": "2026-01-01", "target": 1}])
    assert response.status_code == 200, response.text
    assert response.json()["result"] == {"forecast": [{"item_id": "a", "mean": 1.0}]}


def test_a_job_cancelled_as_its_process_starts_ends_cancelled(client, monkeypatch):
    """The cancel lands after the claim but before the process is known: the runner stops it as it starts."""
    runner = client.app.state.jobs
    set_pid = runner.store.set_pid

    async def set_pid_after_a_cancel(id, pid):
        await runner.store.request_cancel(id)
        await set_pid(id, pid)

    monkeypatch.setattr(runner.store, "set_pid", set_pid_after_a_cancel)
    started = time.monotonic()
    id = submit(client, "test-sleep")
    assert wait(client, id)["status"] == "cancelled"
    assert time.monotonic() - started < 20  # stopped, not slept out


def test_a_result_of_any_shape_comes_back_as_it_was(arcade):
    """A job's result is JSON text in its row: a top-level @type or a big integer would not survive ArcadeDB's maps."""
    from jobs.store import JobStore
    from vectorstore.tables import TableStore

    store = JobStore(arcade, TableStore(arcade))
    result = {"@type": "Thing", "rows": [1, {"big": 2 ** 64}], "note": "done"}
    asyncio.run(store.insert("job1", "test-echo", {"text": "x", "nested": {"a": [1, 2]}}))
    asyncio.run(store.claim("job1"))
    assert asyncio.run(store.finish("job1", SUCCEEDED, result=result))
    row = asyncio.run(store.get("job1"))
    assert row["result"] == result and row["params"] == {"text": "x", "nested": {"a": [1, 2]}}
    assert [r["id"] for r in asyncio.run(store.list())] == ["job1"]
