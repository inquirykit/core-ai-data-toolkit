# AGENTS.md

How to work in this repo, for people and coding agents. Read it before changing anything.
[STRUCTURE.md](STRUCTURE.md) is how the code is built, with a map of every folder; [docs/GUIDE.md](docs/GUIDE.md)
is for running and calling the service; [models/README.md](models/README.md) and [jobs/README.md](jobs/README.md)
are the guides for adding models and jobs.

## What this is

A self-hosted FastAPI service for small, efficient models (BERT-style classifiers and encoders, rerankers,
speech-to-text, short scripts such as polars transforms) and the data work around them: one queue of background
work, an embedded DuckDB database, traces and evals, monitoring and an audit log, enrichers, evidence capture, and
React pages (`/admin`, with its playground, and `/admin/setup`).

- Big LLMs are not run here. They are forwarded to a server the operator already runs (Ollama, LM Studio, vLLM)
  through model server entries (`models/servers/`). Judge performance work by small models and short scripts: keep
  the service's own cost per request small, and remember that batching (`models/batching.py`) is the main lever.
- One process, one database. The embedded DuckDB file can be opened by one process only. Don't design for several
  processes unless asked.

## Layers

Packages import only downward. A cycle or an upward import is a bug. `tests/test_layers.py` reads every import
(those inside functions too) and fails on one that breaks the rules below. A new package gets its layer in the
test's `LAYERS` and in this table.

```
core        settings, errors, priorities, discovery, child processes, every() imports none of ours
db          Database, store bases, /db tables, graph and vectors              core
tasks       the one queue of background work, schedules, webhooks             core, db
models      task contracts, registry, cache, manager, families, servers       core
jobs evals evidence enrichers monitoring playground buckets views workflows access (domain)
                                                                              the layers above, never each other
api         FastAPI: the app, routers, deps, response models                  everything
frontend    React pages, typed from the API's schema                          the HTTP API only
```

- Only `api/` imports FastAPI or Starlette. Domain code raises the refusals in `core/errors.py` (`Refused` 400,
  `Unauthorized` 401, `Forbidden` 403, `NotFound` 404, `Conflict` 409, `TooLarge` 413, `Unavailable` 503) or a
  plain `ValueError` (400). `api/app.py` answers them all with their status. Don't raise `HTTPException`, and don't
  map exceptions to statuses in a route. A domain's own error subclasses one of them (`TableError(Refused)`,
  `CapacityError(Unavailable)`), so it carries its status.
- The packages above import `db/`, `tasks/` and `models/` through their public modules only (`PUBLIC` in
  `tests/test_layers.py`: `db`, `db.tables`, `db.graph`, `db.specs`; `tasks.runner`, `tasks.schedule`,
  `tasks.webhooks`; `models` and its contracts, events, capture, chat, registry, servers and toolcalls), so each can
  rearrange its insides. A name they need from another module there is re-exported by a public one (`db/tables.py`
  re-exports `db/schema.py`'s, `tasks/runner.py` `tasks/store.py`'s).
- Domain packages don't import each other. When one needs another, the caller passes it in as an argument: the
  capture route hands `evidence/enrich.py` the enrichers' service (`api/evidence_routes.py`).
- `db`, `tasks`, `jobs`, `evals`, `evidence`, `enrichers`, `monitoring`, `buckets` and `views` never read
  `core.config`: `create_app()` passes them their settings as arguments. Only `api/`, `main.py` and `models/`
  (request limits in the contracts, `IMAGE_DIR`) read `Config`.
- Domain logic lives in the domain package; a route parses the request, calls the service and shapes the reply.

## Wiring

- `api/app.py`'s `create_app()` is the one composition root. It builds every service from `Config` around the one
  `Database`, keeps each on `app.state`, starts and stops them in `lifespan`, and includes the routers in order.
  Shutdown order matters: the task runner stops, models unload, the background writers flush, the database closes
  last.
- `app = create_app()` runs at import: importing `api.app` reads the settings and the built-in enrichers' files; the database
  opens on first use. Tests share that one app.
- Routes get services through the `Annotated` types in `api/deps.py` (`runner: deps.Jobs`), not
  `request.app.state`. The few that read `app.state` say why (a check that must run before the key).
