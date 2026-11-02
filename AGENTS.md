# AGENTS.md

How to work in this repo, for people and coding agents. Read it before changing anything.
[STRUCTURE.md](STRUCTURE.md) is how the code is built, with a map of every file; [docs/GUIDE.md](docs/GUIDE.md)
is for running and calling the service; [models/README.md](models/README.md) and [jobs/README.md](jobs/README.md)
are the guides for adding models and jobs.

## What this is

A self-hosted FastAPI service for small, efficient models (BERT-style classifiers and encoders, rerankers,
speech-to-text, short scripts such as polars transforms) and the data work around them: one queue of background
work, an embedded ArcadeDB database, traces and evals, monitoring and an audit log, enrichers, evidence capture, and
React pages (`/admin`, `/playground`, `/admin/setup`).

- Big LLMs are not run here. They are forwarded to a server the operator already runs (Ollama, LM Studio, vLLM)
  through model server entries (`models/servers/`). Judge performance work by small models and short scripts: keep
  the service's own cost per request small, and remember that batching (`models/batching.py`) is the main lever.
- One process, one database. Embedded ArcadeDB can be opened by one process only. Don't design for several
  processes unless asked.

## Layers

Packages import only downward. A cycle or an upward import is a bug. `tests/test_layers.py` reads every import
(those inside functions too) and fails on one that breaks the rules below. A new package gets its layer in the
test's `LAYERS` and in this table.

```
core        settings, errors, discovery, child processes, every()       imports none of ours
db          Database, store bases, /db tables and graph, collections    core
tasks       the one queue of background work, webhooks                  core, db
models      task contracts, registry, cache, manager, families, servers core, tasks
jobs evals evidence enrichers monitoring playground (domain)            the layers above, never each other
api         FastAPI: the app, routers, deps, response models            everything
frontend    React pages, typed from the API's schema                    the HTTP API only
```

- Only `api/` imports FastAPI or Starlette. Domain code raises the refusals in `core/errors.py` (`Refused` 400,
  `Unauthorized` 401, `Forbidden` 403, `NotFound` 404, `Conflict` 409, `TooLarge` 413, `Unavailable` 503) or a
  plain `ValueError` (400). `api/app.py` answers them all with their status. Don't raise `HTTPException`, and don't
  map exceptions to statuses in a route. A domain's own error subclasses one of them (`TableError(Refused)`,
  `CapacityError(Unavailable)`), so it carries its status.
- Domain packages don't import each other. When one needs another, the caller passes it in as an argument: the
  capture route hands `evidence/enrich.py` the enrichers' service (`api/evidence_routes.py`).
- `db`, `tasks`, `jobs`, `evals`, `evidence`, `enrichers` and `monitoring` never read `core.config`: `create_app()`
  passes them their settings as arguments. Only `api/`, `main.py` and `models/` (request limits in the contracts,
  `IMAGE_DIR`) read `Config`.
- Domain logic lives in the domain package; a route parses the request, calls the service and shapes the reply.

## Wiring

- `api/app.py`'s `create_app()` is the one composition root. It builds every service from `Config` around the one
  `Database`, keeps each on `app.state`, starts and stops them in `lifespan`, and includes the routers in order.
  Shutdown order matters: the task runner stops, models unload, the background writers flush, the database closes
  last.
- `app = create_app()` runs at import: importing `api.app` reads the settings and the enricher files and starts
  ArcadeDB's JVM, which must start on the main thread. Tests share that one app.
- Routes get services through the `Annotated` types in `api/deps.py` (`runner: deps.Jobs`), not
  `request.app.state`. The few that read `app.state` say why (a 503 of their own, a check that must run before
  the key).
- A service can be switched off: no `ARCADEDB_PATH`, `JOBS_DIR`, `ENRICHERS_DIR` or `EVIDENCE_DIR`, or a retention
  of 0 for monitoring and the audit log. It is then `None` on `app.state`. A deps type given an `off` message
  answers 503 naming the setting (`deps.Jobs`); an optional one (`deps.Monitoring`, `deps.Audit`,
  `deps.OptionalEnrichers`) hands the route `None`, and the route does without.
