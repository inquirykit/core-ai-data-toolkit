import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import tests.fake_jobs  # noqa: F401  registers the test-* jobs
from api import app as app_module
from core.config import Config
from core.errors import Refused, TooLarge
from jobs.data import DataFiles
from tasks.store import FAILED, SUCCEEDED
from models.families.autogluon import predictor as ag
from tests import waits


@pytest.fixture
def client():
    with TestClient(app_module.app) as c:
        yield c


def submit(client, job, **params):
    response = client.post("/jobs", json={"job": job, "params": params})
    assert response.status_code == 202, response.text
    return response.json()["id"]


def wait(client, id, until=("succeeded", "failed", "cancelled"), timeout=30.0):
    """The job once its status is one of ``until``."""
    def ended():
        job = client.get(f"/jobs/{id}").json()
        return job if job["status"] in until else None
    return waits.until(ended, timeout, lambda: f"job {id} is still {client.get(f'/jobs/{id}').json()['status']}")


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
    for name in ("autogluon-tabular", "autogluon-timeseries", "autogluon-predict"):
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
    ("test-echo", {"text": "x", "data": "../../etc/passwd"}, "DATA_DIR/datasets"),
    ("test-echo", {"text": "x", "data": "missing.csv"}, "DATA_DIR/datasets"),
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
    child_pid_file = Config.JOBS_DIR / id / "child.pid"
    waits.until(child_pid_file.exists, 20, "the job never started its child")
    child = int(child_pid_file.read_text())
    assert client.get(f"/jobs/{id}").json()["status"] == "running"

    assert client.post(f"/jobs/{id}/cancel").json()["status"] == "cancelled"
    waits.until(lambda: not alive(child), 5, "its child is still alive")


def test_a_failing_job_is_retried_and_its_log_marks_each_retry(client):
    r = client.post("/jobs", json={"job": "test-fail", "retries": 1, "retry_delay_s": 1})
    assert r.status_code == 202, r.text
    assert r.json()["retries"] == 1 and r.json()["retried"] == 0
    job = wait(client, r.json()["id"])
    assert job["status"] == "failed" and job["retried"] == 1 and "boom" in job["error"]
    log = client.get(f"/jobs/{job['id']}/log").text
    assert log.count("boom") >= 2 and "--- retry 1 of 1 ---" in log


def test_a_job_past_its_timeout_is_stopped_with_its_whole_process_group(client):
    r = client.post("/jobs", json={"job": "test-sleep", "timeout_s": 1})
    assert r.status_code == 202, r.text
    id = r.json()["id"]
    child_pid_file = Path(Config.JOBS_DIR) / id / "child.pid"
    waits.until(child_pid_file.exists, 20, "the job never started its child")
    child = int(child_pid_file.read_text())
    job = wait(client, id)
    assert job["status"] == "failed" and "Timed out" in job["error"]
    waits.until(lambda: not alive(child), 5, "its child is still alive")


@pytest.mark.parametrize("policy", [{"retries": 11}, {"retry_delay_s": 0}, {"timeout_s": 0.5}])
def test_retries_and_timeouts_out_of_range_are_refused(client, policy):
    assert client.post("/jobs", json={"job": "test-echo", "params": {"text": "x"}, **policy}).status_code == 422


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


def test_a_job_that_cannot_be_queued_leaves_no_folder(client, monkeypatch):
    runner = client.app.state.jobs
    before = set(runner.jobs_dir.glob("*"))

    async def refused(*args, **kwargs):
        raise RuntimeError("the database refused the row")

    monkeypatch.setattr(runner.tasks, "submit", refused)
    with pytest.raises(RuntimeError):
        client.portal.call(runner.submit, "test-echo", {"text": "x"})
    assert set(runner.jobs_dir.glob("*")) == before


def test_jobs_are_listed_a_page_at_a_time(client):
    ids = [submit(client, "test-echo", text=str(i)) for i in range(3)]
    first = client.get("/jobs", params={"limit": 2}).json()
    assert [j["id"] for j in first["jobs"]] == [ids[2], ids[1]]
    # The next page goes on from there, whatever was submitted since.
    later = submit(client, "test-echo", text="later")
    older = client.get("/jobs", params={"limit": 2, "cursor": first["next"]}).json()["jobs"]
    assert older[0]["id"] == ids[0]
    ids.append(later)
    for id in ids:
        wait(client, id)