- Every service is always there, around the one database in `DATA_DIR`, whose layout is `LAYOUT` in
  `core/config.py`. Only monitoring and the audit log can be off (a retention of 0): they are then `None` on
  `app.state`, `deps.Monitoring` and `deps.Audit` hand the route `None`, and the route does without.
- Work that outlives a request is one of two things, never a task a route starts and forgets. Periodic work is a
  `core.background.every()` loop started in `lifespan` (prunes are hourly). Queued work is a `Kind` of task on the
  one `TaskRunner` (`tasks/runner.py`), declared once by its owner: `tasks.add(Kind(...))`.
- Child processes (jobs, model servers, predictors) start through `core/procs.py` with `child_env`: only
  the variables a program needs to run (`PASSED`, `PASSED_PREFIXES`), never the service's settings or a key it holds,
  whatever it is named. A child that needs more is given it explicitly (a model server entry's `env`, a job's own),
  and a feature that names a secret registers nothing.

## Rules

### API
- Every router declares its key once: `APIRouter(..., dependencies=deps.CLIENT)` or `deps.ADMIN`, and its routes
  add none. A module with routes of both has two routers, `router` and `admin_router` (included first, so its
  `/db/{workspace}/_query` is not taken for a table). The keys are security schemes, so the schema says which each
  route takes. Only `api/public_routes.py` and `api/setup_routes.py` have routes without a key. `tests/test_access.py`
  fails on any other, on a client route not in its `CLIENT_ROUTES`, and on a route another one takes.
- Only `api/app.py`'s `Requests` (one ASGI middleware: a request's tags and priority, internal errors, the audit log)
  wraps every request, inside CORS. Not `@app.middleware("http")`, whose each costs about 0.25 ms a request. A
  dependency that does no blocking work is `async def`, or FastAPI runs it on a thread.
- `ALLOWED_MODELS` is checked by `ModelManager` on every load, run and estimate (`deps.model_allowed`), so routes
  don't check it. Use `deps.check_model_allowed` only to refuse before other work. `tests/test_allowed_models.py`
  finds every route with a `model` or `model_id` in the schema and sends it one outside the list. It fails on a
  route it has no request for, so a new such route goes in its `REQUESTS` (or `NAMES_ONLY`).
- Request bodies are Pydantic models (a bad body is a 422). A route that returns a dict declares its response
  model in the `api/responses/` module of its area, built on `Out` (`api/responses/__init__.py` takes every
  module's names), with `**responses.answering(responses.Model)`. The frontend's types are generated
  from these. `answering` leaves out the fields a route did not set, rather than filling in defaults. `Out` lets
  extra fields through at run time, but the schema lists only the declared ones, so a field the pages read must be
  declared. A response's list of values is the domain's own (`TaskStatus` is `tasks/store.py`'s `Status`; `Literal[TYPES]`),
  never a copy; the pages key theirs by the schema's type (`Record<FieldType, ...>`), so tsc fails on a member missing.
- The audit log keeps every change through an admin route and every delete, by middleware (`api/audit.py`). Mark a
  POST that only reads with `@audit.reads`.
- A client key's grants (`access/grants.py`) are checked from the route alone: a client route under `/db` or
  `/evidence` takes its method's action (GET read, DELETE delete, any other write) on the table in its path. One that
  takes another action goes in `NEEDS`, and one that reaches past its table (by a query parameter, a body) goes in
  `target`, which then asks for the whole workspace. No test can see what a new route reaches.

### Data
- A store builds on `db/engine.py`: `Store`, `DocumentStore` (declared types and /db tables), or `QueuedStore`
  (rows written in the background, pruned by age); or on `SpecStore` (`db/specs.py`) for what an admin saves as a
  JSON spec by name (a view, a workflow, a remote, an enricher). It takes the shared `Database`, plus the `TableStore` when its
  rows are /db tables.
- Which: rows an admin would read or query in /db (the task queue, evals, the playground's chats, the enrichers'
  cache) are `TABLES`; the service's own bookkeeping that nobody queries there (the request log, the audit log,
  saved specs) is `SCHEMA`. One store, one of the two.
- Declare types in `SCHEMA`; /db tables in `TABLES` with `WORKSPACE` and `OWNER`. On first use, a `SCHEMA` type
  only gains the properties and indexes it lacks. A `TABLES` schema that changed is applied to the existing table:
  indexes of changed fields are dropped and rebuilt, and the change is refused (logged) when the rows don't fit it.
  There are no migrations of data: the data is test data. A change that existing data does not fit says so in its
  commit, and the data folder is deleted.
- A new store's `WORKSPACE` must be a name no admin could already have made through /db. Startup refuses a
  workspace that already exists.
- DuckDB calls block. Never call the database on the event loop: a store's async method is `@on_pool` on the
  method, or `name = on_pool(_name)` to keep the blocking `_name` for its own blocking code. A call that may take
  long (SQL someone wrote, a check of every row) is `@on_long_pool` (or `_run(..., long=True)`): it runs on a pool of
  its own, so it never holds the threads the short calls need. Each thread works through its own cursor
  (`Database.open()`), so a transaction is the thread's.
- Build conditions with `Where` (`db/engine.py`), which passes values as `$name` parameters and escapes LIKE. Never
  format values into SQL; names go through `quote` (`db/engine.py`). Full-text search reuses `search_query`
  (`db/schema.py`) and `TableStore.searching` (`db/rows.py`).
- A /db table's rows are columns: a declared field's own, the undeclared ones in `_rest` (JSON), and `_seq`. A store
  that keeps its rows in /db tables writes them with `TableStore.write_rows` (`write_row` for one), so `_rest` and
  `_seq` are set, and reads a whole row back with `decoded` (`db/rows.py`). Many rows go in one call: it is one
  statement, where a row at a time costs a statement each (about 1.5 ms).
- Traces, requests, audit rows and the enrichers' cache have a retention setting and an hourly prune (the loops in
  `lifespan`). The task table, evals, evidence and /db tables are kept until deleted. A new store that records per
  request or per event needs a retention and a place in those loops.
