# Core AI & Data toolkit: how it is built

## Overview
The Core AI & Data toolkit is a FastAPI service that runs Hugging Face models locally. It loads models on demand, keeps a small LRU cache of them budgeted by model size, and exposes them through its own REST API and an OpenAI-compatible API (chat with images and audio included). A model can also be served by an OpenAI-compatible server: one that already runs (Ollama, LM Studio, a GPU box), or its own process (MLX, vLLM, llama.cpp), which the cache starts and stops like any other model. Such a model lists backends tried in order when it loads, the last resort being in-process Transformers, and the admin's Model servers section edits them. An admin dashboard at `/admin` shows request metrics and loaded models, and can load and unload them; its Data section creates namespaces for the `/db` tables of JSON rows and browses their rows. A playground at `/playground` has a tab per category of task: Chat lets you chat with a model or send the same chat to several and compare their replies; the others (Vision, Text, Audio, Decision, Other) run one model of the chosen task. Prediction (the AutoGluon tasks) is not offered yet. Long-running scripts, such as training AutoGluon predictors, run as background jobs in their own process and Python environment (`/jobs`), and the predictors they train are served like models. Enrichers (`/enrich`) look a value such as an IP address, domain or file hash up in outside services; each is a small editable definition of one HTTP call, managed in the admin's Enrichers section.

## Directory Structure

```
ai_toolkit/
├── api/                          # API layer
│   ├── app.py                    # App factory: CORS, startup warnings, shutdown cleanup, the handlers every error goes through: a refusal (errors.py) with its status, a ValueError as 400, anything unexpected as 500
│   ├── deps.py                   # What routes depend on: the key checks (CLIENT, ADMIN), the model allow-list (model_allowed, which the manager checks on every load), typed services (deps.Manager, deps.Jobs, deps.Evidence...)
│   ├── public_routes.py          # The routes without a key: /, /health and the pages (the wizard is the only other)
│   ├── routes.py                 # Native endpoints: inference, the model cache, tasks; image path checks
│   ├── openai_routes.py          # OpenAI-compatible /v1 endpoints: chat, completions (logprobs, echo), embeddings, rerank, transcriptions
│   ├── metrics_routes.py         # GET /metrics in Prometheus text format
│   ├── hub_routes.py             # /hub: search the Hugging Face Hub, download weights ahead of time, list and delete what is on disk
│   ├── playground_routes.py      # /playground page, limits, saved conversations and presets (the playground namespace of /db), conversation -> eval example
│   ├── decision_routes.py        # Perplexity-compatible /v1/decisions
│   ├── admin_routes.py           # /admin dashboard page and the JSON it polls
│   ├── setup_routes.py           # /admin/setup first-run wizard: writes keys and allow-list to .env
│   ├── collection_routes.py      # /collections: documents embedded with a model, vector search, SQL filters
│   ├── table_routes.py           # /db: namespaced tables of JSON rows, schemas, bulk routes, transactions; graph tables' edges, neighbors, traversal, read-only queries, and _graph (rows and edges to draw)
│   ├── job_routes.py             # /jobs: submit, follow, cancel and delete jobs; their files; input uploads
│   ├── enrich_routes.py          # /enrich and /enrichers: look values up; list, edit, test enrichers and their keys
│   ├── model_server_routes.py    # /admin/model-servers: list, edit and probe model servers and their providers
│   ├── eval_routes.py            # /admin/traces, /admin/datasets, /admin/runs: traces, datasets, runs, scores, the judge
│   ├── evidence_routes.py        # /evidence: workspaces, cases, captures uploaded by the browser extension (multipart), their annotations, case todos, verify, public key, time stamp retries; the origin check
│   ├── pages.py                  # Serves the built pages, or how to build them
│   ├── static/                   # Built by frontend/ (npm run build); not committed
│   ├── models.py                 # Request/response models; /inference's are built from the task contracts
│   ├── streaming.py              # Server-sent events for /v1's streamed replies: the chunk envelope, a stream begun so a failed load still answers with its status
│   └── responses/                # Response models of the routes that answer with dicts, one module per router (common.py: Out, answering): checked on the way out, typed in the schema
├── jobs/                         # Long-running scripts run in their own process (see jobs/README.md)
│   ├── base.py                   # Job, JobParams, JobResult, JobContext, DataFile (needs only pydantic)
│   ├── registry.py               # @register_job, job file discovery (discovery.py)
│   ├── tasks.py                  # TaskRunner: the one queue of background work; kinds (jobs, eval runs, judging, Hub downloads) declared once, each with its limit; starts the most urgent task, then one whose model is loaded, then the oldest; a running task the service stops under is queued again when its kind can go on (eval runs, downloads; at most twice after a crash) or failed (jobs, judging)
│   ├── runner.py                 # JobRunner: the job kind: scripts in worker processes, progress, cancel, files; the scheduler's loop
│   ├── store.py                  # The task and schedule tables: /db tables in the jobs namespace, written with the store's own SQL (claims are conditional updates)
│   ├── worker.py                 # python -m jobs.worker: runs one job in its environment's interpreter
│   └── autogluon/                # autogluon-tabular, -multimodal, -timeseries; _serve.py serves the predictors
├── enrichers/                    # Look values up in outside services (see the Enrichers section)
│   ├── spec.py                   # EnricherSpec: one HTTP call with inputs and outputs; EntityType; TTL and rate parsing
│   ├── classify.py               # Which entity type a value is (IP, domain, hash, CVE...), normalised
│   ├── store.py                  # Built-in files, the operator's files (ENRICHERS_DIR) and Python enrichers, merged
│   ├── cache.py                  # Cache of results, per (enricher, type, value), by the enricher's TTL: a /db table in the enrichers namespace, pruned hourly; MemoryCache without a database
│   ├── runner.py                 # EnrichService: fan-out, rendering, rate limits, 429 cooldowns, public-address check
│   └── builtin/                  # The shipped enrichers, one JSON file each
├── evidence/                     # Evidence capture: signed, hash-chained records of captured web pages (see the Evidence section)
│   ├── record.py                 # The record, its RFC 8785 canonical bytes, hash and chain; checking uploads against declared hashes
│   ├── signing.py                # Ed25519 key file (made on first use), fingerprint, sign and verify
│   ├── store.py                  # The ledger: /db tables evidence/cases and evidence/captures; append under one lock, time stamps
│   ├── files.py                  # EVIDENCE_DIR/captures/<id>/: artifacts/, record.json, record.sig, record.tsr, read-only
│   ├── tsa.py                    # RFC 3161 requests and token checks (rfc3161-client); retries for records without one
│   ├── enrich.py                 # The record's enrichment: the page's address and hosts, looked up with the enrichers
│   └── verify.py                 # Re-checks the whole ledger: sequence, chain, canonical form, signatures, files, tokens
├── vectorstore/                  # Embedded ArcadeDB: the Database every store shares, DocumentStore (declared types kept up to date, migrations run once, _select and _prune), on_pool, BatchWriter (engine.py); /collections (store.py); /db: what a table is (schema.py), the namespaces and tables (tables.py), edges between rows (graph.py, the GraphStore the app runs)
├── evals/                        # Traces (each request's input and output), datasets, runs and scores, in ArcadeDB
│   ├── capture.py                # compact(): what a trace keeps, compacted(): whether it was cut; text_of() for the index and previews
│   ├── store.py                  # EvalStore: day buckets for traces; datasets, runs, items, scores as /db tables (evals/*)
│   ├── scoring.py                # Scoring on Pydantic Evals: metrics (exact match, contains, JSON valid) and the LLM judge (rubric, pairwise) as evaluators
│   └── runner.py                 # RunExecutor: the eval run and judge kinds of task (jobs/tasks.py): runs a model over a dataset, a page of examples at once, resuming one that stopped short; judges a run, its API key kept in memory only
├── monitoring/                   # Request metrics, model events and the log buffer for /admin
│   ├── metrics.py                # Metrics: the manager's monitor; Prometheus counters and histogram, rendered by /metrics, forwarded to the store
│   ├── store.py                  # Request and event tables (ArcadeDB): queued background writes, summaries, retention
│   └── system.py                 # Process, host and GPU figures
├── models/                       # Model management and implementations (see models/README.md)
│   ├── base.py                   # BaseModel, ModelInfo, shared torch helpers
│   ├── tasks.py                  # Task contracts: the input and output model of each task
│   ├── registry.py               # @register, model file discovery (discovery.py), (model_id, task) -> class
│   ├── manager.py                # ModelManager: load, run inference, unload, list models
│   ├── events.py                 # RequestEvent: what the manager hands its sinks (metrics, traces) per request; the request's tags and priority (request_context: X-Trace-Tag, X-Session-Id, X-Priority)
│   ├── chat.py                   # Chats over text-generation: OpenAI's messages, the turns and transcript a model gets, the reply cut back (ReplyStream)
│   ├── cache.py                  # LRU cache: room made before loading, by model size; busy models kept, idle ones for MODEL_KEEP_S; a load that outranks a busy model's work drains it; loads waiting for room go by priority, then age, each until MODEL_WAIT_TIMEOUT after it was asked for
│   ├── memory.py                 # Size estimates from safetensors headers; device and free memory
│   ├── server_specs.py           # Model server entries: providers, backends, the file and the saved file layered
│   ├── model_server.py           # ServerModel: probes its backends, picks one at load, falls back
│   ├── server_registry.py        # The model server entries in effect: load_configured, reload, configured; registered with ServerModel
│   ├── server_process.py         # Started model servers: spawn (without the service's secrets), wait until ready, sleep and wake, stop; Ollama keep_alive 0 on unload
│   ├── openai_client.py          # OpenAIClient: the hand-written OpenAI-compatible client (chat, completions, embeddings, rerank, transcriptions; SSE, tool-call fragments)
│   ├── hub.py                    # Hub search, repo info, Downloads (the download kind of task: a child process each, with progress), the cache's repos
│   ├── media.py                  # Audio decoding (soundfile, or PCM WAV without it) and resampling
│   ├── generic/                  # Fallbacks for any Hub ID: transformers_model.py (text tasks),
│   │                             #   vision_model.py, embedding_model.py, speech_model.py
│   ├── vision/                   # fastvlm.py (Apple FastVLM, image + text)
│   ├── decision/                 # vega.py (vegaml typed decisions)
│   ├── autogluon/                # predictor.py: predictors trained by jobs ("autogluon:<job id>"), and their tasks
│   └── custom/                   # custom_model.py (smallest example, "custom:" IDs)
├── frontend/                     # The browser pages: React + TypeScript, built with Vite into api/static
│   ├── admin.html, setup.html, playground.html   # Vite entry pages
│   ├── src/components/ui/        # shadcn/ui components (Tailwind v4 + Radix), owned and edited here
│   ├── src/shared/               # API client (api.ts: openapi-fetch and openapi-react-query, typed by path from the generated api-schema.d.ts, npm run gen:api), the query cache (query.ts: TanStack Query, polling, errors; mount.tsx renders each page inside it), useAction (action.ts: one way to run a write, a mutation with its busy state and error), CodeEditor (code-editor.tsx: CodeMirror for SQL, Cypher, JSON and JSON Lines, loaded the first time a page shows one), SchemaForm (schema-form.tsx: forms made from the JSON Schema the API serves, RJSF with its shadcn theme; form-schema.ts makes a schema plain and gives each field its editor by its shape, so a page writes only a uiSchema of sections and wording), DataGrid (grid.tsx: the plain tables), StatusLabel and SelectField (components.tsx), EChart (echart.tsx: ECharts with the page's colors and tooltips, loaded the first time a page shows a chart), parseJsonObject (json.ts), parseHash and downloads (browser.ts), sign-in, formatting, theme (globals.css), shell and its menu (nav.ts); types.ts names the generated types
│   ├── src/admin/                # Dashboard: each section's view in sections.tsx; the master-detail layout the list sections share (layout.tsx); Overview (OverviewSection.tsx), tiles, charts (charts.tsx, on ECharts), models, Activity & logs (with each request's trace, TraceDialog.tsx), Evals (EvalsSection.tsx; a dataset EvalsDataset.tsx, a run EvalsRun.tsx, two runs compared EvalsCompare.tsx), Data (DataSection.tsx; a namespace DataNamespace.tsx, a table's rows TableBrowser.tsx with the grid DataRows.tsx and a node table's Cypher filter DataGraphRows.tsx, its schema DataSchema.tsx, the row dialog DataRowEditor.tsx, an edge type's page DataEdgeType.tsx, the SQL and Graph query pages DataQuery.tsx; the graph view GraphView.tsx with sigma.js, its bookkeeping in graph-store.ts, the sigma wiring in use-sigma.ts, its filters in graph.ts), Jobs (JobsSection.tsx; JobsSchedules, JobsTable, JobsSubmit, JobsLog, JobsWebhooks), Model servers (ServersSection.tsx, servers.ts), Evidence (EvidenceSection.tsx)
│   ├── src/setup/                # First-run wizard
│   ├── src/playground/           # Category tabs (categories.ts); Chat compares models over /v1/chat/completions, the other tabs use TryPanel
│   └── openapi.json              # The API schema the types come from (npm run gen:api)
├── scripts/export_openapi.py     # Writes frontend/openapi.json
├── scripts/setup_job_env.sh      # Creates a job environment: envs/<name> from requirements-<name>.txt (uv)
├── envs/                         # Job environments (not committed), e.g. envs/autogluon
├── data/jobs/                    # Default JOBS_DIR: the job table and one directory per job
├── data/datasets/                # Default JOB_DATA_DIR: input files jobs read
├── data/enrichers/               # Default ENRICHERS_DIR: the operator's enrichers (the result cache is a /db table)
├── data/images/                  # Default IMAGE_DIR for /vision-language
├── tests/                        # Unit tests (fake models, no downloads or torch); test_access.py and test_allowed_models.py hold every route to the key and the allow-list, test_schemathesis.py every operation to the schema (pytest -m 'not schemathesis' skips it)
├── main.py                       # Entry point (uvicorn)
├── config.py                     # Settings read from environment variables and .env
├── procs.py                      # Child process groups (jobs, model servers, predictors): stop gracefully, kill at exit; their environment without the service's secrets (hide)
├── discovery.py                  # Imports a package's files so their registrations run (the model and job registries)
├── envfile.py                    # Reads and writes .env (no dependency)
├── errors.py                     # The refusals the domain and the routes raise, each with its HTTP status: Refused 400, Unauthorized 401, Forbidden 403, NotFound 404, Conflict 409, TooLarge 413, Unavailable 503
├── background.py                 # every(): periodic work (queued work is jobs/tasks.py's)
├── env.example                   # Example .env
├── model_servers.example.toml    # Example MODEL_SERVERS_FILE: Ollama/LM Studio with fallback, MLX, vLLM, llama.cpp
├── pyproject.toml, uv.lock       # Dependencies (dev group: pytest, httpx, ruff; extra "vega": vegaml for /v1/decisions)
├── requirements.txt              # Pinned export of uv.lock for pip and the Dockerfile (make lock)
├── Makefile                      # install, run, dev, test, lint, lock, up, down, logs
├── requirements-autogluon.txt    # The autogluon job environment (Python 3.13, its own torch)
├── Dockerfile, docker-compose.yml  # Docker and Podman (podman compose reads the same file)
└── podman-run.sh                 # Podman without compose
```