def test_a_restart_requeues_queued_jobs_and_fails_interrupted_ones():
    runner = app_module.app.state.jobs
    queued = asyncio.run(runner.submit("test-echo", {"text": "after restart"}))["id"]
    stale = asyncio.run(runner.submit("test-echo", {"text": "was running"}))["id"]
    asyncio.run(runner.tasks.store.claim(stale))  # as a service that stopped left it

    with TestClient(app_module.app) as c:
        assert wait(c, queued)["result"] == {"echo": "after restart", "data_bytes": None}
        interrupted = c.get(f"/jobs/{stale}").json()
        assert interrupted["status"] == FAILED and "Interrupted" in interrupted["error"]


def test_jobs_are_listed_by_job(client):
    echo = submit(client, "test-echo", text="x")
    wait(client, echo)
    listed = client.get("/jobs", params={"job": ["test-echo", "test-fail"]}).json()["jobs"]
    assert echo in [j["id"] for j in listed] and {j["job"] for j in listed} <= {"test-echo", "test-fail"}
    every = client.get("/jobs", params={"limit": 500}).json()["jobs"]
    sleeps = client.get("/jobs", params={"job": "test-sleep", "limit": 500}).json()["jobs"]
    assert [j["id"] for j in sleeps] == [j["id"] for j in every if j["job"] == "test-sleep"]


def test_jobs_are_listed_by_what_they_work_on(client):
    """A job names a param as its ref (autogluon-predict, its predictor): its jobs are found by it, however many
    others came since."""
    mine = submit(client, "test-echo", text="predictor-a")
    for i in range(3):
        submit(client, "test-echo", text=f"other-{i}")
    listed = client.get("/jobs", params={"ref": "predictor-a", "job": "test-echo", "limit": 1}).json()["jobs"]
    assert [j["id"] for j in listed] == [mine] and listed[0]["ref"] == "predictor-a"
    for j in client.get("/jobs", params={"job": "test-echo", "limit": 4}).json()["jobs"]:
        wait(client, j["id"])


def test_a_data_files_summary_gives_its_columns_and_first_rows(client):
    import duckdb

    csv = "age;income;city;churn;day\n70;41.2;oslo;no;2026-01-01\n57;;rome;yes;2026-01-02\n31;12.5;oslo;no;2026-01-03\n"
    assert client.post("/jobs/data", files={"file": ("summary.csv", csv.encode())}).status_code == 201
    summary = client.get("/jobs/data/summary.csv/summary").json()
    assert summary["rows"] == 3 and [c["name"] for c in summary["columns"]] == ["age", "income", "city", "churn", "day"]
    columns = {c["name"]: c for c in summary["columns"]}
    assert columns["age"]["type"] == "BIGINT" and columns["age"]["min"] == "31"
    assert columns["age"]["mean"] == pytest.approx(52.67, abs=0.01)
    assert columns["income"]["missing"] == pytest.approx(1 / 3, abs=0.01)
    assert columns["churn"]["type"] == "VARCHAR"  # yes/no stays text, as AutoGluon reads it
    assert columns["day"]["type"] == "DATE" and columns["city"]["distinct"] == 2
    assert summary["head"][0] == {"age": 70, "income": 41.2, "city": "oslo", "churn": "no", "day": "2026-01-01"}

    # A spreadsheet's ";" file with decimal commas: split as training splits it, its numbers numbers.
    german = "datum;umsatz;preis\n" + "".join(f"2026-01-{d:02d};{d},5;1,25\n" for d in range(1, 29))
    assert client.post("/jobs/data", files={"file": ("umsatz.csv", german.encode())}).status_code == 201
    columns = {c["name"]: c for c in client.get("/jobs/data/umsatz.csv/summary").json()["columns"]}
    assert list(columns) == ["datum", "umsatz", "preis"] and columns["umsatz"]["type"] == "DOUBLE"
    assert columns["umsatz"]["min"] == "1.5"

    parquet = Config.JOB_DATA_DIR / "summary.parquet"
    duckdb.connect().execute(f"COPY (SELECT range AS n, 'x' || range AS s FROM range(50)) TO '{parquet}'")
    summary = client.get("/jobs/data/summary.parquet/summary").json()
    assert summary["rows"] == 50 and len(summary["head"]) == 20