- A route that lists rows of a growing table pages them (`encode_cursor`/`decode_cursor`, `db/schema.py`); it
  never returns them all. `tests/test_paging.py` fails on a GET answering a list without paging that it does not name
  as small (with why) or as still to page. It takes `limit` and `cursor` (`deps.Cursor`) and answers `next`, the next page's cursor
  (`responses.page`); /db's routes call them `_limit` and `_cursor`, since their other parameters are a table's
  fields. A list new rows arrive at the top of (the queue, the audit log, eval runs) pages by a number each row
  keeps, not by an offset, so a page goes on where the last stopped. The dashboard's views of a time range (the
  request log, model events) take `since`, `until` and `limit` instead, and a tail (events, the server log) follows
  with `after_id`.

### Models, tasks, jobs
- A model is one file in `models/families/<family>/` with `@register`. Heavy imports (torch, transformers, an SDK)
  go inside `load()` and `predict()`, since every model file is imported at first use.
- Nothing outside `models/` imports a model file. What the rest needs to know about a model, its class answers
  through `BaseModel`'s hooks, asked by the registry: `operator_chose` (an ID allowed whatever `ALLOWED_MODELS`
  says, like a server entry's or a trained predictor's), `weights_source`.
- Inputs and outputs follow the task's contract in `models/contracts.py`. A result that breaks it is a
  `ContractViolation` (500).
- A job is one file in a subfolder of `jobs/` with `@register_job`; it runs in its own process and environment.
  At the top of the file it imports only pydantic and `jobs`; its libraries go inside `run()`.
- Files and folders whose names start with `_` are not registered, so they can hold shared helpers.

### Settings
- A setting is one line in `core/config.py`, `NAME = setting(parse, default, ...)`, and an entry in `env.example`
  (`tests/test_config.py` fails without it). Mark keys `secret=True`. A value the service cannot run with is refused
  by the setting's `parse` (startup stops, naming it); a risky one goes in `Config.warnings()`.
- A key typed into `/admin` goes through `save_secret` (`api/settings.py`) to the encrypted `KeyStore`
  (`core/keystore.py`), never to `.env`. Tests never reach the developer's credential store: they set a master key.
- A variable an admin names to hold a secret (an enricher's `secret`, a remote's `secrets`) goes through
  `check_secret_name` (`api/settings.py`), which refuses the service's settings, the providers' keys and the names
  every child gets (`procs.passed`).

### Frontend
- Every call goes through the typed client (`frontend/src/shared/api.ts`), by path as the API declares it
  (`client.useQuery("get", "/jobs/{id}", {params: {path: {id}}})`); types come from
  `frontend/src/shared/api-schema.d.ts`. A part gets the page's client with `useApi()`, which each page's App gives
  with `ApiProvider`, not as a prop.
