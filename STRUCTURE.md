# Core AI & Data toolkit: how it is built

## Overview
The Core AI & Data toolkit is a FastAPI service that runs Hugging Face models locally. It loads models on demand, keeps a small LRU cache of them budgeted by model size, and exposes them through its own REST API and an OpenAI-compatible API (chat with images and audio included). A model can also be served by an OpenAI-compatible server: one that already runs (Ollama, LM Studio, a GPU box), or its own process (MLX, vLLM, llama.cpp), which the cache starts and stops like any other model; such a model lists backends tried in order when it loads, the last resort being in-process Transformers. An admin dashboard at `/admin` shows request metrics and loaded models, and edits what the service keeps: model servers, enrichers, jobs, and the `/db` workspaces of tables of JSON rows and buckets of files, with views (saved reads with parameters, drawn as tables and charts, which client keys run) and dashboards of them. A playground in `/admin` has a tab per category of task: Chat chats with a model, or sends the same chat to several and compares their replies; the others run one model of the chosen task. Long-running scripts, such as training AutoGluon predictors or fine-tuning a text classifier, run as background jobs in their own process and Python environment (`/jobs`), and the models they train are served like any other. Enrichers (`/enrich`) look a value such as an IP address, domain or file hash up in outside services, each a small editable definition of one HTTP call. Evidence capture (`/evidence`) keeps signed, hash-chained records of the web pages a browser extension captures.

What a module does, and why, is its docstring; this file is the map, the routers, and how the parts work together.

## Directory Structure

One line per folder; each module's docstring says what the module is. A new folder gets its line here
(`tests/test_docs.py` fails without it).

```
ai_toolkit/
├── main.py                   # Entry point (uvicorn)
├── core/                     # What every package may import, importing none of them: settings, refusals, priorities, periodic work, child processes; PROJECT_ROOT
├── db/                       # Embedded DuckDB: the Database and the store bases (engine.py, specs.py), and the /db workspaces of tables, edges and vectors (tables.py, graph.py)
├── tasks/                    # The one queue of background work (runner.py), its schedules and webhooks
├── models/                   # Model management (see models/README.md): the task contracts, the registry, the cache budgeted by model size, the manager and its batching
│   ├── families/             # The model files, a subfolder per family: generic/ (any Hub ID), vision/, decision/, autogluon/ and trained/ (what jobs trained), custom/ (the smallest example); a new model is a new file
│   └── servers/              # Models served by OpenAI-compatible servers, each with backends tried in order
├── jobs/                     # Long-running scripts in their own process and environment (see jobs/README.md): the job files, their kind of task, their files
│   ├── autogluon/            # The AutoGluon jobs and the process that serves their predictors
│   ├── envs/                 # The setup-env job, which installs a job environment
│   └── train/                # Fine-tuning small Hugging Face models: the text-classifier job
├── evals/                    # Datasets, runs and scores, with the LLM judge
├── evidence/                 # Signed, hash-chained records of captured web pages, per workspace
├── enrichers/                # Looking values up in outside services, each enricher a JSON spec of one HTTP call
│   └── builtin/              # The shipped enrichers, one JSON file each
├── monitoring/               # The request log with its traces, model events, the audit log, host and GPU figures
├── playground/               # The playground's saved conversations and prompt presets
├── buckets/                  # The files of the /db buckets, and their copies at remotes outside the service
├── workflows/                # A table's rows through a graph of nodes, a kind of task (nodes.py: the node types)
├── access/                   # The client keys an admin makes, one per server or machine, and what each may reach (grants.py)
├── views/                    # Views and dashboards: saved reads over a workspace, run within limits
├── api/                      # FastAPI: the app and its wiring (app.py: create_app), what routes depend on (deps.py), one *_routes.py per router (see API below); static/ is built by frontend/ and not committed
│   └── responses/            # The response models, one module per area
├── frontend/                 # The browser pages: React + TypeScript, built with Vite into api/static (see Browser Pages); openapi.json is the schema the types come from
│   ├── e2e/                  # The smoke test (npm run test:e2e), against a service of its own (serve.sh)
│   └── src/
│       ├── shared/           # What every page uses: the typed client (api.ts), queries and actions, the shell and its menu (nav.ts), the shared pieces
│       │   ├── model-browser/   # The model browser: a page in /admin, a dialog elsewhere
│       │   ├── styles/       # The styles besides modern (globals.css), one CSS file each
│       │   └── task-io/      # A task's inputs and outputs, for trying a model
│       ├── admin/            # The dashboard: each section's view (sections.tsx), the layout they share (layout.tsx), the one-file sections; a section with parts has a folder:
│       │   ├── activity/, audit/, autogluon/, data/, enrichers/, evals/, evidence/, jobs/, keys/, overview/, playground/, remotes/, servers/, workflows/   # its view <Name>Section.tsx, its parts, and its logic in <name>.ts beside its test
│       │   └── data/tables/, data/graph/, data/views/, data/query/, data/buckets/, data/diagram/   # the Data section's areas, each with its own logic; what they share stays in data/ (data.ts, DataRows.tsx)
│       ├── setup/            # The first-run wizard
│       ├── components/ui/    # shadcn/ui components (Tailwind v4 + Radix), owned and edited here
│       └── lib/              # shadcn's class-name helper
├── scripts/                  # export_openapi.py (frontend/openapi.json), setup_job_env.sh (a job environment), make_zip.sh (`make zip`)
├── tests/                    # pytest with fake models (no downloads or torch), and the guard tests (AGENTS.md, Checks)
├── data/                     # Default data folders, not committed: the database, jobs, datasets, images, evidence, the buckets' files
├── envs/                     # Job environments, not committed, e.g. envs/autogluon
└── env.example, model_servers.example.toml, pyproject.toml, uv.lock, requirements*.txt, Makefile, Dockerfile, docker-compose.yml, podman-run.sh
```