def test_a_summary_reads_only_tables_inside_the_datasets_folder(client):
    (Config.JOB_DATA_DIR / "broken.parquet").write_bytes(b"not a parquet file")
    assert client.post("/jobs/data", files={"file": ("photo.png", b"png")}).status_code == 201
    assert client.get("/jobs/data/../jobs.db/summary").status_code in (400, 404)
    assert client.get("/jobs/data/missing.csv/summary").status_code == 404
    assert client.get("/jobs/data/photo.png/summary").status_code == 400
    unreadable = client.get("/jobs/data/broken.parquet/summary")
    assert unreadable.status_code == 400 and "could not be read as a table" in unreadable.text


def test_uploads_keep_to_plain_names(client):
    assert client.post("/jobs/data", files={"file": ("../evil.csv", b"x")}).status_code == 400
    assert client.post("/jobs/data", files={"file": (".hidden", b"x")}).status_code == 400
    assert client.post("/jobs/data", files={"file": ("gone.csv", b"x")}).status_code == 201
    assert client.delete("/jobs/data/gone.csv").status_code == 200
    assert client.delete("/jobs/data/gone.csv").status_code == 404


def test_an_upload_past_the_limit_leaves_nothing_and_keeps_the_old_file(tmp_path):
    files = DataFiles(tmp_path, max_bytes=10)

    async def chunks(*parts: bytes):
        for part in parts:
            yield part

    assert asyncio.run(files.save("a.csv", chunks(b"12345", b"678"))) == {"path": "a.csv", "bytes": 8}
    with pytest.raises(TooLarge):
        asyncio.run(files.save("a.csv", chunks(b"12345", b"678901")))
    assert (tmp_path / "a.csv").read_bytes() == b"12345678"
    assert [p.name for p in tmp_path.iterdir()] == ["a.csv"]  # no temporary file left


def test_the_data_files_stay_in_their_folder(tmp_path):
    files = DataFiles(tmp_path, max_bytes=10)
    for leaving in (files.write_lines("../out.jsonl", [{"x": 1}]), files.delete("../a.csv"), files.summary("../a.csv")):
        with pytest.raises(Refused):
            asyncio.run(leaving)
    asyncio.run(files.write_lines("evals/d.jsonl", [{"x": 1}, {"x": 2}]))
    assert (tmp_path / "evals/d.jsonl").read_text() == '{"x": 1}\n{"x": 2}\n'


# ------------------------------------------------- job environments
def test_an_environment_is_ready_once_its_install_finished_and_its_python_resolves(tmp_path):
    from core import procs

    target = tmp_path / "env"
    (target / "bin").mkdir(parents=True)
    (target / "bin" / "python").symlink_to(sys.executable)
    assert procs.env_python(tmp_path, "env") is None  # an install that stopped halfway
    (target / ".ready").write_text("pkg==1\n")
    assert procs.env_python(tmp_path, "env") == target / "bin" / "python"
    (target / "bin" / "python").unlink()
    (target / "bin" / "python").symlink_to(tmp_path / "gone")  # the Python uv kept for it was removed
    assert procs.env_python(tmp_path, "env") is None