- Loads are TanStack Query queries (`shared/query.ts`, `poll()` for polling). Writes go through `useAction`
  (`shared/action.ts`), which gives busy state and error reporting, and invalidate what they changed.
- Use the shared pieces before writing new ones: `DataGrid` (`shared/grid.tsx`) for tables, `SchemaForm`
  (`shared/schema-form.tsx`) for forms made from the API's JSON Schema, `LoadedCard` and the pager
  (`admin/layout.tsx`), `shared/tasks.ts` for a task's status, polling and cadence wherever tasks are listed, and
  `shared/components.tsx`. The shadcn components in `src/components/ui/` are owned here.
- An admin section is an entry in `shared/nav.ts` plus its view in `admin/sections.tsx`; tsc fails when one lacks
  the other. One set up once and seldom opened is a tab of Settings (`under: "settings"`), not a menu entry. A section with parts gets a folder `admin/<name>/`: its view `<Name>Section.tsx`, its parts, and its
  logic in `<name>.ts` beside its test. A large one groups its parts by area in subfolders, each area's logic beside
  its test, and keeps at its top what the areas share (`admin/data/`: `tables/`, `graph/`, `views/`, `query/`,
  `buckets/`).

### Prefer data over code
When something could be a file of code per case or one mechanism driven by data, choose the data. Enrichers are
JSON specs edited from `/admin`, settings are one line each, task kinds are declared once, a task's playground tab
comes from its `category`. Where an operator would want to change it, make it editable from `/admin`.

## How to add

| To add | Do this | Proven by |
|---|---|---|
| A model | One file in `models/families/<family>/` (see `models/README.md`) | It is in `GET /models/list`; a test with the fakes in `tests/fakes.py` |
| A task | `define_task(...)` in the model file, or a contract in `models/contracts.py`; give it `category` and `label`. A new category also needs its tab in `frontend/src/shared/categories.ts` | `GET /tasks`; tsc for the category |
| A job | One file in a subfolder of `jobs/` with `@register_job` (see `jobs/README.md`); a new environment is `requirements-<name>.txt` plus `scripts/setup_job_env.sh` | `GET /jobs/types` |
| An enricher | A JSON file in `enrichers/builtin/` named after the enricher, or from the admin's Enrichers section. No code | `POST /enrichers/{name}/test` |
| A store | A class in its domain package on `DocumentStore`, `QueuedStore` or `SpecStore`; built in `create_app()`; a deps type if routes use it; a prune loop in `lifespan` if it grows; closed at shutdown if it writes in the background (`pruned` and `writers` in `create_app()`) | A test against the throwaway database `tests/conftest.py` sets up; `tests/test_api.py` fails on a `prune()` or a writer missing from those lists |
| A router | `api/<name>_routes.py` with `deps.CLIENT` or `deps.ADMIN`, a docstring on every route; response models in `api/responses/<name>.py`; included in `create_app()`; its row in `STRUCTURE.md`'s API table; then `npm run gen:api` | `tests/test_access.py`, `tests/test_docs.py`, `tests/test_schemathesis.py`, `tests/test_openapi_snapshot.py` |
| A kind of background work | `tasks.add(Kind(name, run=..., concurrency=..., on_interrupt=...))` in the owning domain's runner; its run awaits its work through `tasks.stoppable(task, work)`, which a cancel or a timeout stops (`stop=` only for work elsewhere: a process, a thread) | `tests/test_task_runner.py` |
| A setting | One line in `core/config.py` and its entry in `env.example` | `tests/test_config.py` |
| An admin section | `shared/nav.ts` entry, `admin/sections.tsx` view, folder `admin/<name>/` | `npm run typecheck`, `npm run test:e2e` |
| A style of the pages | `shared/styles/<name>.css`, imported by `globals.css`, setting its variables under `:root[data-style="<name>"]` (and `.dark`); its name in `STYLES` (`shared/theme.ts`) | `npm run test:e2e` |

## Checks

```sh
make test                                  # pytest, then vitest; no model downloads, no torch needed
make lint                                  # ruff, pyright on api/app.py, then tsc
uv run --frozen pytest -m 'not schemathesis' -q   # quicker; --frozen keeps uv.lock as it is
cd frontend && npm run gen:api             # after any API change: frontend/openapi.json and api-schema.d.ts
cd frontend && npm run build               # the pages the service serves (api/static/)
cd frontend && npm run test:e2e            # build them, then open every page, section and tab in a browser
make lock                                  # after editing pyproject.toml: uv.lock and requirements.txt
```