## Tasks

| Task | Built-in models | Input → result | Endpoint |
|------|-----------------|----------------|----------|
| `text-generation` | LFM2.5, Granite 4.1, Qwen2.5, GPT-2, or any Hub causal LM | `text` (+ `messages`, generation fields) → `text` | `/inference`, `/v1/chat/completions` |
| `text-classification` | ModernBERT and mmBERT sentiment, DistilBERT SST-2, any Hub classifier, `trained:<job id>` from the `text-classifier` job | `text` → `label`, `confidence`, `scores` | `/inference` |
| `zero-shot-classification` | DeBERTa-v3 and ModernBERT zero-shot v2.0, mDeBERTa XNLI, BART MNLI, any Hub NLI model | `text`, `labels` (+ `multi_label`, `hypothesis`) → `label`, `confidence`, `scores` | `/inference` |
| `question-answering` | deepset DeBERTa-v3 and TinyRoBERTa SQuAD 2.0, DistilBERT SQuAD, any Hub QA model | `question`, `context` → `answer`, `confidence`, positions | `/inference` |
| `embedding` | Granite Embedding R2 (English, multilingual), GTE ModernBERT, any sentence-transformers model | `texts` → `embeddings`, `tokens` | `/v1/embeddings`, `/inference` |
| `rerank` | BGE reranker v2 m3, GTE reranker ModernBERT, mxbai rerank, any cross-encoder | `query`, `documents` (+ `top_n`) → `results` (`index`, `score`), `tokens` | `/v1/rerank`, `/inference` |
| `speech-to-text` | Moonshine tiny and base, Parakeet TDT v3, Qwen3-ASR | `audio` (+ `language`, `prompt`) → `text`, `language` | `/v1/audio/transcriptions`, `/inference` |
| `vision-language` | Qwen3.5, LFM2.5-VL, MiniCPM-V 4.6, SmolVLM2, Qwen3-VL, Apple FastVLM | `text`, `image` (+ generation fields) → `text` | `/vision-language`, `/inference` |
| `decision` | vegaml (`vega-0.8b`, `vega-4b`) | `state` or `image`, `questions` → `answers`, `n_tokens` | `/v1/decisions`, `/inference` |
| `custom` | `custom:*` echo example | `text` → `text` | `/inference` |
| `tabular-prediction` | `autogluon:<job id>` from the `autogluon-tabular` job | `rows` (+ `probabilities`) → `predictions`, `probabilities` | `/inference` |
| `timeseries-forecast` | `autogluon:<job id>` from the `autogluon-timeseries` job | `rows` (history), `known_covariates` → `forecast` | `/inference` |

A task exists as soon as a model file registers for it.

## Task Contracts