def test_the_setup_job_installs_an_environment_from_its_requirements(client, monkeypatch, tmp_path):
    """The job runs scripts/setup_job_env.sh with the service's uv; a stand-in uv here, so nothing is downloaded."""
    import uv
    from jobs.base import JobContext
    from jobs.envs import setup

    # Makes a venv (its last argument) whose python is this one; installs fail while FAIL_INSTALL names a file.
    fake_uv = tmp_path / "uv"
    failing = tmp_path / "fail-install"
    fake_uv.write_text(f'#!/bin/sh\nfor last; do :; done\ncase "$1" in\n'
                       f'  venv) rm -rf "$last" && mkdir -p "$last/bin" && ln -sf "{sys.executable}" "$last/bin/python" ;;\n'
                       f'  pip) [ -e "{failing}" ] && exit 1; echo "installed $*" ;;\nesac\n')
    fake_uv.chmod(0o755)
    monkeypatch.setattr(uv, "find_uv_bin", lambda: str(fake_uv))
    runner = client.app.state.jobs
    target = runner.envs_dir / "autogluon"
    assert not runner.env_ready("autogluon") and not runner.env_current("autogluon")
    try:
        params = setup.SetupParams(name="autogluon")
        result = setup.SetupEnv().run(JobContext("j1", tmp_path, 1, "cpu"), params)
        assert Path(result.python).parent.resolve() == (target / "bin").resolve()
        assert runner.env_ready("autogluon") and runner.env_current("autogluon")
        types = {j["name"]: j for j in client.get("/jobs/types").json()["jobs"]}
        assert types["autogluon-tabular"]["env_ready"] and types["autogluon-tabular"]["env_current"]
        # The requirements changed since: still ready, but not current, so the pages offer an update.
        (target / ".ready").write_text("autogluon==0.1\n")
        assert runner.env_ready("autogluon") and not runner.env_current("autogluon")
        # An update that fails leaves it serving as it was, still out of date.
        failing.touch()
        with pytest.raises(subprocess.CalledProcessError):
            setup.SetupEnv().run(JobContext("j2", tmp_path, 1, "cpu"), params)
        assert runner.env_ready("autogluon") and not runner.env_current("autogluon")
        failing.unlink()
        # The Python it linked to is gone: Set up makes it again.
        (target / "bin" / "python").unlink()
        (target / "bin" / "python").symlink_to(tmp_path / "removed-python")
        assert not runner.env_ready("autogluon")
        setup.SetupEnv().run(JobContext("j3", tmp_path, 1, "cpu"), params)
        assert runner.env_ready("autogluon") and runner.env_current("autogluon")
    finally:
        shutil.rmtree(target, ignore_errors=True)


@pytest.mark.parametrize("name, detail", [("nope", "No requirements-nope.txt"), ("../autogluon", "pattern")])
def test_the_setup_job_installs_only_environments_with_a_requirements_file(client, name, detail):
    response = client.post("/jobs", json={"job": "setup-env", "params": {"name": name}})
    assert response.status_code == 400 and detail in response.text


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
    ready = Config.JOB_ENVS_DIR / "autogluon" / ".ready"
    ready.write_text("autogluon\n")
    monkeypatch.setattr(ag.AutoGluonPredictor, "serve_module", "tests.fake_predictor_server")

    made = []

    def make(task="tabular-prediction", **params):
        """A job folder as a training job leaves it: the predictor, params.json and result.json."""
        id = f"{len(made) + 1:02d}" + "ab" * 6
        workdir = Path(Config.JOBS_DIR) / id
        (workdir / "predictor").mkdir(parents=True, exist_ok=True)
        (workdir / "predictor" / "model.pkl").write_bytes(b"0" * 1000)
        (workdir / "params.json").write_text(json.dumps({"train_file": "/data/churn.csv", **params}))
        (workdir / "result.json").write_text(json.dumps({"model_id": f"autogluon:{id}", "task": task}))
        made.append(workdir)
        return f"autogluon:{id}"

    yield make
    python.unlink()
    ready.unlink()
    for workdir in made:  # the model lists of later tests do not show them
        shutil.rmtree(workdir, ignore_errors=True)


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
    unknown = infer(client, "autogluon:00000000ffff", "tabular-prediction", rows=[{"x": 1}])
    assert unknown.status_code == 400 and "no trained predictor" in unknown.text  # told, with DEBUG off too
    assert infer(client, model_id, "tabular-prediction", rows=[]).status_code == 422


def test_trained_predictors_pass_the_model_allow_list(client, predictor, monkeypatch):
    model_id = predictor()
    monkeypatch.setattr(Config, "ALLOWED_MODELS", ["gpt2"])
    assert infer(client, model_id, "tabular-prediction", rows=[{"x": 1}]).status_code == 200
    assert infer(client, "autogluon:00000000ffff", "tabular-prediction", rows=[{"x": 1}]).status_code == 403


