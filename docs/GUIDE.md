# Core AI & Data toolkit: guide

The full reference behind the [README](../README.md): every endpoint, the
settings, and a section per feature. How the code is organised is in
[STRUCTURE.md](../STRUCTURE.md); every setting with its default is in
[env.example](../env.example).

- [Setup details](#setup-details)
- [API endpoints](#api-endpoints)
- [Configuration](#configuration)
- [Example usage](#example-usage)
- [Images and audio in chat](#images-and-audio-in-chat)
- [Tools and JSON replies](#tools-and-json-replies)
- [Completions, log probabilities and token counts](#completions-log-probabilities-and-token-counts)
- [Embeddings, reranking, transcription and model status](#embeddings-reranking-transcription-and-model-status)
- [Collections: documents with vector search](#collections-documents-with-vector-search)
- [Tables: namespaced JSON rows](#tables-namespaced-json-rows)
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
make down                            # stop; model weights and settings stay in named volumes
```

Model weights, the wizard's settings, request history, collections, jobs and
evidence captures (with their signing key) live in named volumes, so they survive `down`/`up` and image rebuilds. On an
NVIDIA host, uncomment the `deploy:` block in `docker-compose.yml` (it needs the
NVIDIA Container Toolkit). `JOB_ENVS=autogluon make up` also builds the AutoGluon
job environment into the image (several GB).

Without compose, mount at least the weights and settings so they persist:

```bash
docker build -t core-ai-data-toolkit .      # podman build -t core-ai-data-toolkit .
docker run -d -p 8000:8000 --name core-ai-data-toolkit \
  -v core-ai-data-toolkit-hf-cache:/app/.cache/huggingface \
  -v core-ai-data-toolkit-config:/app/data/config \
  core-ai-data-toolkit
```

`./podman-run.sh {build|run|stop|restart|logs|clean|shell}` does the same with
every volume for Podman.

### Local

Needs [uv](https://docs.astral.sh/uv/) and, for the browser pages, Node 22.12+.

```bash
make install    # uv sync (Python deps from uv.lock into .venv) + build the pages into api/static
make run        # python main.py; then open the setup link it logs
make dev        # the same with auto-reload
make test       # unit tests, Python and frontend (no model downloads, no torch needed)
make lint       # ruff (unused imports and variables, undefined names) and the frontend's tsc
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
npm run dev        # http://localhost:5173/admin, /admin/setup, /playground with hot reload;
                   # API calls go to the server on :8000 (AI_TOOLKIT_URL to change it)
npm test           # unit tests
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

## API endpoints

### Health Check
```bash
GET /health
```

### Run Inference
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
  "cold_start": false
}
```

Each task has a fixed contract: what `input` takes and what `result` holds,
whichever model serves it. A field the task does not take is rejected with 422.
`GET /tasks` returns every contract as JSON Schema, and `/docs` shows each
task's request and response.

| Task | `input` | `result` |
|------|---------|----------|
| `text-generation` | `text` (or `tokens`, the prompt as token IDs), optional `messages`, `tools`, `tool_choice`, `json_schema`, `stop`, `logprobs`, `echo`, `max_length`, `temperature`, `top_p`, `do_sample` | `text` (the new text only), `tool_calls`, `prompt_tokens`, `completion_tokens`, `logprobs` |
| `text-classification` | `text` | `label`, `confidence`, `scores` (every label) |
| `question-answering` | `question`, `context` | `answer`, `confidence`, `start_position`, `end_position` |
| `vision-language` | `text`, optional `image` (path inside `IMAGE_DIR`) or `messages` (a chat with OpenAI `image_url` / `input_audio` parts), and the generation fields | `text` |
| `rerank` | `query`, `documents`, optional `top_n` | `results` (`index`, `score`, best first), `tokens` |
| `decision` | `state` or `image` (base64), `questions` in vegaml's format | `answers`, `n_tokens` |

`max_length` is the number of new tokens to generate; the prompt does not count.
Left out, the model's own default applies. Prompts longer than the model's
context window are trimmed from the start.

### List Task Contracts
```bash
GET /tasks
```

### Load Model
```bash
POST /models/load
Content-Type: application/json

{
  "model_id": "AnkitAI/Sensible-ModernBERT-Sentiment-Analysis",
  "task": "text-classification"
}
```

### Get Cache Status
```bash
GET /models/status
```

Besides the loaded models and memory use, it reports `loads_pending` and,
per model, `in_use`, `queued` (requests waiting for a slot), `avg_request_ms`
and `estimated_wait_ms` (what the next request would wait), so a client can
pick the model with the shortest line.

### Pin a Model
```bash
POST /models/load   {"model_id": "Qwen/Qwen2.5-0.5B-Instruct", "task": "text-generation", "pin": true}
PUT  /models/{model_id}/{task}/pin   {"pinned": true}
```

A pinned model is never evicted by the cache, the idle timeout or a lower
limit; only `DELETE /models/{model_id}/{task}` (or `/models/clear`) removes
it. `PINNED_MODELS` pins at startup.

### Unload Model
```bash
DELETE /models/{model_id}/{task}
```

### Clear All Models
```bash
DELETE /models/clear
```

### List Available Models
```bash
GET /models/list
```

### Browse Models
```bash
GET /models/catalog                                  # each task's models: description, tags, status, on disk
GET /models/estimate?model_id=Qwen/Qwen2.5-0.5B-Instruct&task=text-generation   # memory needed, and what loading would unload (admin)
```

The *Browse models* button on `/admin` and the *Browse* buttons on
`/playground` show the same catalog as cards, a tab per task. Describe your own
models with a `ModelCard` (see [models/README.md](../models/README.md)), or with
`description` and `tags` in `MODEL_SERVERS_FILE`.

### Jobs
```bash
GET  /jobs/types                       # every job, its params schema, whether its environment is set up
POST /jobs/data   (multipart file)     # upload an input table to JOB_DATA_DIR
POST /jobs        {"job": "autogluon-tabular", "params": {"train_file": "churn.csv", "label": "churn"}}
GET  /jobs/{id}/events                 # follow it: status and log, as server-sent events
POST /inference   {"model_id": "autogluon:<id>", "task": "tabular-prediction", "input": {"rows": [...]}}
```

All need the admin key. See [jobs/README.md](../jobs/README.md) to add a job. In
containers, build with `JOB_ENVS=autogluon` (e.g. `JOB_ENVS=autogluon
make up`) to include the AutoGluon environment.

**Queued work and priority.** Jobs, eval runs, judging and Hub downloads
wait in one queue (`jobs/tasks.py`), each kind with a limit of its own:
`MAX_RUNNING_JOBS` jobs at once, one eval run, two judges, `MAX_HUB_DOWNLOADS`
downloads, so a long training job never holds up a run. `POST /jobs`,
`POST /jobs/schedules`, `POST /admin/runs`, `POST /admin/runs/{id}/judge` and
`POST /hub/downloads` take a `"priority"` of `"high"`, `"normal"` (the
default) or `"low"`. Each kind starts
the most urgent task first, then one whose model is loaded already, then the
oldest: the runs for the models in memory finish before another model has to
be loaded. An eval run's requests carry its priority, so live requests (high
unless they send `X-Priority`) go first when models compete for memory. Work
queued when the service stops is still queued when it starts again. An eval
run or a download the service stopped under goes back in the queue and goes on
where it stopped (a run skips the examples it answered, a download keeps its
files); after a crash it is resumed at most twice, so a run that brings the
service down cannot do it at every start. A job or a judge that was running is
marked failed (interrupted): it would start over from scratch.

#### Schedules and webhooks
```bash
POST /jobs/schedules   {"name": "nightly", "job": "autogluon-tabular", "params": {...}, "cron": "0 3 * * *"}
POST /jobs/schedules   {"job": "word-count", "params": {...}, "interval_s": 3600, "webhook_url": "https://example.com/hook"}
GET  /jobs/schedules                     # each with next_run, last_run and the last job's status
PUT  /jobs/schedules/{id}                # change it (the next run is recomputed); "enabled": false pauses it
POST /jobs/schedules/{id}/run            # run it now (202 with the job)
DELETE /jobs/schedules/{id}
GET  /jobs/webhooks                      # the configured URLs, whether a secret is set, the event names
POST /jobs/webhooks/test {"url": "..."}  # send a test event now and report the answer
```

A schedule runs a job on a five-field cron expression (`minute hour
day-of-month month day-of-week`, server local time; `*/15`, `1-5`, `1,15`
work as in cron) or every `interval_s` seconds (at least 60). The scheduler
checks every 15 seconds, and never overlaps a schedule with itself: a run that
comes due while the previous job is still going is skipped. A run missed while
the service was down is made once when it is back, not once per miss; a cron
run more than an hour late is skipped until its next time, so a nightly job
does not start at noon after a long downtime.
The dashboard's **Jobs** section (`/admin#/jobs`) edits schedules, submits
jobs, follows their progress and logs, and tests webhooks.

Webhooks: every finished job (`job.finished`) and eval run (`run.finished`)
is POSTed as `{"event", "time", "data"}` to the URLs in `WEBHOOK_URLS` and to
the `webhook_url` the job, schedule or run names itself (`POST /jobs`,
`POST /jobs/schedules` and `POST /admin/runs` take one). `X-Toolkit-Event`
names the event. Each message carries the [Standard Webhooks](https://www.standardwebhooks.com)
headers: `webhook-id` (the same across its retries, so a receiver can drop a
duplicate), `webhook-timestamp` and, with `WEBHOOK_SECRET` set,
`webhook-signature` (`v1,<base64 HMAC-SHA256 of "id.timestamp.body">`), which
any Standard Webhooks library verifies given the secret as
`whsec_<base64 of WEBHOOK_SECRET>`; `X-Toolkit-Signature` is still
`sha256=<HMAC-SHA256 of the body>`. Delivery retries three times over about
half a minute, then gives up with a warning in the log.

### Hub downloads and weights on disk
```bash
GET    /hub/search?q=qwen&task=text-generation&limit=30   # models on the Hub, most downloaded first, with on_disk
GET    /hub/repos/Qwen/Qwen2.5-0.5B-Instruct               # its files and sizes, total and weight bytes, license, gated
POST   /hub/downloads   {"repo_id": "Qwen/Qwen2.5-0.5B-Instruct"}   # download in the background (202); 409 if under way
GET    /hub/downloads                                       # queued, running and finished downloads with done_bytes / total_bytes
DELETE /hub/downloads/{id}                                  # cancel (partial files resume next time) or dismiss
GET    /hub/local                                           # repos in the Hub cache: size, last used, loaded, claimed tasks
DELETE /hub/local/Qwen/Qwen2.5-0.5B-Instruct                # delete its files; 409 while loaded or downloading
```

All need the admin key; the dashboard's **Hub** section (`/admin#/hub`) is
built on them. Downloads run in a child process each (`MAX_HUB_DOWNLOADS` at
once), so a cold start no longer has to wait for the weights. They are tasks
in the service's queue, so they need the database, and one queued or running
when the service stops goes on when it is back.

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
GPU memory. The dashboard's own history stays in the ArcadeDB database.

### Enrichers
```bash
POST /enrich            {"value": "8.8.8.8"}                       # every enricher for the value's type
POST /enrich            {"value": "example.com", "enrichers": ["rdap_domain", "dns_mx"], "refresh": true}
GET  /enrichers                                                    # the enrichers, and whether each can run
PUT  /enrichers/{name}  {...the definition...}                     # create or change one (admin)
PUT  /enrichers/{name}/secret  {"value": "the API key"}            # save its key to .env (admin)
POST /enrichers/{name}/test    {"value": "8.8.8.8"}                # run it now, uncached (admin)
DELETE /enrichers/{name}                                           # remove it, or a built-in's changes (admin)
DELETE /enrichers/cache                                            # forget cached results (admin)
```

See [Enrichers](#enrichers-look-values-up-in-outside-services) below.

### Evidence capture
```bash
GET  /evidence/workspaces                                          # the workspaces ("default" always exists)
POST /evidence/workspaces {"id": "<platform id>", "name": "Fraud ring"}  # a new workspace with its own ledger (admin; id optional)
GET  /evidence/cases                                               # the cases ("inbox" always exists)
POST /evidence/cases    {"name": "Case 42"}                        # a new case
GET  /evidence/cases/{id}/captures                                 # its captures, newest first
POST /evidence/captures  multipart: metadata=<JSON> file=@screenshot.png ...   # store a capture (see below)
GET  /evidence/captures/{id}                                       # the signed record, hash, signature, time stamp
GET  /evidence/captures/{id}/artifacts/{name}                      # one captured file
GET  /evidence/cases/{id}/annotations                              # tags and notes of the case's captures that have any
PUT  /evidence/captures/{id}/annotations  {"tags": [...], "notes": [{"id"?, "text"}]}  # replace either list (a key left out keeps it)
GET  /evidence/cases/{id}/todos                                    # the case's to-do list
PUT  /evidence/cases/{id}/todos  {"todos": [{"id"?, "text", "done"?}]}  # replace it (a todo left out is deleted)
GET  /evidence/verify                                              # re-check the workspace's whole ledger
GET  /evidence/public-key                                          # the signing key's public half, PEM
POST /evidence/timestamp-pending                                   # ask the TSA again for records without a token, all workspaces (admin)
```

Evidence is kept by workspace, the unit that is isolated, verified and exported: each has its own cases, its own hash chain and its own files, in its own `/db` namespace (`evidence` for the default workspace, `evidence_<id>` for the others, admin-only and read-only through `/db`) and under `EVIDENCE_DIR/workspaces/<id>/`. The routes about cases, captures and verifying take `?workspace=<id>`; without it they act in the default workspace, which holds everything captured before workspaces existed (so the browser extension, which names none yet, captures there). A workspace id is lower-case letters and digits in groups joined by single hyphens, up to 36 characters, so UUIDs serve as they are.

Annotations are tags and analyst notes added to a capture after it was taken. They are mutable and kept apart from the signed records, in the workspace's `annotations` table: nothing about them enters the record, its hash, the chain or `/evidence/verify`. `PUT` replaces the lists it names; tags are trimmed, their inner whitespace collapsed and repeats dropped case-insensitively (at most 64, 1–64 characters each); notes keep their `id` and `createdAt` when the body names a known id, get `updatedAt` when their text changed, and are new otherwise (at most 200, each up to 20000 characters). The reply, and each item of the `GET`, is `{captureId, tags, notes: [{id, text, createdAt, updatedAt}], updatedAt}`.

Todos are a case's to-do list, kept the same way in the workspace's `todos` table, one row per case. `PUT` replaces the list, in the order given; a todo keeps its `id` and `createdAt` when the body names a known id, gets `updatedAt` when its text changed and `doneAt` when it was checked (cleared when unchecked), and is new otherwise (at most 500, each up to 2000 characters; `done` is false when left out). The reply, and the `GET`, is `{caseId, todos: [{id, text, done, createdAt, updatedAt, doneAt}], updatedAt}`, with `updatedAt` null for a case whose list was never saved.

The `metadata` field is the extension's JSON: `caseId`, `artifacts` (each `{name, sha256, size?, mediaType?}`, hashed in the browser before upload) and whatever else it recorded (`page`, `network`, a note); one `file` field per artifact, named after it. The server re-hashes every file and refuses a mismatch, then builds the record (`evidence-capture/record/v3`; `v2` before workspaces): its `workspaceId`, the verified artifacts, the client metadata verbatim, the enrichers' findings about the page's server address and hosts, the receive time and the previous record's hash in the workspace's chain. Its RFC 8785 canonical bytes are hashed, Ed25519-signed and kept verbatim, in the database and as `EVIDENCE_DIR/captures/<id>/record.json` (`EVIDENCE_DIR/workspaces/<workspace>/captures/<id>/` in another workspace), with `record.sig`, `record.tsr` and the read-only `artifacts/`. Refusals come back as `{"detail": "...", "error": "..."}` (the `error` key is this API's; a body that does not validate is FastAPI's 422 `{"detail": [...]}`). A request with an `Origin` header must come from a browser extension (or one of `EVIDENCE_ALLOWED_ORIGINS`), so web pages cannot use these routes even with the key.

## Configuration

Set environment variables to configure the service:

- `MAX_MODELS`: Maximum number of models to cache (default: 3)
- `MAX_MEMORY_GB`: Memory budget for the loaded models, by their sizes (default: 8.0; 0 = no budget)
- `MODEL_WAIT_TIMEOUT`: Seconds a request may wait for its model, for room to load it or behind the requests ahead of it, before answering 503 (default: 120)
- `MODEL_IDLE_TIMEOUT`: Unload models unused for this many seconds (default: 0, never)
- `MODEL_KEEP_S`: Seconds an idle model keeps its place before another model may take it (default: 3)
- `MODEL_TURN_S`: Seconds a load waits before it may take a busy model's place from work of the same priority (default: 15; 0, never)
- `PRELOAD_MODELS`: Models to load at startup, comma-separated `model_id:task` (or `model_id` alone for the task a model file claims it for, else `text-generation`); the service answers while they load
- `PINNED_MODELS`: Like `PRELOAD_MODELS`, and never evicted
- `MODEL_SERVERS_FILE`: Models served by OpenAI-compatible servers, with backends tried in order (default: `model_servers.toml`)
- `MODEL_SERVERS_SAVED_FILE`: Where `/admin` saves its edits of them (default: `data/model_servers.json`; empty: read-only there)
- `MODEL_SERVERS_ALLOW_COMMANDS`: Let `/admin` add server commands, which run on this host (default: `false`)
- `MAX_MEDIA_BYTES`, `MAX_MEDIA_PARTS`: Limits on images and audio in a chat (default: 10 MB, 8)
- `MAX_PROMPT_TOKENS`, `MAX_COMPLETION_PROMPTS`: Token IDs a prompt given as tokens may hold, and prompts per `/v1/completions` request (default: 32768, 64)
- `MAX_HUB_DOWNLOADS`: Hub downloads running at once from `/admin` (default: 2)
- `WEBHOOK_URLS`, `WEBHOOK_SECRET`: URLs POSTed to when a job or eval run finishes, and the secret that signs the body (default: none)
- `HOST`: API host (default: 127.0.0.1, this machine only; the image uses 0.0.0.0)
- `PORT`: API port (default: 8000)
- `LOG_LEVEL`: Logging level (default: INFO)
- `TRACE_RETENTION_DAYS`: Days each request's input and output are kept as traces for debugging and datasets (default: 3; 0 keeps none)
- `MONITORING_RETENTION_DAYS`: Days of the dashboard's request history, token counts and model events, kept in the ArcadeDB database (default: 7; 0 records nothing; `/metrics` works either way)
- `JUDGE_MODEL`, `JUDGE_BASE_URL`, `JUDGE_API_KEY`: The default LLM judge for benchmark runs (a model this service runs, or an OpenAI-compatible endpoint; the key is sent to that endpoint only)
- `ENRICHERS_DIR`: Your own enrichers (default: `data/enrichers`; empty turns enrichers off); their results are cached in the ArcadeDB database
- `ENRICHER_TERMS`: Sources that may run, by their terms: `open`, `attribution`, `non_commercial`, `paid` (default: all)
- `ENRICH_ALLOW_PRIVATE`: Let enrichers call private addresses (default: false)

## Example usage

These leave the key out. With `API_KEY` set, pass
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
(a string or up to 8) ends the reply before the first match. The `/playground`
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
L2-normalized vector per input. Any Hub encoder works: pooling follows the
model's sentence-transformers config (mean for all-MiniLM, CLS for BGE).

```bash
curl localhost:8000/v1/embeddings -H 'Content-Type: application/json' \
  -d '{"model": "ibm-granite/granite-embedding-small-english-r2", "input": ["A cat sits on the mat.", "Markets fell."]}'
```

`POST /v1/rerank` takes Cohere's and Jina's request (`query`, `documents` as
strings or `{"text": ...}`, optional `top_n` and `return_documents`) and
returns the documents' indexes with a `relevance_score`, best first. Any Hub
cross-encoder works (BGE reranker, GTE reranker, MiniLM MS MARCO); the
scores are probabilities, so a threshold holds across requests.

```bash
curl localhost:8000/v1/rerank -H 'Content-Type: application/json' -d '{
  "model": "BAAI/bge-reranker-v2-m3", "query": "How do I reset my password?",
  "documents": ["Open Settings and choose Reset password.", "Our office hours are 9 to 5."], "top_n": 1
}'
```

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

## Collections: documents with vector search

`/collections` stores documents with their embeddings and your own typed
fields, and searches them by meaning, optionally narrowed by a SQL filter on
those fields. The data lives in [ArcadeDB](https://github.com/ArcadeData/arcadedb),
**embedded** in this process (the `arcadedb-embedded` package bundles its own
Java runtime): no separate database server. It is stored in `ARCADEDB_PATH`
(default `data/arcadedb`; a volume in the containers) and opened on the first
collection request, which takes about two seconds.

```bash
# Create (admin key): every item's text is embedded with this model
curl localhost:8000/collections -H 'Content-Type: application/json' -d '{
  "name": "kb", "model": "ibm-granite/granite-embedding-small-english-r2",
  "fields": {"tenant": "STRING", "year": "INTEGER", "tags": "LIST"}}'

# Add items (an existing id is replaced)
curl localhost:8000/collections/kb/items -H 'Content-Type: application/json' -d '{"items": [
  {"id": "r1", "text": "Refunds are issued within 14 days.", "tenant": "acme", "year": 2025, "tags": ["billing"]}]}'

# Search by meaning, among the items the SQL condition selects
curl localhost:8000/collections/kb/search -H 'Content-Type: application/json' -d '{
  "query": "how do I get my money back?", "k": 5,
  "where": "tenant = :t AND year >= 2024", "params": {"t": "acme"}}'
```

| Endpoint | Key | What it does |
|---|---|---|
| `GET /collections` | client | Collections with model, fields and item count |
| `POST /collections` | admin | Create: `name`, `model`, `fields` (`STRING`, `INTEGER`, `LONG`, `FLOAT`, `DOUBLE`, `BOOLEAN`, `LIST`), `quantization` (`NONE` or `INT8`) |
| `DELETE /collections/{name}` | admin | Drop it and its items |
| `POST /collections/{name}/items` | client | Embed and store up to 1000 items |
| `GET`, `DELETE /collections/{name}/items/{id}` | client | One item |
| `POST /collections/{name}/search` | client | `query`, `k` (≤ 100), optional `where` and `params`; nearest first, `distance` 0 is identical |
| `POST /collections/{name}/query` | client | Items `where` selects, without vector search, `limit` / `skip` |

`where` is an [ArcadeDB SQL](https://docs.arcadedb.com/) condition on the
declared fields, with `:name` placeholders filled from `params` (e.g.
`'billing' IN tags`). It runs read-only, so it can filter but never change
data. Every declared field is indexed. Any client with the client key can
read every collection.

Embedded means one process: run with `WORKERS=1` (the default), since a
second process cannot open the same database. ArcadeDB's JVM shares memory
with the models; `ARCADEDB_HEAP` (default `1g`) caps it.

## Tables: namespaced JSON rows

`/db` stores plain JSON rows in tables, grouped in namespaces, in the same
embedded ArcadeDB database as the collections. An admin creates namespaces
and tables, and edits each table's schema, in the dashboard's **Data**
section (`/admin#/data`), which also lets you browse, filter, sort, edit and
delete rows. Clients write to `/db/<namespace>/<table>`; a table also appears
on its first write. A row is any JSON object with a string `id`, within its
table's schema, which is empty (anything goes) until an admin declares one.
Each table and namespace reports its `size_bytes` on disk: the table's
bucket file plus one file per index. ArcadeDB grows them a page at a time
(64 KB for a bucket, 256 KB for an index), so an empty table already has
a few hundred KB, and deleting rows frees space for later writes without
shrinking the files.

```bash
curl -X PUT localhost:8000/db/shop/products/p1 -H 'Content-Type: application/json' \
  -d '{"name": "Milk", "price": 1.2, "tags": ["dairy", "sale"]}'
curl 'localhost:8000/db/shop/products?tags=sale&_sort=-price&_limit=50'
```

| Endpoint | Body | Response |
|---|---|---|
| `GET /db` | | `{namespaces}`, each with its tables, row counts and sizes |
| `POST /db` (admin) | `{name}` | `201` + the namespace; `409` if it exists |
| `GET /db/{ns}` | | `{name, size_bytes, tables: [{name, count, size_bytes, strict, graph, fields}], edges: [{name, count, size_bytes}]}` |
| `POST /db/{ns}` (admin) | `{name, fields?, strict?, graph?}` | `201` + the empty table (a graph table unless `graph: false`); `409` if it exists |
| `DELETE /db/{ns}` (admin) | | `204`; drops every table in it |
| `POST /db/{ns}/_transaction` | `{puts?: {table: rows}, deletes?: {table: ids}, edges?, delete_edges?}` | `204`; all applied, or none |
| `GET /db/{ns}/{table}` | | `{rows, next?}`; see the parameters below |
| `GET /db/{ns}/{table}/_count` | | `{count}`; takes the same filters |
| `POST /db/{ns}/{table}` | row | `201` + the row, with `Location`; a missing id is made (UUIDv7); `409` if taken |
| `DELETE /db/{ns}/{table}` (admin) | | `204`; drops the table |
| `GET /db/{ns}/{table}/_schema?_sample=500` | | `{fields, strict, observed}` |
| `PUT /db/{ns}/{table}/_schema` (admin) | `{fields, strict?}` | `200` + the schema; `400` if rows there now do not fit |
| `POST /db/{ns}/{table}/_bulk-get` | `{ids}` | `{rows}` in the order asked; missing ids left out |
| `PUT /db/{ns}/{table}/_bulk` | `{rows}` | `204`; upserts, all or none |
| `POST /db/{ns}/{table}/_bulk-delete` | `{ids}` | `204`; all or none |
| `GET /db/{ns}/{table}/{id}` | | the row, or `404` |
| `PUT /db/{ns}/{table}/{id}` | row | `201` created or `200` replaced, + the row |
| `PATCH /db/{ns}/{table}/{id}` | partial row | `200` + the merged row, or `404` |
| `DELETE /db/{ns}/{table}/{id}` | | `204`, also when there was no row |

Everything without "(admin)" takes the client key, except in the
service's own namespaces (`evals`, `jobs`, `enrichers`, `evidence` and its
workspaces' `evidence_<id>`, `playground`): those need the admin key for
every request, and are read-only here, also while the part of the service
that keeps one is switched off. The list takes
`field=value` filters, which must all hold; a list field matches when it
holds the value. On a declared field the value is read as the field's type.
On other fields it is JSON when it parses as a number, `true`, `false` or
`null` (quote text that looks like one: `code="30"`), else text. `_sort` takes
comma-separated fields, `-field` for descending (default: `id`); `_limit`
takes 1 to 1000 (default 100); `_cursor` takes the previous page's `next`.
`PATCH` is [JSON Merge Patch](https://www.rfc-editor.org/rfc/rfc7396): `null`
removes a field, and objects merge. Bulk requests and transactions take up
to 1000 rows and ids.

**Schemas.** A table's schema lists fields, each
`{name, type, list?, required?, indexed?, unique?}`:

- `type`: `string`, `integer`, `number`, `boolean`, `object` (a JSON
  object) or `any`; with `list: true`, a list of that type, and
  `field=value` finds rows whose list holds the value.
- `required`: every row has it, not null. Otherwise it may be left out.
- `indexed`: filters and sorts on it use an index instead of reading every
  row. Worth it for the fields clients filter or sort on in big tables; only
  `string`, `integer`, `number` and `boolean` fields can be indexed.
- `unique`: no two rows hold the same value (`409`); rows without it are
  fine. Implies `indexed`; not for lists.

`strict: true` also refuses fields the schema does not declare. Writes that
break the schema get `400`. The service checks rows itself because ArcadeDB
would silently convert (`2.7` into an integer field would be stored as
`2`). `PUT _schema` replaces the whole schema: it first checks the rows
already there against what it adds, and if any break it, answers `400`
naming a few and changes nothing. Fields left out lose their rules and
index, never their values. It never changes whether the table is a graph
table (below). `GET _schema` also returns `observed`: which
fields the newest rows hold, in how many, with which types; the dashboard
shows it beside the declared fields, with a button to declare each. A
`number` field returns whole numbers as `3.0`. `id` is built in: a string,
indexed and unique.

Names are letters, digits and single underscores, starting with a letter.
Ids are strings without `/`. The ids `POST` makes are UUIDv7 as 32 hex
digits: they sort in creation order, so lists (sorted by `id` unless
`_sort` says otherwise) show rows oldest first, and new rows go at the end of
the `id` index rather than all over it. The first 12 digits are the creation
time in milliseconds, so anyone who sees an id can tell when its row was made. Neither may start with `_`, which marks the
API's own path segments and parameters (`_count`, `_bulk`, `_limit`...), so
they never collide with a table or a row. The routes live under `/db`
rather than at the root, so a namespace can never shadow `/admin`, `/v1` or
the others.

### Graph tables: rows joined by edges

Tables are graph tables: ArcadeDB vertex types, whose rows are the graph's
nodes, rather than document types. That holds for a table made by
`POST /db/{ns}` (where `graph` defaults to `true`), by a client's first
write, or by a schema put before any row; only `graph: false` makes a plain
table, as do the service's own stores (evals, jobs and the like) for theirs.
A graph table's rows are read and written exactly like any other table's,
with the same filters, schemas and transactions, but they can also be
joined by **edges**. The kind is chosen when a table is created and cannot
change later: to link the rows of a plain table (one made with
`graph: false`, or before tables were graph tables), create a table and
copy them over. The same embedded database holds both kinds,
so a graph table and a plain one can sit side by side in one namespace, and a
row of either can hold a plain reference (such as another row's id) to the
other; only edges need graph tables at both ends.

An edge has a type, goes from one row to another (`table/id`, both in graph
tables of the same namespace) and carries properties of its own. One edge
of a type joins a given pair of rows, so storing it again replaces its
properties. Edge types appear on the first edge of their name, or empty
when an admin makes one (`POST /db/{ns}/_edges`), and are listed per
namespace; an edge type cannot share a table's name. Deleting a row deletes
its edges, and dropping a graph table or an edge type deletes the edges with
it.

```bash
curl -X PUT localhost:8000/db/crm/_edges -H 'Content-Type: application/json' -d '{"edges": [
  {"type": "knows", "from": "people/ann", "to": "people/bob", "since": 2020},
  {"type": "works_at", "from": "people/ann", "to": "companies/acme", "role": "CTO"}]}'

# Ann's edges, the rows next to her, and everyone within two "knows" steps of her
curl 'localhost:8000/db/crm/people/ann/_edges'
curl 'localhost:8000/db/crm/people/ann/_neighbors?_type=works_at'
curl 'localhost:8000/db/crm/people/ann/_traverse?_type=knows&_depth=2'

# A read-only query, in ArcadeDB SQL or Cypher, naming tables and edge types as above
curl localhost:8000/db/crm/_query -H 'Content-Type: application/json' -d '{
  "language": "cypher", "params": {"n": "Ann"},
  "query": "MATCH (p:people)-[:works_at]->(c:companies) WHERE p.name = $n RETURN p.id, c.name"}'
```

| Endpoint | Body | Response |
|---|---|---|
| `GET /db/{ns}/_edges` | | `{edges: [{name, count, size_bytes, joins: [{from, to, count}]}]}`, the namespace's edge types with the pairs of tables their edges join, most edges first; it reads every edge, which `GET /db/{ns}` does not |
| `POST /db/{ns}/_edges` (admin) | `{name}` | `201` + the empty edge type; `409` if it exists or a table has its name |
| `PUT /db/{ns}/_edges` | `{edges: [{type, from, to, ...properties}]}` | `204`; stores them, all or none; `400` if an end is not a row of a graph table |
| `POST /db/{ns}/_edges/_delete` | `{edges: [{type, from, to}]}` | `204`; all or none; edges not there are ignored |
| `DELETE /db/{ns}/_edges/{type}` (admin) | | `204`; drops the edge type with its edges |
| `GET /db/{ns}/{table}/{id}/_edges` | | `{edges, next?}`: the row's edges, `_type` (repeatable), `_direction` (`out`, `in`, `both`), `_limit`, `_cursor` |
| `GET /db/{ns}/{table}/{id}/_neighbors` | | `{neighbors: [{table, row, edge}], next?}`: the rows one edge away, same parameters |
| `GET /db/{ns}/{table}/{id}/_traverse` | | `{rows: [{table, depth, row}]}`: rows within `_depth` edges (1 to 10, default 2), nearest first, each once; `_type`, `_direction` (default `out`), `_limit` |
| `POST /db/{ns}/_query` (admin) | `{language?, query, params?, limit?}` | `{rows}`: up to `limit` (≤ 1000) rows of a read-only `sql` (default) or `cypher` query |
| `GET /db/{ns}/_graph` | | `{nodes: [{table, row}], edges, tables, outside, more}`: the graph tables' first rows (`_table`, repeatable; all when none; `_limit` ≤ 5000 shared among them) and the edges of the `_type`s named (all when none; `_edge_limit` ≤ 20000) that join two of them; `outside` counts edges to rows not loaded, `more` says whether a limit cut the result |

**In the dashboard.** The Data section uses everyday words, as Access and
Notion do: tables, and **relationships** for edge types (their edges are
relationships too, as in Neo4j). A namespace lists its **Tables** and its
**Relationships**, and its **New** form makes either; a plain table, which
relationships cannot join, is marked so. A table's page has its rows
filtered by a Cypher condition (`n.age > 30`, `(n)--()` for rows with
relationships); a row opens with its relationships listed, each linked to
its type and to the row at the other end. A relationship's page lists its
edges (from, to and their properties) filtered by a condition on `e`, the
row `a` it goes from and the row `b` it goes to, and names the tables it
joins (`people → companies`). The graph itself is drawn in one place, the
**Graph query**; the table, relationship and row pages each open it at
what they show.

**Querying graph data.** The quick reads above cover most needs: a row's
edges, its neighbors with the edge each is reached by, and a breadth-first
traversal that reports how far each row is. For anything else, `_query`
takes a query over plain and graph tables alike; the dashboard's Data
section has two query pages per namespace, **SQL query** over every table
and **Graph query** in Cypher over the graph tables, each with example
queries, the namespace's names a click away, and the result as a grid to
export as JSON or CSV.

**Seeing the graph.** The Data section's **Graph query** draws what a
Cypher query returns (rows, edges and paths), and before a query runs, a
sample of the graph tables' rows and the edges among them, with sigma.js,
each table in its own color.
`GET /db/{ns}/_graph` loads a sample (the first rows of each table, the
limit shared among them); double-clicking a row loads its neighbors
(`_neighbors`), so a graph is explored outward from where it matters. The
filters run in the browser, as in Neo4j Browser and Bloom: the legend
switches tables and edge types off; rules on a field (`age ≥ 30`,
`tags contains lead`, `email missing`, on any table or one of them; the
`degree` and `table` of a row count as fields) gray out the rows that fail
them, or hide them; a search highlights the rows whose values contain it;
and isolated rows can be dropped. Rows are labeled by a field of your choice
(`name` or `title` when most rows have one), sized by degree or all alike,
and the view exports as JSON. `_query` takes [ArcadeDB SQL](https://docs.arcadedb.com/#SQL) (`out('knows')`,
`in()`, `both()`, `MATCH` patterns, `TRAVERSE`, `shortestPath()`) or
[Cypher](https://docs.arcadedb.com/#Open-Cypher) (`MATCH (a)-[:knows*1..3]->(b)`).
Write the query with the namespace's table and edge type names: where the
query names a type, a name of the namespace's is turned into the underlying
type before the query runs. In SQL that is after `FROM` or `INSTANCEOF`, a
MATCH's `type:`, the quoted names in `out('knows')`, `in()`, `both()`,
`outE()`, `inE()` and `bothE()`, and `@type = 'people'` or
`@type IN [...]`; in Cypher a label or relationship type (`(n:people)`,
`[e:knows|likes]`, `WHERE n:people`) and the quoted names compared with
`type(e)` or found `IN labels(n)`. Anywhere else a field, an alias, a value
or a keyword called like a table keeps its name. Pass values through
`params` (`:name` in SQL, `$name` in Cypher) all the same. Rows and edges
returned whole come back as the API shows them: a row as `{table, row}`, as
the graph reads above give it, and an edge with `type`, `from` and `to`. The query runs read-only: one that would
change data is refused with `400`, and Gremlin is not available in the
embedded build. `_query` needs the admin key: names resolve within the
namespace, but SQL and Cypher can still reach any table, record ID or
bucket in the database (`MATCH (n)` matches every graph table's rows), so
clients use the reads above instead.

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
field that saves the API key to the `.env` file. Your own enrichers, and your
changes to the built-in ones, are one JSON file each in `ENRICHERS_DIR`;
deleting a built-in's file restores it as shipped. Reverse DNS (`ptr`) is a
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

Every request leaves a trace: its input, output, model, task, timing and outcome, kept at least `TRACE_RETENTION_DAYS` days (default 3; a day's traces are dropped together once the whole day is older than that) in the ArcadeDB database. The admin's Activity & logs section lists the requests of any time range; click one for its input and output, or search the words in them. Long strings are cut and media is kept as its size and hash only. Tag requests with an `X-Trace-Tag` header (comma-separated) and group them with `X-Session-Id` to find them again; the playground tags its own. Set `TRACE_RETENTION_DAYS=0` to keep no traces at all (those kept before are dropped too).

To compare models, save traces (or add examples by hand, or import JSON Lines) to a dataset in the Evals section, then run any model the service can load over it. A run sends its examples as many at once as the model takes: 32 to a classifier, which answers them in one forward pass instead of 32 (about nine times cheaper per text), as many as a model server runs at once, and one at a time to a model that answers one at a time, so a live request never waits behind a pile of the run's. An example is kept as given, so a run sends the model exactly that; a trace whose input was cut cannot become one. A run the service stopped under resumes by itself when it is back; one that failed or was cancelled resumes where it stopped when you ask. Score the outputs with metrics that need no model (exact match, contains, valid JSON), or let an LLM judge score them against a rubric or pick the better of two runs' answers (asked in both orders, so position bias cancels). Scoring runs on [Pydantic Evals](https://pydantic.dev/docs/ai/evals/): the judge is a chat model behind an OpenAI-compatible endpoint, or one this service runs (asked through its own `/v1`), answers with a structured verdict, is retried while its endpoint fails, and an output it could not grade is marked so, not the whole run. Two runs compare example by example. Everything is also reachable as JSON under `/admin/datasets` and `/admin/runs` (see [STRUCTURE.md](../STRUCTURE.md)).

## Model servers

Generative models are best served by a tool built for them. A model ID can
be forwarded to a server you already run (Ollama, LM Studio, vLLM on another
box), started here on demand (MLX on Apple Silicon with `mlx_vlm.server` or
`mlx_lm.server`, vLLM on CUDA, `llama-server` for GGUF), or, as the last
resort, run in this process. One model can list several of those as
backends, tried in order. List them in `model_servers.toml` (copy
[model_servers.example.toml](../model_servers.example.toml)), or add them in
the admin's Model servers section:

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

**Editing in `/admin`.** The Model servers section lists the entries with
their backends in order, edits providers (with Ollama and LM Studio presets)
and entries, and has a Test button that probes the entry as edited and says
which backend a load would use. Changes are saved to
`MODEL_SERVERS_SAVED_FILE` (default `data/model_servers.json`), never to the
TOML file. A saved entry replaces the file's of the same name until it is
reverted, and takes effect at once: a loaded model whose entry changed is
unloaded once its requests finish. Commands run on this host, so the
dashboard can keep the `cmd` and `env` the file gives a model but not add
new ones, unless `MODEL_SERVERS_ALLOW_COMMANDS=true`.

**On a Mac, run the service natively, not in a container.** Containers on
macOS get no Metal GPU, so the Docker and Podman images run models on the
CPU there. MLX and MPS need host processes. A containerized service can
still forward to Ollama or LM Studio on the host at
`http://host.docker.internal:11434` (or `:1234`). In containers the file is
read from `/app/data/config/model_servers.toml` and edits are saved to
`/app/data/config/model_servers.json` (the config volume).

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