- Work that outlives a request is one of two things, never a task a route starts and forgets. Periodic work is a
  `core.background.every()` loop started in `lifespan` (prunes are hourly). Queued work is a `Kind` of task on the
  one `TaskRunner` (`tasks/runner.py`), declared once by its owner: `tasks.add(Kind(...))`.
- Child processes (jobs, model servers, predictors) start through `core/procs.py`, which strips the service's
  secrets from their environment. Every setting marked secret is hidden at startup (`procs.hide`). The enrichers'
  key names are asked for again before each child starts (`procs.hide_from`), so a key named from the dashboard is
  hidden too. Anything else that can name a secret while the service runs registers with `procs.hide_from`.

## Rules

### API
- Every router declares its key once: `APIRouter(..., dependencies=deps.CLIENT)` or `deps.ADMIN`. Only
  `api/public_routes.py` and `api/setup_routes.py` have routes without a key. `tests/test_access.py` fails on any
  other, and on an `/admin`, `/jobs` or `/hub` route that a client key opens.
- `ALLOWED_MODELS` is checked by `ModelManager` on every load, run and estimate (`deps.model_allowed`), so routes
  don't check it. Use `deps.check_model_allowed` only to refuse before other work. `tests/test_allowed_models.py`
  finds every route with a `model` or `model_id` in the schema and sends it one outside the list. It fails on a
  route it has no request for, so a new such route goes in its `REQUESTS` (or `NAMES_ONLY`).
- Request bodies are Pydantic models (a bad body is a 422). A route that returns a dict declares its response
  model in the `api/responses/` module of its area, built on `Out` and re-exported from
  `api/responses/__init__.py`, with `**responses.answering(responses.Model)`. The frontend's types are generated
  from these. `answering` leaves out the fields a route did not set, rather than filling in defaults. `Out` lets
  extra fields through at run time, but the schema lists only the declared ones, so a field the pages read must be
  declared.
- The audit log keeps every change through an admin route and every delete, by middleware (`api/audit.py`). Mark a
  POST that only reads with `@audit.reads`.

### Data
- A store builds on `db/engine.py`: `Store`, `DocumentStore` (declared types and /db tables), or `QueuedStore`
  (rows written in the background, pruned by age). It takes the shared `Database`, plus the `TableStore` when its
  rows are /db tables.
- Declare types in `SCHEMA`; /db tables in `TABLES` with `NAMESPACE` and `OWNER`. On first use, a `SCHEMA` type
  only gains the properties and indexes it lacks. A `TABLES` schema that changed is applied to the existing table:
  indexes of changed fields are dropped and rebuilt, and the change is refused (logged) when the rows don't fit it.
  A change to existing data is a `MIGRATIONS` entry `(id, method)`, run once per database, safe to run again.
- A new store's `NAMESPACE` must be a name no admin could already have made through /db. Startup refuses a
  namespace that already exists, unless the store adopts it on purpose (`ADOPT = True`, as the playground's store does).
- ArcadeDB calls block. Never call the database on the event loop: a store's async method is `@on_pool` on the
  method, or `name = on_pool(_name)` to keep the blocking `_name` for its own blocking code.
- Build conditions with `Where` (`db/engine.py`), which passes values as parameters and escapes LIKE. Never format
  values into SQL. Full-text search reuses `search_query` (`db/schema.py`) and `search_where` (`db/tables.py`).
- Traces, requests, audit rows and the enrichers' cache have a retention setting and an hourly prune (the loops in
  `lifespan`). The task table, evals, evidence and /db tables are kept until deleted. A new store that records per
  request or per event needs a retention and a place in those loops.
- A route that lists rows of a growing table pages them (`encode_cursor`/`decode_cursor`, `db/schema.py`); it
  never returns them all.

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
  (`tests/test_config.py` fails without it). Mark keys `secret=True`. A setting the service cannot run with goes in
  `Config.fatal()`, a risky one in `Config.warnings()`.

