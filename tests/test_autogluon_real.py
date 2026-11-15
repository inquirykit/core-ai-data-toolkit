"""The AutoGluon jobs and the predictor process, run for real in AutoGluon's environment.

The service's own environment has no AutoGluon (nor pandas), so these run only when AUTOGLUON_PYTHON names the
interpreter of one that has it, e.g. ``AUTOGLUON_PYTHON=envs/autogluon/bin/python``; otherwise they are skipped. Each
job runs as the runner runs it (``python -m jobs.worker``), on small tables written here, with the quickest models
(about 30 s in all; nothing is downloaded).
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

from core import PROJECT_ROOT

PYTHON = os.environ.get("AUTOGLUON_PYTHON")
pytestmark = pytest.mark.skipif(not PYTHON, reason="AUTOGLUON_PYTHON is not set: no AutoGluon environment to run in")

QUICK_TABULAR = {"GBM": {}}
QUICK_SERIES = {"Naive": {}, "SeasonalNaive": {}}


@pytest.fixture(scope="module")
def place(tmp_path_factory):
    """A datasets folder with the tables, and a jobs folder."""
    root = tmp_path_factory.mktemp("autogluon")
    data = root / "datasets"
    data.mkdir()
    (root / "jobs").mkdir()
    rows = ["age;income;city;churn"]  # a spreadsheet's ';' CSV
    for i in range(200):
        age = 20 + i % 50
        rows.append(f"{age};{30 + (i * 7) % 40}.5;{['oslo', 'rome', 'lima'][i % 3]};{'yes' if age > 60 else 'no'}")
    (data / "churn.csv").write_text("\n".join(rows) + "\n")
    days = [f"2026-01-{d:02d}" for d in range(1, 32)] + [f"2026-02-{d:02d}" for d in range(1, 29)]
    (data / "single.csv").write_text("day,sold\n" + "".join(f"{d},{10 + i % 7}\n" for i, d in enumerate(days)))
    (data / "two.csv").write_text("shop,day,sold\n" + "".join(
        f"{shop},{d},{10 + i % 7 + n}\n" for n, shop in enumerate(("a", "b")) for i, d in enumerate(days)))
    return root


def run(place: Path, id: str, job: str, params: dict) -> dict:
    """Run a job to its end as the runner would; its last message (result or error)."""
    module, cls = {"tabular": ("jobs.autogluon.tabular", "TrainTabular"),
                   "timeseries": ("jobs.autogluon.timeseries", "TrainTimeSeries"),
                   "predict": ("jobs.autogluon.predict", "PredictFile")}[job]
    workdir = place / "jobs" / id
    workdir.mkdir()
    # The params as the runner writes them: checked (input files inside this datasets folder), with every default.
    from jobs import registry

    was = os.environ.get("JOB_DATA_DIR")
    os.environ["JOB_DATA_DIR"] = str(place / "datasets")
    try:
        checked = registry.get(f"autogluon-{job}").cls.Params.model_validate(params)
    finally:
        os.environ["JOB_DATA_DIR"] = was or ""
    (workdir / "params.json").write_text(checked.model_dump_json())
    env = {**os.environ, "JOB_DATA_DIR": str(place / "datasets"), "JOB_NUM_CPUS": "2", "JOB_DEVICE": "cpu",
           "PYTHONPATH": str(PROJECT_ROOT)}
    done = subprocess.run([PYTHON, "-m", "jobs.worker", module, cls, str(workdir)], cwd=PROJECT_ROOT, env=env,
                          capture_output=True, text=True, timeout=600)
    message = json.loads(done.stdout.strip().splitlines()[-1])
    assert "result" in message or "error" in message, done.stderr[-3000:]
    return message


def test_a_tabular_predictor_is_scored_on_rows_it_did_not_see_and_reported(place):
    message = run(place, "aa00000000000001", "tabular", {
        "train_file": "churn.csv", "label": "churn", "name": "Churn", "time_limit": 30,
        "hyperparameters": QUICK_TABULAR})
    result = message["result"]
    assert result["model_id"] == "autogluon:aa00000000000001" and result["lower_is_better"] is False
    assert result["test_rows"] == 40 and result["train_rows"] == 160 and result["test_score"] is not None
    report = json.loads((place / "jobs" / "aa00000000000001" / "report.json").read_text())
    assert report["columns"] == {"age": "int", "income": "float", "city": "object"}
    assert set(report["sample"]) == {"age", "income", "city"}
    assert {row["feature"] for row in report["importance"]} == {"age", "income", "city"}
    classification = report["classification"]
    assert classification["count"] == 40 and [label["label"] for label in classification["labels"]] == ["no", "yes"]
    assert sum(map(sum, classification["confusion"])) == 40


def test_decimal_commas_are_numbers_as_the_summary_shows_them(place):
    (place / "datasets" / "umsatz.csv").write_text(
        "filiale;preis;umsatz\n" + "".join(f"{['nord', 'sued'][i % 2]};{i % 7},5;{i * 3 % 50},25\n" for i in range(120)))
    result = run(place, "aa00000000000003", "tabular", {
        "train_file": "umsatz.csv", "label": "umsatz", "time_limit": 30, "hyperparameters": QUICK_TABULAR})["result"]
    assert result["problem_type"] == "regression" and result["features"] == ["filiale", "preis"]


def test_held_out_rows_it_cannot_score_leave_the_job_succeeded(place):
    """Log loss over a class training never saw fails in AutoGluon: the predictor is trained, so the job succeeds."""
    rows = (place / "datasets" / "churn.csv").read_text().splitlines()
    (place / "datasets" / "unseen.csv").write_text("\n".join(rows[:5] + [rows[5].rsplit(";", 1)[0] + ";maybe"]) + "\n")
    message = run(place, "aa00000000000004", "tabular", {
        "train_file": "churn.csv", "label": "churn", "test_file": "unseen.csv", "eval_metric": "log_loss",
        "time_limit": 30, "hyperparameters": QUICK_TABULAR})
    assert message["result"]["test_score"] is None and message["result"]["test_rows"] == 0
    report = json.loads((place / "jobs" / "aa00000000000004" / "report.json").read_text())
    assert "scoring the held-out rows" in report["notes"][0] and "importance" not in report


def test_a_regression_scores_an_error_lower_is_better(place):
    result = run(place, "aa00000000000002", "tabular", {
        "train_file": "churn.csv", "label": "income", "time_limit": 30, "hyperparameters": QUICK_TABULAR})["result"]
    assert result["eval_metric"] == "root_mean_squared_error" and result["lower_is_better"] is True
    assert result["test_score"] < 0  # AutoGluon's, negated
    assert "classification" not in json.loads((place / "jobs" / "aa00000000000002" / "report.json").read_text())


def test_one_series_needs_no_id_column_and_is_forecast_after_training(place):
    result = run(place, "bb00000000000001", "timeseries", {
        "train_file": "single.csv", "timestamp_column": "day", "target": "sold", "prediction_length": 4,
        "time_limit": 30, "hyperparameters": QUICK_SERIES})["result"]
    assert result["series"] == 1 and result["lower_is_better"] is True
    folder = place / "jobs" / "bb00000000000001"
    forecast = (folder / "forecast.csv").read_text().splitlines()
    assert forecast[0].startswith("day,mean,0.1") and len(forecast) == 5
    report = json.loads((folder / "report.json").read_text())
    [series] = report["series"]
    assert series["id"] is None and len(series["history"]) == 12 and len(series["forecast"]) == 4
    assert report["band"] == [0.1, 0.9]


def test_several_series_without_an_id_column_are_refused_with_what_to_do(place):
    message = run(place, "bb00000000000002", "timeseries", {
        "train_file": "two.csv", "timestamp_column": "day", "target": "sold", "prediction_length": 4,
        "time_limit": 30, "hyperparameters": QUICK_SERIES})
    assert "pick the series ID column" in message["error"]


def test_a_forecaster_with_known_covariates_skips_the_forecast_after_training(place):
    rows = (place / "datasets" / "two.csv").read_text().splitlines()
    (place / "datasets" / "promo.csv").write_text(
        "\n".join([rows[0] + ",promo"] + [f"{row},{i % 2}" for i, row in enumerate(rows[1:])]) + "\n")
    result = run(place, "bb00000000000003", "timeseries", {
        "train_file": "promo.csv", "id_column": "shop", "timestamp_column": "day", "target": "sold",
        "prediction_length": 2, "known_covariates": ["promo"], "time_limit": 30,
        "hyperparameters": {"Naive": {}, "DirectTabular": {}}})["result"]
    assert result["series"] == 2
    report = json.loads((place / "jobs" / "bb00000000000003" / "report.json").read_text())
    assert "future values" in report["forecast_skipped"] and "series" not in report


def test_files_are_predicted_and_forecast_by_a_job(place):
    tabular = run(place, "cc00000000000001", "predict", {"predictor": "aa00000000000001", "data_file": "churn.csv"})
    assert tabular["result"] == {"file": "predictions.csv", "rows": 200}
    header = (place / "jobs" / "cc00000000000001" / "predictions.csv").read_text().splitlines()[0]
    assert header == "age,income,city,churn,prediction,probability_no,probability_yes"

    forecast = run(place, "cc00000000000002", "predict", {"predictor": "bb00000000000001", "data_file": "single.csv"})
    assert forecast["result"] == {"file": "forecast.csv", "rows": 4}

    gone = run(place, "cc00000000000003", "predict", {"predictor": "ffff000000000000", "data_file": "churn.csv"})
    assert "trained no predictor" in gone["error"]


def test_the_predictor_process_answers_one_series_and_refuses_what_does_not_fit(place):
    def ask(job: str, *requests: dict) -> list:
        done = subprocess.run(
            [PYTHON, "-m", "jobs.autogluon._serve", str(place / "jobs" / job)], cwd=PROJECT_ROOT,
            env={**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}, capture_output=True, text=True, timeout=300,
            input="".join(json.dumps(r) + "\n" for r in requests))
        return [json.loads(line) for line in done.stdout.splitlines()]

    history = [{"day": f"2026-02-{d:02d}", "sold": 10 + d % 7} for d in range(1, 20)]
    ready, answer = ask("bb00000000000001", {"rows": history})
    assert ready == {"ready": True} and len(answer["result"]["forecast"]) == 4
    assert set(answer["result"]["forecast"][0]) >= {"day", "mean", "0.1", "0.9"}

    # Without the future values of its known covariates: the request's fault, a 400.
    rows = [{"shop": "a", "day": f"2026-02-{d:02d}", "sold": 10, "promo": 0} for d in range(1, 20)]
    _, refused = ask("bb00000000000003", {"rows": rows})
    assert refused["client"] is True and "known_covariates" in refused["error"]

    _, predicted, wrong = ask("aa00000000000001", {"rows": [{"age": 70, "income": 41.5, "city": "oslo"}],
                                                   "probabilities": True}, {"rows": [{"nope": 1}]})
    assert predicted["result"]["predictions"] == ["yes"] and set(predicted["result"]["probabilities"][0]) == {"no", "yes"}
    assert wrong["client"] is True
