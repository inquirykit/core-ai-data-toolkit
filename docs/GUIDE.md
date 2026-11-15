# Core AI & Data toolkit: guide

How to use the service, behind the [README](../README.md): a section per
feature, with worked examples. Every route, with its parameters, body and
answer, is in `/docs` (and `/openapi.json`); every setting with its default is
in [env.example](../env.example); how the code is organised is in
[STRUCTURE.md](../STRUCTURE.md).

- [Setup details](#setup-details)
- [Using the API](#using-the-api)
- [Configuration](#configuration)
- [Example usage](#example-usage)
- [Images and audio in chat](#images-and-audio-in-chat)
- [Tools and JSON replies](#tools-and-json-replies)
- [Completions, log probabilities and token counts](#completions-log-probabilities-and-token-counts)
- [Embeddings, reranking, transcription and model status](#embeddings-reranking-transcription-and-model-status)
- [Tables: workspaces of JSON rows](#tables-workspaces-of-json-rows)
- [Enrichers: look values up in outside services](#enrichers-look-values-up-in-outside-services)
- [Traces and evals: debug requests, benchmark models](#traces-and-evals-debug-requests-benchmark-models)
- [Model servers](#model-servers)
- [Model memory](#model-memory)
- [Performance tips](#performance-tips)
- [Troubleshooting](#troubleshooting)

## Setup details

### Container (Docker or Podman)

```bash
make up                              # build the image and start it; same as: docker compose up --build -d
make up COMPOSE="podman compose"     # Podman
make logs                            # follow the log for the setup link
make down                            # stop; model weights and data stay in named volumes
```

Model weights live in one named volume and everything else the service keeps
in another, `data` (its `DATA_DIR`: the wizard's settings, request history,
`/db` tables, the buckets' files, jobs and evidence captures with their
signing key), so they survive `down`/`up` and image rebuilds. On an
NVIDIA host, uncomment the `deploy:` block in `docker-compose.yml` (it needs the
NVIDIA Container Toolkit). A third volume, `envs`, holds the job environments
(AutoGluon's, several GB) that the Set up button installs from `/admin`.

Without compose, mount the weights and the data so they persist:

```bash
docker build -t core-ai-data-toolkit .      # podman build -t core-ai-data-toolkit .
docker run -d -p 8000:8000 --name core-ai-data-toolkit \
  -v core-ai-data-toolkit-hf-cache:/app/.cache/huggingface \
  -v core-ai-data-toolkit-data:/app/data \
  core-ai-data-toolkit
```

`./podman-run.sh {build|run|stop|restart|logs|clean|shell}` does the same for
Podman.

### Local

Needs [uv](https://docs.astral.sh/uv/) and, for the browser pages, Node 22.12+.

```bash
make install    # uv sync (Python deps from uv.lock into .venv) + build the pages into api/static
make run        # python main.py; then open the setup link it logs
make dev        # the same with auto-reload
make test       # unit tests, Python and frontend (no model downloads, no torch needed)
make lint       # ruff (unused imports and variables, undefined names), pyright on api/app.py, and the frontend's tsc
```

Without make or uv: `pip install -r requirements.txt` (the pinned export of
`uv.lock`), `(cd frontend && npm ci && npm run build)`, `python main.py`.
The API works without the page build; the pages then explain how to build them.

Optional extras:

```bash
uv sync --extra vega                 # vegaml, for POST /v1/decisions
scripts/setup_job_env.sh autogluon   # the AutoGluon jobs' own environment (Python 3.13)
make lock                            # after editing pyproject.toml: refresh uv.lock and requirements.txt
```

### Frontend

The pages are a React + TypeScript app in `frontend/`, built with Vite into
`api/static` (not committed). The UI is Tailwind CSS v4 with
[shadcn/ui](https://ui.shadcn.com) components (Radix primitives, copied into
`src/components/ui/`), lucide icons and `motion` for animations. The theme
(light, dark, or the system's) is chosen in the sidebar and kept per browser.

```bash
cd frontend
npm run dev        # http://localhost:5173/admin and /admin/setup with hot reload;
                   # API calls go to the server on :8000 (AI_TOOLKIT_URL to change it)
npm test           # unit tests
npm run test:e2e   # build, then open every page, section and tab in a browser (npx playwright install chromium once)
npm run build      # typecheck, then build into api/static
npm run gen:api    # after an API change: refresh openapi.json and the generated types
```

Every API call in the pages is typed from the API's OpenAPI schema
(`src/shared/api-schema.d.ts`, generated from `frontend/openapi.json`): the
client takes a route's path as the schema writes it (`"/jobs/{id}"`) and gets
its parameters, body and reply types from there. The pytest suite fails when
that file is out of date, so an API change cannot silently break the pages.

Useful environment variables (see `env.example`): `DEFAULT_DEVICE` (auto/cpu/cuda/mps),
`MAX_MODELS` and `MAX_MEMORY_GB` (see [Model memory](#model-memory)), `RELOAD` and
`DEBUG` (development only), `MAX_GENERATION_LENGTH`, and `IMAGE_DIR`, the only
directory `/vision-language` will read images from.

### Podman without compose

`podman compose` reads the same `docker-compose.yml` (`make up COMPOSE="podman compose"`).
The `podman-run.sh` script runs the container without compose:

```bash
# Build the container image
./podman-run.sh build

# Run the container
./podman-run.sh run

# Stop the container
./podman-run.sh stop

# Restart the container
./podman-run.sh restart

# View logs
./podman-run.sh logs

# Open shell in container
./podman-run.sh shell

# Clean up (remove container and image)
./podman-run.sh clean
```

## Using the API

The dashboard calls the same routes a client does, and `/docs` lists them all,
each with the key it takes. These show how they go together.

### Run a model
```bash
POST /inference
Content-Type: application/json

{
  "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
  "task": "text-generation",
  "input": {"text": "Hello, how are you?", "max_length": 100, "temperature": 0.7}
}
```

```json
{
  "task": "text-generation",
  "result": {"text": " I'm fine, thanks."},
  "model_used": "Qwen/Qwen2.5-0.5B-Instruct:text-generation",
  "processing_time": 0.41,
  "cold_start": false,
  "revision": "989aa7980e4cf806f80c7fef2b1adb7bc71aa306"
}
```

`revision` is the Hub commit of the weights that answered, read from the Hub
cache when the model loaded (null for a local folder or a model server's
model); `GET /models/status` shows it for each loaded model too.

Each task has a fixed contract: what `input` takes and what `result` holds,
whichever model serves it. A field the task does not take is rejected with 422.
`GET /tasks` returns every contract as JSON Schema, `/docs` shows each task's
request and response, and [STRUCTURE.md's table](../STRUCTURE.md#tasks) lists
the tasks with their built-in models.

`max_length` is the number of new tokens to generate; the prompt does not count.
Left out, the model's own default applies. Prompts longer than the model's
context window are trimmed from the start.

### Models in memory

A model loads on its first request, or ahead of it with the admin key:

```bash
curl -X POST localhost:8000/models/load -H "X-API-Key: $ADMIN_API_KEY" -H 'content-type: application/json' \
  -d '{"model_id": "AnkitAI/Sensible-ModernBERT-Sentiment-Analysis", "task": "text-classification", "pin": true}'
curl localhost:8000/models/status -H "X-API-Key: $ADMIN_API_KEY"   # loaded models, memory, each one's line
curl -X DELETE localhost:8000/models/AnkitAI/Sensible-ModernBERT-Sentiment-Analysis/text-classification \
  -H "X-API-Key: $ADMIN_API_KEY"
```

A pinned model is never evicted by the cache, the idle timeout or a lower
limit; only unloading it (or `DELETE /models/clear`) removes it, and
`PINNED_MODELS` pins at startup. Besides the loaded models and memory use,
`GET /models/status` gives `loads_pending` and, per model, `in_use`, `queued`
(requests waiting for a slot), `avg_request_ms` and `estimated_wait_ms` (what
the next request would wait), so a client can pick the model with the shortest
line.

`GET /models/catalog` lists each task's models with their description, tags,
status and whether they are on disk, as the Models section of `/admin` and the
*Browse* buttons of its playground show them. Describe your own models with a
`ModelCard` (see [models/README.md](../models/README.md)), or with
`description` and `tags` in `MODEL_SERVERS_FILE`.

### Jobs

Jobs need the admin key. See [jobs/README.md](../jobs/README.md) to add a job. A
job whose environment is not installed yet is refused; the Set up button in
`/admin` installs it, locally and in a container alike, by queueing
`{"job": "setup-env", "params": {"name": "autogluon"}}`.

#### AutoGluon: from a table to a served predictor

In `/admin`, the AutoGluon section does it in a few clicks:

1. The first time, **Set up** installs AutoGluon's environment (about 1.8 GB,
   a few minutes; its log shows as it goes).
2. **New predictor**: choose *Predict a column* or *Forecast a series*, and
   drop in a CSV (any delimiter), TSV, Parquet or JSON Lines file. Its summary
   shows each column's type, missing share and distinct values.
3. Pick the column to predict. Columns with a value per row (IDs) start ticked
   to be ignored. For a series, pick the timestamp, the value, the series ID
   column if the file holds several, and how many steps ahead.
4. Give it a name, a time limit and a quality (Fast, Good or Best), then
   **Train**. Its page shows the progress, then how good it is: the score on
   a fifth of the rows held back from training (an error such as RMSE shown
   positive, "lower is better"), the models it tried, which columns matter,
   and, for classification, each class's precision and recall; for a series,
   its forecast.
5. Try one row on its page, or predict a whole file (a job, to download as
   CSV). Its model ID, `autogluon:<job id>`, works in eval runs, workflows and
   `POST /inference`, and it is listed in `/models/list`.

The same through the API:

```bash
KEY=your-admin-key
# Install the environment once; then upload the table
curl -X POST localhost:8000/jobs -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"job": "setup-env", "params": {"name": "autogluon"}}'
curl -X POST localhost:8000/jobs/data -H "X-API-Key: $KEY" -F file=@churn.csv
curl localhost:8000/jobs/data/churn.csv/summary -H "X-API-Key: $KEY"   # rows, columns, first rows

# Train: 20% of the rows are held back to score on (test_share)
curl -X POST localhost:8000/jobs -H "X-API-Key: $KEY" -H 'content-type: application/json' -d '{
  "job": "autogluon-tabular",
  "params": {"train_file": "churn.csv", "label": "churn", "name": "Churn", "time_limit": 300,
             "presets": "medium_quality", "ignored_columns": ["customer_id"]}}'
# -> 202 {"id": "349f1ef7417244b2", "status": "queued", ...}

# Its result once it succeeded: the model ID, the scores, the leaderboard
curl localhost:8000/jobs/349f1ef7417244b2 -H "X-API-Key: $KEY"
# -> {"status": "succeeded", "result": {"model_id": "autogluon:349f1ef7417244b2", "task": "tabular-prediction",
#     "eval_metric": "accuracy", "test_score": 0.975, "test_rows": 80, "lower_is_better": false, ...}}
curl localhost:8000/jobs/349f1ef7417244b2/files/report.json -H "X-API-Key: $KEY"   # importance, report

# Predict rows (a client key works too), or a whole file as a job
curl -X POST localhost:8000/inference -H "X-API-Key: $KEY" -H 'content-type: application/json' -d '{
  "model_id": "autogluon:349f1ef7417244b2", "task": "tabular-prediction",
  "input": {"rows": [{"age": 70, "income": 41.2, "city": "oslo"}], "probabilities": true}}'
# -> {"result": {"predictions": ["no"], "probabilities": [{"no": 0.99, "yes": 0.01}]}, ...}
curl -X POST localhost:8000/jobs -H "X-API-Key: $KEY" -H 'content-type: application/json' -d '{
  "job": "autogluon-predict", "priority": "high",
  "params": {"predictor": "349f1ef7417244b2", "data_file": "new_customers.csv"}}'
# -> its result names predictions.csv: GET /jobs/<that id>/files/predictions.csv
```

A forecaster is trained the same way with `autogluon-timeseries`
(`"train_file": "sales.csv", "timestamp_column": "day", "target": "sold",
"prediction_length": 7`, and `"id_column"` when the file holds several
series). Its job writes the next steps of every series to `forecast.csv`; the
score is a validation score, on the last steps of each series, which the
models were not fitted on. Every quality downloads small pretrained models
(Chronos-2, Toto-2) from the Hugging Face Hub on its first training.

**Queued work and priority.** Jobs, eval runs and judging wait in one queue
(`tasks/runner.py`), each kind with a limit of its own: `MAX_RUNNING_JOBS`
jobs at once, one eval run, two judges, so a long training job never holds up
a run. `POST /jobs`, `POST /jobs/schedules`, `POST /admin/runs` and
`POST /admin/runs/{id}/judge` take a `"priority"` of `"high"`, `"normal"` (the
default) or `"low"`. Each kind starts
the most urgent task first, then one whose model is loaded already, then the
oldest: the runs for the models in memory finish before another model has to
be loaded. An eval run's requests carry its priority, so live requests (high
unless they send `X-Priority`) go first when models compete for memory. Work
queued when the service stops is still queued when it starts again. An eval
run the service stopped under goes back in the queue and goes on where it
stopped (it skips the examples it answered); after a crash it is resumed at
most twice, so a run that brings the service down cannot do it at every start. A job or a judge that was running is
marked failed (interrupted): it would start over from scratch. Its retries are
not used for it, and its webhook (`job.finished`) is sent as it is marked.

**Retries and timeouts.** `POST /jobs` and `POST /jobs/schedules` (for the jobs
the schedule submits) also take:

```bash
POST /jobs  {"job": "fetch-prices", "params": {...}, "retries": 3, "retry_delay_s": 30, "timeout_s": 600}
```

A job that runs past `timeout_s` seconds is stopped, its whole process group
with it, and fails with a "Timed out" error. A job that fails, by a timeout or
otherwise, goes back in the queue until it has been retried `retries` times
(0 to 10; none by default). It waits `retry_delay_s` (default 30) before the
first retry, then twice as long before each next one, up to an hour. While it
waits it is `queued` with its last `error`, `retried` counts the retries so
far, and `not_before` says when the next one starts. The same log goes on, with
a `--- retry 1 of 3 ---` line where each retry starts. Neither a cancel nor
the service stopping under a job is retried, a wait survives a restart, and
webhooks hear only the last outcome.

#### Schedules and webhooks
```bash
POST /jobs/schedules   {"name": "nightly", "job": "autogluon-tabular", "params": {...}, "cron": "0 3 * * *"}
POST /jobs/schedules   {"job": "word-count", "params": {...}, "interval_s": 3600, "webhook_url": "https://example.com/hook"}
```

A schedule runs a job on a five-field cron expression (`minute hour
day-of-month month day-of-week`, server local time; `*/15`, `1-5`, `1,15`
work as in cron) or every `interval_s` seconds (at least 60). The scheduler
checks every 15 seconds, and never overlaps a schedule with itself: a run that
comes due while the previous job is still going is skipped. A run missed while
the service was down is made once when it is back, not once per miss; a cron
run more than an hour late is skipped until its next time, so a nightly job
does not start at noon after a long downtime.
A schedule is changed with `PUT` (its next run is recomputed; `"enabled": false`
pauses it) and run at once with `POST /jobs/schedules/{id}/run`. The dashboard's
**Jobs** section (`/admin#/jobs`) edits schedules, submits jobs, follows their
progress and logs, and tests webhooks (`POST /queue/webhooks/test` sends a test
event to a receiver and reports its answer).

Webhooks: every finished job (`job.finished`) and eval run (`run.finished`)
is POSTed as `{"event", "time", "data"}` to the URLs in `WEBHOOK_URLS` and to
the `webhook_url` the job, schedule or run names itself (`POST /jobs`,
`POST /jobs/schedules` and `POST /admin/runs` take one). `X-Toolkit-Event`
names the event. Each message carries the [Standard Webhooks](https://www.standardwebhooks.com)
headers: `webhook-id` (the same across its retries, so a receiver can drop a
duplicate), `webhook-timestamp` (when that attempt was sent, so each retry is
signed again) and, with `WEBHOOK_SECRET` set, `webhook-signature`
(`v1,<base64 HMAC-SHA256 of "id.timestamp.body">`), which any Standard
Webhooks library verifies given the secret as
`whsec_<base64 of WEBHOOK_SECRET>`; `X-Toolkit-Signature` is still
`sha256=<HMAC-SHA256 of the body>`.

Only a 2xx answer counts as delivered. Each delivery waits in the task queue,
so a restart does not lose it: one not sent yet goes out at the next start,
and one the service stopped under is sent again, with the same `webhook-id`.
A delivery that gets no answer, or any other status, is tried again after 30
seconds, then after twice as long each time up to an hour: ten retries over
about four hours. A receiver that answers `410 Gone` is not asked again. A
delivery that fails for good leaves a warning in the log.

### The queue: background work of every kind

Jobs, eval runs and their judging, webhook deliveries, bucket copies and workflow runs are all tasks on
one queue, each kind with its own limit. `GET /queue` (admin) lists them in one shape, newest first, and the Running
section of `/admin` shows the newest:

```
GET  /queue?kind=webhook&status=failed&limit=20     # {tasks: [...], next}: pass next as cursor for the page after
GET  /queue?status=queued&status=running            # the work not done yet
POST /queue/{id}/cancel                             # cancel a queued task, or stop a running one
```

Each task says its `kind`, what it works on (`ref`: a run's id, a bucket, a workflow), its `status` (queued, running,
succeeded, failed, cancelled), `progress` and `message`, and its retries. A page goes on where the last one stopped,
however many tasks came since. A kind's own routes say more (a job's log, a run's scores); an eval run's own cancel
stops its judging too. A webhook delivery under way is let finish, and a cancelled copy is asked for again at the
bucket's next upload or the hourly check while its files wait.

### Client keys: one per machine
`ADMIN_API_KEY` is the superadmin key: it opens every route, and nothing in
`/admin` can revoke it. Each server or machine that calls the service gets a
client key of its own. A client key opens the client routes (inference,
`/v1`, `/db` rows, evidence…) and never the admin's. You can give it an
expiry and revoke it on its own.

```bash
# Make one (admin key): the reply is the only time the token is shown
curl -X POST localhost:8000/admin/keys -H "X-API-Key: $ADMIN_API_KEY" \
  -H "Content-Type: application/json" -d '{"name": "worker-3", "expires_in_days": 90}'
# -> 201 {"id": "3f2a9c1e", "name": "worker-3", "state": "live", "created_at": 1793750400.0,
#         "expires_at": 1801526400.0, "revoked_at": null, "last_used_at": null,
#         "token": "aitk_3f2a9c1e_Qm9vbGVhbi..."}

# The machine sends it as a Bearer token or X-API-Key
curl localhost:8000/v1/models -H "Authorization: Bearer aitk_3f2a9c1e_Qm9vbGVhbi..."

# The keys, newest first, with when each was last used; never a token
curl "localhost:8000/admin/keys?state=live" -H "X-API-Key: $ADMIN_API_KEY"

# Rotate: make worker-3's next key, move the machine to it, then revoke the old one
curl -X POST localhost:8000/admin/keys/3f2a9c1e/revoke -H "X-API-Key: $ADMIN_API_KEY"
# From its next request on: 401 {"detail": "API key revoked"} ("API key expired" past its expiry)
```

Once the first client key is made, the client routes need a key, even without
`API_KEY`. Revoking every client key does not open them again. So once the
machines have client keys, you can delete `API_KEY` from `.env`, and the only
key left in plain text is the superadmin's. The service keeps only each key's
SHA-256. It reads all of them into memory at startup, so checking a request
never touches the database, and a revoke holds from the very next request.
The audit log names the client key an entry carried (`key_id`). The **API keys** tab of the dashboard's
Settings (`/admin#/keys`) does the same as these calls. Making a key is refused (409)
while `ADMIN_API_KEY` is not set.

#### What a key may reach: grants
A key made without grants reaches every table and model a client key does. Its `grants` narrow that: `tables`
maps `workspace.table`, `workspace.*` or `*.*` to the actions it may take there (`read`, `write`, `delete`), `models`
lists the only models it may run, and `manage_models` lets it load, unload and pin those models. A field left out
(null) leaves that alone.

```bash
# An indexer: reads and writes the docs workspace, deletes only in docs.chunks,
# reads the default workspace's evidence, runs one embedding model
curl -X POST localhost:8000/admin/keys -H "X-API-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" -d '{
  "name": "indexer",
  "grants": {
    "tables": {"docs.*": ["read", "write"], "docs.chunks": ["delete"], "default._evidence": ["read"]},
    "models": ["BAAI/bge-small-en-v1.5"]
  }}'

# Change them later, under the same token: from the key's next request on
curl -X PATCH localhost:8000/admin/keys/3f2a9c1e -H "X-API-Key: $ADMIN_API_KEY" -H "Content-Type: application/json" \
  -d '{"grants": {"tables": {"docs.*": ["read"]}, "manage_models": true}}'

# What it may not do: 403 {"detail": "This key may not delete docs.notes"}
curl -X DELETE localhost:8000/db/docs/notes/n1 -H "Authorization: Bearer aitk_3f2a9c1e_..."
```

Entries add up: a key may do what any entry naming the table (`docs.chunks`), its workspace (`docs.*`) or every
workspace (`*.*`) allows, and a table no entry names is shut. A request on one table (its rows, a bucket's files)
needs the action there. One that reaches past one table needs it on the workspace's own entry (`docs.*` or `*.*`):
listing the workspace's tables, a transaction, edges and the graph, a row's walks, a view, `_expand`. Listing every
workspace (`GET /db`) needs `*.*`. A workspace's evidence is `workspace._evidence`. `PATCH` on a row answers the
whole row, so it takes `read` as well as `write`. A delete that cascades follows the table's schema, which the admin
chose. The service's own workspaces still need the admin key.

`models` holds every model the key's own requests run, the embedding of a table's vector field among them: a key
whose list leaves out a table's embedding model gets 403 writing rows there or searching it by text. A workflow the
key invokes runs the models the admin chose for it.

### Audit log
```bash
GET /admin/audit                                         # the newest 100 changes, newest first
GET /admin/audit?method=DELETE&outcome=refused&actor=wrong&route=/db&since=<unix time>&cursor=<the last page's next>
```

Every change made through a route that needs the admin key or through the
setup wizard (`POST`, `PUT`, `PATCH`, `DELETE`), and every `DELETE` whatever
key it takes, goes in the audit log, refused attempts too: a `DELETE` with a
wrong key is there with its `401`. An entry holds the method, the route
(`/db/{workspace}/{table}`) and the path asked for with its query, the
status, how long it took, the key it carried (`actor`: `admin`, `client`,
`none`, `wrong` for a key that opens nothing, or `open` while no key is set;
`key_id` names a client key, live or not), the client's address,
`X-Forwarded-For` and the User-Agent. Request bodies are never kept, so no
key an admin saves lands in it. The admin POSTs that only read or try
something are left out: `/db/{ws}/_query`, the model server probe, an
enricher's test and the webhook test (marked `@audit.reads` where each is
declared, `api/audit.py`). Entries are
kept `AUDIT_RETENTION_DAYS` (default 365; 0 keeps none) in the
database. The **Audit log** tab of the dashboard's Settings (`/admin#/audit`) lists them
with the same filters. It needs the admin key.

### Metrics
```bash
GET /metrics            # Prometheus text format; the client or admin key as the scrape's bearer token
```

Counters since the process started (`ai_toolkit_requests_total` by model, task
and status, `ai_toolkit_cold_starts_total`, `ai_toolkit_tokens_total` by
prompt and completion, `ai_toolkit_model_events_total`), a latency histogram
per task (`ai_toolkit_request_duration_seconds`), and gauges read when
scraped: loaded models and their budgeted memory, requests in flight and
queued, the budget and the device's free memory, process and host memory,
GPU memory. The dashboard's own history stays in the database.

### Evidence capture

A browser extension stores each page it captures with `POST /evidence/captures`
(its body is below): a signed record in a hash chain, with the page's files. The
routes under `/evidence` list a workspace's cases and captures, annotate and
search them, keep each case's to-do list, and verify the chain.

Evidence is kept by workspace, the unit that is isolated, verified and exported, and its workspaces are the
[`/db` workspaces](#tables-workspaces-of-json-rows): each holds its own cases, its own hash chain and its own files,
in five tables of its own (`evidence_cases`, `evidence_captures`, `evidence_annotations`, `evidence_todos`,
`evidence_search`). Those are kept tables: `/db` shows them with the admin key, read-only, and a workspace that holds
them cannot be dropped. They are made on the workspace's first capture, case or to-do list; until then the
workspace reads as an empty ledger with only the inbox, so looking at a workspace from the Evidence section leaves it
as it was. Since that first write makes a workspace one that cannot be dropped, in any workspace but the default one
it takes the admin key: a client key (an application's) writes evidence only in the default workspace and in
workspaces that hold their ledger already (`403` elsewhere). The routes about cases, captures and verifying take
`?workspace=`, the workspace's name; without it they act in the default workspace (so the browser extension, which
names none, captures there). Make a workspace as any other, with a label saying what it holds, open it to evidence
with a first case made with the admin key (as the Evidence section's are), and capture into it:

```bash
curl -X POST localhost:8000/db -H 'Content-Type: application/json' -d '{"name": "fraud_ring", "label": "Fraud ring"}'
curl -X POST 'localhost:8000/evidence/cases?workspace=fraud_ring' -H 'Content-Type: application/json' -d '{"name": "Case 42"}'
curl -X POST 'localhost:8000/evidence/captures?workspace=fraud_ring' -F metadata=@metadata.json -F file=@screenshot.png
curl 'localhost:8000/evidence/verify?workspace=fraud_ring'    # {"workspace": "fraud_ring", "ok": true, ...}
```

Each record names its workspace by that name, which is also the folder of its files: `DATA_DIR/evidence` for the
default workspace, `DATA_DIR/evidence/workspaces/fraud_ring/` for `fraud_ring`.

Annotations are tags and analyst notes added to a capture after it was taken. They are mutable and kept apart from the signed records, in the workspace's `evidence_annotations` table: nothing about them enters the record, its hash, the chain or `/evidence/verify`. `PUT` replaces the lists it names; tags are trimmed, their inner whitespace collapsed and repeats dropped case-insensitively (at most 64, 1–64 characters each); notes keep their `id` and `createdAt` when the body names a known id, get `updatedAt` when their text changed, and are new otherwise (at most 200, each up to 20000 characters). The reply, and each item of the `GET`, is `{captureId, tags, notes: [{id, text, createdAt, updatedAt}], updatedAt}`.

Search finds a workspace's captures across its cases, the way an investigator looks for every page they saw something on. `GET /evidence/search` takes `q`, words the captures must hold in their pages' text (the visible text of their HTML and MHTML snapshots and plain text files, read when they were captured), address, title, tags or notes. As in the tables' full-text search, every word must be there, a `"quoted phrase"` as written, `-word` must not, `word*` finds words starting so, and a domain such as `site.example.com` is one word. It also takes `case` (one case only), `tag` (as written), `since` and `until` (UTC times as ISO 8601 prefixes: `until=2026-10` takes all of October), `sort` (`relevance`, the default with `q`, `newest`, the default without, or `oldest`), `limit` (up to 200, default 50) and `cursor` (the previous page's `next`). Without `q` it lists the captures newest first: the workspace's timeline. Each result is a capture summary with its `caseId`, its `tags` and, for a search, a `snippet` of the text around the first word found. The words live in the workspace's `evidence_search` table, a row per capture written with it and again whenever its annotations change, derived and never part of a record. The dashboard's Evidence section has the search box above the cases.

Todos are a case's to-do list, kept the same way in the workspace's `evidence_todos` table, one row per case. `PUT` replaces the list, in the order given; a todo keeps its `id` and `createdAt` when the body names a known id, gets `updatedAt` when its text changed and `doneAt` when it was checked (cleared when unchecked), and is new otherwise (at most 500, each up to 2000 characters; `done` is false when left out). The reply, and the `GET`, is `{caseId, todos: [{id, text, done, createdAt, updatedAt, doneAt}], updatedAt}`, with `updatedAt` null for a case whose list was never saved.

The `metadata` field is the extension's JSON: `caseId`, `artifacts` (each `{name, sha256, size?, mediaType?}`, hashed in the browser before upload) and whatever else it recorded (`page`, `network`, a note); one `file` field per artifact, named after it. The server re-hashes every file and refuses a mismatch, then builds the record (`evidence-capture/record/v3`; `v2` before workspaces): its `workspaceId`, the verified artifacts, the client metadata verbatim, the enrichers' findings about the page's server address and hosts, the receive time and the previous record's hash in the workspace's chain. Its RFC 8785 canonical bytes are hashed, Ed25519-signed and kept verbatim, in the database and as `DATA_DIR/evidence/captures/<id>/record.json` (`DATA_DIR/evidence/workspaces/<workspace>/captures/<id>/` in another workspace), with `record.sig`, `record.tsr` and the read-only `artifacts/`. Refusals come back as `{"detail": "...", "error": "..."}` (the `error` key is this API's; a body that does not validate is FastAPI's 422 `{"detail": [...]}`). A request with an `Origin` header must come from a browser extension (or one of `EVIDENCE_ALLOWED_ORIGINS`), so web pages cannot use these routes even with the key.

## Configuration

Settings are environment variables, read from the `.env` file too (a non-empty environment variable wins).
[`env.example`](../env.example) lists every one with its default and what it does; copy it to `.env`, or let the setup
wizard at `/admin/setup` write the keys. `GET /admin/config` (admin key) shows the values in effect, with keys reduced to
whether they are set, and the dashboard's Settings section shows the same with the warnings.

### Backups

Everything the service keeps is in one folder, `DATA_DIR` (`data/` by default, the `data` volume in the containers):
the database `toolkit.duckdb`, the buckets' files, the jobs' folders and inputs, what `/admin` saves (enrichers,
model servers, the API keys typed into it, encrypted) and the evidence captures with their signing key. To back it up,
stop the service and copy the folder; to restore, copy it back before the service starts. A copy taken while the
service runs may catch the database between writes. Three things live outside it and go with a backup: the settings
file (`.env`, or `ENV_FILE`), `model_servers.toml` (`MODEL_SERVERS_FILE`), and the master key that opens the saved API
keys (`SECRETS_MASTER_KEY`, or the operating system's credential store on a desktop).

## Example usage

These leave the key out. With `API_KEY` set or a client key made, pass
`headers={"Authorization": "Bearer <key>"}` too.

### Text Generation
```python
import requests

response = requests.post("http://localhost:8000/inference", json={
    "model_id": "Qwen/Qwen2.5-0.5B-Instruct",
    "task": "text-generation",
    "input": {"text": "The future of AI is", "max_length": 50, "temperature": 0.8},
})

print(response.json()["result"]["text"])
```

### Text Classification
```python
response = requests.post("http://localhost:8000/inference", json={
    "model_id": "AnkitAI/Sensible-ModernBERT-Sentiment-Analysis",
    "task": "text-classification",
    "input": {"text": "I love this movie!"},
})

print(response.json()["result"]["label"])
```

With labels of your own, no training needed (a zero-shot NLI model reads each label against the text;
`"multi_label": true` scores each label on its own):

```python
response = requests.post("http://localhost:8000/inference", json={
    "model_id": "MoritzLaurer/deberta-v3-base-zeroshot-v2.0",
    "task": "zero-shot-classification",
    "input": {"text": "The striker scored twice in the second half.", "labels": ["sports", "politics", "science"]},
})

print(response.json()["result"]["scores"])
```

### Question Answering
```python
response = requests.post("http://localhost:8000/inference", json={
    "model_id": "deepset/tinyroberta-squad2",
    "task": "question-answering",
    "input": {"question": "What is the capital of France?", "context": "The capital of France is Paris."},
})

print(response.json()["result"]["answer"])
```

## Images and audio in chat

`/v1/chat/completions` takes OpenAI's content parts. Images must be inline
base64 data URLs (PNG, JPEG, WebP or GIF); the server never fetches a URL a
client names. Audio is `input_audio` (base64 WAV or MP3).

```bash
IMG=$(base64 < cat.png | tr -d '\n')
curl localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "Qwen/Qwen3-VL-2B-Instruct",
  "messages": [{"role": "user", "content": [
    {"type": "text", "text": "What is in this picture?"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,'"$IMG"'"}}
  ]}]
}'
```

A chat goes to the `vision-language` task when its model is registered for
it, or when it holds images or audio and no model file claims the model (the
generic `ImageTextToTextModel` then serves any Transformers vision-language
model from the Hub). A model that only does text generation answers 400.

## Tools and JSON replies

`/v1/chat/completions` takes OpenAI's `tools` (function definitions),
`tool_choice` (`none`, `auto`, `required` or a named function) and
`response_format` (`json_object`, or `json_schema` with a schema). A reply
that calls a function ends with `finish_reason: "tool_calls"` and the calls
in `message.tool_calls`; send the result back as a `tool` message, as with
OpenAI. A JSON reply is checked (and, with a schema, validated) before it is
returned.

```bash
curl localhost:8000/v1/chat/completions -H 'Content-Type: application/json' -d '{
  "model": "Qwen/Qwen2.5-0.5B-Instruct",
  "messages": [{"role": "user", "content": "What is the weather in Oslo?"}],
  "tools": [{"type": "function", "function": {
    "name": "get_weather", "description": "Current weather in a city",
    "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}
  }}]
}'
```

How it works: a model whose chat template takes tools (Qwen, Granite, Llama
3, Mistral, SmolLM, ...) gets them in that format and answers in the format
it was trained on; any other model gets the functions, or the schema, as an
instruction in the system prompt. The reply is then parsed for calls in the
common formats (`<tool_call>`, Granite's `<|tool_call|>`, Mistral's
`[TOOL_CALLS]`, or a bare JSON call) or for the JSON value. A model server
(vLLM, llama.cpp) does all of this itself. Decoding is not constrained, so a
model that does not comply gets a `502`: the JSON was missing or did not
match the schema. Small chat models call tools reliably when there are few
of them; `tool_choice: "required"` or a named function helps. Streamed
replies with tools or a response format arrive in one chunk at the end, since
calls and JSON can only be told from the whole reply. These are
text-generation features; vision-language models answer 400.

## Completions, log probabilities and token counts

Every reply from `/v1/chat/completions` carries OpenAI's `usage`
(`prompt_tokens`, `completion_tokens`, `total_tokens`) when the model counts
tokens: in-process models always do, model servers pass on what they report
(a streamed reply gets them in its last chunk, and `stream_options:
{"include_usage": true}` adds OpenAI's closing chunk as well). `finish_reason`
is `length` when the reply used every token `max_tokens` allowed. `stop`
(a string or up to 8) ends the reply before the first match. The playground
shows each reply's token count and tokens per second.

`POST /v1/completions` is the legacy prompt form, which evaluation harnesses
use: `prompt` is a string, a list of strings, or token IDs (one list, or a list
of lists), and each prompt comes back as one choice. `logprobs: n` returns
each generated token's log probability with its `n` most likely alternatives
(`0` for the chosen token alone), and `echo: true` scores the prompt's tokens
too, which is how lm-evaluation-harness measures the likelihood of each
answer to a multiple-choice question (`max_tokens: 0` then generates nothing).
`logprobs` has OpenAI's `tokens`, `token_logprobs`, `top_logprobs` and
`text_offset`. Streaming works for one text prompt without `logprobs` or
`echo`; `n` above 1 and `suffix` are refused.

```bash
# Score the continuation " Paris" after the context, token by token
curl localhost:8000/v1/completions -H 'Content-Type: application/json' -d '{
  "model": "Qwen/Qwen2.5-0.5B-Instruct",
  "prompt": "The capital of France is Paris", "max_tokens": 0, "echo": true, "logprobs": 1}'

# lm-evaluation-harness against this server
lm_eval --model local-completions --tasks hellaswag \
  --model_args model=Qwen/Qwen2.5-0.5B-Instruct,base_url=http://localhost:8000/v1/completions,tokenizer_backend=huggingface
```

In-process models answer these through `generate()` with the logits kept, and
one forward pass over the prompt for `echo`; a model server gets the request
as its own `/v1/completions` (vLLM and llama.cpp support both fields). The
same fields exist on the `text-generation` task (`tokens`, `logprobs`, `echo`,
`stop`) for `POST /inference`.

## Embeddings, reranking, transcription and model status

`POST /v1/embeddings` takes OpenAI's request (`input` is a string or a list
of strings; `encoding_format` `float` or `base64`) and returns one
L2-normalized vector per input. Any model sentence-transformers loads works,
run as its repo declares: its pooling (mean for all-MiniLM, CLS for BGE,
the last token for Qwen3-Embedding) and any layer after it. A plain Hub
encoder that declares none is pooled by mean.

```bash
curl localhost:8000/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model": "ibm-granite/granite-embedding-small-english-r2", "input": ["A cat sits on the mat.", "Markets fell."]}'
```

`POST /v1/rerank` takes Cohere's and Jina's request (`query`, `documents` as
strings or `{"text": ...}`, optional `top_n` and `return_documents`) and
returns the documents' indexes with a `relevance_score`, best first. Any
reranker sentence-transformers' CrossEncoder loads works: cross-encoders
(BGE reranker, GTE reranker, MiniLM MS MARCO) and LLMs that answer whether a
document is relevant (Qwen3-Reranker, with the instruction its config
gives). The scores are probabilities, whatever the model's config says, so a
threshold holds across requests.

```bash
curl localhost:8000/v1/rerank -H 'Content-Type: application/json' -d '{
  "model": "BAAI/bge-reranker-v2-m3", "query": "How do I reset my password?",
  "documents": ["Open Settings and choose Reset password.", "Our office hours are 9 to 5."], "top_n": 1
}'
```

Most embedding and rerank models need no settings: their repos say how they
run. One that does gets a [model server](#model-servers) entry with an
in-process (`hub`) backend, and is named by the entry's ID. `revision` pins
a branch, tag or commit of the repo, and `max_length` cuts what the model
reads of each text or pair: BGE reranker v2 reads up to 8K tokens, and
reading 512 is much faster on long documents.

```toml
[models."bge-reranker"]
task = "rerank"
hub = "BAAI/bge-reranker-v2-m3"
max_length = 512
```

A repo that needs its own Python code to load (GTE multilingual) is refused
unless `TRUST_REMOTE_CODE_MODELS` lists it, since that code runs in this
process.

`POST /v1/audio/transcriptions` takes OpenAI's multipart upload (`file`,
`model`, optional `language`, `prompt`, `temperature`, and `response_format`
`json` or `text`). Whisper and other Transformers speech models work; audio
longer than 30 seconds is done in chunks. WAV, MP3, FLAC and OGG are decoded
with soundfile, no ffmpeg needed. Uploads are limited by `MAX_AUDIO_BYTES`.

```bash
curl localhost:8000/v1/audio/transcriptions -F model=UsefulSensors/moonshine-base -F file=@talk.mp3
```

All three are tasks like the others (`embedding`, `rerank`, `speech-to-text`),
so `POST /inference` takes them too, and a model server can serve them (see
below).

`GET /v1/models` gives each model a `status`: `loaded`, `loading` or
`unloaded`, so clients can prefer a model that needs no cold start. The
playground lists loaded models first.

The playground's Chat draws a reply as Markdown (GitHub's, with tables and
task lists), and a fenced block in the language `chart` as a chart: a view's
bar, line or pie display (see Views and dashboards) beside the rows it draws.
The chart button next to the system prompt adds to it how a model writes one:

````markdown
```chart
{"type": "bar", "title": "Sales", "x": "month", "y": ["sales"],
 "rows": [{"month": "Jan", "sales": 120}, {"month": "Feb", "sales": 150}]}
```
````

A pie names `label` and `value` instead of `x` and `y`. A block that is not
such a chart shows as its text, with what is wrong with it.

## Tables: workspaces of JSON rows

`/db` stores plain JSON rows in tables, grouped in workspaces, in
[DuckDB](https://duckdb.org), **embedded** in this process: no separate
database server. It is one file, `toolkit.duckdb` in `DATA_DIR` (see
Backups), opened on the first request that needs it. Embedded means one
process: a second one cannot open the same file. DuckDB shares
memory with the models; `DATABASE_MEMORY` (default `1GB`) caps what it uses.
Its full-text extension is downloaded on the first start (the image has it
already); after that the service turns off DuckDB's reading of other files,
so SQL sent to it reads only the database.

The `default` workspace is
always there: it cannot be deleted, though its tables can, one by one. An
admin creates other workspaces, each with an optional label that says what
it holds, and tables, and edits each table's schema, in the dashboard's
**Data** section (`/admin#/data`), which also lets you browse, filter, sort,
edit and delete rows. Clients write to `/db/<workspace>/<table>`; a table
also appears on its first write. A row is any JSON object with a string
`id`, within its table's schema, which is empty (anything goes) until an
admin declares one. In the database a workspace is a schema of its name and a
table a table in it: each declared field is a column of its type, and the
fields a table does not declare are kept together in its `_rest` column, as
JSON; SQL reads them as `_rest->>'field'` (below). Names compare ignoring
case, as DuckDB compares them, so `People` and `people` are one name, and
workspace names DuckDB keeps (`main`, `toolkit`, `fts_...`) are refused.
Each table and workspace reports its `size_bytes`, an estimate of the room it
takes in the database (DuckDB compresses its columns and shares its blocks):
an empty table takes none, and deleting rows frees room for later writes
without shrinking the file. A bucket's `size_bytes` counts its rows, not its
files' bytes, which are kept apart in `DATA_DIR/files` (below) and add up to the
sum of its rows' `size`.

```bash
curl -X POST localhost:8000/db -H 'Content-Type: application/json' \
  -d '{"name": "shop", "label": "Web shop catalogue"}'
curl -X PUT localhost:8000/db/shop/products/p1 -H 'Content-Type: application/json' \
  -d '{"name": "Milk", "price": 1.2, "tags": ["dairy", "sale"]}'
curl 'localhost:8000/db/shop/products?tags=sale&_sort=-price&_limit=50'

# Rows need no workspace of their own: default is always there
curl -X PUT localhost:8000/db/default/notes/n1 -H 'Content-Type: application/json' -d '{"text": "hi"}'

# A new label, or none (null); and the list with the service's own too (admin key)
curl -X PATCH localhost:8000/db/shop -H 'Content-Type: application/json' -d '{"label": "Shop, 2026"}'
curl 'localhost:8000/db?all=true'
```

| Endpoint | Body | Response |
|---|---|---|
| `GET /db` | | `{workspaces, next?}`: a page (`_limit` ≤ 1000, `_cursor`) of the workspaces by name, each with its label and its tables and edge types by name and kind (`{name, graph, bucket, vector, kept}`), from memory, so it costs the same however many tables there are; `?all=true` (admin) adds the service's own |
| `POST /db` (admin) | `{name, label?}` | `201` + the workspace; `409` if it exists |
| `GET /db/{ws}` | | `{name, label, created, managed, size_bytes, tables: [{name, count, size_bytes, strict, graph, bucket, vector, fields, kept}], edges: [{name, count, size_bytes}]}`: its tables counted and sized; a bucket's remote and its copy are `GET /db/{ws}/{bucket}/_remote`'s |
| `PATCH /db/{ws}` (admin) | `{label}` | `200` + the workspace; a label is 1 to 200 characters, `null` clears it |
| `POST /db/{ws}` (admin) | `{name, fields?, strict?, graph?, bucket?}` | `201` + the empty table (a graph table unless `graph: false`; a bucket of files with `bucket: true`, below); `409` if it exists |
| `DELETE /db/{ws}` (admin) | | `204`; drops every table in it (a bucket with its files); `403` for `default` and for one that holds kept tables |
| `POST /db/{ws}/_transaction` | `{puts?: {table: rows}, deletes?: {table: ids}, edges?, delete_edges?}` | `204`; all applied, or none |
| `GET /db/{ws}/{table}` | | `{rows, next?}`; see the parameters below |
| `GET /db/{ws}/{table}/_count` | | `{count}`; takes the same filters and `_search` |
| `POST /db/{ws}/{table}` | row | `201` + the row, with `Location`; a missing id is made (UUIDv7); `409` if taken |
| `DELETE /db/{ws}/{table}` (admin) | | `204`; drops the table (a bucket with its files) |
| `GET /db/{ws}/{table}/_schema?_sample=500` | | `{fields, strict, observed}` |
| `PUT /db/{ws}/{table}/_schema` (admin) | `{fields, strict?}` | `200` + the schema; `400` if rows there now do not fit |
| `POST /db/{ws}/{table}/_bulk-get` | `{ids}` | `{rows}` in the order asked; missing ids left out |
| `PUT /db/{ws}/{table}/_bulk` | `{rows}` | `204`; upserts, all or none |
| `POST /db/{ws}/{table}/_bulk-delete` | `{ids}` | `204`; all or none |
| `POST /db/{ws}/{table}/_search` | `{vector \| text, k?, filters?, q?}` | `{results: [{row, distance}], tokens?}`: the rows nearest, for a table with a vector field (below) |
| `GET /db/{ws}/{table}/{id}` | | the row, or `404` |
| `PUT /db/{ws}/{table}/{id}` | row | `201` created or `200` replaced, + the row |
| `PATCH /db/{ws}/{table}/{id}` | partial row | `200` + the merged row, or `404` |
| `DELETE /db/{ws}/{table}/{id}` | | `204`, also when there was no row |

Everything without "(admin)" takes the client key, except in the
service's own data (`evals`, `jobs`, `enrichers`, `playground`). Those
are not workspaces:
`GET /db` lists them only with `?all=true`, which takes the admin key, with
`managed` naming what keeps each (it is `null` for a workspace). They need
the admin key for every request, and are read-only here. A workspace can also
hold *kept tables*: tables a part of the service makes there and keeps,
whose names start with a prefix it reserves (the evidence ledger's
`evidence_`). `GET /db` names what keeps
each as its `kept` (`null` for the others). Like the service's own data,
they are read with the admin key only and are read-only here: a write
gets `403` naming what keeps the table, as does deleting a workspace that
holds one, and `_graph` draws one only when `_table` names it. The list takes
`field=value` filters, which must all hold; a list field matches when it
holds the value. On a declared field the value is read as the field's type.
On other fields it is JSON when it parses as a number, `true`, `false` or
`null` (quote text that looks like one: `code="30"`), else text. `_sort` takes
comma-separated fields, `-field` for descending (default: `id`, or the best
match first with `_search`); `_limit` takes 1 to 1000 (default 100); `_cursor`
takes the previous page's `next`. `_search` finds words in the table's
full-text fields, and `_expand` follows refs, here and on one row's `GET`
(both below).
`PATCH` is [JSON Merge Patch](https://www.rfc-editor.org/rfc/rfc7396): `null`
removes a field, and objects merge. Bulk requests and transactions take up
to 1000 rows and ids.

**Schemas.** A table's schema lists fields, each
`{name, type, list?, required?, indexed?, unique?, fulltext?}`:

- `type`: `string`, `integer`, `number`, `boolean`, `object` (a JSON
  object) or `any`; with `list: true`, a list of that type, and
  `field=value` finds rows whose list holds the value. Or `ref`, the id of
  a row of another table of the workspace (`to`), which joins the two
  (below). Or `vector`, a table's one field of numbers searched by
  nearness, with options of its own (below).
- `required`: every row has it, not null. Otherwise it may be left out.
- `indexed`: filters and sorts on it use an index instead of reading every
  row. Worth it for the fields clients filter or sort on in big tables; only
  `string`, `integer`, `number` and `boolean` fields can be indexed.
- `unique`: no two rows hold the same value (`409`); rows without it are
  fine. Implies `indexed`; not for lists.
- `fulltext`: its words are searchable with `_search`. Only for a `string`
  field that is not a list, and not with `indexed` (a filter on it still
  works, by reading the rows).

**Full-text search.** A table's full-text fields share one index, so a
search finds its words across them:

```bash
curl -G localhost:8000/db/notes/reports --data-urlencode '_search=phish* "login page" -test' --data-urlencode status=open
```

Every word must be in the row, in any of its full-text fields, case aside. A
`"quoted phrase"` must be there as written, `-word` or `-"phrase"` must not,
and `word*` finds words starting so. The index splits text on spaces and most
punctuation, but keeps a domain, a version or an address whole
(`site.example.com`, `v1.2`, `10.0.0.1`): search it whole, or with `*` after
its start. Accents and case aside, words are matched as written: no stop
words are dropped and none is stemmed. `field=value` filters still apply, the
best match comes first (BM25) unless `_sort` says otherwise, and `_count`
takes `_search` too. A table without a full-text field answers `400`.
Marking a field full-text indexes the rows already there, and the
dashboard's table page then has a search box (for a graph table too). The
index (DuckDB's `fts`) is built again by a search when rows changed since,
unless it was a moment ago; until then the rows written since are found by
reading them for the words, so a row is found as soon as it is written, and
comes first, unscored.

`strict: true` also refuses fields the schema does not declare. Writes that
break the schema get `400`. The service checks rows itself because DuckDB
would silently convert (`2.7` into an integer column would be stored as
`3`). `PUT _schema` replaces the whole schema: it first checks the rows
already there against what it adds, and if any break it, answers `400`
naming a few and changes nothing; then it builds the table again with its new
columns, in one transaction. Fields left out lose their rules and index,
never their values, which go among the undeclared ones; a field declared now
takes the values the rows hold under its name. It never changes whether the
table is a graph table (below). `GET _schema` also returns `observed`: which
fields the newest rows hold, in how many, with which types; the dashboard
shows it beside the declared fields, with a button to declare each. A whole
number in a `number` field reads back whole (`3`, not `3.0`). A list field
can be `indexed`, but DuckDB indexes no list: its filters read the rows.
`id` is built in: a string, indexed and unique.

Names are letters, digits and single underscores, starting with a letter.
Ids are strings without `/`. The ids `POST` makes are UUIDv7 as 32 hex
digits: they sort in creation order, so lists (sorted by `id` unless
`_sort` says otherwise) show rows oldest first, and new rows go at the end of
the `id` index rather than all over it. The first 12 digits are the creation
time in milliseconds, so anyone who sees an id can tell when its row was made. Neither may start with `_`, which marks the
API's own path segments and parameters (`_count`, `_bulk`, `_limit`...), so
they never collide with a table or a row. The routes live under `/db`
rather than at the root, so a workspace can never shadow `/admin`, `/v1` or
the others.

### Refs: tables joined by their rows' ids

A field of type `ref` holds the id of a row of another table of the same
workspace, named by `to`, or with `list: true` a list of ids. Declaring it is
all it takes to join the two tables:

```bash
J='Content-Type: application/json'
curl -X POST localhost:8000/db/shop -H "$J" -d '{"name": "customers", "fields": [{"name": "name", "type": "string"}]}'
curl -X POST localhost:8000/db/shop -H "$J" -d '{"name": "orders", "fields": [
  {"name": "customer", "type": "ref", "to": "customers", "required": true},
  {"name": "status", "type": "string"}]}'
curl -X POST localhost:8000/db/shop -H "$J" -d '{"name": "items", "fields": [
  {"name": "order", "type": "ref", "to": "orders", "on_delete": "cascade"},
  {"name": "qty", "type": "integer"}]}'

curl -X POST localhost:8000/db/shop/customers -H "$J" -d '{"id": "c1", "name": "Ada"}'
curl -X POST localhost:8000/db/shop/orders -H "$J" -d '{"id": "o1", "customer": "c1"}'
curl -X POST localhost:8000/db/shop/orders -H "$J" -d '{"id": "o2", "customer": "nobody"}'
# 400: customer of orders row 'o2': customers has no row 'nobody'

# The order with its customer and its items: _expand follows a ref forward by its name, back by table:field
curl 'localhost:8000/db/shop/orders/o1?_expand=customer,items:order'
# {"id": "o1", "customer": "c1", "_expand": {"customer": {"id": "c1", "name": "Ada"}, "items:order": [...]}}
```

- **Writes.** Every id a write leaves in a ref field must be a row that is
  there when it commits (`400` otherwise). Checked at the end, so one
  `_transaction` can write a customer and the orders naming it in any order.
- **Deletes.** `on_delete` says what deleting a row does to the rows naming
  it: `restrict` (the default) refuses the delete with `409` naming a few of
  them, `unset` takes the id out (the field becomes null, or the id leaves the
  list), `cascade` deletes them too, and what refers to them in turn. Delete
  both in one `_transaction` and `restrict` is satisfied. A list of refs is
  not cascaded, and a required ref is not unset. A table another table's ref
  names cannot be dropped (`409`) until that field goes; dropping the
  workspace drops them all.
- **Reads.** `_expand` takes comma-separated paths: a ref field gives the row
  it names (a list of rows for a list), `table:field` the rows of that table
  whose ref names this row (the 100 lowest ids; the row's `_expand_more` names
  a step that had more, which `GET /db/shop/items?order=o1` pages), and a dot
  goes on (`items:order.product`, three steps at most). What is found goes
  under `_expand`, so the ref field keeps its id. A read that would expand to
  more than 10,000 rows is refused (`413`).
- A single ref is indexed; a list of refs cannot be, so its lookups read the
  table. `field=value` filters on a ref as on a string, and a view's SQL joins
  on it as on any column. A ref cannot name a bucket or a kept table.
- Refs are for "belongs to": an order and its customer. A many-to-many link is
  a list of refs, or a table of two refs when the link has data of its own.
  Graph edges (below) are for links of any table to any other, with
  properties, followed several hops deep.
- **In /admin.** The Data section's schema editor calls a ref field a *table
  ref*: picked as a field's type, it asks whether a row names one row or
  several, of which table, and what deleting one of them does, in words. A
  table's rows show the name of each row a ref names (its `name`, `title` or
  `label`, else its first text field), a link that opens that row, and a
  column for each ref of another table naming them, how many of its rows name
  each, a link to those rows. A row's editor lists its refs under the JSON,
  each set by picking from the rows of its table, and the rows naming it. The
  workspace page's **Diagram** draws its tables with their fields, a line for
  each table ref from its field to the table it names, and its relationships
  dashed.

### Vector fields: rows found by meaning

A table may have one field of type `vector`: a list of numbers per row,
which its own index searches for the rows nearest to a vector. With a
`model` and `from`, the vector is *embedded* from the row's `from` text
(a string field of the table) by that embedding model on every write that
sets the text, so clients send only the text, and a search can be a text
too:

```bash
# A table whose embedding is made from text (admin key); its dimensions are the model's
curl -X POST localhost:8000/db/default -H 'Content-Type: application/json' -d '{
  "name": "kb", "graph": false, "fields": [
    {"name": "text", "type": "string", "fulltext": true},
    {"name": "tenant", "type": "string", "indexed": true},
    {"name": "year", "type": "integer", "indexed": true},
    {"name": "tags", "type": "string", "list": true},
    {"name": "embedding", "type": "vector", "model": "ibm-granite/granite-embedding-small-english-r2", "from": "text"}]}'

# Rows, as for any table: every text is embedded, all of a write's in one batch
curl -X PUT localhost:8000/db/default/kb/_bulk -H 'Content-Type: application/json' -d '{"rows": [
  {"id": "r1", "text": "Refunds are issued within 14 days.", "tenant": "acme", "year": 2025, "tags": ["billing"]}]}'

# Search by meaning, among the rows the filters and the full-text words select
curl localhost:8000/db/default/kb/_search -H 'Content-Type: application/json' -d '{
  "text": "how do I get my money back?", "k": 5,
  "filters": {"tenant": "acme", "year": 2025}, "q": "refund*"}'
```

A vector field's options are `dimensions` (1 to 4096; with a model, left
out to take the model's, found by embedding a probe text when the field is
made), `similarity` (`COSINE`, the default, `DOT_PRODUCT` or `EUCLIDEAN`),
`quantization` (`NONE`, the one there is: each number a 4-byte float) and
the optional `model` with `from`. A vector field is never a list, indexed, unique,
full-text or a filter. Without a model, a row carries its vector itself, as
a list of exactly `dimensions` numbers or `null`, and a search gives a
`vector`. With one, a row that sends the vector is refused (`400`: it is
embedded from its text), a row without the text gets `null`, and a `PATCH`
embeds the text it sets; a model outside `ALLOWED_MODELS` is refused
(`403`) when the field is made, on writes and on searches by text.

With a model, two more options say what goes before the texts it embeds:
`document_prompt` before each row's text and `query_prompt` before a
search's (`""` for none). Some models are trained with them and find worse
without: Qwen3-Embedding's query instruction, EmbeddingGemma's
`task: search result | query: `. Left out when the field is made, they are
the model's own, those its sentence-transformers config names `query` and
`document` (none for a model without any). A model whose prompts have other
names (Harrier's `web_search_query`) is refused (`400`, its prompts listed)
until the query prompt is given. The schema shows the prompts in effect. The
document prompt is in every stored vector, so it never changes; the query
prompt may, by a schema put with the field's new one, since only searches
use it.

`_search` takes a `vector` or a `text` (one of them), `k` (1 to 100,
default 10), `filters` (`{"field": value}`, all of which must hold, read as
the list's `field=value`) and `q` (words of the full-text fields, as the
list's `_search`). The filters and the words apply during the search, which
compares the vector with every row they select (so it is exact), so `k` of
the rows they select come back when there are that many, nearest first, each
`{row, distance}`: the row without its vector, and the distance
by the field's similarity, lower nearer: for `COSINE` one minus the cosine
and for `EUCLIDEAN` the squared distance (`0` for both is the same vector),
for `DOT_PRODUCT` -(1 + the dot product) / 2, which runs from `-1` (the same
unit vector) to `0`. `tokens` says what the text took to embed. A row whose
text is longer than `MAX_INPUT_CHARS` is refused (`400`, naming it), as the
model would refuse the text. Lists, searches, bulk reads and the graph view
leave the vectors out, since a row's vector is hundreds of numbers;
`GET /db/{ws}/{table}/{id}` gives the row whole.

**Hybrid.** Meaning alone misses what words find well: a name, a code, a
rare term. On a table with full-text fields, `"hybrid": true` with a `text`
ranks by both: the nearest rows, and the rows whose full-text fields hold the
most and rarest of the text's words (any of them; the index scores them as
BM25 does), four times `k` of each, fused by reciprocal rank (a row scores
1 / (60 + its rank) in each list it is in). The best `k` come back, each with
its `score`; a row only the words found has no `distance`. `filters` and `q`
narrow both lists.

```bash
curl localhost:8000/db/default/kb/_search -H 'Content-Type: application/json' -d '{
  "text": "refund for order A-1043", "k": 5, "hybrid": true}'
```

There are no stop words, so a text of common words matches most rows, and
each is scored: over a table near `VECTOR_MAX_ROWS` that takes about half a
second (measured on 100,000 rows), against tens of milliseconds for rarer
words.

**Rerank.** A rerank model (the `rerank` task: a cross-encoder) reads the
text together with each row's and ranks better than nearness alone.
`"rerank": "<model>"` with a `text` has it read four times `k` rows found
(nearest, or hybrid; at most 100), the text against each row's `from` text,
and keeps the `k` it scores best, each with its `score` (0 to 1, comparable
across searches). It reads them in one batched pass: at `k` 10, 40 texts.
`tokens` counts its tokens too, and `ALLOWED_MODELS` holds for it as for any
model.

```bash
curl localhost:8000/db/default/kb/_search -H 'Content-Type: application/json' -d '{
  "text": "how do I get my money back?", "k": 5, "hybrid": true,
  "rerank": "Alibaba-NLP/gte-reranker-modernbert-base"}'
```

A view's `search` source takes `hybrid` and `rerank` too; its rows get the
score as `_score`, beside `_distance`.

The field's column is made when the field is. Its options never change,
but for the query prompt: to embed with another model, or to change the
dimensions, remove the field from the schema (its vectors go with it) and
add it again; rows get vectors as they are written again, since nothing is
embedded in the background. A table with a vector field holds
`VECTOR_MAX_ROWS` rows at most (default 100,000; `413` past it), as a search
compares every row: about 50 ms per 50,000 vectors of 384 numbers.

### Buckets: tables of files

A *bucket* is a table of files: a row per file, with its `name` (as
uploaded), `size`, `sha256`, `content_type`, `created` (seconds since the
epoch) and `copied` (when it reached the bucket's remote, below), and the
file's bytes on disk, in `DATA_DIR/files`. An admin makes one with `bucket: true` and
no fields: a bucket's fields are fixed, and it is a plain table. Its rows are
listed, filtered and counted as any table's, with the client key, but only
its file routes write them, so every row has its bytes: a row written
through `/db` (`PUT`, `PATCH`, `DELETE`, `_bulk`, `_transaction`, an edge)
is refused with `403`, and a schema put with `400`.

```bash
# A bucket (admin key)
curl -X POST localhost:8000/db/default -H 'Content-Type: application/json' -d '{"name": "scans", "bucket": true}'

# Upload: the body is the file's bytes, its Content-Type kept as the file's
curl -X POST 'localhost:8000/db/default/scans/_files?name=invoice-0042.pdf' \
  -H 'Content-Type: application/pdf' --data-binary @invoice-0042.pdf
# -> 201 {"id": "3f2a…", "name": "invoice-0042.pdf", "size": 48213, "sha256": "9b1c…",
#         "content_type": "application/pdf", "created": 1793750400.0}

# The list of files is the bucket's list of rows: newest first, or one by its hash
curl 'localhost:8000/db/default/scans?_sort=-created&_limit=50'
curl 'localhost:8000/db/default/scans?sha256=9b1c…'

# Download it, or a part of it (206, with Content-Range)
curl -OJ localhost:8000/db/default/scans/_files/3f2a…
curl -H 'Range: bytes=0-1023' localhost:8000/db/default/scans/_files/3f2a… -o first-kb.bin

# Delete it: its row, then its bytes
curl -X DELETE localhost:8000/db/default/scans/_files/3f2a…
```

| Endpoint | Body | Response |
|---|---|---|
| `POST /db/{ws}/{bucket}/_files?name=` | the file's bytes | `201` + its row, with `Location`; `413` past `MAX_FILE_BYTES`; `404` for no such bucket, `400` for a table that is not one |
| `GET /db/{ws}/{bucket}/_files/{id}` | | the bytes, as an attachment named as uploaded; a `Range` gets `206`; `404` for no such file, or one whose bytes are missing from disk (it says so) |
| `DELETE /db/{ws}/{bucket}/_files/{id}` | | `204`, also when there was no file |

An upload streams to disk as it arrives, so a file never sits in memory;
`MAX_FILE_BYTES` (default 1 GiB) caps it, at once when the request's
`Content-Length` says it is too big, else when the body gets there. An
empty body is an empty file. A name is 1 to 255 characters without control
characters, and need not be unique: uploading the same file again adds a
second one, with its own id (files are not stored by hash). The id is made
by the service, 32 hex digits. A download carries the type the file was
uploaded with, but as an attachment, with `X-Content-Type-Options: nosniff`
and `Content-Security-Policy: sandbox`: an uploaded web page is saved, never
shown as a page of the service, where `/admin` keeps its key. A bucket is
kept until it is deleted: dropping it, or its workspace, deletes its files
too. Each file is `DATA_DIR/files/<workspace>/<bucket>/<id>`; a new bucket whose
folder already holds files, or whose name differs from another bucket's
folder only by case (one folder on macOS's disks), is refused (`409`).

### A bucket's remote: its files copied outside the service

A *remote* is a place outside the service that a bucket's files are copied
to: S3, and what speaks its API (Cloudflare R2, Backblaze B2, MinIO), B2's
own API, Google Cloud Storage, Azure Blob, or a folder (a mounted disk, say).
The service talks to it through [OpenDAL](https://opendal.apache.org): a
remote is OpenDAL's `scheme` with its `options`. A key, a password or a
token is never sent to the API: `secrets` names, for each such option, the
environment variable of the service that holds it. Set those in the
service's `.env` file (or its environment) and restart it; their values are
read when the remote is used, never stored, shown or logged, and jobs and
model servers do not get them. An option in `options` whose name looks like
a secret's (`key`, `secret`, `password`, `token`, `credential` or
`connection_string` in it), or whose value is a URL with a password in it
(`https://user:password@host`), is refused with `400`.

```bash
# In the service's .env (or its environment), then a restart:
#   R2_ACCESS_KEY_ID=...
#   R2_SECRET_ACCESS_KEY=...

# A remote on R2 (admin key): OpenDAL's s3 service at R2's endpoint, its keys by name
curl -X PUT localhost:8000/remotes/r2 -H 'Content-Type: application/json' -d '{
  "name": "r2", "scheme": "s3", "label": "Backups on R2",
  "options": {"bucket": "toolkit-files", "region": "auto",
              "endpoint": "https://<account id>.r2.cloudflarestorage.com"},
  "secrets": {"access_key_id": "R2_ACCESS_KEY_ID", "secret_access_key": "R2_SECRET_ACCESS_KEY"}}'
# -> {"name": "r2", ..., "buckets": [], "missing": []}   (missing: variables not set)

# Try it: a probe object under .toolkit-probe/ written, read back, listed and deleted
curl -X POST localhost:8000/remotes/r2/test
# -> {"name": "r2", "scheme": "s3", "steps": [{"step": "write", "ms": 182.4}, {"step": "read", ...}, ...]}
# or 503 {"detail": "The remote r2 could not write a probe object: PermissionDenied (permanent) at write ..."}

# Name it on a bucket: every file of it is copied there by a task queued now
curl -X PUT localhost:8000/db/default/scans/_remote -H 'Content-Type: application/json' -d '{"remote": "r2"}'

# Upload as before: a copy follows
curl -X POST 'localhost:8000/db/default/scans/_files?name=invoice-0043.pdf' \
  -H 'Content-Type: application/pdf' --data-binary @invoice-0043.pdf

# How the copy stands: files copied and waiting, and the latest copy (its error, if it failed)
curl localhost:8000/db/default/scans/_remote
# -> {"remote": "r2", "count": 2, "copied": 2, "waiting": 0,
#     "task": {"status": "succeeded", "result": {"remote": "r2", "copied": 2, "deleted": 0, "skipped": 0}, ...}}

# Each file's row says when it was copied; the files still waiting have no copied
curl 'localhost:8000/db/default/scans?copied=null'
```

On Backblaze B2 through its S3 API the remote is the same with
`"endpoint": "https://s3.<region>.backblazeb2.com"` and `"region":
"<region>"` (the region in the bucket's endpoint, `us-west-004` say), and its
application key as the secrets: `{"access_key_id": "B2_KEY_ID",
"secret_access_key": "B2_APPLICATION_KEY"}`. A folder is
`{"scheme": "fs", "options": {"root": "/mnt/backup/toolkit"}}`, outside
`DATA_DIR/files`: a root that is that folder, in it or above it is refused (`400`),
since the copies would be written over the files they copy.

| Endpoint | Body | Response |
|---|---|---|
| `GET /remotes` (admin) | | `{remotes}`: each spec, the buckets that name it, and `missing`, the variables its secrets name that are not set |
| `GET /remotes/{name}` (admin) | | the remote, as listed |
| `PUT /remotes/{name}` (admin) | `{name, scheme, options?, secrets?, label?}` | the remote; `400` for a secret in `options`, or a folder over `DATA_DIR/files`; `409` to change where its copies are while a bucket names it |
| `DELETE /remotes/{name}` (admin) | | `204`; `409` while a bucket names it |
| `POST /remotes/{name}/test` (admin) | | `{name, scheme, steps}`; `503` with the remote's error, or a variable not set |
| `GET /db/{ws}/{bucket}/_remote` (admin) | | `{remote, count, copied, waiting, task}` |
| `PUT /db/{ws}/{bucket}/_remote` (admin) | `{remote}` | the same; `null` names none; the one it names already: copy now what is behind; `404` for no such remote |

Each file goes to `<workspace>/<bucket>/<file id>` under the remote's
`root` (its bucket's top without one). A copy is a task of the kind `copy`,
one for a bucket at a time (a change while it runs queues one more, which waits for it):
it is queued after each upload and each delete in a bucket with a remote,
when the bucket's remote is set, and when the service starts and then every
hour, for each bucket with files waiting or whose last copy failed. It
streams each file not yet copied from disk to the remote, in parts, and
marks it `copied`, then deletes there the copies of files deleted here. A
remote that fails (down, a wrong key, a variable not set) fails the copy
with its message, which `_remote` shows and the bucket's page too; it is
tried again after 1, 2, 4, 8 and 16 minutes, and uploads go on meanwhile:
the files wait, and are copied once the remote works, within the hour after
the last retry, or at once with **Copy now** on the bucket's page (a
`PUT .../_remote` naming the remote it names already), which also starts a
retry that waits. Saving the remote (a variable of its secrets changed) does
the same for the buckets that name it. Nothing already copied is copied
again.

What stays where:

- **A file deleted** here is deleted at the remote by the next copy.
- **A bucket dropped**, or its workspace, deletes its files here and leaves
  their copies at the remote: delete them there (`<workspace>/<bucket>/`)
  if they should go. A new bucket of the same name and remote takes the
  folder over, and its first copy deletes what it does not hold.
- **A remote changed** on a bucket (another one, or none) leaves the copies
  at the old one where they are; every file is copied again to the new one.
  While a bucket names a remote, the remote cannot be deleted, nor where
  its copies are changed (`409`: its scheme, its options, or the variable a
  root or a bucket is read from): make another and name it on the bucket.
  Its label, and the variables its secret options name, can change (a key
  rotated).
- **A copy stopped or failed in the middle of a file** deletes what it sent
  of it, and the next copy sends it whole. On S3, R2 and B2 an unfinished
  multipart upload may stay at the provider, not listed with the objects but
  stored, until it is aborted: give the bucket a lifecycle rule that aborts
  incomplete multipart uploads after a day or so.
- **A file missing from disk** (a disk replaced, a file removed by hand) is
  read back from the remote when its download is asked for, if it was
  copied: into its place, checked against its size and SHA-256, then
  answered as usual, a `Range` too (`503` when the remote fails or holds
  something else). One never copied stays a `404`.

### Graph tables: rows joined by edges

Tables are graph tables, whose rows are the graph's nodes, unless made
plain. That holds for a table made by
`POST /db/{ws}` (where `graph` defaults to `true`), by a client's first
write, or by a schema put before any row; only `graph: false` makes a plain
table, as do the service's own stores (evals, jobs and the like) for theirs.
A graph table's rows are read and written exactly like any other table's,
with the same filters, schemas and transactions, but they can also be
joined by **edges**. The kind is chosen when a table is created and cannot
change later: to link the rows of a plain table (one made with
`graph: false`, or before tables were graph tables), create a table and
copy them over. The same embedded database holds both kinds,
so a graph table and a plain one can sit side by side in one workspace, and a
row of either can hold a plain reference (such as another row's id) to the
other; only edges need graph tables at both ends.

An edge has a type, goes from one row to another (`table/id`, both in graph
tables of the same workspace) and carries properties of its own. One edge
of a type joins a given pair of rows, so storing it again replaces its
properties. Edge types appear on the first edge of their name, or empty
when an admin makes one (`POST /db/{ws}/_edges`), and are listed per
workspace; an edge type cannot share a table's name. Deleting a row deletes
its edges, and dropping a graph table or an edge type deletes the edges with
it. In the database, a workspace's edges, of every type, are rows of its
`_edges` table: `type`, `from_table`, `from_id`, `to_table`, `to_id` and
`props` (the edge's properties, as JSON), which SQL reads as any table.

```bash
# Ann, Bob and Acme, in tables that declare name, so SQL reads it as a column
curl -X POST localhost:8000/db -H 'Content-Type: application/json' -d '{"name": "crm"}'
curl -X POST localhost:8000/db/crm -H 'Content-Type: application/json' -d '{"name": "people", "fields": [{"name": "name", "type": "string"}]}'
curl -X POST localhost:8000/db/crm -H 'Content-Type: application/json' -d '{"name": "companies", "fields": [{"name": "name", "type": "string"}]}'
curl -X POST localhost:8000/db/crm/_transaction -H 'Content-Type: application/json' -d '{"puts": {
  "people": [{"id": "ann", "name": "Ann"}, {"id": "bob", "name": "Bob"}],
  "companies": [{"id": "acme", "name": "Acme"}]}}'

curl -X PUT localhost:8000/db/crm/_edges -H 'Content-Type: application/json' -d '{"edges": [
  {"type": "knows", "from": "people/ann", "to": "people/bob", "since": 2020},
  {"type": "works_at", "from": "people/ann", "to": "companies/acme", "role": "CTO"}]}'

# Ann's edges, the rows next to her, and everyone within two "knows" steps of her
curl 'localhost:8000/db/crm/people/ann/_edges'
curl 'localhost:8000/db/crm/people/ann/_neighbors?_type=works_at'
curl 'localhost:8000/db/crm/people/ann/_traverse?_type=knows&_depth=2'

# A read-only query: one SELECT, its tables named as above, its edges rows of _edges
curl localhost:8000/db/crm/_query -H 'Content-Type: application/json' -d '{
  "params": {"n": "Ann"},
  "query": "SELECT p.id, c.name FROM people p JOIN _edges e ON e.type = '\''works_at'\'' AND e.from_id = p.id JOIN companies c ON c.id = e.to_id WHERE p.name = $n"}'
```

| Endpoint | Body | Response |
|---|---|---|
| `GET /db/{ws}/_edges` | | `{edges: [{name, count, size_bytes, joins: [{from, to, count}]}]}`, the workspace's edge types with the pairs of tables their edges join, most edges first; it reads every edge, which `GET /db/{ws}` does not |
| `POST /db/{ws}/_edges` (admin) | `{name}` | `201` + the empty edge type; `409` if it exists or a table has its name |
| `PUT /db/{ws}/_edges` | `{edges: [{type, from, to, ...properties}]}` | `204`; stores them, all or none; `400` if an end is not a row of a graph table |
| `POST /db/{ws}/_edges/_delete` | `{edges: [{type, from, to}]}` | `204`; all or none; edges not there are ignored |
| `DELETE /db/{ws}/_edges/{type}` (admin) | | `204`; drops the edge type with its edges |
| `GET /db/{ws}/{table}/{id}/_edges` | | `{edges, next?}`: the row's edges, `_type` (repeatable), `_direction` (`out`, `in`, `both`), `_limit`, `_cursor` |
| `GET /db/{ws}/{table}/{id}/_neighbors` | | `{neighbors: [{table, row, edge}], next?}`: the rows one edge away, same parameters |
| `GET /db/{ws}/{table}/{id}/_traverse` | | `{rows: [{table, depth, row}]}`: rows within `_depth` edges (1 to 10, default 2), nearest first, each once; `_type`, `_direction` (default `out`), `_limit` |
| `POST /db/{ws}/_query` (admin) | `{query, params?, limit?}` | `{rows}`: up to `limit` (≤ 1000) rows of one read-only SELECT |
| `GET /db/{ws}/_graph` | | `{nodes: [{table, row}], edges, tables, outside, more}`: the graph tables' first rows (`_table`, repeatable; all when none; `_limit` ≤ 5000 shared among them) and the edges of the `_type`s named (all when none; `_edge_limit` ≤ 20000) that join two of them; `outside` counts edges to rows not loaded, `more` says whether a limit cut the result |

**In the dashboard.** The Data section uses everyday words, as Access and
Notion do: tables, and **relationships** for edge types (their edges are
relationships too, as in Neo4j). The list on the left shows each
workspace with its label, and under the open one its **SQL query**, then its
contents in groups: **Tables**, **Graph**, which holds the **Graph
query** and the relationships, **Vectors**, the tables with a vector
field, **Buckets**, the tables of files, and **Views**, its views and
dashboards ([below](#views-and-dashboards-saved-reads-a-client-runs)). The service's own data comes last, folded
away under **The service's own**, browsable but read-only. A kept table is
browsable but read-only too, marked with a lock and what keeps it. A workspace's
page shows its label, which the pencil beside it changes, lists its
**Tables** (and **Vectors** and **Buckets**) and its **Relationships**, and its
**New** form makes a table, a relationship or a bucket; a plain table, which
relationships cannot join, is marked so. A bucket's page lists its files
(name, size, type, when, the start of its hash, when it was copied to the
remote) and uploads, downloads and deletes them; several files can be picked
to upload at once. Above them, its remote is picked among the remotes (or
none), with how many files are copied and waiting, how the last copy
went, and **Copy now** while it is behind. The **Remotes** tab of Settings (`/admin#/remotes`) makes and edits the
remotes, in a form started blank or from a preset (R2, B2, S3, a folder),
their secrets named as variables, and tests one with a button.
A table's schema offers the `vector` type with its options, the model picked
among the embedding models the service lists (or any other ID typed in),
`from` among the table's string fields, and its two prompts (empty when made:
the model's own). A table with a vector field has a search box above its
rows: a text its model embeds (a vector, as JSON, without a model), how many
rows, words for its full-text fields, a **Hybrid** switch when it has them,
and a rerank model; the rows found are listed with their distance, and the
score of a hybrid or reranked search. A table's page
has its rows filtered by an SQL condition (`age > 30`, `name LIKE 'A%'`,
`_rest->>'kind' = 'lead'` for a field it does not declare); a row opens with
its relationships listed, each linked to its type and to the row at the other
end. A relationship's page lists its edges (from, to and their properties)
filtered by a condition on its columns (`from_id = 'ann'`,
`props->>'since' >= '2020'`), and names the tables it joins
(`people → companies`). The graph itself is drawn in one place, the
**Graph query**; the table, relationship and row pages each open it at
what they show.

**Querying graph data.** The quick reads above cover most needs: a row's
edges, its neighbors with the edge each is reached by, and a breadth-first
traversal that reports how far each row is. For anything else, `_query`
takes a query over plain and graph tables alike; the dashboard's Data
section has two query pages per workspace, **SQL query** over every table
and **Graph query**, whose relationships (rows of `_edges`) are drawn, each
with example queries, the workspace's names a click away, and the result as
a grid to export as JSON or CSV.

**Seeing the graph.** The Data section's **Graph query** draws the
relationships a query returns (rows of `_edges`, or any with `from_table`,
`from_id`, `to_table` and `to_id`), and before a query runs, a
sample of the graph tables' rows and the edges among them, with sigma.js,
each table in its own color.
`GET /db/{ws}/_graph` loads a sample (the first rows of each table, the
limit shared among them); double-clicking a row loads its neighbors
(`_neighbors`), so a graph is explored outward from where it matters. The
filters run in the browser, as in Neo4j Browser and Bloom: the legend
switches tables and edge types off; rules on a field (`age ≥ 30`,
`tags contains lead`, `email missing`, on any table or one of them; the
`degree` and `table` of a row count as fields) gray out the rows that fail
them, or hide them; a search highlights the rows whose values contain it;
and isolated rows can be dropped. Rows are labeled by a field of your choice
(`name` or `title` when most rows have one), sized by degree or all alike,
and the view exports as JSON. `_query` takes one
[DuckDB SELECT](https://duckdb.org/docs/sql/query_syntax/select) (a
`WITH ... SELECT` among them): the workspace's tables by their names (the
workspace is the schema a bare name is read in; quote one SQL keeps, such as
`"order"`), their declared fields as columns and the others through
`_rest` (`_rest->>'kind'`), and the edges as rows of `_edges`: a row's
neighbors are a join, a walk of several steps a `WITH RECURSIVE` (the
dashboard's examples show both). Pass values through `params`, as
`$name`. Rows come back as DuckDB gives them: a table's `_rest` as an
object, `_seq` (the number of the write that last changed the row) among
the columns. Anything but one SELECT is refused with `400`, saying why (a
`WITH` whose part inserts, updates or deletes as it runs, in a read-only
transaction), and DuckDB reads no file there. `_query` needs the admin key: names resolve
within the workspace, but SQL can name any schema (`evals.datasets`,
`main.tk_audit`), so it can read everything the service stores; clients use
the reads above instead.

### Views and dashboards: saved reads a client runs

A **view** is a read of a workspace's data saved under a name, with
parameters and a display: SQL as `_query` takes it, a table's rows as its
list gives them, or a table's nearest rows as `_search` finds them, drawn as
a table, a number, a line, bar or pie chart, a graph or a bucket's files. The
admin writes views; a client key runs them, so a view is how the admin
chooses what a client may read without handing it `_query`. A **dashboard**
lays a workspace's views out on a page, sharing their parameters and
refreshing them together. Both are kept beside the workspace and go when it
is dropped.

Say the `shop` workspace holds orders:

```bash
ADMIN=admin-key; CLIENT=client-key
curl -X POST localhost:8000/db -H "X-API-Key: $ADMIN" -H 'Content-Type: application/json' -d '{"name": "shop"}'
curl -X PUT localhost:8000/db/shop/orders/_schema -H "X-API-Key: $CLIENT" -H 'Content-Type: application/json' -d '{"fields": [
  {"name": "region", "type": "string"}, {"name": "year", "type": "integer"}, {"name": "total", "type": "integer"}]}'
curl -X PUT localhost:8000/db/shop/orders/_bulk -H "X-API-Key: $CLIENT" -H 'Content-Type: application/json' -d '{"rows": [
  {"id": "o1", "region": "north", "year": 2025, "total": 120},
  {"id": "o2", "region": "south", "year": 2025, "total": 80},
  {"id": "o3", "region": "north", "year": 2026, "total": 95}]}'
```

A view of sales by region for a year, drawn as a bar chart. `params` is a
JSON Schema of an object: here one integer, `year`, that defaults to 2025.
The SQL takes it as `$year`, bound as a value, never written into the text:

```bash
curl -X PUT localhost:8000/db/shop/_views/sales_by_region -H "X-API-Key: $ADMIN" \
  -H 'Content-Type: application/json' -d '{
  "name": "sales_by_region", "label": "Sales by region",
  "source": {"kind": "sql",
             "query": "SELECT region, sum(total) AS total FROM orders WHERE year = $year GROUP BY region ORDER BY region"},
  "params": {"type": "object", "properties": {"year": {"type": "integer", "default": 2025}}},
  "display": {"type": "bar", "x": "region", "y": ["total"]}}'
```

Saving checks the SQL with DuckDB's own parser: one `SELECT`, so it changes
nothing, with no `$name` the params leave out; anything else is refused
(`400`, saying why). A `WITH` whose part writes reads as a `SELECT` to the
parser: it is saved, and each run is refused. The SQL reads a table's declared fields as columns (here
`orders` declares `region`, `year` and `total`); an undeclared one is
`_rest->>'name'`. A client runs the view with its key:

```bash
curl -X POST localhost:8000/db/shop/_views/sales_by_region -H "X-API-Key: $CLIENT" \
  -H 'Content-Type: application/json' -d '{"params": {"year": 2026}}'
# {"rows": [{"region": "north", "total": 95}], "columns": ["region", "total"],
#  "truncated": false, "ran_at": 1793718048.9, "cached": false}
```

A parameter the view does not declare, a required one left out or one of
the wrong type is refused (`400`, naming it), and a missing one takes its
default. Each run is held to a time limit, `VIEW_TIME_LIMIT_MS` (a view's
`time_limit_ms` may ask for less), past which its query is stopped, so no
text in the SQL gets past it: a view that takes longer is stopped and answers
`503`. It reads at most `VIEW_MAX_ROWS` rows (`max_rows` may ask for fewer);
`truncated` says when that cut them. Its answer is kept for the view's
`refresh_s` (30 s unless it says otherwise, 5 at least) for runs with the
same parameters: those within it get the kept answer (`cached: true`), so a
dashboard open on many screens asks the database once per interval. Saving
the view again, or deleting it, drops what was kept.

The other sources take the bodies their routes take, and a value written
`{"$param": "name"}` anywhere in them takes that parameter's value; one with
no value (not given, no default) is left out, so a filter applies only when
its parameter is given:

```bash
curl -X PUT localhost:8000/db/shop/_views/latest -H "X-API-Key: $ADMIN" -H 'Content-Type: application/json' -d '{
  "name": "latest", "label": "Latest orders",
  "source": {"kind": "rows", "table": "orders", "filters": {"region": {"$param": "region"}}, "sort": "-id", "limit": 20},
  "params": {"type": "object", "properties": {"region": {"type": "string", "enum": ["north", "south"]}}},
  "display": {"type": "table"}}'
```

A dashboard of the two, the chart two thirds of the page's width, sharing
`year` and `region` (each view takes the ones it declares), refreshed every
30 seconds (0 for never; 5 at least):

```bash
curl -X PUT localhost:8000/db/shop/_dashboards/sales -H "X-API-Key: $ADMIN" -H 'Content-Type: application/json' -d '{
  "name": "sales", "title": "Sales", "refresh_s": 30,
  "params": {"type": "object", "properties": {"year": {"type": "integer", "default": 2025},
                                              "region": {"type": "string", "enum": ["north", "south"]}}},
  "items": [{"view": "sales_by_region", "width": 8}, {"view": "latest", "width": 4}]}'
```

A dashboard is saved only when its views are the workspace's, and a view a
dashboard shows is not deleted (`409`, naming the dashboard). Reading a
view's or a dashboard's spec (`GET /db/{ws}/_views/{name}`) and every write
need the admin key; only running a view takes the client key.

**In the dashboard.** The Data section lists a workspace's dashboards and
views in its **Views** group, and its page has **New view** and **New
dashboard**. A view's page has its parameters as a form at the top (their
values go in the address, so a link opens the same view with the same
values), its answer drawn, when it ran and whether it came from the cache,
and, behind **Edit**, its editor: the SQL in the query editor, or a rows or
search source as JSON, the params' JSON Schema, the display and its fields
(offered from the last answer's columns), its caps, **Preview** (which runs
what is typed and keeps nothing), **Save** and **Delete**. A chart reads as a
table too, and a display naming a field the answer lacks says so. A
dashboard's page has the shared parameters at the top, its views on a
12-column grid in order (one column on a phone), each refreshed at its
interval, and an editor for its title, parameters, refresh and views (add,
width, up, down, remove).

### Workflows: a table's rows through models, conditions and writes

A workflow takes the rows of one table through a graph of nodes and writes
what comes out. Each row is a JSON object: the table's row, and each node's
result under the node's id, so a later node reads an earlier one's by a
JMESPath on the row. Tag new tickets, send the spam to one table and the rest,
with their label, to another; rows the classifier cannot do go to a third:

```bash
POST /workflows {"name": "Tag tickets", "page_size": 100, "trigger": {"interval_s": 3600},
  "nodes": [
    {"id": "A", "type": "table.read", "settings": {"workspace": "support", "table": "tickets", "only_new": true}},
    {"id": "B", "type": "task", "settings": {"task": "text-classification", "model": "distilbert-base-uncased-finetuned-sst-2-english",
                                             "inputs": {"text": "body"}}},
    {"id": "S", "type": "switch", "settings": {"cases": ["B.label == 'SPAM'"]}},
    {"id": "T", "type": "table.write", "settings": {"workspace": "support", "table": "trash"}},
    {"id": "K", "type": "table.write", "settings": {"workspace": "support", "table": "tagged",
                                                    "fields": {"text": "body", "label": "B.label", "score": "B.confidence"}}},
    {"id": "F", "type": "table.write", "settings": {"workspace": "support", "table": "failed", "fields": {"why": "_error.message"}}}],
  "edges": [{"source": "A", "target": "B"}, {"source": "B", "target": "S"}, {"source": "B", "output": "error", "target": "F"},
            {"source": "S", "output": "0", "target": "T"}, {"source": "S", "output": "default", "target": "K"}]}
POST /workflows/dry-run {"spec": {...}, "limit": 20}   # the first rows through it, nothing written
POST /workflows/{id}/run {}                              # 202 with the run
GET  /workflows/runs/{run}                               # its status, rows along each edge, each node's counts
```

The node types, with each one's settings as a JSON Schema, are
`GET /workflows/nodes`: Table read, Task (any task of `GET /tasks`, its inputs
JMESPaths on the row), Enrich, IF (outputs `true` and `false`), Switch (a
case's number each, and `default`), Set, Merge (puts back together the rows
of one source row that went different ways), SQL and Table write (merged
into the row of the same id: the fields it writes are set, the row's others
kept, its vector among them; a row with no such id is added). A node with
several edges in gets the rows of all of them.

A SQL node runs one SELECT over the rows that reach it, as a table named
`rows` (each field a column; a node's result, being nested, is JSON), and the
tables of its workspace; the rows it gives go on in their place. So a page of
tagged tickets can be joined to the team that owns each domain, filtered and
reshaped in one node (a field a table does not declare is in its `_rest`):

```sql
SELECT r.id, r._row, r.B->>'label' AS label, o._rest->>'team' AS team
FROM rows r JOIN owners o ON o.id = r.domain
WHERE r.B->>'label' <> 'SPAM'
```

It reads, never writes, within 30 seconds and 10,000 rows a page. A row
keeps its place (for a Merge after, and in an invoke's answer) by keeping
`_row`; a row the SQL makes (a count) is one of its own. It sees one page at a
time (`page_size`, at most 1,000 rows), so a count or a dedupe is of a page,
not of the whole table; a join reads the whole of the other table.
Saving checks the graph: one Table read, no cycle, every node reached, every
JMESPath, each task known, given every input it needs and its model in
`ALLOWED_MODELS`, and each node's result read only after it and by the fields
it has (`B.lable` of a classifier is refused, saying it gives `label`,
`confidence`, `scores`). A workflow that reads only new rows (`only_new`)
cannot write into its own table: each run would read back what the last one
wrote. A node's panel in the editor lists what it can read.

A run reads its table a page at a time, oldest write first, up to the newest
write when it started, and takes each page through the graph in memory. A
Task node sends each row as its own request, as many at once as the model
takes, so the rows share batches and a row that fails fails alone, to the
node's `error` output with `_error` saying why. With nothing on that output
the run fails, after the pages it finished. The run's requests are low
priority: a client's request for the same model goes first. After each page
the run stores how far it read, so a run the service stopped under goes on
from there. With `only_new`, each run reads only the rows written since the
last one finished; `POST /workflows/{id}/reset` reads the whole table again.
Runs of a workflow never overlap: one may wait while one runs, and asking for
another while one waits gives that one. A trigger runs it on a cron
expression or every `interval_s` seconds, as a job's schedule does.

A saved workflow can also be a call: `POST /workflows/{id}/invoke` takes up to
100 rows in place of its table's, all at once, and answers each, in order,
with every node's result it got. It writes nothing (Table write nodes count
what they would write), so it takes the client key: several models and a
branch as one request. Only a workflow saved with `"invokable": true` takes
one (403 otherwise), since its SQL nodes read as the admin, the service's own
tables among them. With the workflow above, made invokable:

```bash
POST /workflows/{id}/invoke {"rows": [{"body": "spam offer"}, {"body": "lovely"}]}
{"rows": [{"body": "spam offer", "B": {"label": "SPAM", "confidence": 0.98, ...}},
          {"body": "lovely", "B": {"label": "POSITIVE", "confidence": 0.99, ...}}],
 "edges": [...], "nodes": {...}}
```

A row a node failed carries `_error`; the others go on. An error nobody
expected (a model's own) is named there by its type only, as in any answer of
the service, unless `DEBUG` is on; its text is in the log. Its requests go
before the low-priority ones of runs, as someone waits for them.

Each row a run writes says which run wrote it, in `_run` (a Table write's
`stamp: false` leaves it out). `GET /workflows/runs/{run}` has what made it:
the spec as it ran, and in each Task node's counts the `revisions`, the Hub
commits of the weights that answered (usually one). So a label in a table
can be traced to the model, and the version of it, that gave it:

```bash
GET /db/support/tagged/1        # {"id": "1", "label": "SPAM", "_run": "86527dd6dc504a75"}
GET /workflows/runs/86527dd6dc504a75
# {"spec": {...}, "nodes": {"B": {"rows_in": 100, ..., "revisions": ["989aa79..."]}}, ...}
```

The dashboard's **Workflows** section (`/admin#/workflows`) draws a workflow
on a canvas: add nodes from the palette, join an output to a node's input,
and click a node for a form of its settings. **Dry run** takes the first rows
through what is drawn, saved or not, labels each edge with the rows that went
along it and shows the rows as they left the node you click; a run picked in
the runs list shows its counts the same way.

## Enrichers: look values up in outside services

`POST /enrich {"value": "8.8.8.8"}` tells you what outside services know about
a value: its type is detected (IPv4, IPv6, CIDR, AS number, domain, URL, email,
phone, MD5/SHA-1/SHA-256, CVE, crypto wallet, or generic text), every enricher
that takes that type runs at once, and the reply holds each one's result and
the outputs merged. Like Maltego transforms, but each enricher is a small
definition, not code: an HTTP request with `{value}` (and `{secret}`, the API
key, and `{type}`) filled in, and `outputs` that pick fields from the JSON reply
with [JMESPath](https://jmespath.org) expressions.

```json
{
  "name": "abuseipdb",
  "inputs": ["ipv4", "ipv6"],
  "terms": "non_commercial",
  "secret": "ABUSEIPDB_KEY",
  "ttl": "24h",
  "rate_limit": "1000/day",
  "request": {
    "method": "GET",
    "url": "https://api.abuseipdb.com/api/v2/check",
    "query": {"ipAddress": "{value}", "maxAgeInDays": "90"},
    "headers": {"Key": "{secret}"}
  },
  "outputs": {"score": "data.abuseConfidenceScore", "reports": "data.totalReports", "is_tor": "data.isTor"}
}
```

The dashboard's **Enrichers** section (`/admin#/enrichers`) lists them, looks
any value up, and edits them: a form for the request and outputs, a test
button that shows the request sent (key masked) and the mapped result, and a
field that saves the API key, encrypted (see API keys below). Your own enrichers, and your
changes to the built-in ones, are kept in the database; deleting a changed
built-in restores it as shipped. Reverse DNS (`ptr`) is a
request too: the `reverse_ptr` value filter turns the address into its
`in-addr.arpa` name for a DNS-over-HTTPS query.

What runs: an enricher needs to be enabled, its terms must be in
`ENRICHER_TERMS`, and its key must be set unless it is marked optional. Each
result says why it did not run otherwise (`no_key`, `disabled`, `not_allowed`,
`rate_limited`). Results are cached per value for the enricher's `ttl`
(`refresh: true` skips the cache), its `rate_limit` is enforced before a
request goes out, and a 429 from the source pauses it. Requests go to public
addresses only (`ENRICH_ALLOW_PRIVATE=true` lifts that), since the templates
are editable. The source's raw reply is kept with the result and served to
the toolkit's own clients only.

Built in: ipwho.is, ipapi.is, IPinfo Lite, AbuseIPDB, Shodan InternetDB,
RIPEstat (network, abuse contacts, AS), RDAP for IPs and domains, DNS over
HTTPS (A, AAAA, MX, NS, TXT), crt.sh, URLhaus, ThreatFox, MalwareBazaar, CIRCL
hashlookup, CVE Services, EPSS, NVD, Blockstream Esplora (Bitcoin), Gravatar,
LeakCheck public, GLEIF, and reverse DNS. Each carries its `terms` (`open`,
`attribution`, `non_commercial` or `paid`) as published by the source in
October 2026: VirusTotal-style free tiers that forbid commercial use are
marked `non_commercial`, so a commercial deployment sets
`ENRICHER_TERMS=open,attribution,paid` and adds the paid ones it subscribes to.

## Traces and evals: debug requests, benchmark models

Every request leaves a trace: its input and output, kept `TRACE_RETENTION_DAYS` days (default 3; those older are deleted hourly) in the database beside its request's model, task, timing and outcome, which are kept `MONITORING_RETENTION_DAYS` (a trace never longer than its request). The admin's Activity & logs section lists the requests of any time range; click one for its input and output, or search the words in them (`GET /admin/requests?q=...`). Long strings are cut and media is kept as its size and hash only. Tag requests with an `X-Trace-Tag` header (comma-separated) and group them with `X-Session-Id` to find them again; the playground tags its own. Each request also says where it came from (`source`): `api` for an app's (a client key), `admin` for the admin key's (the playground, the admin's pages), `data` for a table's vectors and searches, `workflow` and `eval` for their runs; the Overview's router counts them so. Set `TRACE_RETENTION_DAYS=0` to keep no traces at all (those kept before are dropped too).

To compare models, save traces (or add examples by hand, or import JSON Lines) to a dataset in the Evals section, then run any model the service can load over it. A run sends its examples as many at once as the model takes: 32 to a classifier, which answers them in one forward pass instead of 32 (about nine times cheaper per text), as many as a model server runs at once, and one at a time to a model that answers one at a time, so a live request never waits behind a pile of the run's. An example is kept as given, so a run sends the model exactly that; a trace whose input was cut cannot become one. A run the service stopped under resumes by itself when it is back; one that failed or was cancelled resumes where it stopped when you ask. Score the outputs with metrics that need no model (exact match, contains, valid JSON; word error rate for transcripts; MRR and nDCG@10 for rerank outputs, whose reference lists the relevant documents' indexes, `[0, 3]`), or let an LLM judge score them against a rubric or pick the better of two runs' answers (asked in both orders, so position bias cancels). Scoring runs on [Pydantic Evals](https://pydantic.dev/docs/ai/evals/): the judge is any chat model the service serves, asked through its own `/v1`: one it runs, or one on another server (Ollama, vLLM, a provider) through a model server entry, so its key stays in the key store; it answers with a structured verdict, is retried while its endpoint fails, and an output it could not grade is marked so, not the whole run. A run of a classifier (text or zero-shot classification) also shows its classification report over the examples with a reference, with nothing to start: accuracy, macro F1, each label's precision, recall and F1, and the confusion matrix. Two runs compare example by example. Everything is also reachable as JSON under `/admin/datasets` and `/admin/runs` (see [STRUCTURE.md](../STRUCTURE.md)).

To label examples, give the dataset its labels (`positive, negative, neutral`) in its Label tab: it shows the examples without a reference one at a time, the least confident suggestion first. The suggestion is what the model a trace came from answered, or the answers of a run you pick, so a zero-shot or older classifier run over unlabeled examples pre-labels them, and the ones it was least sure of come first. Press a label's number, Enter to take the suggestion, or S to pass over one. Without labels you type each reference, starting from the suggestion: a corrected transcript, an answer. Any reference can be changed or taken away later from the Examples tab; a run's classification report follows at once, but its metric scores stay as they were scored until you score it again. The same through the API:

```bash
curl -X PATCH localhost:8000/admin/datasets/$DATASET -H "X-API-Key: $ADMIN_API_KEY" \
  -H 'Content-Type: application/json' -d '{"labels": ["positive", "negative"]}'
curl "localhost:8000/admin/datasets/$DATASET/to-label?run_id=$RUN&limit=20" -H "X-API-Key: $ADMIN_API_KEY"
curl -X PATCH localhost:8000/admin/datasets/$DATASET/examples/$EXAMPLE -H "X-API-Key: $ADMIN_API_KEY" \
  -H 'Content-Type: application/json' -d '{"reference": "positive"}'
```

To train a classifier on the labeled examples, press the dataset's Train button: it writes them as the training file of the `text-classifier` job and submits it with the params you pick (the base model, epochs, learning rate). The job fine-tunes the encoder, scores each epoch on a held-out share of each label, keeps the best, and its result names the model, `trained:<job id>`, served for `text-classification` like any classifier ([jobs/README.md](../jobs/README.md)). Then measure it: a run of `trained:<job id>` over another dataset (or the same, knowing it learned from it) shows the classification report, and a run of the classifier you had before over the same dataset compares with it.

```bash
curl -X POST localhost:8000/admin/datasets/$DATASET/training-file -H "X-API-Key: $ADMIN_API_KEY"
# {"path": "evals/<dataset id>.jsonl", "rows": 412, "labels": {"negative": 198, "positive": 214}}
curl -X POST localhost:8000/jobs -H "X-API-Key: $ADMIN_API_KEY" -H 'Content-Type: application/json' \
  -d '{"job": "text-classifier", "params": {"train_file": "evals/'$DATASET'.jsonl", "epochs": 3}}'
```

## Model servers

Generative models are best served by a tool built for them. A model ID can
be forwarded to a server you already run (Ollama, LM Studio, vLLM on another
box), started here on demand (MLX on Apple Silicon with `mlx_vlm.server` or
`mlx_lm.server`, vLLM on CUDA, `llama-server` for GGUF), or, as the last
resort, run in this process. One model can list several of those as
backends, tried in order. List them in `model_servers.toml` (copy
[model_servers.example.toml](../model_servers.example.toml)), or add them in
the Model servers tab of the admin's Settings:

```toml
[providers.ollama]
url = "http://127.0.0.1:11434"
on_evict = "unload"   # free Ollama's memory when this cache evicts the model

[models."qwen3.5-4b"]
task = "vision-language"

[[models."qwen3.5-4b".backends]]
provider = "ollama"
upstream_model = "qwen3.5:4b"

[[models."qwen3.5-4b".backends]]
cmd = "mlx_vlm.server --model mlx-community/Qwen3.5-4B-4bit --host 127.0.0.1 --port {port}"
upstream_model = "mlx-community/Qwen3.5-4B-4bit"
memory_gb = 3.5
```

When `qwen3.5-4b` loads, each backend is probed and the first that can serve
it is used until the model unloads: Ollama if it answers and lists
`qwen3.5:4b`, else the MLX server, which the service starts and waits for.
The choice is made at load time, not per request, because the memory the
model counts depends on it and a streamed reply cannot switch servers. If a
started server fails to start, the next backend is tried, and the failed one
is skipped for a minute. If Ollama stops answering later, the model is
dropped and the request runs again on the next backend. Each response names
the backend that answered (`backend`, `toolkit.backend` in `/v1` replies),
and so does its trace, so evals can tell Ollama's weights from MLX's. A
single `cmd` or `url` beside `task`, without `backends`, still works as one
backend.

The cache treats a model server like any other model: it counts `memory_gb`
against `MAX_MEMORY_GB` (a server elsewhere counts 0 unless it says), evicts
it when idle and room is needed, and stops a started server's process, which
frees all of its memory. If the process dies (a Metal abort, an
out-of-memory kill) only that model fails, and the next request starts it
again. On CUDA, `on_evict = "sleep"` uses vLLM's sleep mode, so an evicted
model wakes in seconds instead of reloading.

Each model server runs up to `max_concurrency` requests at once (default 8),
since those servers batch; in-process models run one at a time. Servers
given by `url` can be on other machines, which is how to spread models over
several GPUs or hosts behind this one gateway.

**Another toolkit.** One toolkit can be another's provider, for chat,
completions, embeddings, rerank and transcriptions (the five tasks an entry
takes). On the toolkit with the GPU, make a client key; on this one, add a
provider with that toolkit's URL and paste the key, then a model on it:

```toml
[providers.gpu-box]
url = "http://gpu-box:8000"

[models."qwen3:8b"]
task = "text-generation"

[[models."qwen3:8b".backends]]
provider = "gpu-box"
```

This one sees that the server is a toolkit, from its model list and from the
`toolkit` field of each reply, and names the other's backend after its own:
`backend` is `gpu-box: qwen3:8b → ollama: qwen3:8b`, in the reply, the
request log and the trace (`→ in-process` when the other ran it itself).
The Overview's Queues card marks the model `⇄ gpu-box`; its queue there is
only what waits here, and the other toolkit's Overview shows its own.

**Editing in `/admin`.** The Model servers tab of Settings lists the entries with
their backends in order, edits providers (with presets for Ollama, LM
Studio, llama.cpp, MLX LM, Jan, LocalAI, vLLM and SGLang at their default
ports) and entries, and has a Test button that probes the entry as edited and says
which backend a load would use. Changes are saved to
`model_servers.json` in `DATA_DIR`, never to the TOML file. A saved entry replaces the file's of the same name until it is
reverted, and takes effect at once: a loaded model whose entry changed is
unloaded once its requests finish. Commands run on this host, so the
dashboard can keep the `cmd` and `env` the file gives a model but not add
new ones, unless `MODEL_SERVERS_ALLOW_COMMANDS=true`.

**API keys.** To use a hosted service, pick it under Add hosted in the
Providers card (OpenAI, OpenRouter, Groq, Mistral, DeepSeek, Together,
Fireworks, xAI), paste its key and save. Then add a model on that provider,
with the service's own name for it as `upstream_model`. Any other
OpenAI-compatible server that asks for a key works the same way through
Other, with what comes before `/v1` as its URL.

The key is sent as a bearer token to that provider alone and never shown
again. A new key works from the next request, with no reload. To change a
provider's URL, remove its key first: it was given for the old one. A key set
in the environment instead goes with a provider in `MODEL_SERVERS_FILE`:
`/admin` will not make a new provider over a key that is already set. If a
provider refuses its key, the Test button says so.

Keys typed into `/admin` (providers and enrichers) are encrypted in
`DATA_DIR/secrets.json`, so a copied data folder or a backup gives none of them away.
The master key that opens them is kept elsewhere. On a desktop there is nothing
to set: the operating system's credential store keeps it. A container or a
headless server has none, so saving a key answers 503 until you set
`SECRETS_MASTER_KEY` in its real environment (`env.example` shows how to make
one). Lose the master key and you paste the keys in again. Encryption protects
copies of the files, not a running service, which needs the real keys to call
others.

An enricher names the variable that holds its key, and a remote the variables
of its secrets. Neither may name one of this service's settings, a provider's
key, or a variable the service's child processes get, but any other variable
in the service's environment they can, and their requests carry its value to
their own URLs: give the service only the keys an admin may use.

Jobs and the servers the service starts get only what a program needs of its
environment (paths, locale, proxies and certificates, the GPUs, the settings
of the ML runtimes and caches, the Hub token), never its settings or keys. A
server that needs more is given it in its entry's `env`.

**On a Mac, run the service natively, not in a container.** Containers on
macOS get no Metal GPU, so the Docker and Podman images run models on the
CPU there. MLX and MPS need host processes. A containerized service can
still forward to Ollama or LM Studio on the host at
`http://host.docker.internal:11434` (or `:1234`). In containers the file is
read from `/app/data/model_servers.toml` and edits are saved to
`/app/data/model_servers.json` (the data volume).

## Model memory

The cache budgets loaded models by their size, not by a process-wide figure.
Before a model loads, its size is estimated from its safetensors headers (no
download, nothing allocated), in the dtype it will load in, plus 20% for
inference. A model server counts its `memory_gb`. A model with no estimate
counts what loading actually took.

Room is made **before** loading: idle models are evicted, least recently used
first, until the new one fits `MAX_MODELS`, `MAX_MEMORY_GB` and the memory
actually free on the device (other processes included). So the old and new
model are never loaded together, and only one model loads at a time. A model
with a request in progress is never evicted, and neither is a pinned one
(`PINNED_MODELS`, or `pin` when loading). Nor is a model evicted the moment
it goes idle: it keeps its place for `MODEL_KEEP_S` seconds (default 3), since
its client's next request is usually on the way, and two clients taking turns
would otherwise cost a load on every request. When no model can go, a load
waits up to `MODEL_WAIT_TIMEOUT` seconds, then answers 503 with `Retry-After`
set from the busy models' recent request times. The same limit holds once the
model is loaded: a request that would wait longer than `MODEL_WAIT_TIMEOUT` for
it, behind the requests ahead (for a slot, or for a batch), is answered 503 at
once with `Retry-After`, rather than queued.

**Priority.** A request says how urgent it is with `X-Priority: high`, `normal`
or `low`; without the header it is `high`, since someone is waiting for the
reply. The eval runner and the judge send `normal`; send `low` for bulk work a
script can wait for. A waiting load takes a busy model's place when it
outranks that model's work: at once when its priority is higher, or after
`MODEL_TURN_S` seconds (default 15) when it is the same, so a model with steady
work cannot keep the room from another for ever. The busy model is drained:
its requests finish, new ones for it wait, and it is unloaded, then loaded
again when its turn comes. Running requests are never interrupted, and a
pinned model is never drained. Keep `MODEL_TURN_S` well above a model's load
time (each turn costs a load) and below `MODEL_WAIT_TIMEOUT`. `/models/status`
and `/admin` show each model's budgeted size, the device's real memory use,
what is free, and each model's queue and expected wait.

**Changing the limits.** The *Memory limits* card on `/admin` (or
`PUT /admin/limits` with the admin key, body `{"max_memory_gb": 6, "max_models": 2}`)
changes `MAX_MEMORY_GB` and `MAX_MODELS` at once: lower limits unload idle
models straight away, and models busy with a request stay until it finishes.
The values are saved to the settings file (`ENV_FILE`), so they last across
restarts, and the change shows in the dashboard's event feed. A real
environment variable still wins over the file on restart; the compose files
therefore leave both empty unless the host sets them, and the dashboard says
when one is set.

## Performance tips

1. **GPU Memory**: Set `MAX_MEMORY_GB` a little below what the GPU (or, on a Mac, the unified memory) can spare
2. **Model Selection**: Use smaller models for faster loading (e.g., `LiquidAI/LFM2.5-230M` instead of `Qwen/Qwen2.5-0.5B-Instruct`)
3. **Generation Speed**: Serve chat and vision models through a model server: MLX on Apple Silicon, vLLM on CUDA. Both batch concurrent requests
4. **Model Preloading**: Load frequently used models at startup

## Troubleshooting

### Out of Memory Errors
- Reduce `MAX_MODELS` or `MAX_MEMORY_GB`
- Set `memory_gb` for each model server, so the budget knows its size
- Use smaller models
- Ensure sufficient GPU memory

### 503 "every loaded model is busy"
- All `MAX_MODELS` slots are taken by models with requests in progress; raise `MAX_MODELS` or `MAX_MEMORY_GB`, or retry after `Retry-After` (seconds, from the busy models' recent request times). `GET /models/status` shows `loads_pending` and each model's `queued` and `estimated_wait_ms`

### Model Loading Failures
- Check internet connection for model downloads
- Verify model IDs are correct
- Check available disk space

### Performance Issues
- Monitor GPU memory usage
- Consider using CPU-only mode for smaller models
- Adjust cache settings based on usage patterns

### Podman Issues
- Ensure Podman is installed: `podman --version`
- For rootless mode, configure subuid/subgid: `podman system migrate`
- SELinux hosts: the `./data/images` bind mount carries the `Z` label; add it to any other bind mount you add