def test_a_crashed_predictor_is_dropped_and_restarted(client, predictor):
    from core import procs

    model_id = predictor()
    assert infer(client, model_id, "tabular-prediction", rows=[{"x": 1}]).status_code == 200
    tracked = len(procs._tracked)
    assert infer(client, model_id, "tabular-prediction", rows=[{"x": 1, "crash": True}]).status_code == 500
    response = infer(client, model_id, "tabular-prediction", rows=[{"x": 3}])
    assert response.status_code == 200 and response.json()["cold_start"] is True
    # The crashed one is let go: at exit nothing kills a pid that may since belong to another process.
    assert len(procs._tracked) == tracked


def test_a_cancelled_predict_leaves_no_answer_for_the_next_caller(client, predictor):
    """The predictor owes the cancelled request an answer; it is stopped, so that answer never reaches the next one."""
    model_id = predictor()
    manager = client.app.state.manager

    async def cancel_one_then_ask_again():
        await manager.run_inference(model_id, "tabular-prediction", {"rows": [{"x": 1}]})  # loaded
        slow = asyncio.create_task(
            manager.run_inference(model_id, "tabular-prediction", {"rows": [{"x": 1, "sleep": 2}]}))
        await asyncio.sleep(0.5)
        slow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow
        return await manager.run_inference(model_id, "tabular-prediction", {"rows": [{"x": 7}]})

    answer = client.portal.call(cancel_one_then_ask_again)
    assert answer["result"].predictions == [14] and answer["cold_start"] is True


def test_a_request_waiting_behind_a_cancelled_one_is_served_by_a_new_process(client, predictor):
    """The cancelled request's process is stopped while the next one waits its turn on it: that one gets a new process,
    not an error."""
    model_id = predictor()
    manager = client.app.state.manager

    async def cancel_the_first():
        await manager.run_inference(model_id, "tabular-prediction", {"rows": [{"x": 1}]})  # loaded
        slow = asyncio.create_task(
            manager.run_inference(model_id, "tabular-prediction", {"rows": [{"x": 1, "sleep": 2}]}))
        await asyncio.sleep(0.3)
        waiting = asyncio.create_task(manager.run_inference(model_id, "tabular-prediction", {"rows": [{"x": 4}]}))
        await asyncio.sleep(0.3)
        slow.cancel()
        with pytest.raises(asyncio.CancelledError):
            await slow
        return await waiting

    answer = client.portal.call(cancel_the_first)
    assert answer["result"].predictions == [8] and answer["cold_start"] is True


def test_a_cancelled_load_leaves_no_predictor_process(client, predictor):
    model_id = predictor(start_sleep=5)
    pid_file = Path(Config.JOBS_DIR) / model_id.split(":")[1] / "serve.pid"
    manager = client.app.state.manager

    async def cancel_while_loading():
        loading = asyncio.create_task(manager.run_inference(model_id, "tabular-prediction", {"rows": [{"x": 1}]}))
        await waits.until_async(pid_file.exists, 20, "the predictor process never started")
        loading.cancel()
        with pytest.raises(asyncio.CancelledError):
            await loading

    client.portal.call(cancel_while_loading)
    pid = int(pid_file.read_text())
    waits.until(lambda: not alive(pid), 5, "the predictor process is still running")
    assert manager.model_state(model_id, "tabular-prediction") == "unloaded"


def test_deleting_a_job_unloads_the_predictor_it_trained(client, predictor):
    model_id = predictor()
    id = model_id.split(":")[1]
    store = client.app.state.jobs.tasks.store
    client.portal.call(store.insert, id, "autogluon-tabular", {})
    client.portal.call(store.claim, id)
    client.portal.call(lambda: store.finish(id, SUCCEEDED, result={"model_id": model_id, "task": "tabular-prediction"}))
    assert infer(client, model_id, "tabular-prediction", rows=[{"x": 1}]).status_code == 200

    assert client.delete(f"/jobs/{id}").status_code == 200
    assert client.app.state.manager.model_state(model_id, "tabular-prediction") == "unloaded"
    assert infer(client, model_id, "tabular-prediction", rows=[{"x": 1}]).status_code == 400