## Tasks

| Task | Built-in models | Input → result | Endpoint |
|------|-----------------|----------------|----------|
| `text-generation` | LFM2.5, Granite 4.1, Qwen2.5, GPT-2, or any Hub causal LM | `text` (+ `messages`, generation fields) → `text` | `/inference`, `/v1/chat/completions` |
| `text-classification` | ModernBERT and mmBERT sentiment, DistilBERT SST-2, any Hub classifier | `text` → `label`, `confidence`, `scores` | `/inference` |
| `question-answering` | deepset DeBERTa-v3 and TinyRoBERTa SQuAD 2.0, DistilBERT SQuAD, any Hub QA model | `question`, `context` → `answer`, `confidence`, positions | `/inference` |
| `vision-language` | Qwen3.5, LFM2.5-VL, MiniCPM-V 4.6, SmolVLM2, Qwen3-VL, Apple FastVLM | `text`, `image` (+ generation fields) → `text` | `/vision-language`, `/inference` |
| `decision` | vegaml (`vega-0.8b`, `vega-4b`) | `state` or `image`, `questions` → `answers`, `n_tokens` | `/v1/decisions`, `/inference` |
| `custom` | `custom:*` echo example | `text` → `text` | `/inference` |
| `tabular-prediction` | `autogluon:<job id>` from the `autogluon-tabular` job | `rows` (+ `probabilities`) → `predictions`, `probabilities` | `/inference` |
| `multimodal-prediction` | `autogluon:<job id>` from the `autogluon-multimodal` job | `rows` (image columns: paths in `IMAGE_DIR`) → `predictions`, `probabilities` | `/inference` |
| `timeseries-forecast` | `autogluon:<job id>` from the `autogluon-timeseries` job | `rows` (history), `known_covariates` → `forecast` | `/inference` |

A task exists as soon as a model file registers for it.

## Task Contracts

Each task has one contract in `models/tasks.py`: a Pydantic model for what `predict()` receives and one for what it returns. Every model for a task follows it, so models are interchangeable and a client sees one shape per task whichever model answers.

