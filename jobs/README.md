# Jobs

A job is a long-running script: training, a batch over a file, an evaluation. Where a model answers one request in seconds, a job is submitted, runs in the background for minutes or hours, reports progress, can be cancelled, and leaves files behind.

To add a job, add one file to a subfolder of `jobs/`. Nothing else needs editing: the service finds it when it first needs a job, and it appears in `GET /jobs/types`.

## Adding a job

```python
# jobs/batch/word_count.py
from pydantic import Field

from jobs import DataFile, Job, JobContext, JobParams, JobResult, register_job


class Params(JobParams):
    input_file: DataFile                       # a file in DATA_DIR/datasets; run() gets its absolute path
    min_length: int = Field(1, ge=1)


class Result(JobResult):
    words: int


@register_job("word-count")
class WordCount(Job):
    """Count the words of a text file"""

    Params = Params
    Result = Result

    def run(self, ctx: JobContext, p: Params) -> Result:
        lines = open(p.input_file).read().splitlines()
        words = 0
        for i, line in enumerate(lines):
            words += sum(len(w) >= p.min_length for w in line.split())
            ctx.progress((i + 1) / len(lines), "counting")
        ctx.path("count.txt").write_text(str(words))   # an output file, served by GET /jobs/{id}/files
        return Result(words=words)
```

`POST /jobs {"job": "word-count", "params": {"input_file": "book.txt"}}` now queues it.