def test_tables_are_read_with_their_own_delimiter_and_decimal_mark(tmp_path):
    from core.csvfiles import dialect

    (tmp_path / "semi.csv").write_text("a;b;c\n1;2,5;x\n3;4,25;y\n")
    (tmp_path / "semi_dots.csv").write_text("age;income;city\n70;41.2;oslo\n57;42.9;rome\n")
    (tmp_path / "numbers.csv").write_text("a;b\n" + "1,5;2,5\n" * 30)
    (tmp_path / "comma.csv").write_text('a,b\n"1;2",3\n4,5\n')
    (tmp_path / "one.csv").write_text("a\n1\n2\n")
    assert dialect(str(tmp_path / "semi.csv")) == (";", ",")
    assert dialect(str(tmp_path / "semi_dots.csv")) == (";", ".")
    assert dialect(str(tmp_path / "numbers.csv")) == (";", ",")
    assert dialect(str(tmp_path / "comma.csv")) == (",", ".")
    assert dialect(str(tmp_path / "one.csv")) == (",", ".")


def test_trained_models_are_listed_under_their_task_with_their_name(client, predictor, trained, monkeypatch, request):
    from models import registry

    tabular = predictor(name="Churn")
    forecaster = predictor("timeseries-forecast", id_column="item_id", timestamp_column="timestamp")
    # A job that failed or is still running has no result.json: nothing to serve, nothing listed.
    unfinished = Path(Config.JOBS_DIR) / ("ef" * 8)
    (unfinished / "predictor").mkdir(parents=True, exist_ok=True)
    request.addfinalizer(lambda: shutil.rmtree(unfinished))

    listed = client.get("/models/list").json()
    assert tabular in listed["tabular-prediction"] and forecaster in listed["timeseries-forecast"]
    assert tabular not in listed["timeseries-forecast"] and trained in listed["text-classification"]
    assert "autogluon:" + "ef" * 8 not in str(listed)
    catalog = {t["task"]: t for t in client.get("/models/catalog").json()["tasks"]}
    card = next(m for m in catalog["tabular-prediction"]["models"] if m["id"] == tabular)
    assert card["description"] == "Churn" and card["served_by"] == "AutoGluonPredictor"
    unnamed = next(m for m in catalog["timeseries-forecast"]["models"] if m["id"] == forecaster)
    assert unnamed["description"] == "Trained on churn.csv"
    # Each serves its one task, which a bare ID in PRELOAD_MODELS loads under.
    assert registry.claimed_tasks(tabular) == ["tabular-prediction"]
    assert registry.claimed_tasks(forecaster) == ["timeseries-forecast"]

    # The operator chose them: they stay listed under an allow-list that does not name them.
    monkeypatch.setattr(Config, "ALLOWED_MODELS", ["gpt2"])
    listed = client.get("/models/list").json()
    assert tabular in listed["tabular-prediction"] and trained in listed["text-classification"]
    ids = [m["id"] for m in client.get("/v1/models").json()["data"]]
    assert tabular in ids and forecaster in ids


def test_forecasts_follow_their_contract(client, predictor):
    model_id = predictor("timeseries-forecast", id_column="item_id", timestamp_column="timestamp")
    response = infer(client, model_id, "timeseries-forecast", rows=[{"item_id": "a", "timestamp": "2026-01-01", "target": 1}])
    assert response.status_code == 200, response.text
    assert response.json()["result"] == {"forecast": [{"item_id": "a", "timestamp": "2026-01-02", "mean": 1.0}]}

    # Trained on one series (no ID column): its history is rows of time and value alone.
    one = predictor("timeseries-forecast", timestamp_column="timestamp")
    response = infer(client, one, "timeseries-forecast", rows=[{"timestamp": "2026-01-01", "target": 1}])
    assert response.status_code == 200, response.text
    assert response.json()["result"] == {"forecast": [{"timestamp": "2026-01-02", "mean": 1.0}]}


def test_a_job_cancelled_as_its_process_starts_ends_cancelled(client, monkeypatch):
    """The cancel lands after the claim but before the process is known: the runner stops it as it starts."""
    runner = client.app.state.jobs
    set_pid = runner.tasks.store.set_pid

    async def set_pid_after_a_cancel(id, pid):
        await runner.tasks.store.request_cancel(id)
        await set_pid(id, pid)

    monkeypatch.setattr(runner.tasks.store, "set_pid", set_pid_after_a_cancel)
    started = time.monotonic()
    id = submit(client, "test-sleep")
    assert wait(client, id)["status"] == "cancelled"
    assert time.monotonic() - started < 20  # stopped, not slept out