- `BaseModel.run()` validates the request into the input model before `predict()` and the result against the output model after it. A bad request is a `ValueError` (400; 422 when FastAPI catches it first). A result that breaks the contract is a `ContractViolation` (500), because it is a bug in the model, not the client's fault.
- Input models reject unknown fields. Output models drop fields beyond the contract, so clients only ever see documented fields.
- `POST /inference` is `{model_id, task, input}`, where `input` is the task's input model, and returns the output model as `result`. The request and response are unions picked by `task`, built from the contracts at startup, so `/docs` lists each task's exact fields. `GET /tasks` serves the same contracts as JSON Schema.
- Routes that follow an outside API (`/v1/chat/completions`, `/v1/decisions`, and `/vision-language` with its own field names) translate into and out of the same contracts.
- A model file can add a task with `define_task(name, Input, Output, description, hub_tag=...)` (see `models/custom/custom_model.py`); `hub_tag`, the Hub's pipeline tag for such models, puts the task in the Hub search's filter. A task registered without one still works, untyped: `input` reaches the model as sent and the result is returned as is.

## How a Request Is Served

1. The route checks the API key (if `API_KEY` is set; the admin key is accepted too). Each router states once which key it needs (`api/deps.py`: `CLIENT`, or `ADMIN` for the admin's routes); the only routes without one are in `api/public_routes.py` and the first-run wizard, and `tests/test_access.py` fails on any other. The model ID is checked against `ALLOWED_MODELS` by the manager itself, on every load, run and estimate (`ModelManager.check_allowed`, given `deps.model_allowed`), so no route can forget it; `tests/test_allowed_models.py` sends every route that names a model one outside the list. A refusal anywhere below (an unknown job, a model not loaded, a full cache) is one of `errors.py`'s, answered by `api/app.py` with its status and message; a plain `ValueError` (a model refusing its input) is a 400 and anything unexpected a 500 naming the error's type (its text too with `DEBUG`), so routes never map errors to statuses.
2. `ModelManager` builds a cache key `model_id:task` and takes that key's lock, so a model loads only once and is never evicted while a request is using it.
3. On a cache miss, the registry chooses the class that serves `(model_id, task)`: a class listing the ID, else one whose pattern matches it (FastVLM for `*fastvlm*`, vega for `vega-*`, custom for `custom:*`), else the generic `TransformersModel`. A model ID that a class claims for a different task is rejected with 400 before anything loads.
4. Before adding the model, the cache evicts the least recently used idle models until it is under `MAX_MODELS` and `MAX_MEMORY_GB`. Memory is GPU memory on CUDA and the process's RSS on CPU or MPS. A model idle for less than `MODEL_KEEP_S` is not evicted yet, since its client's next request is usually on the way. When nothing can go, the load waits; if it outranks a busy model's work (a higher `X-Priority`, or the same after waiting `MODEL_TURN_S`), it drains that model: its requests finish, new ones wait, and it is evicted.
5. Single requests to a model with `predict_batch()` (the generic classifiers and question-answering models) share forward passes (`models/batching.py`): one worker takes the model's slot, then everything waiting (up to the model's `batch_size`) as one batch, so requests that arrive while the model is busy go out together and batches grow with the load. Only while the model is idle does it wait for company, up to `BATCH_WAIT_MS`. Each request keeps its own trace and count, a batch one request spoils is answered again one by one. A request that would wait longer than `MODEL_WAIT_TIMEOUT` is refused (503 with Retry-After), batched or not: the cache entry checks it (`Entry.admit`), counting the batches ahead of a batched request and the requests ahead per slot for any other. Inference runs in a worker thread, so the event loop stays free and different models can run in parallel. On MPS (Apple GPUs) loading, inference and unloading take turns across all models, because PyTorch's MPS backend crashes the process when two threads use it at once.

Each request's input and output are kept as a trace for `TRACE_RETENTION_DAYS` (3 by default; `0` keeps none), images and audio only as their size and hash (`evals/store.py`). The service only opens images inside `IMAGE_DIR`.

## Adding New Models

Add one file to a subfolder of `models/`. Nothing else needs editing; see [models/README.md](models/README.md) for the full guide.

```python
# models/custom/my_model.py
from models import BaseModel, register
from models.tasks import GeneratedText, TextGenerationInput


@register(task="text-generation", ids=["my-org/my-model"])
class MyModel(BaseModel):
    def load(self):
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(self.model_id)
        self.model = AutoModelForCausalLM.from_pretrained(self.model_id, torch_dtype=self.dtype)
        self.model.to(self.device).eval()

    def predict(self, request: TextGenerationInput) -> GeneratedText:
        inputs = self.tokenizer(request.text, return_tensors="pt").to(self.device)
        out = self.model.generate(
            **inputs,
            max_new_tokens=request.max_length or 100,
            temperature=request.temperature,
            do_sample=request.do_sample,
        )
        return GeneratedText(text=self.tokenizer.decode(out[0][inputs.input_ids.shape[1]:], skip_special_tokens=True))
```

- `load()` and `predict()` are plain blocking methods. They run in a worker thread.
- Attributes that `load()` sets are dropped on unload, so there is no `unload()` to write.
- `predict()` receives the task's input model and returns its output model (a dict with the same fields also works). Defaults live in the input model; a field such as `max_length` is `None` when the client left it out, so the model can apply its own default.
- `@register` takes `task`, `ids` (listed in `/models/list`; strings, or `ModelCard`s with a description and tags for the model browser), `match` (glob patterns for other IDs) and `fallback`. Stack it to serve several tasks.
- At first use the service imports every `.py` file in the subfolders of `models/`. Files whose names start with `_` are skipped, and a file that fails to import is logged and skipped.

## Configuration

All settings come from environment variables or the `.env` file (see `env.example`). A non-empty environment variable wins over the file.