Each task has one contract in `models/contracts.py`: a Pydantic model for what `predict()` receives and one for what it returns, so the models of a task are interchangeable and a client sees one shape whichever model answers. A request that does not fit is a 400 (422 when FastAPI catches it first); a result that does not fit is a `ContractViolation` (500), since it is a bug in the model. `POST /inference` and `GET /tasks` are built from the contracts, so `/docs` lists each task's exact fields, and the routes that follow an outside API (`/v1/chat/completions`, `/v1/decisions`, `/vision-language`) translate into and out of the same contracts. A model file adds a task with `define_task()`, naming its `category` (its tab in the playground and the model browser, drawn as `frontend/src/shared/categories.ts` says) and `label`, or `variant_of` for a task that does another's job with another kind of model. Zero-shot versions of a task are such variants, never an optional field of the task, since a model that needs the labels cannot stand in for one that takes none. [models/README.md](models/README.md) has the details.

## How a Request Is Served

1. The router checks the key (`api/deps.py`). The manager checks the model ID against `ALLOWED_MODELS` itself, on every load, run and estimate (`ModelManager.check_allowed`), so no route can forget it. A refusal anywhere below (an unknown job, a model not loaded, a full cache) is one of `core/errors.py`'s, which `api/app.py` answers with its status; a plain `ValueError` is a 400, and anything unexpected a 500 naming the error's type (its text too with `DEBUG`).
2. `ModelManager` builds a cache key `model_id:task` and takes that key's lock, so a model loads only once and is never evicted while a request is using it.
3. On a cache miss, the registry chooses the class that serves `(model_id, task)`: a class listing the ID, else one whose pattern matches it (FastVLM for `*fastvlm*`, vega for `vega-*`, custom for `custom:*`), else the generic `TransformersModel`; model server entries are registered last, so they win (`models/registry.py`). A model ID that a class claims for a different task is rejected with 400 before anything loads.
4. Before adding the model, the cache evicts the least recently used idle models until it is under `MAX_MODELS` and `MAX_MEMORY_GB` (GPU memory on CUDA, the process's RSS on CPU or MPS). A model idle for less than `MODEL_KEEP_S` is not evicted yet. When nothing can go, the load waits; if it outranks a busy model's work (a higher `X-Priority`, or the same after waiting `MODEL_TURN_S`), that model is drained and evicted (`models/cache.py`).
5. Single requests to a model with `predict_batch()` (the generic classifiers, question-answering, embedding and rerank models, also when a model server entry runs them in-process) share forward passes (`models/batching.py`): one worker takes the model's slot, then everything waiting as one batch, so batches grow with the load; only an idle model waits for company, up to `BATCH_WAIT_MS`. A request that would wait longer than `MODEL_WAIT_TIMEOUT` is refused (503 with Retry-After), batched or not (`Entry.admit`). Inference runs in a worker thread, so the event loop stays free and different models run in parallel; on MPS (Apple GPUs) loading, inference and unloading take turns across all models, because PyTorch's MPS backend crashes the process when two threads use it at once.
6. The manager hands each request, as one `RequestEvent` (`models/events.py`), to its sink, the metrics (`monitoring/metrics.py`), which count it and hand it to the request log (`monitoring/store.py`): a row per request, and a trace per input with its input and output for `TRACE_RETENTION_DAYS` (`models/capture.py` cuts them down: images and audio only as their size and hash). It does not slow the request: the rows are queued for a writer thread. The service only opens images inside `IMAGE_DIR`.

## Adding New Models

A model is one file in a subfolder of `models/families/`: a `BaseModel` subclass with `load()` and `predict()`, registered with `@register`. Nothing else needs editing; [models/README.md](models/README.md) is the guide, with a worked example.

## Configuration

All settings come from environment variables or the `.env` file. A non-empty environment variable wins over the file.
`env.example` lists every setting with its default and what it does (`tests/test_config.py` fails on one it lacks);
`core/config.py` declares them, one line each, and `GET /admin/config` shows the values in effect.

Set `API_KEY`, `ADMIN_API_KEY` and `ALLOWED_MODELS` before exposing the service. The server logs a warning at startup for each risky setting, and `/admin` shows the same list.

## Running the Service

```bash
# Local (make alone lists the commands; needs uv, and Node 22.12+ for the pages)
make install          # uv sync + build the browser pages; the API runs without them
make run              # python main.py; then open the setup link from the log, or: cp env.example .env and edit

# Frontend development: hot reload on :5173, API calls proxied to :8000
(cd frontend && npm run dev)

# Containers
make up                      # docker compose up --build -d; Podman: make up COMPOSE="podman compose"

# Job environments: AutoGluon (tabular and time series); or the Set up button in /admin
scripts/setup_job_env.sh autogluon

# Unit tests (no model downloads, no torch needed)
make test                    # uv run pytest, then the frontend's vitest
make lint                    # uv run ruff check, pyright on api/app.py, then the frontend's tsc
(cd frontend && npm run test:e2e)   # build the pages and open each in a browser (Playwright; npx playwright install chromium once)
```

## Background Work

Work that outlives a request is queued or periodic. Queued work is a task on the one `TaskRunner` (`tasks/runner.py`, which says how limits, priorities, retries, timeouts and restarts work), a row of the `tasks` table (`tasks/store.py`), of a kind its owner declares: jobs (`jobs/runner.py`), eval runs and judging (`evals/runner.py`), webhook deliveries (`tasks/webhooks.py`), copies of buckets' files to their remotes (`buckets/copies.py`) and workflow runs (`workflows/runner.py`). Each is the one record, `Task`, which `GET /queue` lists across the kinds (`api/queue_routes.py`) and the Running section shows; a kind's owner reads and notes its tasks through the runner, never the store. Periodic work is a `core.background.every()` loop that `lifespan` in `api/app.py` starts: the hourly prunes of what has a retention, the retries of evidence time stamps, the buckets' copies that fell behind, the scheduler's check for schedules that are due, and unloading idle models. `lifespan` also holds the order in which the services start and stop.

## Browser Pages

`/admin` and `/admin/setup` are a React + TypeScript app in `frontend/`, one Vite entry per page sharing `src/shared/`. `npm run build` typechecks and writes them to `api/static`, which FastAPI serves at `/admin/static`; `api/pages.py` serves each page, or a 503 page with build instructions when it has not been built. The bundle loads nothing from outside hosts; the Dockerfile builds it in a Node stage.

The pages are ordinary API clients: they ask for a key, keep it for the tab and send it as `X-API-Key`. Every call goes through the typed client (`src/shared/api.ts`, on openapi-fetch), one per page, which its App gives to its parts with `ApiProvider` and they read with `useApi()`; its types are generated from the API's schema (`npm run gen:api`: `scripts/export_openapi.py`, then `src/shared/api-schema.d.ts`), so a misspelt route, a missing parameter or a reply read wrongly fails the typecheck; replies are typed by the routes' response models (`api/responses/`, named for the pages in `src/shared/types.ts`). Loads are TanStack Query queries, one cache per page (`src/shared/query.ts`, with `poll()`), and writes go through `useAction` (`src/shared/action.ts`).

`/admin` is drawn in a frame (`src/shared/Shell.tsx`): the menu of `src/shared/nav.ts`, whose entries say what each section is, and the section beside it. What is set up once and seldom opened (model servers, remotes, API keys, the audit log) is a tab of Settings rather than an entry of the menu (`under` in `nav.ts`), each still a section with its own hash. An `/admin` section is picked by the URL hash, so it can be linked and the back button works; `src/admin/sections.tsx` gives each menu entry its view, and the typecheck fails on one without the other. A section, tab or page that throws while it is drawn shows a card in its place (`src/shared/boundary.tsx`), so the rest keeps working. A section with parts has its folder under `src/admin/`, whose files say what each part draws; the model browser (`src/shared/model-browser/`) is the Models section and a dialog elsewhere, and a model is tried with `src/shared/task-io/`, an input and an output per task. The Playground section (`src/admin/playground/`) has a tab per category of task (Prediction is not offered yet), its chats and presets saved on the server when asked. Chat replies are drawn as Markdown, the parser loaded with the first reply (`MarkdownView.tsx`), and a fenced `chart` block in one (`blocks.ts`) as a view's chart, by the Data section's `RowsView`.

## API

The reference is `/docs` (and `/openapi.json`): every route with its parameters, body, reply and what it does, from the
routes' docstrings and models (`frontend/openapi.json` is the same schema, committed; `tests/test_docs.py` fails on a
route without a docstring). [docs/GUIDE.md](docs/GUIDE.md) shows how to call them. The routers, in the order
`create_app()` includes them, with the key each needs (`api/deps.py`: client is `API_KEY`, a live client key or the
admin key, admin is `ADMIN_API_KEY`, or `API_KEY` when no admin key is set, never a client key):