def test_a_result_of_any_shape_comes_back_as_it_was(own_db):
    """A job's result is JSON text in its row, kept as it came: a top-level @type or a big integer too."""
    from tasks.store import TaskStore
    from db.tables import TableStore

    store = TaskStore(own_db, TableStore(own_db))
    result = {"@type": "Thing", "rows": [1, {"big": 2 ** 64}], "note": "done"}
    asyncio.run(store.insert("job1", "test-echo", {"text": "x", "nested": {"a": [1, 2]}}))
    asyncio.run(store.claim("job1"))
    assert asyncio.run(store.finish("job1", SUCCEEDED, result=result))
    row = asyncio.run(store.get("job1"))
    assert row["result"] == result and row["params"] == {"text": "x", "nested": {"a": [1, 2]}}
    assert [r["id"] for r in asyncio.run(store.list())] == ["job1"]


# ------------------------------------------------- the text-classifier job and what it trains
def test_the_classifier_job_reads_splits_and_scores_its_rows(tmp_path):
    from jobs.train import text_classifier as tc

    lines = tmp_path / "rows.jsonl"
    lines.write_text('{"text": "good", "label": "pos"}\n\n{"text": "bad", "label": "neg"}\n{"text": "", "label": "x"}\n'
                     '{"text": "no label"}\n{"body": "other field", "y": 1}\n')
    assert tc.read_rows(str(lines), "text", "label") == [("good", "pos"), ("bad", "neg")]
    table = tmp_path / "rows.csv"
    table.write_text("body,y\nfine,1\nmeh, 0 \n")
    assert tc.read_rows(str(table), "body", "y") == [("fine", "1"), ("meh", "0")]

    pairs = [(f"a{i}", "a") for i in range(10)] + [(f"b{i}", "b") for i in range(4)] + [("c0", "c")]
    train, held = tc.split(pairs, 0.25, seed=1)
    # A quarter of each label, rounded down: the one "c" stays in training.
    assert sorted(label for _, label in held) == ["a", "a", "b"]
    assert sorted(train + held) == sorted(pairs) and ("c0", "c") in train
    assert tc.split(pairs, 0.25, seed=1) == (train, held)

    assert tc.scores([0, 0, 1, 1], [0, 1, 1, 1], 3) == {"accuracy": 0.75, "macro_f1": pytest.approx((2 / 3 + 0.8) / 2)}


def test_the_classifier_job_is_offered_in_the_services_environment(client):
    job = {j["name"]: j for j in client.get("/jobs/types").json()["jobs"]}["text-classifier"]
    assert job["env"] == "default" and job["env_ready"] is True
    assert job["params"]["required"] == ["train_file"] and "base_model" in job["params"]["properties"]
    assert "model_id" in job["result"]["properties"]


@pytest.fixture
def trained():
    """A classifier "trained" by a job here: its model folder and result.json, as the text-classifier job leaves them."""
    id = "cd" * 8
    workdir = Path(Config.JOBS_DIR) / id
    (workdir / "model").mkdir(parents=True, exist_ok=True)
    (workdir / "result.json").write_text(json.dumps({"model_id": f"trained:{id}", "task": "text-classification"}))
    yield f"trained:{id}"
    shutil.rmtree(workdir)


def test_a_trained_classifier_is_served_from_its_job_and_passes_the_allow_list(client, trained, monkeypatch):
    from models import registry
    from models.families.trained.classifier import TrainedClassifier

    assert registry.resolve(trained, "text-classification") is TrainedClassifier
    assert TrainedClassifier.weights_source(trained) == str(Path(Config.JOBS_DIR) / trained.split(":")[1] / "model")
    # Another task is not what it was trained for; an ID no job here trained serves nothing.
    with pytest.raises(registry.UnsupportedModel):
        registry.resolve(trained, "embedding")
    assert TrainedClassifier.weights_source("trained:" + "0" * 16) is None
    manager = client.app.state.manager
    monkeypatch.setattr(Config, "ALLOWED_MODELS", ["gpt2"])
    manager.check_allowed(trained)
    with pytest.raises(Exception, match="ALLOWED_MODELS"):
        manager.check_allowed("trained:" + "0" * 16)
