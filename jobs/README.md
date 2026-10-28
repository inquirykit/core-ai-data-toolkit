# Jobs

A job is a long-running script: training, a batch over a file, an evaluation. Where a model answers one request in seconds, a job is submitted, runs in the background for minutes or hours, reports progress, can be cancelled, and leaves files behind.

To add a job, add one file to a subfolder of `jobs/`. Nothing else needs editing: the service finds it when it first needs a job, and it appears in `GET /jobs/types`.

## Adding a job

```python
# jobs/batch/word_count.py
from pydantic import Field

from jobs import DataFile, Job, JobContext, JobParams, JobResult, register_job


class Params(JobParams):
    input_file: DataFile                       # a file in JOB_DATA_DIR; run() gets its absolute path
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
| `run(ctx, params)` | Plain blocking code. It runs in its own process, so it may take hours, use every core it is given, and crash without harming the service. |
| `@register_job(name, env=..., description=...)` | `env` is the environment it runs in (below). The description defaults to the docstring's first line. |

What `ctx` offers:

| | |
|---|---|
| `ctx.path(*parts)` | A path in the job's own directory (`JOBS_DIR/<id>/`). Write outputs there. |
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

A job whose environment is missing is refused when submitted, with the command that sets it up. `GET /jobs/types` says which environments are ready.

The service imports every job file to read its name and params, so **import the job's libraries inside `run()`**, not at the top of the file; a job file may import only pydantic and `jobs` at the top. Files and folders whose names start with `_` are not registered (helpers such as `jobs/autogluon/_common.py`), but jobs can import them.

## How a job runs

1. `POST /jobs` checks the params, writes them to `JOBS_DIR/<id>/params.json`, records the job as `queued` and returns its ID (202).
2. Up to `MAX_RUNNING_JOBS` jobs run at once (default 1); the rest wait, the most urgent first (`"priority": "high"`, `"normal"` (the default) or `"low"` in `POST /jobs`, or on a schedule), then the oldest. Jobs are one kind of task in the service's one queue (`jobs/tasks.py`); eval runs are another, with a limit of their own, so a long training job does not hold up an eval run.
3. A job runs as `python -m jobs.worker` in its environment's interpreter, in a new process group, with the project root on `PYTHONPATH`. Its stdout carries progress and the result to the service; whatever it prints is moved to stderr, which becomes `job.log`.
4. It ends `succeeded` (with `result`, also in `result.json`), `failed` (with `error`: the exception, or how the process died, such as a SIGKILL from running out of memory) or `cancelled`.

Cancelling a running job sends SIGTERM to its whole process group (then SIGKILL after 10 seconds), so processes it started, such as Ray workers, stop too. A job also ends itself if the service that started it goes away. When the service restarts, queued jobs are still queued (the table is the queue) and jobs it was running are marked failed (interrupted). The job table is in the ArcadeDB database, which only one process can open, so run the service with `WORKERS=1` to use jobs.

## AutoGluon

`jobs/autogluon/` trains [AutoGluon](https://auto.gluon.ai) predictors in the `autogluon` environment (all of AutoGluon 1.6: tabular with every model family, multimodal and time series):

| Job | Trains | Key params | Then serve it as task |
|---|---|---|---|
| `autogluon-tabular` | `TabularPredictor` | `train_file`, `label`, `presets`, `time_limit`, `test_file`, `ignored_columns`, `hyperparameters` | `tabular-prediction` |
| `autogluon-multimodal` | `MultiModalPredictor` (text, images and tabular columns) | `train_file`, `label`, `image_columns`, `presets`, `time_limit` | `multimodal-prediction` |
| `autogluon-timeseries` | `TimeSeriesPredictor` (incl. Chronos) | `train_file` (long format), `prediction_length`, `target`, `id_column`, `timestamp_column`, `known_covariates`, `presets` | `timeseries-forecast` |

Tables can be CSV, TSV, Parquet, JSON Lines or Excel. Each job writes the predictor to `predictor/`, `leaderboard.csv` and `predictor.json`, and its result has the `model_id` to serve it with, `autogluon:<job id>`:

```bash
curl -X POST localhost:8000/inference -H "X-API-Key: $KEY" -H 'content-type: application/json' -d '{
  "model_id": "autogluon:2f3c154d490e4f6e", "task": "tabular-prediction",
  "input": {"rows": [{"age": 30, "income": 41.5, "city": "oslo"}], "probabilities": true}}'
```

A served predictor is a process in the `autogluon` environment (`jobs/autogluon/_serve.py`) that `models/autogluon/predictor.py` starts on first use. The model cache treats it like any other model: it counts its size on disk against `MAX_MEMORY_GB`, evicts it when idle, and starts it again if it crashes. Trained predictors are always allowed, whatever `ALLOWED_MODELS` says, since only an admin can train one. Deleting the job (`DELETE /jobs/{id}`) deletes the predictor.

Resources: AutoGluon would take every core and, for multimodal, the GPU. The jobs pass `ctx.num_cpus` to `fit()` (and cap thread pools through `OMP_NUM_THREADS`), and with `JOB_DEVICE=cpu` keep off CUDA and Apple GPUs. `extreme_quality` and the multimodal presets are much faster with a GPU: set `JOB_DEVICE=auto` (or `mps`) to let them use one. A job in its own process does not take turns with the service's models on an Apple GPU.

## Schedules and webhooks

A schedule (`POST /jobs/schedules`, or the dashboard's Jobs section) submits a job again and again: on a cron expression (`0 3 * * *`, server local time) or every `interval_s` seconds. `jobs/schedule.py` parses the expression and computes the next run; its `Scheduler.tick()` submits the schedules that are due, every 15 seconds, and skips a schedule whose previous job is still queued or running. A run missed while the service was down is made once when it is back; a cron run more than an hour late (`CRON_DEADLINE_S`) is skipped until its next time. Jobs a schedule submitted carry its `schedule_id`.

When a job finishes (succeeded, failed or cancelled) it is POSTed to `WEBHOOK_URLS` and to the `webhook_url` given when it was submitted or on its schedule, as `{"event": "job.finished", "time": ..., "data": <the job as GET /jobs/{id} shows it>}`; eval runs arrive the same way as `run.finished`. With `WEBHOOK_SECRET`, `X-Toolkit-Signature` is `sha256=<HMAC-SHA256 of the body>`. See `jobs/webhooks.py`.