| Routes | Router | Key |
|---|---|---|
| `/`, `/health`, the page `/admin` | `public_routes.py` | none |
| `/inference`, `/inference/batch`, `/tasks`, `/vision-language`, `/models/...` | `inference_routes.py` | client; admin to load, unload and pin, and for `/models/status` and `/models/estimate` |
| `/v1/models`, `/v1/chat/completions`, `/v1/completions`, `/v1/embeddings`, `/v1/rerank`, `/v1/audio/transcriptions` | `openai_routes.py` | client |
| `/v1/decisions` (Perplexity's Decisions API, below) | `decision_routes.py` | client |
| `/admin/stats`, `timeline`, `events`, `requests`, `traces/{id}`, `logs`, `config`, `limits`, `audit` | `admin_routes.py` | admin |
| `/admin/setup` | `setup_routes.py` | none; saving needs the setup code from the server log |
| `/admin/keys` (the client keys: make, list, change their grants, revoke) | `key_routes.py` | admin; making one needs `ADMIN_API_KEY` set |
| `/playground/limits`, `conversations`, `presets` | `playground_routes.py` | admin |
| `/db/{ws}/_views` and `/db/{ws}/_dashboards` (a workspace's views: saved reads with params and a display; and pages of them); before the file routes and `/db`'s, which would take `_views` for a table | `view_routes.py` | admin, but for running a view (`POST /db/{ws}/_views/{name}`), which is the client's |
| `/db/{ws}/{bucket}/_files` (a bucket's files: upload, download, delete) and `_remote` (the remote they are copied to); before `/db`'s routes, whose `/db/{ws}/{table}/{id}` would take `_remote` for an id | `file_routes.py` | client, under `/db`'s rules; admin for `_remote` |
| `/remotes` (places outside the service that buckets' files are copied to: edit, test) | `remote_routes.py` | admin |
| `/db` | `table_routes.py` | client for rows, vector searches (`_search`) and the list of workspaces; admin to make, label and drop workspaces (never `default`) and tables (buckets among them), change schemas (vector fields among them) and edge types and run queries, and for the service's own workspaces (listed with `?all=true`) and kept tables, which are read-only |
| `/jobs` | `job_routes.py` | admin |
| `/workflows` (workflows, their node types, dry runs, runs and invokes) | `workflow_routes.py` | admin, but for invoking a saved one (`POST /workflows/{id}/invoke`, which writes nothing), which is the client's |
| `/admin/datasets`, `runs`, `evals/metrics` | `eval_routes.py` | admin |
| `/enrich`, `/enrichers` | `enrich_routes.py` | client to look up; admin to edit, test, set keys and clear the cache |
| `/admin/model-servers` | `model_server_routes.py` | admin |
| `/metrics` | `metrics_routes.py` | client |
| `/evidence` | `evidence_routes.py` | client, and an `Origin`, if sent, must be an extension's; admin to make a workspace (on its way out: `POST /db`) and for time stamp retries |
| `/queue` (the tasks of every kind on the one queue: list, cancel; the webhooks finished jobs and eval runs are posted to) | `queue_routes.py` | admin |

## First-Run Setup

A fresh instance has no keys. At startup it logs a line like:

```
Not set up yet. Open http://localhost:8000/admin/setup#code=Xy3... to choose API keys and allowed models
```

`/admin` redirects there while setup is pending. The wizard (`api/setup_routes.py`, `frontend/src/setup/`) makes an admin key and a client key, picks the allowed models and the browser origins, and saves them to `ENV_FILE`; saving needs the setup code from the log, so whoever reaches a fresh instance first cannot lock its owner out. It runs once: after that, settings are changed in the file.

## Data (`/db`)

The `/db` tables are JSON rows in the embedded DuckDB database (`db/engine.py`), grouped in workspaces: a workspace is a DuckDB schema, a table a DuckDB table whose declared fields are columns and the rest JSON (`_rest`). `db/tables.py` tells the model (schemas, filters, full-text search, graph tables, ref fields, vector fields, buckets) and is the store, made of `workspaces.py`, `ddl.py`, `refs.py` (tables joined by their rows' ids: the checks, deletes and `_expand`), `rows.py` and `vectors.py`; `db/graph.py` adds edges and queries, and `api/table_routes.py` is the routes. The service keeps its own data there too, written only by the part that keeps it: the evals, the task queue, the enrichers' cache and the playground each manage a workspace of their own, which `/db` shows to the admin, read-only, and the evidence ledgers are kept tables (`evidence_*`) in the workspaces.

db/ imports none of the packages that build on it, so `create_app()` hands the table store what it needs of them: `embed`, the model manager's embedding, for the vector fields (so `ALLOWED_MODELS` holds there as everywhere); `files`, the buckets' `BucketFiles`; and `on_drop`, which `Views.forget` joins. A *bucket* is a table of files whose rows only `buckets/files.py` writes, keeping each file's bytes in `DATA_DIR/files` (routes in `api/file_routes.py`). A bucket may name a *remote*, a place outside the service its files are copied to through OpenDAL (`buckets/remotes.py`; which remote, and which files are copied, in `buckets/naming.py`; the copying, a kind of task, in `buckets/copies.py`; routes in `api/remote_routes.py`).

A *view* is a saved read over a workspace's data (SQL, a table's rows or a vector search) with parameters and a display, and a *dashboard* a page of views that share their parameters (`views/spec.py`, `views/store.py`, `api/view_routes.py`). The admin writes them and a client key runs a view, within a time limit past which its query is stopped (`GraphStore.timed_read`) and a row cap, its answer kept for its refresh interval; they are kept beside their workspace and go when it is dropped. The Data and Remotes sections of `/admin` (`frontend/src/admin/data/`, `remotes/`) browse, edit, query and draw all of it.

## Traces and Evals

A trace is one input of a request with its output, kept by the request log (`monitoring/store.py`, below) beside its request's row, which has its model, task, outcome, timing, serving backend, and the tags and session its `X-Trace-Tag` and `X-Session-Id` headers gave it; `models/capture.py` cuts it down to keep, and it is dropped after `TRACE_RETENTION_DAYS`. Traces have no page of their own: a row of the requests table of Activity & logs opens one, and searching that table by words finds the requests whose traces hold them. Datasets, examples, runs, items and scores are the tables of the `evals` workspace. A run is a kind of task (`evals/runner.py`) that sends a dataset's examples through the model manager, as many at once as the model takes, with tracing off, and resumes where it stopped. Scoring is on Pydantic Evals (`evals/scoring.py`): a metric, or an LLM judge, which may be a model this service runs, asked through its own `/v1` in-process (those requests are ordinary ones, tagged `judge`). The routes are `api/eval_routes.py`, the Evals section `frontend/src/admin/evals/`.

## Monitoring

The manager records every request (model, task, outcome, duration, whether it had to load the model first, tags, session, and its inputs and outputs as traces) and the cache every load, eviction and unload. `monitoring/store.py`, the request log, writes them from a queue in batches, with an hourly summary for long ranges, and `monitoring/metrics.py` counts them for `GET /metrics`. The audit log (`api/audit.py` says which requests, `monitoring/audit.py` keeps them) and the latest log lines (`monitoring/logs.py`, in memory only) are here too. History lives in the database, so it survives restarts; the loaded models and memory figures are the process's live state. The Overview, Running, Activity & logs and Audit log sections draw it.

## Workflows (`/workflows`)

A *workflow* takes the rows of one table through a graph of nodes, models, enrichers, conditions, SQL and writes, as a JSON spec (`workflows/spec.py`; each node type one entry in `workflows/nodes.py`; `api/workflow_routes.py`). A run is a kind of task that reads the table a page at a time and takes each page through the graph in memory, each Task node's rows sent one request each through the model manager as wide as the model takes them (`ModelManager.width`), at low priority; it keeps how far it read on its task after each page, so it resumes after a restart (`workflows/runner.py`). A trigger is a schedule of its own kind (`tasks/schedule.py`). The Workflows section (`frontend/src/admin/workflows/`) draws a workflow on a canvas (React Flow), its node types' settings as forms from their schemas, and shows a dry run's or a run's rows along each edge.

## Jobs (`/jobs`)

A job is a long-running script, submitted and then followed (`api/job_routes.py`). It runs as its own process in its own Python environment (`jobs/runner.py`, `jobs/worker.py`), so it can use libraries the service cannot install, and a crash or an out-of-memory kill ends only the job. A job file is one file in a subfolder of `jobs/` ([jobs/README.md](jobs/README.md)); schedules are the Scheduler's (`tasks/schedule.py`), which jobs declare themselves to, and a finished job or eval run is POSTed to the webhooks (`tasks/webhooks.py`). The AutoGluon jobs train predictors that are served as `autogluon:<job id>`, each from a process in the `autogluon` environment (`jobs/autogluon/_serve.py`) that the model cache loads, budgets and evicts like any other model. The text-classifier job fine-tunes a small encoder on the labeled examples an eval dataset writes as its training file, and its model is served as `trained:<job id>` by the generic classifier, in this process (`models/families/trained/`). The Jobs section is `frontend/src/admin/jobs/`.

## Model servers

A model server entry maps a model ID to a task and its backends, tried in order when it loads: a server that already runs (`url`, or a `provider` such as Ollama), one this service starts (`cmd`), or a Hub model run in this process (`hub`). `models/servers/__init__.py` tells how a backend is chosen and how a failure moves on to the next one; `entries.py` reads `MODEL_SERVERS_FILE` and the file `/admin` saves, `model.py` probes the backends, `processes.py` starts, sleeps and stops servers, `registry.py` keeps the entries in effect, and `openai_client.py` makes the requests. The Model servers section (`frontend/src/admin/servers/`) edits entries and providers through `api/model_server_routes.py`, which applies them at once.

## Enrichers (`/enrich`)

An enricher is one HTTP call to an outside service, described as data (`enrichers/spec.py`): a request template, the outputs picked from its JSON reply with JMESPath, the source's terms, a cache TTL and a rate limit. The built-in ones are JSON files in `enrichers/builtin/`, the operator's are specs in the database (`tk_enricher`, `enrichers/store.py`). `POST /enrich` classifies the value (`enrichers/classify.py`) and runs every enricher that takes its type at once (`enrichers/runner.py`), refusing private addresses since the templates are editable, with the results cached (`enrichers/cache.py`). The Enrichers section (`frontend/src/admin/enrichers/`) lists, tries, edits and keys them, its form made from the `EnricherSpec` schema.

## Evidence (`/evidence`)

A browser extension (not part of this repository) captures a web page and uploads its files and metadata as one multipart request to `api/evidence_routes.py`, which refuses a request from a web page's `Origin`, even with the key. Evidence is kept by workspace: each `/db` workspace holds its own ledger in kept tables (`evidence/store.py`). A capture goes through several modules: the route re-hashes every upload against the SHA-256 the browser declared; `evidence/enrich.py` looks the page's hosts up with the enrichers, never failing the capture; `Ledger.append` builds the record (`evidence/record.py`), serializes it once as RFC 8785 canonical JSON, hashes and signs those bytes (`evidence/signing.py`), and under the workspace's lock writes the files (`evidence/files.py`) and only then commits the row, chained to the previous record; then the hash goes to the RFC 3161 time stamp authority (`evidence/tsa.py`), retried hourly until granted. `GET /evidence/verify` re-checks a workspace's whole ledger (`evidence/verify.py`). The Evidence section is `frontend/src/admin/evidence/`.

## Decisions (`/v1/decisions`)

`POST /v1/decisions` (`api/decision_routes.py`) takes requests in the format of Perplexity's Decisions API and answers them with vegaml (`models/families/decision/vega.py`; it needs `uv sync --extra vega`): a `state` and up to 128 named questions in, probabilities instead of text out.

```json
{
  "model": "vega-0.8b",
  "state": {"subject": "Invoice overdue", "body": "Third notice. Pay now or I cancel."},
  "questions": {
    "team":    {"type": "choice", "instructions": "Which team handles this?",
                "criteria": {"billing": "payments", "technical": "bugs", "sales": null}},
    "churn":   {"type": "noul", "instructions": "Is the customer about to leave?"},
    "urgency": {"type": "score", "instructions": "How urgent is it?",
                "criteria": ["not urgent", "soon", "today"]}
  }
}
```

`noul` returns the probability of yes; `choice` the top option, `confidence` and `probabilities`; `score` the probability-weighted level, `confidence`, `legend` and `probabilities`. The route's docstring says how it differs from Perplexity's.