### Frontend
- Every call goes through the typed client (`frontend/src/shared/api.ts`), by path as the API declares it
  (`client.useQuery("get", "/jobs/{id}", {params: {path: {id}}})`); types come from
  `frontend/src/shared/api-schema.d.ts`.
- Loads are TanStack Query queries (`shared/query.ts`, `poll()` for polling). Writes go through `useAction`
  (`shared/action.ts`), which gives busy state and error reporting, and invalidate what they changed.
- Use the shared pieces before writing new ones: `DataGrid` (`shared/grid.tsx`) for tables, `SchemaForm`
  (`shared/schema-form.tsx`) for forms made from the API's JSON Schema, `LoadedCard` and the pager
  (`admin/layout.tsx`), and `shared/components.tsx`. The shadcn components in `src/components/ui/` are owned here.
- An admin section is an entry in `shared/nav.ts` plus its view in `admin/sections.tsx`; tsc fails when one lacks
  the other. A section with parts gets a folder `admin/<name>/`: its view `<Name>Section.tsx`, its parts, and its
  logic in `<name>.ts` beside its test.

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
| A store | A class in its domain package on `DocumentStore` or `QueuedStore`; built in `create_app()`; a deps type if routes use it; a prune loop in `lifespan` if it grows; closed at shutdown if it writes in the background | A test against the throwaway database `tests/conftest.py` sets up |
| A router | `api/<name>_routes.py` with `deps.CLIENT` or `deps.ADMIN`, a docstring on every route; response models in `api/responses/<name>.py`; included in `create_app()`; its row in `STRUCTURE.md`'s API table; then `npm run gen:api` | `tests/test_access.py`, `tests/test_docs.py`, `tests/test_schemathesis.py`, `tests/test_openapi_snapshot.py` |
| A kind of background work | `tasks.add(Kind(name, run=..., stop=..., concurrency=..., on_interrupt=...))` in the owning domain's runner | `tests/test_task_runner.py` |
| A setting | One line in `core/config.py` and its entry in `env.example` | `tests/test_config.py` |
| An admin section | `shared/nav.ts` entry, `admin/sections.tsx` view, folder `admin/<name>/` | `npm run typecheck`, `npm run test:e2e` |

## Checks

```sh
make test                                  # pytest, then vitest; no model downloads, no torch needed
make lint                                  # ruff, then tsc
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
`tests/waits.py` (`until`, `until_async`), never a loop of its own. A test that uses a store before any request starts
the JVM first (`app_module.app.state.database.start_jvm()`). A store built outside the app uses the `arcade` fixture.

Python has no type checker here: its type hints are documentation. Only the frontend is type-checked (`tsc`).

These tests guard the structure. When one fails, fix the code, not the test:

| Test | Fails when |
|---|---|
| `tests/test_layers.py` | a package imports one in its own layer or above, FastAPI is imported outside `api/`, `core.config` is read outside `api/`, `core/` and `models/`, or a model file is imported outside `models/` |
| `tests/test_access.py` | a route has no key check, or a client key opens an `/admin`, `/jobs` or `/hub` route |
| `tests/test_allowed_models.py` | a route that names a model lets one outside `ALLOWED_MODELS` through |
| `tests/test_audit.py` | the audit rule stops holding on its sample routes (a sample, not every route) |
| `tests/test_openapi_snapshot.py` | the API changed and `npm run gen:api` was not run |
| `tests/test_schemathesis.py` | an answer does not match the schema the route declares |
| `tests/test_config.py` | a setting is missing from `env.example` |
| `tests/test_version.py` | a file names a version other than `pyproject.toml`'s |
| `tests/test_docs.py` | a route has no docstring, a router no row in `STRUCTURE.md`'s API table, or a folder of source files no line in its map |
| `tests/test_errors.py` | a refusal lost its status |
| `npm run typecheck` | a menu entry lacks its view, a task category lacks its tab, a call names a path the API lacks |
| `npm run test:e2e` | a page, an /admin section in the menu or a playground tab fails to open, throws, logs an error or gets a 5xx |

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