| Variable | Default | Purpose |
|----------|---------|---------|
| `MAX_MODELS` | `3` | Models kept in the cache |
| `BATCH_WAIT_MS` | `2` | Milliseconds a single request to an idle model waits for others to share a forward pass with, on models that answer several in one call (`models/batching.py`); requests that wait for a busy model are batched without it; 0 answers each alone |
| `MAX_MEMORY_GB` | `8.0` | Memory limit before eviction |
| `MODEL_KEEP_S` | `3` | Seconds an idle model keeps its place before another model may take it (`models/cache.py`) |
| `MODEL_TURN_S` | `15` | Seconds a load waits before it drains a busy model whose work has the same priority (`X-Priority`); higher priority drains at once; 0 never |
| `DEFAULT_DEVICE` | `auto` | `auto`, `cpu`, `cuda` or `mps` |
| `HOST`, `PORT` | `127.0.0.1`, `8000` | Bind address (the image sets `0.0.0.0`) |
| `WORKERS` | `1` | Each worker has its own cache, so memory use multiplies; more than one needs `ARCADEDB_PATH` empty, since one process can open the database |
| `RELOAD`, `DEBUG` | `false` | Development only. `DEBUG` puts exception text in error responses |
| `MAX_INPUT_CHARS` | `20000` | Longest accepted input |
| `MAX_GENERATION_LENGTH` | `1024` | Highest allowed `max_length` / `max_new_tokens` |
| `CORS_ORIGINS` | `*` | Comma-separated origins. Credentials are allowed only when origins are listed explicitly |
| `API_KEY` | unset | When set, every endpoint except `/` and `/health` needs `Authorization: Bearer <key>` or `X-API-Key: <key>` |
| `ADMIN_API_KEY` | unset | Required by the admin endpoints below. Unset means `API_KEY` guards them. Accepted wherever `API_KEY` is |
| `ALLOWED_MODELS` | empty | Comma-separated model IDs. Empty means any Hub model can be downloaded |
| `TRUST_REMOTE_CODE`, `TRUST_REMOTE_CODE_MODELS` | `true`, FastVLM repos | Repos allowed to run their own Python code |
| `IMAGE_DIR` | `data/images` | The only directory `/vision-language` reads images from |
| `MODEL_SERVERS_FILE` | `model_servers.toml` | Models served by OpenAI-compatible servers, with their backends and providers (see Model servers below) |
| `MODEL_SERVERS_SAVED_FILE` | `data/model_servers.json` | Where `/admin` saves its edits; each replaces the file's entry of the same name. Empty: read-only in `/admin` |
| `MODEL_SERVERS_ALLOW_COMMANDS` | `false` | Let `/admin` save `cmd` and `env`, which run on this host. Off: only the file's commands can be kept |
| `LOG_LEVEL` | `INFO` | Logging level |
| `ENV_FILE` | `.env` next to `config.py` | Settings file read at startup and written by the setup wizard. Containers use `/app/data/config/settings.env` on the `config` volume |
| `SETUP_WIZARD` | `true` | While no key is set, `/admin` opens the setup wizard. `false` turns it off |
| `MONITORING_RETENTION_DAYS` | `7` | Days of the dashboard's request history (token counts included) and model events, in the ArcadeDB database; older rows are deleted hourly. 0 records none; `/metrics` counts in memory regardless |
| `MAX_PROMPT_TOKENS` | `32768` | Token IDs a prompt given as tokens may hold |
| `MAX_COMPLETION_PROMPTS` | `64` | Prompts per `/v1/completions` request |
| `MAX_HUB_DOWNLOADS` | `2` | Hub downloads running at once; more wait, the most urgent first, then the oldest |
| `WEBHOOK_URLS` | empty | URLs POSTed to when a job or eval run finishes |
| `WEBHOOK_SECRET` | empty | Signs each webhook: Standard Webhooks `webhook-signature` (verify with `whsec_<base64 of the secret>`), and `X-Toolkit-Signature: sha256=...` of the body |
| `TRACE_RETENTION_DAYS` | `3` | Days each request's input and output are kept (as traces, in ArcadeDB); a day's traces are dropped together. `0` keeps none |
| `TRACE_MAX_CHARS` | `8000` | Longest string a trace keeps per field |
| `JUDGE_MODEL`, `JUDGE_BASE_URL`, `JUDGE_API_KEY` | unset | The default LLM judge for benchmark runs: a model this service runs (asked through its own `/v1`), or one behind an OpenAI-compatible endpoint; the key is sent to that endpoint only |
| `JOBS_DIR` | `data/jobs` | The job table and each job's files. Empty disables jobs |
| `JOB_DATA_DIR` | `data/datasets` | The only directory job params can name input files in (`POST /jobs/data` uploads there) |
| `JOB_ENVS_DIR` | `envs` | Job environments: `<dir>/<env>/bin/python` |
| `MAX_RUNNING_JOBS` | `1` | Jobs run at once; more wait, the most urgent first (`priority`), then the oldest |
| `JOB_NUM_CPUS` | `0` | Cores a job may use; 0 is every core but one |
| `JOB_DEVICE` | `cpu` | `cpu`, `cuda`, `mps` or `auto`, for jobs and served AutoGluon predictors |
| `JOB_MAX_UPLOAD_BYTES` | 2 GiB | Largest file `POST /jobs/data` accepts |
| `ENRICHERS_DIR` | `data/enrichers` | The operator's enrichers (one file each, written by `/admin`). Empty disables enrichers |
| `ENRICHER_TERMS` | all four | Sources that may run, by what their terms allow: `open`, `attribution`, `non_commercial`, `paid` |
| `ENRICH_ALLOW_PRIVATE` | `false` | Let enrichers call private and local addresses |
| `ENRICH_CONCURRENCY` | `8` | Enrichers running at once across all lookups |
| `EVIDENCE_DIR` | `data/evidence` | Captured files, each capture's record and signature, and the signing key. Empty disables `/evidence`; it also needs `ARCADEDB_PATH` |
| `EVIDENCE_TSA_URL` | `https://freetsa.org/tsr` | RFC 3161 time stamping authority each record's hash is sent to; empty disables time stamping |
| `EVIDENCE_KEY_FILE` | `<EVIDENCE_DIR>/keys/ed25519.pem` | Ed25519 signing key (PKCS#8 PEM, 0600), made on first use; `ed25519.pub.pem` is written beside it |
| `EVIDENCE_ALLOWED_ORIGINS` | empty | Browser origins allowed on `/evidence`, exactly; empty means any `chrome-extension://` or `moz-extension://` origin |
| `EVIDENCE_ENRICH`, `EVIDENCE_ENRICH_TIMEOUT` | `true`, `20` | Look the page's server address and hosts up with the enrichers and keep the results inside the signed record, within this many seconds in all |

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

# Job environments: AutoGluon (all of it: tabular, multimodal, time series)
scripts/setup_job_env.sh autogluon

# Unit tests (no model downloads, no torch needed)
make test                    # uv run pytest, then the frontend's vitest
make lint                    # uv run ruff check, then the frontend's tsc
```

## Browser Pages

`/admin`, `/admin/setup` and `/playground` are a React + TypeScript app in `frontend/`, one Vite entry per page sharing `src/shared/`. `npm run build` typechecks and writes the pages and their hashed scripts and styles to `api/static`, which FastAPI serves at `/admin/static`; `api/pages.py` serves each page with `Cache-Control: no-cache`, or a 503 page with build instructions when it has not been built. The bundle is self-contained: no CDN or other external scripts. The Dockerfile builds it in a Node stage.

The pages are ordinary API clients. They ask for a key, keep it in `sessionStorage` for the tab, and send it as `X-API-Key`; the playground also accepts the admin key. Every call is typed from the OpenAPI schema: `npm run gen:api` exports it with `scripts/export_openapi.py` (default settings, not `.env`) and generates `src/shared/api-schema.d.ts`. The client (`src/shared/api.ts`, built on [openapi-fetch](https://openapi-ts.dev/openapi-fetch/)) takes a route's path as the schema writes it, `client.get("/jobs/{id}", { params: { path: { id }, query } })` with `body` beside `params`, and gets the parameters, body and reply types from the schema, so a misspelt route, a missing parameter or a reply read wrongly fails the typecheck; it throws `AuthError` on a 401 (after showing the sign-in form) and `ApiError` with the server's detail otherwise, `parseAs: "text"` reads a plain-text reply, and `client.raw` returns the reply itself, for streams and files. `tests/test_openapi_snapshot.py` fails when the committed schema no longer matches the API. Replies are typed by the routes' response models (`api/responses/`), which `src/shared/types.ts` names for the pages; only documents the API passes through as they are (the signed evidence record, the tasks' JSON Schemas) are typed by hand.

`/admin` and `/playground` share a frame (`src/shared/Shell.tsx`, menu items in `nav.ts`): a menu on the left (a strip across the top on narrow screens) and the page beside it. Each page is one entry in `nav.ts` (key, label, link, blurb, icon; `off` for a section switched off, as Hub is), and `src/admin/sections.tsx` gives each `/admin` section its view, keyed by the entries' types, so a menu entry without a view or a view the menu does not list fails the typecheck. The pages load what they show with [TanStack Query](https://tanstack.com/query): `client.useQuery("get", "/jobs", { params }, options)` (openapi-react-query) for one route, keyed by method, path and options, or `useQuery` with a key of its own for a load of several calls. Each page has one cache (`src/shared/query.ts`), so views asking for the same thing share a request, and a write marks what it changed for loading again (`invalidateQueries`) or sets it (`setQueryData`); a sign-in loads every query again. Its defaults differ from the library's: a failed load is not retried, a query loads again while it shows only when it polls (`poll()`: fixed, or worked out from the data, paused while the tab is hidden and at once when it comes back), and the last value stays while the options change until the new one arrives. A load that depends on something it does not send (a job ending, a model loaded elsewhere) loads again with `useReloadOn`, and one that adds to the value it replaces (new log lines, a download's speed) reads that value from the cache. Writes go through `useAction` (`src/shared/action.ts`), a mutation that toasts its outcome or keeps it under the form. No page loads data any other way. The query devtools show in `npm run dev`. `/admin` is split into sections, picked by the URL hash so they can be linked and the back button works: Overview (tiles and charts), Models (the model browser as a page, where models are also tried), Hub (below), Jobs (below), Running (loaded models, manual load, memory limits), Model servers (below), Activity & logs and Evals (below), Data (the `/db` namespaces and tables, with a switch that makes a new table a graph table, the namespace's edge types, a Query panel that runs read-only SQL or Cypher over them, and a Graph view that draws the graph tables' rows and edges with sigma.js and filters them in the browser: tables and edge types switched off, rules on fields that dim or hide, a search that highlights, the degree), Enrichers (below), and Settings (configuration and process). Playground is its own page. `/admin/setup` keeps a plain header, since it runs before there is a key.

Activity & logs (`src/admin/ActivitySection.tsx`) charts requests and latency over a range the reader zooms and pans (`ActivityChart.tsx`, on ECharts: drag to zoom into a range (its brush), scroll to zoom (its inside zoom), drag the navigator strip's window (its slider), shift+drag to pan; each reports the range asked for, which the page loads, and the arithmetic is in `activity.ts`), marks model events beneath, and lists the range's requests, per-model figures and model events in tabs, plus the server log. It polls `GET /admin/timeline?since&until&buckets` (the metrics of `/admin/stats` over any range, at any resolution), `/admin/events` and `/admin/requests` with the same bounds (and a status or model filter), and `GET /admin/logs?after_id`, which serves the process's newest log lines from an in-memory ring (`monitoring/logs.py`, attached to the root and uvicorn loggers in `api/app.py`); nothing is written to disk and the buffer starts empty on each restart. A range ending now is live and follows the clock.

Traces are each request's input and output, recorded by the model manager (`models/manager.py` hands every request, as one `RequestEvent` (`models/events.py`), to each of its sinks: the metrics, which the monitoring store keeps for the dashboard, and `evals/store.py`) with the model, task, outcome, timing, and the tags and session the request's `X-Trace-Tag` and `X-Session-Id` headers gave it (the playground tags its own). They live in the ArcadeDB database, a day's traces in one bucket (`tk_trace_d20261017`), and expiry drops whole buckets older than `TRACE_RETENTION_DAYS` instead of deleting rows; `compact()` cuts long strings to `TRACE_MAX_CHARS`, keeps media as its size and hash, and caps long lists. They have no page of their own: the requests table of Activity & logs (`src/admin/ActivitySection.tsx`) is the one list of requests, and a row opens its trace (`src/admin/TraceDialog.tsx`, at `#/activity/<trace id>`; old `#/traces/<id>` links still work). The table normally lists the request metadata of the monitoring store for the chart's range; typing words or a tag switches it to `GET /admin/traces`, since only traces have the words (a full-text index backs that) and tags, and the table then says it matched among the kept inputs and outputs only. `/inference` answers carry a `trace_id`. Recording never blocks a request: traces queue up for one writer thread. Runs (below) leave no traces; a judge that is a model this service runs is asked through its own `/v1`, and those requests are ordinary ones, tagged `judge`.

Datasets, examples, runs, items and scores are `/db` tables in the `evals` namespace, jobs and schedules in `jobs`, the enrichers' results in `enrichers`, the playground's saved chats and presets in `playground`, and the evidence ledger's cases and captures in the `evidence` namespace, each other evidence workspace's in its `evidence_<id>` namespace (`vectorstore/tables.py`). Each store declares its namespace with its tables (`TableStore.manage(namespace, owner, tables)`, `manage_prefix` for the workspaces), and the table store makes them, or brings their schemas up to date, when it first loads, so `/db` lists them before their stores are used. The Data section browses and queries them like any other tables, but they need the admin key there (401 otherwise, also for reads) and are read-only: `/db` refuses to write to these namespaces (403) or to make or drop them, since only their stores keep their invariants (cascading deletes, the hash chain). The database marks each as kept, so one stays protected when its store is switched off (`EVIDENCE_DIR` or `ENRICHERS_DIR` left empty): it shows as kept by "a part of the service that is switched off".

Evals (`src/admin/EvalsSection.tsx`; a dataset in `EvalsDataset.tsx`, a run in `EvalsRun.tsx`, two runs compared in `EvalsCompare.tsx`) build on traces: a dataset holds examples (an input, maybe a reference answer, a task), copied from traces, added by hand or imported as JSON Lines (the export writes the same), kept as given: a run sends the model exactly that, so a trace whose input was cut when recorded is refused. A run is one model, with fixed params, over a dataset: `evals/runner.py` queues runs as tasks (`jobs/tasks.py`) and executes them one at a time through the model manager, each example's output becoming a run item; the examples go as many at once as the model takes (`ModelManager.parallelism`: a batch of 32 for one that answers several in one pass, its slots otherwise, so one that answers one at a time still gets them one at a time), and the items are kept in the dataset's order; a queued run waits by its `priority`, then goes before others if its model is loaded already, so the runs for the models in memory finish before another model loads, and its requests carry that priority when models compete for room; a run the service stopped under resumes by itself, and one that failed or was cancelled on `POST /admin/runs/{id}/resume`, skipping the examples it has items for. Items are scored on [Pydantic Evals](https://pydantic.dev/docs/ai/evals/) (`evals/scoring.py`): each item is a case whose task looks up the stored output, and one evaluator scores the cases, by a metric that needs no model (exact match, contains, JSON valid) or by the LLM judge: a chat model behind an OpenAI-compatible endpoint, or one this service runs, asked through its own `/v1` in-process (`JUDGE_*` give the defaults; `JUDGE_API_KEY` goes to `JUDGE_BASE_URL` only), that pydantic-ai's grading agent asks for a structured verdict (a score 0–1, pass or fail, and the reason) per output, or, against another run of the same dataset, which of two outputs is better, asked in both orders so position bias cancels (disagreement is a tie). A judge call that fails is asked again with backoff, then recorded on that item alone, so one bad reply never loses the run's scores. Two runs compare example by example.

The model browser (`src/shared/model-browser/`) is the Models page on `/admin` (`ModelBrowserPanel`) and a dialog elsewhere (`ModelBrowser`): the tasks down the side, and for the chosen task a search, status and tag filters, model cards, and a box for any other ID the task takes. `ModelBrowser.tsx` is the dialog and task list, `ModelList.tsx` and `ModelDetails.tsx` the two panes, `catalog.ts` the logic (filtering, plan wording, the memory bar; tested in `catalog.test.ts`), and `model-browser.css` its styles. On the Models page a model opens its details in two views. *Overview*: the memory it needs in the chosen dtype and what loading it would unload (`GET /models/estimate`), with Load or Unload. *Try it*: send it requests (below). "Browse" next to each playground model list picks a model instead.

Trying a model (`src/shared/task-io/`) follows the task contracts. `TryPanel.tsx` gives each built-in task its own input and output (`inputs.tsx`, `outputs.tsx`): a prompt with generation settings, an image upload for vision-language (sent as an image part, a data URL), one text per line for embeddings (shown with a cosine similarity grid), an audio file for speech-to-text, a question builder for decisions (choice, yes/no, score), and for results classification bars, the answer highlighted in its context, decision probabilities. A task without its own input gets a form made from its schema (`SchemaFormInput`, on `src/shared/schema-form.tsx`), or raw JSON when it has no contract. Caps, ranges and defaults come from `GET /tasks` (the schemas, plus `limits` for media sizes), so the page checks what the server checks before sending; `build.ts` turns the fields into the input and says what is missing. Requests go to `POST /inference`. The cards come from `GET /models/catalog`: the `ModelCard`s of the model files and of `MODEL_SERVERS_FILE` entries, with each model's status and the weights it has in the local Hub cache.

## API Endpoints

Native:
- `GET /`: Service status
- `GET /health`: Health check, device and cache status
- `POST /inference`: Run a model: `{model_id, task, input}` in, the task's `result` out (see Task Contracts)
- `GET /tasks`: Every task's input and result as JSON Schema, and `limits`: the caps schemas cannot show (media and audio sizes, batch sizes)
- `POST /vision-language`: Run FastVLM on text plus an optional image (`image_path` is relative to `IMAGE_DIR`); the same as `/inference` with task `vision-language`, under older field names
- `POST /models/load`: Preload a model; `pin: true` keeps it loaded until it is unloaded explicitly (admin)
- `PUT /models/{model_id}/{task}/pin`: `{"pinned": true|false}` on a loaded model (admin)
- `DELETE /models/{model_id}/{task}`: Unload a model, `model_id` may contain `/` (admin)
- `DELETE /models/clear`: Unload all models (admin)
- `GET /models/status`: Cache contents and memory use, `loads_pending`, and per model `pinned`, `in_use`, `queued`, `avg_request_ms` and `estimated_wait_ms` (admin)
- `GET /models/list`: Available models by task (filtered by `ALLOWED_MODELS`)
- `GET /models/types`: Every registered model class with its task, IDs, patterns and whether it is a fallback
- `GET /models/catalog`: Every task with its models as the model browser shows them: description, tags, serving class, status, weights on disk (filtered by `ALLOWED_MODELS`; reads only the local Hub cache)
- `GET /models/estimate?model_id=&task=&torch_dtype=`: The memory a model would need and what the cache would unload to load it now; nothing is loaded (admin)

OpenAI-compatible (same API key rules):
- `GET /v1/models`: Models with a non-standard `task` field
- `POST /v1/rerank`: `{model, query, documents, top_n?, return_documents?}` over a `rerank` model (a cross-encoder); results with `index` and `relevance_score`, best first
- `POST /v1/chat/completions`: Chat over a `text-generation` model, plain or streamed. Models whose tokenizer has a chat template get the chat in that format (with thinking mode off); others get a `User:`/`Assistant:` transcript. Chats longer than `MAX_INPUT_CHARS` lose their oldest turns first, keeping the system prompt. Message content may be a string or a list of text parts (image parts are refused with 422). Streamed replies arrive as the model writes them (from models whose `predict` names `on_text`, which the built-in text-generation models do; others send one chunk), and generation stops when the reply ends or the client disconnects. A model that fails to load still gets an HTTP error; a failure mid-reply arrives as an `{"error": ...}` event. The reply (the final chunk, when streamed) carries a non-standard `toolkit` field: `processing_ms` and `cold_start` (the model had to be loaded first). `tools`, `tool_choice` and `response_format` (`json_object`, `json_schema`) work as with OpenAI: a chat template that takes tools renders them, other models get an instruction, and the reply is parsed for calls or JSON (`models/toolcalls.py`); a model server does it itself. A reply without the JSON asked for (or not matching the schema) is a 502; streamed replies with tools or a response format arrive as one chunk at the end. OpenAI-compatible clients such as the AI SDK's `@ai-sdk/openai-compatible` work with `baseURL` set to `http://<host>:8000/v1`. The reply carries `usage` when the model counts tokens (in-process models do; servers pass theirs on, streamed in a closing chunk when asked with `stream_options.include_usage`), `finish_reason: "length"` when `max_tokens` was used up, `stop` strings end the reply, and `logprobs` / `top_logprobs` return the generated tokens' log probabilities in the chat shape.
- `POST /v1/completions`: The legacy prompt form evaluation harnesses use. `prompt` is text, a list of texts, or token IDs (one list or a list of lists), one choice per prompt; `logprobs: n` gives each generated token's log probability with `n` alternatives (`tokens`, `token_logprobs`, `top_logprobs`, `text_offset`), and `echo` scores the prompt's tokens as well (`max_tokens: 0` to score alone), which is lm-evaluation-harness's loglikelihood request. In-process models answer through `generate()` with logits kept plus a forward pass for the echo (`TransformersModel._score`); a model server gets its own `/v1/completions`. Streaming takes one text prompt without logprobs or echo.
- `GET /metrics`: Prometheus text format (`monitoring/metrics.py`): request, cold-start, token and model-event counters since start, a latency histogram per task, and gauges from the cache and host at scrape time. Client or admin key.
- `/hub/*` (admin): `GET /hub/search?q&task&limit` (the Hub's most downloaded matches, with `on_disk`), `GET /hub/repos/{id}` (files, sizes, license, gated), `POST /hub/downloads {repo_id}` (202; a child process runs `snapshot_download`, progress from the cache folder's size against the repo's total), `GET /hub/downloads`, `DELETE /hub/downloads/{id}` (cancel or dismiss), `GET /hub/local` (the cache's repos with size, last use, whether loaded and the tasks that claim them), `DELETE /hub/local/{id}` (409 while loaded or downloading). `HF_HUB_OFFLINE` makes the Hub routes answer 503.

Playground (same API key rules; the page itself is public and asks for the key):
- `GET /playground`: A tab per category (`src/playground/categories.ts`), picked by the URL hash (`#text`). Each picks its model from a list of the catalog's models (loaded first), or Browse for the full catalog and other IDs. *Chat*: chat with a model, or add columns to send each message to up to `MAX_MODELS` models side by side. Replies stream in and can be stopped. Each column keeps its own history and shows the time to the first text, the total time and cold loads. *Vision*, *Text* (classification, question answering, embedding), *Audio*, *Decision* and *Other* (custom tasks): one model of the chosen task, with its form and result (`TryPanel`, as in the admin's Try it). *Prediction* (tabular, multimodal, time series) is disabled and not shown for now. `?model=ID&task=TASK` opens on that model. The tab, settings and model choices are remembered in the browser; conversations are not.
- `GET /playground/limits`: `MAX_MODELS`, `MAX_GENERATION_LENGTH` and `MAX_INPUT_CHARS`, which size the page's controls

Jobs (need `ADMIN_API_KEY`, or `API_KEY` when no admin key is set; see [jobs/README.md](jobs/README.md)):
- `GET /jobs/types`: Every job with its environment (and whether it is set up), and its params and result as JSON Schema
- `POST /jobs`: `{job, params}`; queues the job and returns it with its `id` (202). 400 for bad params or an environment not set up
- `GET /jobs?status=&limit=`, `GET /jobs/{id}`: Status, progress, message, result, error
- `GET /jobs/{id}/events`: Server-sent events until it ends: `status` on every change, `log` with new log text
- `GET /jobs/{id}/log`: The end of its log
- `POST /jobs/{id}/cancel`: Cancel it, stopping its whole process group
- `DELETE /jobs/{id}`: Delete a finished job and its files (a trained predictor too)
- `GET /jobs/{id}/files`, `GET /jobs/{id}/files/{path}`: Its directory: params, log, result, outputs
- `GET /jobs/data`, `POST /jobs/data` (multipart `file`), `DELETE /jobs/data/{path}`: Input files in `JOB_DATA_DIR`

Model servers (admin key; see Model servers below):
- `GET /admin/model-servers`: The providers and models in effect, where each comes from (`file`, `saved`, `replaced`), each model's status and serving backend, and problems
- `PUT /admin/model-servers/providers/{name}`, `DELETE ...`: Create or replace a provider; delete a saved one, or revert one in the file. 409 while a saved one is in use
- `PUT /admin/model-servers/models/{model_id}`, `DELETE ...`: Create or replace a model's entry (`task`, `backends`, `description`, `tags`); delete a saved one, or revert one in the file. 403 for a command the file does not give, unless `MODEL_SERVERS_ALLOW_COMMANDS`
- `POST /admin/model-servers/probe`: `{model_id, entry?}`; probes each backend of the entry in effect or a draft, as a load would, and says which it would use. Nothing starts

Enrichers (looking up needs the client key; editing the admin key):
- `POST /enrich`: `{value, type?, enrichers?, refresh?}`; the value's type is detected unless given, every enabled enricher taking it runs at once, and the reply holds `results` (one per enricher: `status`, `outputs`, `raw`, `cached`, timings) plus `merged` outputs with their `sources`
- `GET /enrichers`, `GET /enrichers/{name}`: Every enricher's definition, whether it is built-in or changed, whether its key is set, and whether it can run now (`usable`), with its request counter and cooldown
- `PUT /enrichers/{name}`: Create or replace one; a built-in's name changes the built-in (admin)
- `DELETE /enrichers/{name}`: Remove one, or a built-in's changes (admin)
- `PUT /enrichers/{name}/secret`: `{value}`; saves its API key to `ENV_FILE` and applies it (admin)
- `POST /enrichers/{name}/test`: `{value, type?}`; runs it now without the cache, even when disabled, and shows the request sent with the key masked (admin)
- `DELETE /enrichers/cache?enricher=`: Forget cached results (admin)

Evidence (the client key; an `Origin`, if sent, must be an extension's; see the Evidence section):
- `GET /evidence/cases`, `POST /evidence/cases {name}`: The cases (`inbox` always exists) and a new one
- `GET /evidence/cases/{id}/captures`: The case's captures, newest first: `seq`, `captureId`, `receivedAt`, `url`, `title`, `recordHash`, `timestampedAt`
- `POST /evidence/captures`: Multipart `metadata` (JSON with `caseId` and `artifacts`) plus one `file` per artifact; answers the record's `captureId`, `seq`, `receivedAt`, `recordHash`, `signature` and `timestamp`
- `GET /evidence/captures/{id}`: The parsed record with its `recordHash`, `signature` and `timestamp` (`genTime`, `error`)
- `GET /evidence/captures/{id}/artifacts/{name}`: One captured file, with its media type
- `GET /evidence/cases/{id}/annotations`: The tags and notes of the case's captures that have any: `{captureId, tags, notes: [{id, text, createdAt, updatedAt}], updatedAt}` each
- `PUT /evidence/captures/{id}/annotations {tags?, notes?}`: Replace the capture's tags, notes or both (a key left out keeps that list); never touches the record
- `GET /evidence/cases/{id}/todos`: The case's to-do list: `{caseId, todos: [{id, text, done, createdAt, updatedAt, doneAt}], updatedAt}`
- `PUT /evidence/cases/{id}/todos {todos}`: Replace it, in the order given (a known `id` keeps the todo, a todo left out is deleted); never touches a record
- `GET /evidence/verify`: `ok`, `records`, `timestamped`, `headHash`, `signingKeySha256` and the `issues` (`severity`, `seq`, `captureId`, `message`)
- `GET /evidence/public-key`: The signing key's public half, PEM
- `POST /evidence/timestamp-pending`: Ask the TSA again for records without a token; `{ok, failed}` (admin)

Perplexity-compatible (same API key rules):
- `POST /v1/decisions`: Typed decisions with the request and response format of [Perplexity's Decisions API](https://docs.perplexity.ai/docs/decisions/quickstart), served by vegaml. See below.

Setup (public; saving needs the setup code from the server log):
- `GET /admin/setup`: The first-run wizard
- `GET /admin/setup/status`: Whether setup is still needed
- `POST /admin/setup`: Save keys, allowed models and CORS origins to `ENV_FILE`
- `POST /admin/setup/skip`: Run without keys and stop showing the wizard

Admin (need `ADMIN_API_KEY`, or `API_KEY` when no admin key is set):
- `GET /admin`: The dashboard page. It holds no data and asks for the key itself
- `GET /admin/stats?window=1h|24h|7d`: Process and cache state, request totals, per-model stats and a 60-interval timeline
- `GET /admin/events?after_id=&limit=`: Model loads, failed loads, unloads and evictions, newest first
- `GET /admin/requests?limit=`: The latest requests' metadata
- `GET /admin/config`: Effective settings (keys shown only as set or not set) and warnings
- `GET /admin/traces?since&until&before&model&task&status&tag&session&q&limit`: Traces, newest first, without their inputs and outputs; `q` searches their words; `GET /admin/traces/{id}` has the whole trace
- `GET|POST /admin/datasets`, `GET|DELETE /admin/datasets/{id}`: Datasets; `GET|POST /admin/datasets/{id}/examples` lists and adds examples (given, as `trace_ids`, or as `jsonl`), `DELETE .../examples/{id}`, `GET .../export` writes JSON Lines
- `GET|POST /admin/runs`, `GET|DELETE /admin/runs/{id}`, `POST /admin/runs/{id}/cancel`, `POST /admin/runs/{id}/resume`: Runs of a model over a dataset; the detail has the items with their example's input and reference, their scores and a summary per metric
- `POST /admin/runs/{id}/score`: Score with metrics (`GET /admin/evals/metrics` lists them); `POST /admin/runs/{id}/judge`: start the LLM judge, by rubric or `against` another run; `GET /admin/runs/compare/{a}/{b}`: two runs side by side

## First-Run Setup

A fresh instance has no keys. At startup it logs a line like:

```
Not set up yet. Open http://localhost:8000/admin/setup#code=Xy3... to choose API keys and allowed models
```

`/admin` redirects there while setup is pending. The wizard generates an admin key and a client key in the browser, lets you pick the allowed models (models with their own file are preselected) and the browser origins, and saves them to `ENV_FILE`. Keys and the allow-list take effect at once; CORS changes, and every worker but one when `WORKERS > 1`, need a restart.

Saving needs the setup code from the log. Without it, anyone who reached a fresh instance first could set their own keys and lock its owner out. The wizard runs once: after that, change settings in the file.

## Monitoring

The manager records every inference (model, task, outcome, duration, whether it had to load the model first) and the cache records every load, eviction and unload. These rows hold no request text or images; only traces keep those, for `TRACE_RETENTION_DAYS`. Rows go onto a queue that one background thread writes to the ArcadeDB database in batches, so recording never slows a request; if the queue fills up, rows are dropped and counted.

History lives in the database, so it survives restarts (so `WORKERS` must be 1: only one process can open the database, and the service refuses to start otherwise). The loaded-models list and memory figures are the live state of whichever worker answers the dashboard.

## Jobs (`/jobs`)

A job is a long-running script, submitted and then followed: `POST /jobs {"job": "autogluon-tabular", "params": {...}}` returns at once with an ID, and the job runs in the background, one at a time by default. Each runs as its own process in its own Python environment, so it can use libraries the service cannot install (AutoGluon 1.6 needs Python <3.14, torch <2.14 and transformers <5.15, which vegaml and FastVLM cannot use), and a crash or an out-of-memory kill ends only the job. A job file is one file in a subfolder of `jobs/`, like a model file; see [jobs/README.md](jobs/README.md).

The AutoGluon jobs train a predictor and say in their result which model ID serves it: `autogluon:<job id>`, for the `tabular-prediction`, `multimodal-prediction` or `timeseries-forecast` task. Serving starts a process in the `autogluon` environment that the model cache loads, budgets and evicts like any other model.

```bash
scripts/setup_job_env.sh autogluon                                   # once
curl -X POST localhost:8000/jobs/data -H "X-API-Key: $ADMIN" -F file=@churn.csv
curl -X POST localhost:8000/jobs -H "X-API-Key: $ADMIN" -H 'content-type: application/json' \
     -d '{"job": "autogluon-tabular", "params": {"train_file": "churn.csv", "label": "churn", "time_limit": 600}}'
curl -N localhost:8000/jobs/<id>/events -H "X-API-Key: $ADMIN"      # follow it
```

## Model servers

A model server entry (`models/server_specs.py`) maps a model ID to a `task` and its `backends`, tried in order. A backend says where with one of `provider` (a server named once under `[providers]`, whose `url`, `health_path`, `max_concurrency`, `request_timeout` and `on_evict` it takes unless it sets its own), `url`, `cmd` (started here, `{port}` filled in) or `hub` (a Hub model run in this process by the model file that serves it). An entry without `backends` is its own one backend, as the file always took. `ServerStore` reads `MODEL_SERVERS_FILE` (TOML) and `MODEL_SERVERS_SAVED_FILE` (JSON, written by `/admin`), and a saved entry or provider replaces the file's of the same name. A bad entry is listed as a problem and skipped; the rest still load.

`ServerModel` (`models/model_server.py`) picks its backend on first need, which is `estimate_bytes()`: the cache asks for the size before it makes room, and the size depends on the backend. All backends are probed at once: a url backend must answer its `health_path` within 2 s and, when that is a model list, list `upstream_model` (Ollama's `name:latest` counts for `name`); one that answers without listing it is used only when nothing else can serve the model. A cmd backend's program must be on `PATH`, and a hub backend needs a class that serves its ID for the task. The first that can serve the model wins until it unloads; responses and traces name it (`backend`, e.g. `ollama: qwen3.5:4b`), so runs over different weights of one model ID can be told apart. No backend at all is `NoBackend`, a 503 with each one's reason.

A failure moves on rather than failing the request. A started server that exits or never answers, or a hub model that fails to load, is skipped for 60 s when the entry has another backend, and the load raises `TryNextBackend`; the manager then checks the model out again, and the fresh instance picks the next backend. A url server that stops answering (a connection refused before the request reached it) marks the instance dead: the cache drops it and the manager runs the request once more, on whichever backend is chosen next. On eviction, `on_evict = "unload"` asks an Ollama server to unload the model (`keep_alive: 0`), and `"sleep"` puts a vLLM server to sleep.

Editing in `/admin` (`api/model_server_routes.py`, the Model servers section in `src/admin/ServersSection.tsx` with helpers in `servers.ts`) writes the saved file and applies it at once: `model_server.reload()` registers the entries again (`registry.unregister` drops the old registrations), unloads the loaded models whose entry or provider changed once their requests finish, and stops their sleeping servers. Commands run on this host, so a save may only keep the `cmd` and `env` of the file's entry and of the entry in effect (`new_commands`), unless `MODEL_SERVERS_ALLOW_COMMANDS` is on. The section lists the entries with their backends in order, edits providers (a form made from the `Provider` schema, with Ollama and LM Studio presets), and probes an entry as edited before it is saved. The entry editor stays written by hand: which fields a backend shows depends on its kind, which is no field of the schema, and a backend's empty settings take its provider's, which a form filling in the schema's defaults would override.

## Enrichers (`/enrich`)

An enricher is one HTTP call to an outside service, described as data (`enrichers/spec.py`): its `inputs` (entity types), a `request` template (`{value}` is the value, URL-encoded in the URL; `{secret}` the key from the environment variable `secret` names; `{type}` the entity type), and `outputs` mapping names to JMESPath expressions over the JSON reply. It also carries `terms` (what the source allows: `open`, `attribution`, `non_commercial`, `paid`), a `ttl` for its cache, a `rate_limit`, a `timeout`, and `value_filters` (lower, upper, strip, md5, sha1, sha256, reverse_ptr; Gravatar wants the email's SHA-256, reverse DNS the address's `in-addr.arpa` name). The built-in ones are JSON files in `enrichers/builtin/`; the operator's are files in `ENRICHERS_DIR`, written by the dashboard, and one with a built-in's name replaces it until deleted.

`POST /enrich` classifies the value (`classify.py`: IPv4, IPv6, CIDR, ASN, domain, URL, email, phone, MD5, SHA-1, SHA-256, CVE, crypto wallet, else generic; defanged forms such as `1.2.3[.]4` are read), then `EnrichService.enrich()` runs every enricher taking that type at once, under `ENRICH_CONCURRENCY`. Per enricher, `run_one()` answers `disabled`, `not_allowed` (terms outside `ENRICHER_TERMS`) or `no_key` without a request, serves a cached row if the TTL has not passed, counts the request against `rate_limit` (`rate_limited` when reached, or while a 429 from the source has it paused), renders and sends the request, and maps the reply: 404 is `not_found`, other errors `error` with the HTTP status, and a non-JSON reply is kept as text. `ok` and `not_found` results are cached (`cache.py`: `ResultCache` in the ArcadeDB database, `MemoryCache` without one; expired rows are pruned hourly from `api/app.py`). Before a request, the host is resolved and private, loopback and link-local addresses are refused (`ENRICH_ALLOW_PRIVATE` lifts that), because the templates are editable. Redirects are followed up to five hops with the same check each time, and replies over 1 MB are refused.

The Enrichers section of `/admin` (`src/admin/EnrichersSection.tsx`, helpers in `enrichers.ts`) lists the enrichers with whether each can run and why not, looks a value up across them, and edits each in a form made from the `EnricherSpec` schema (the page adds only its sections, wording and placeholders, `enricherUi`, so a field added to the spec shows up by itself), a test button (`POST /enrichers/{name}/test`) that shows the resolved request and result, and a key field (`PUT /enrichers/{name}/secret`) that writes the key to `ENV_FILE`.

The Jobs section (`src/admin/JobsSection.tsx` and its parts in `Jobs*.tsx`, helpers in `jobs.ts`) lists schedules (`GET /jobs/schedules`: cadence, next and last run, the last job's status) and edits them in a form (job type, its params as a form made from the job's params schema in `GET /jobs/types`, cron or interval, webhook URL, enabled), runs one now, submits jobs by hand, follows jobs with progress and their logs (`GET /jobs/{id}/log`), and shows the webhook configuration with a test button (`GET /jobs/webhooks`, `POST /jobs/webhooks/test`). Schedules live in the `jobs/schedules` table (`jobs/store.py`); `jobs/schedule.py` parses cron, computes the next run, and its `Scheduler.tick()` submits due schedules every 15 seconds, skipping one whose previous job is still queued or running. `jobs/webhooks.py` POSTs `job.finished` and `run.finished` events (signed with `WEBHOOK_SECRET`) to `WEBHOOK_URLS` plus the URL a job, schedule or run names, with retries, in the background.

The playground's chat saves conversations and presets (`src/playground/saved.ts`) through `/playground/conversations` and `/playground/presets`, stored as rows of the `playground` namespace in the `/db` tables (one the service keeps, so read-only there; the namespace the playground made through `/db` before 2026-10 is adopted), and offers "Add to dataset" (admin key) which `POST /playground/conversations/{id}/example` turns into a dataset example.

The Hub section (`src/admin/HubSection.tsx`, helpers in `hub.ts`) searches the Hugging Face Hub as you type with a task filter (`GET /hub/search`), shows each result's downloads, likes and whether its weights are already on disk, and downloads one after confirming its size (`GET /hub/repos/{id}`, then `POST /hub/downloads`). A downloads card polls `GET /hub/downloads` (quickly while one runs) with a progress bar, speed, cancel and dismiss; an on-disk card lists the cache's repos (`GET /hub/local`) with size, last use and whether a model is loaded from them, and deletes weights after a confirm (`DELETE /hub/local/{id}`), refused while the model is loaded.

## Evidence (`/evidence`)

A browser extension (not part of this repository) captures a web page (screenshot, MHTML, live DOM, response headers, server IP, redirects, TLS details) and uploads it as one multipart request: a `metadata` JSON field and one `file` field per artifact, each artifact's SHA-256 computed in the browser and declared in `metadata.artifacts`. `api/evidence_routes.py` checks every route's API key and, when the request carries an `Origin`, that it is an extension origin (or one of `EVIDENCE_ALLOWED_ORIGINS`), so web pages cannot reach the routes even with the key. Its refusals are `errors.py`'s like every route's (the shared handler answers `{"detail": ..., "error": ...}`, the `error` key being what the extension reads); a JSON body that does not validate is FastAPI's 422. Evidence capture needs `ARCADEDB_PATH` and `EVIDENCE_DIR`; without them the routes answer 503.

Evidence is kept by workspace (`evidence/store.py`): `EvidenceStore` keeps the workspaces (`tk_evidence_workspace`; `default` always exists, others are made with the admin key, `POST /evidence/workspaces`, by the ids of the application they stand for) and hands out one `Ledger` per workspace. A ledger is the same four tables, cases, captures, annotations and todos (`TABLES`, the template every workspace gets; annotations are the tags and analyst notes added after a capture and todos a case's to-do list, the mutable tables, kept apart from the records and never part of a record, its hash, the chain or `verify`), in its own `/db` namespace: `evidence` for the default workspace, which held every capture before workspaces, and `evidence_<id, - as _>` for the others, a prefix reserved for the service (`TableStore.manage_prefix`), admin-only and read-only through `/db`. Each ledger has its own chain, append lock and files (`EVIDENCE_DIR/workspaces/<id>/` for a workspace other than the default one). The routes about cases, captures and verifying take `?workspace=` (the default one when left out), and a capture is found only in its own workspace.

`POST /evidence/captures` re-hashes every upload and refuses a mismatch, an undeclared or missing file, a bad artifact name (1–100 bytes of `[a-z0-9._-]`, starting with a letter or digit, since it becomes a file name) or an unknown case. It then looks the page's server address and hosts up with the enrichers (`EVIDENCE_ENRICH`, within `EVIDENCE_ENRICH_TIMEOUT` in all; a source that fails or times out is recorded as such, never failing the capture) and `Ledger.append` (`evidence/store.py`) builds the record under the workspace's lock: schema `evidence-capture/record/v3`, `seq`, `captureId`, `workspaceId`, `caseId`, `receivedAt`, `prevRecordHash` (the chain head's hash, or 64 zeros for the first record), `service` (name, version, the signing key's SHA-256), the verified `artifacts`, the `client` metadata verbatim and the `enrichment`. `rfc8785` serializes it to canonical JSON; those bytes are SHA-256 hashed and Ed25519-signed (`signing.py`, the key in `EVIDENCE_KEY_FILE`, made on first use), written to `EVIDENCE_DIR/captures/<id>/` (`record.json`, `record.sig`, read-only `artifacts/`; `files.py`) and only then committed as an `evidence/captures` row, which keeps the canonical bytes verbatim; a failed commit removes the directory. The record's hash is then sent to the RFC 3161 authority at `EVIDENCE_TSA_URL` (`tsa.py`, SHA-256 imprint, random nonce, the TSA's certificate requested); a granted token is stored whole as `record.tsr` and in the row, a failure is recorded and retried hourly and by `POST /evidence/timestamp-pending`. The reply carries the capture's id, `seq`, `receivedAt`, `recordHash`, `signature` and the time stamp outcome (`granted`, `failed` or `disabled`).

`GET /evidence/verify` (`verify.py`) re-reads every record of the workspace in order and reports issues per record: a sequence gap, bytes that no longer hash to the stored hash, a signature that fails, non-canonical bytes, a broken chain link (checked against what the previous record claims, so one bad record gives one error, not a cascade), a `seq`, `captureId` or `caseId` that differs from the row, a `workspaceId` other than the ledger's (v2 records, which have none, are the default workspace's; so a record copied into another workspace's ledger is caught, though its signature still verifies), a different signing key (a warning), `record.json` or an artifact on disk that differs, and a time stamp token that is missing (a warning) or does not cover the record's hash. A chain cannot show that the newest records were removed: keep `headHash` elsewhere, or rely on the tokens. `openssl pkeyutl` and `openssl ts -verify` check a capture directory independently, with `GET /evidence/public-key`.

The Evidence section of `/admin` (`src/admin/EvidenceSection.tsx`, helpers in `evidence.ts`) picks the workspace (`?workspace=` in the hash) and makes new ones, lists its cases and creates them, lists a case's captures (sequence, time, page, time stamp state, record hash) and opens one: the record's fields, the artifacts with downloads, what the enrichers found, and the signed record as JSON. Its Ledger card verifies the workspace's ledger and retries the time stamps of every workspace.

## Decisions (`/v1/decisions`)

Send a `state` and up to 128 named questions, and get back probabilities instead of text. It needs `uv sync --extra vega`.

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

- `noul` returns the probability of yes. `choice` returns the top option, `confidence` and `probabilities`. `score` returns the probability-weighted level, `confidence`, `legend` and `probabilities`.
- Each answer also carries vegaml's `abstain` (the engine does not stand behind it), `prediction_set` (conformal answer set) and `unbound` (input looks out of distribution).
- `state` is a string, an object, or an array. An array of `{"type": "text"}` parts is joined into one text. One `{"type": "image_url"}` part holding a PNG, JPEG or WebP data URL is answered from the image. vegaml reads the image in place of the text, so an image cannot be mixed with text parts.
- Models are `vega-0.8b` (default checkpoint) and `vega-4b`. Other `vega-*` or `owner/vega-*` IDs are loaded as Hub repos or local paths.
- Unlike Perplexity, validation errors are FastAPI's `422 {"detail": ...}`, and an over-long state is `413`.