| Part | What it is |
|---|---|
| `Params` | A `JobParams` subclass: what `POST /jobs` takes, checked when the job is submitted (400 if it does not fit). Unknown fields are refused. `GET /jobs/types` serves it as JSON Schema. |
| `Result` | Optional. A `JobResult` subclass that `run()`'s return value is checked against; fields beyond it are dropped. Without it, return anything JSON can hold. |
| `ref_param` | Optional. The param naming what the job works on (`autogluon-predict`'s `predictor`): its value is kept as the job's `ref`, so `GET /jobs?ref=<value>` lists that thing's jobs. |
| `run(ctx, params)` | Plain blocking code. It runs in its own process, so it may take hours, use every core it is given, and crash without harming the service. |
| `@register_job(name, env=..., description=...)` | `env` is the environment it runs in (below). The description defaults to the docstring's first line. |

What `ctx` offers:

| | |
|---|---|
| `ctx.path(*parts)` | A path in the job's own directory (`DATA_DIR/jobs/<id>/`). Write outputs there. |
| `ctx.progress(fraction, message)` | Progress (0 to 1, or None if unknown) and a short status line. Throttled, so call it as often as you like. |
| `ctx.num_cpus` | Cores the job may use (`JOB_NUM_CPUS`). Pass it to libraries that take one. |
| `ctx.device` | `JOB_DEVICE`: `cpu` (CUDA is already hidden), `cuda`, `mps` or `auto`. |
| `ctx.id`, `ctx.workdir` | The job's ID and directory. |

Anything the job prints or logs goes to its log (`GET /jobs/{id}/log`, and live in `GET /jobs/{id}/events`).

## Environments

A job runs in the service's own interpreter (`env="default"`) or in a separate one, for libraries the service cannot install next to its models. AutoGluon is one: it needs Python below 3.14, torch below 2.14 (2.11 on macOS), transformers below 5.15 and timm below 1.0.7, while vegaml and FastVLM need newer ones.

An environment `<name>` is a virtualenv at `JOB_ENVS_DIR/<name>` (default `envs/<name>`) with the packages in `requirements-<name>.txt`:

```bash
scripts/setup_job_env.sh autogluon        # needs uv; PYTHON=3.12 picks another Python
```

The `setup-env` job runs that script (`{"name": "autogluon"}`), with the uv the service depends on, so the pages' Set up button installs an environment without a terminal; its output is the job's log. Nothing installs unless asked: a job whose environment is missing is refused when submitted. The script writes `JOB_ENVS_DIR/<name>/.ready`, a copy of the requirements file, last: an environment is ready once that file exists and its `bin/python` resolves, so a first install cancelled halfway is not taken for one. An update that fails keeps the `.ready` of the install before it, so the environment goes on serving (and still shows as out of date), and Set up makes an environment whose interpreter no longer resolves again. An environment installed before `.ready` existed shows as not set up: one Set up makes it ready, quickly, since its packages are there. `GET /jobs/types` says, per job, whether its environment is ready (`env_ready`) and whether it was installed from the requirements file as it is now (`env_current`; when not, Set up updates it). Remove an environment by deleting its folder.

In the container, `JOB_ENVS_DIR` is `/opt/envs`, a volume of its own (`envs` in `docker-compose.yml`), and the Python 3.13 that uv downloads for the environments lives there too (`UV_PYTHON_INSTALL_DIR`), since each environment's interpreter links to it. It is not backed up: an environment is rebuilt from its requirements file. Installing needs the network (PyPI, and a Python build when missing); the proxy variables reach the job.

The service imports every job file to read its name and params, so **import the job's libraries inside `run()`**, not at the top of the file; a job file may import only pydantic and `jobs` at the top. Files and folders whose names start with `_` are not registered (helpers such as `jobs/autogluon/_common.py`), but jobs can import them.

## How a job runs

1. `POST /jobs` checks the params, writes them to `DATA_DIR/jobs/<id>/params.json`, records the job as `queued` and returns its ID (202).
2. Up to `MAX_RUNNING_JOBS` jobs run at once (default 1); the rest wait, the most urgent first (`"priority": "high"`, `"normal"` (the default) or `"low"` in `POST /jobs`, or on a schedule), then the oldest. Jobs are one kind of task in the service's one queue (`tasks/runner.py`); eval runs are another, with a limit of their own, so a long training job does not hold up an eval run.
3. A job runs as `python -m jobs.worker` in its environment's interpreter, in a new process group, with the project root on `PYTHONPATH`. Its stdout carries progress and the result to the service; whatever it prints is moved to stderr, which becomes `job.log`.
4. It ends `succeeded` (with `result`, also in `result.json`), `failed` (with `error`: the exception, or how the process died, such as a SIGKILL from running out of memory) or `cancelled`.

Cancelling a running job sends SIGTERM to its whole process group (then SIGKILL after 10 seconds), so processes it started, such as Ray workers, stop too. A job also ends itself if the service that started it goes away. When the service restarts, queued jobs are still queued (the table is the queue) and jobs it was running are marked failed (interrupted).

## AutoGluon

`jobs/autogluon/` trains [AutoGluon](https://auto.gluon.ai) predictors in the `autogluon` environment (AutoGluon 1.6's tabular part, with every model family, and its time-series part). The AutoGluon section of `/admin` (`frontend/src/admin/autogluon/`) runs these jobs for an admin, and `docs/GUIDE.md` walks through it, then the same with `curl`; what follows is for changing them.

| Job | Does | Key params | Then serve it as task |
|---|---|---|---|
| `autogluon-tabular` | trains a `TabularPredictor` | `train_file`, `label`, `name`, `presets`, `time_limit`, `test_file` or `test_share`, `ignored_columns`, `hyperparameters` | `tabular-prediction` |
| `autogluon-timeseries` | trains a `TimeSeriesPredictor` (incl. Chronos) | `train_file` (long format), `prediction_length`, `target`, `timestamp_column`, `id_column` (unset: one series), `name`, `known_covariates`, `presets` | `timeseries-forecast` |
| `autogluon-predict` | predicts a file with a trained predictor | `predictor` (the training job's ID), `data_file`, `known_covariates_file` | (writes `predictions.csv` or `forecast.csv`) |

Tables can be CSV (its delimiter guessed), TSV, Parquet or JSON Lines. Each training job writes the predictor to `predictor/`, `leaderboard.csv` and `report.json`, and its result has the `model_id` to serve it with, `autogluon:<job id>`, and whether its metric is an error (`lower_is_better`: AutoGluon reports RMSE, log loss or WQL negated, so every score is higher-is-better). The result stays small, since it is kept on the job's row and posted to the webhooks; `report.json` holds what the pages draw:

- tabular: each input column's type, a training row, and, on rows held out from training (`test_share`, 20% by default, or the `test_file`, which also give `test_score`), each column's importance (`feature_importance.csv` too) and, for classification, the classification report in the Evals section's shape;
- time series: up to 20 series' recent history with the forecast of their next steps (mean and the 10 to 90% band), while `forecast.csv` has every series. A forecaster with known covariates skips it, since a forecast needs their future values: the `autogluon-predict` job takes them. A failure here is noted in the report and never fails the job. The score is AutoGluon's validation score: on the last steps of each series, which the models were not fitted on, though the best model was chosen on them. Every preset uses small pretrained models (Chronos-2, Toto-2) beside the statistical ones, which the first training downloads from the Hugging Face Hub.

```bash
curl -X POST localhost:8000/inference -H "X-API-Key: $KEY" -H 'content-type: application/json' -d '{
  "model_id": "autogluon:2f3c154d490e4f6e", "task": "tabular-prediction",
  "input": {"rows": [{"age": 30, "income": 41.5, "city": "oslo"}], "probabilities": true}}'
```

`tests/test_autogluon_real.py` runs these jobs and the predictor process for real, when `AUTOGLUON_PYTHON` names the environment's interpreter (`AUTOGLUON_PYTHON=envs/autogluon/bin/python uv run pytest tests/test_autogluon_real.py`); otherwise it is skipped.

A served predictor is a process in the `autogluon` environment (`jobs/autogluon/_serve.py`) that `models/families/autogluon/predictor.py` starts on first use. The model cache treats it like any other model: it counts its size on disk against `MAX_MEMORY_GB`, evicts it when idle, and starts it again if it crashes. Trained predictors are always allowed, whatever `ALLOWED_MODELS` says, since only an admin can train one. Deleting the job (`DELETE /jobs/{id}`) deletes the predictor and unloads it.

Resources: AutoGluon would take every core. The jobs pass `ctx.num_cpus` to `fit()` (and cap thread pools through `OMP_NUM_THREADS`), and with `JOB_DEVICE=cpu` keep off CUDA and Apple GPUs. `extreme_quality` is much faster with a GPU: set `JOB_DEVICE=auto` (or `mps`) to let it use one. A job in its own process does not take turns with the service's models on an Apple GPU.

## Text classifiers

`jobs/train/text_classifier.py` (the `text-classifier` job) fine-tunes a small encoder to classify texts with Hugging Face's Trainer, in the service's own environment (torch, transformers and accelerate are already there). Its `train_file` is JSON Lines or CSV with a text and a label per row (`text_column`, `label_column`); an eval dataset writes its labeled examples as one (the Train button of a dataset, or `POST /admin/datasets/{id}/training-file`, which answers the path). Of each label's examples `validation_share` is held out: the model is scored on them after every epoch, the best epoch is kept, and the result has its accuracy and macro F1 with each epoch's.

The default `base_model` is `answerdotai/ModernBERT-base`; `jhu-clsp/mmBERT-small` reads many languages, and `google/electra-small-discriminator` (14M parameters) trains in seconds on a CPU. With `JOB_DEVICE=cpu` it stays off Apple and CUDA GPUs; `auto` or `mps` lets the Trainer use one.

The model is saved to `model/` (reading as many tokens as it was trained on, `max_length`), and served as `trained:<job id>` for `text-classification` by `models/families/trained/classifier.py`: the generic classifier loaded from that folder, in the service's process, so it batches like any Hub classifier. Like a predictor, it is allowed whatever `ALLOWED_MODELS` says, and deleting the job deletes it.

Both kinds are found by `models/trained.py`, from the job's `result.json` (written only when the job succeeds: its `model_id` and `task`) and `params.json` (a `name`, if given). The model lists (`/models/list`, `/v1/models`, the model browser) show each under its task, with that name, also when `ALLOWED_MODELS` is set. A trainer and the family that serves its models each keep their own copy of the model ID's prefix, the task and the folder, since neither may import the other: `tests/test_trained_models.py` fails when they differ, so a new trainer adds its pair there.

## Schedules and webhooks

A schedule (`POST /jobs/schedules`, or the dashboard's Jobs section) submits a job again and again: on a cron expression (`0 3 * * *`, server local time) or every `interval_s` seconds. `tasks/schedule.py` parses the expression and computes the next run; its `Scheduler.tick()` submits the schedules that are due, every 15 seconds, and skips a schedule whose previous job is still queued or running. A run missed while the service was down is made once when it is back; a cron run more than an hour late (`CRON_DEADLINE_S`) is skipped until its next time. Jobs a schedule submitted carry its `schedule_id`.

When a job finishes (succeeded, failed or cancelled) it is POSTed to `WEBHOOK_URLS` and to the `webhook_url` given when it was submitted or on its schedule, as `{"event": "job.finished", "time": ..., "data": <the job as GET /jobs/{id} shows it>}`; eval runs arrive the same way as `run.finished`. With `WEBHOOK_SECRET`, `X-Toolkit-Signature` is `sha256=<HMAC-SHA256 of the body>`. See `tasks/webhooks.py`.