`tests/conftest.py` gives each run throwaway directories and database, and clears the keys and the allow-list, so
a developer's `.env` never reaches the tests. Tests use the one app (`TestClient(app_module.app)`) with fake models
swapped in (`tests/fakes.py`: `swapped`, `registered`, built on `registry.restored()`; `servers.restored()` for
model server entries), and `FakeModel` starts each test reset. A test that waits for background work uses
`tests/waits.py` (`until`, `until_async`), never a loop of its own. A store built outside the app uses the `own_db`
fixture, a database of its own.

Python's type hints are documentation, but for `api/app.py`, the composition root, which pyright checks (`make lint`):
every service `create_app()` hands to another must fit the parameter or port it fills. The frontend is type-checked
whole (`tsc`).

These tests guard the structure. When one fails, fix the code, not the test:

| Test | Fails when |
|---|---|
| `tests/test_layers.py` | a package imports one in its own layer or above, or a module of `db/`, `tasks/` or `models/` that is not public, FastAPI is imported outside `api/`, `core.config` is read outside `api/`, `core/` and `models/`, or a model file is imported outside `models/` |
| `tests/test_access.py` | a route has no key check, takes both keys, is opened by a client key without being in `CLIENT_ROUTES`, or is taken by a route included before it |
| `tests/test_trained_models.py` | a training job and the model family that serves what it trained disagree on the model ID, task or folder |
| `tests/test_vocabularies.py` | a list of values kept twice because one side cannot import the other (the Enrich node's statuses, the pages' limits of db/) differs |
| `tests/test_allowed_models.py` | a route that names a model lets one outside `ALLOWED_MODELS` through |
| `tests/test_key_grants.py` | a key reaches a table, action or model its grants do not give, a route `NEEDS` or `MANAGE_MODELS` names is gone, or a client POST that deletes takes only write |
| `tests/test_audit.py` | the audit rule stops holding on its sample routes (a sample, not every route) |
| `tests/test_openapi_snapshot.py` | the API changed and `npm run gen:api` was not run |
| `tests/test_schemathesis.py` | an answer does not match the schema the route declares |
| `tests/test_config.py` | a setting is missing from `env.example` |
| `tests/test_version.py` | a file names a version other than `pyproject.toml`'s |
| `tests/test_paging.py` | a GET answering a list takes no `limit` and `cursor` and is not named as small or still to page |
| `tests/test_docs.py` | a route has no docstring, a router no row in `STRUCTURE.md`'s API table, or a folder of source files no line in its map |
| `tests/test_errors.py` | a refusal lost its status |
| `npm run typecheck` | a menu entry lacks its view, a task category lacks its tab, a call names a path the API lacks |
| `npm run test:e2e` | a page, an /admin section in the menu or Settings' tabs, or a playground tab fails to open, throws, logs an error or gets a 5xx |

## Working here

- Generated files are never edited by hand: `frontend/openapi.json`, `frontend/src/shared/api-schema.d.ts`,
  `api/static/`, `requirements.txt`, `uv.lock`.
- Keep the docs true in the same change, each fact in one place. A setting is documented in `env.example`, a route
  by its docstring (what `/docs` shows), a module by its docstring, a folder by its line in `STRUCTURE.md`'s map, a
  router by its row in `STRUCTURE.md`'s API table. `docs/GUIDE.md` shows how to use a feature with worked examples;
  it does not list routes or settings. `tests/test_docs.py` and `tests/test_config.py` fail on what is missing.
- The version is written in `pyproject.toml`, `APP_VERSION` in `api/app.py`, `frontend/package.json` (and its
  lock file) and twice in `README.md`. Change them together, then run `npm install` and `npm run gen:api`, since
  `frontend/openapi.json` carries it too. `tests/test_version.py` names any that differ.
- Work on a branch, one commit per idea, the subject in plain words led by the area (`Tasks: a cancel during a
  retry ends the task`). Don't push, merge or delete branches without the owner's word.
- Comments and docstrings say what a thing is and why, in plain sentences, as the existing modules do.
- Before a large new feature, write the research in `research_notes/<topic>/` and the result in
  `reports/<topic>.md`.
