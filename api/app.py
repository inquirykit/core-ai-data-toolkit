"""
FastAPI application with CORS support
"""

import asyncio
import contextlib
import logging
import secrets
import time
from contextlib import asynccontextmanager
from typing import Any, Dict, Optional, cast
import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from . import (public_routes, inference_routes, openai_routes, decision_routes, admin_routes, setup_routes,
               key_routes, playground_routes, table_routes, view_routes, file_routes, remote_routes, job_routes,
               eval_routes, enrich_routes, model_server_routes, metrics_routes, evidence_routes, workflow_routes, queue_routes)
from . import audit
from .pages import STATIC_DIR
from access import ClientKeys
from buckets import BucketFiles, Copies, Namings, Remotes
from buckets.copies import CHECK_S as COPY_CHECK_S
from core.keystore import KeyStore
from core.background import every
from models import ModelManager, servers
from evals import EvalStore, RunExecutor
from models.events import RequestContext, request_context
from monitoring import CONSOLE_ONLY, AuditLog, LogBuffer, Metrics, MonitoringStore
from db import Database
from db.graph import GraphStore
from jobs.runner import JobRunner
from core.priorities import HIGH, PRIORITIES
from tasks.runner import TaskRunner, TaskStore
from tasks.schedule import POLL_S, Scheduler
from tasks.webhooks import Webhooks
from workflows import WorkflowRunner, WorkflowStore
from enrichers import EnrichService, EnricherStore, ResultCache
from evidence import CaptureFiles, EvidenceStore, Signer, Tsa, timestamp_pending
from playground import PlaygroundStore
from views import Views
from core.config import Config
from core.errors import ServiceError, describe
from .responses.common import ErrorDetail, ValidationProblem
from . import deps

logging.basicConfig(level=getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO))
# httpx logs each request's whole URL at INFO, so a model server's user and password and an enricher's key in a query
# would be in the log, and in /admin/logs; its warnings still are.
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
# The dashboard's "Server log" tab reads this. Uvicorn's loggers do not
# propagate to the root, so they are attached too.
log_buffer = LogBuffer().install(None, "uvicorn", "uvicorn.access")
# The service's version, as the API schema and each evidence record state it.
APP_VERSION = "0.5alpha28"


def _answer(request: Request, message: str, status: int, headers: Optional[Dict[str, str]] = None) -> JSONResponse:
    """An error as FastAPI's look: ``{"detail": message}`` with its status.

    Under /evidence the message is ``error`` too, which the browser extension reads; /v1 keeps ``detail`` alone,
    since an OpenAI client takes ``error`` for an object."""
    body = {"detail": message}
    if request.url.path.startswith("/evidence"):
        body["error"] = message
    return JSONResponse(body, status_code=status, headers=headers)


async def service_error_response(request: Request, exc: ServiceError) -> JSONResponse:
    """A refusal (core/errors.py): its own status and headers (a full cache's Retry-After)."""
    if exc.status >= 500:
        logger.warning("%s %s: %s", request.method, request.url.path, exc)
    return _answer(request, str(exc), exc.status, exc.headers)


async def value_error_response(request: Request, exc: ValueError) -> JSONResponse:
    """A bad value that got past validation (a model refusing its input, a bad option) is the client's: 400.

    Its text reaches the client only with DEBUG on, as an internal error's does, since nobody wrote it for them."""
    logger.info("%s %s refused: %s: %s", request.method, request.url.path, type(exc).__name__, exc)
    return _answer(request, describe(exc, "Invalid request", Config.DEBUG), 400)


class Requests:
    """What every HTTP request goes through, as one ASGI middleware (Starlette's ``@app.middleware("http")``, one per
    concern, cost about 0.25 ms a request each):

    - its trace's tags from X-Trace-Tag (comma-separated) and X-Session-Id, and from X-Priority (high, normal or low;
      high when not given) how urgent it is when models compete for room (models/cache.py): a 400 for another;
    - anything a route did not expect is a 500 naming the error's type (its text too with DEBUG on), logged with its
      traceback, so no route maps exceptions to statuses; only a reply streamed already reports its own failure
      (api/streaming.py), since its status has gone out;
    - every change through an admin's route, and every delete, goes in the audit log (api/audit.py) with the status it
      got, a refusal's or an internal error's among them.
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started, request = time.time(), Request(scope)
        status, answered = 500, False

        async def sending(message: Message) -> None:
            nonlocal status, answered
            if message["type"] == "http.response.start":
                status, answered = message["status"], True
            await send(message)

        try:
            context = _context(request)
            if context is None:
                await _answer(request, "X-Priority must be high, normal or low", 400)(scope, receive, sending)
                return
            # Given back after: an in-process caller (the judge, through httpx's ASGITransport) runs the app in its own
            # task, whose own context it keeps.
            token = request_context.set(context)
            try:
                await self.app(scope, receive, sending)
            except Exception as e:
                if answered:
                    raise
                logger.exception("%s %s failed", request.method, request.url.path)
                await _answer(request, describe(e, "Internal error", Config.DEBUG), 500)(scope, receive, sending)
            finally:
                request_context.reset(token)
        finally:
            audit.record(request.app.state.audit, request, status, started)


def _context(request: Request) -> Optional[RequestContext]:
    """The request's context (models/events.py): its trace's tags and session, its priority, where it came from (an
    app, or a /db table; the key's guard says when it is the admin's: api/deps.py). None for a bad X-Priority."""
    tags = [t.strip()[:64] for t in request.headers.get("x-trace-tag", "").split(",") if t.strip()][:8]
    session = (request.headers.get("x-session-id") or "").strip()[:128] or None
    named = (request.headers.get("x-priority") or "").strip().lower()
    if named and named not in PRIORITIES:
        return None
    source = "data" if request.url.path.startswith("/db") else "api"
    return RequestContext(tags=tags, session=session, priority=PRIORITIES.get(named, HIGH), source=source)


class _AdminKey(httpx.Auth):
    """Signs a request this service sends itself (the judge's) with the admin's key, as it is when the request goes
    (setup may set it after startup)."""

    def auth_flow(self, request: httpx.Request):
        key = Config.ADMIN_API_KEY or Config.API_KEY
        if key:
            request.headers["Authorization"] = f"Bearer {key}"
        yield request


def create_app() -> FastAPI:
    """Create and configure the FastAPI application"""
    # One DuckDB database in DATA_DIR holds everything the service stores, and every store below shares it; opened
    # on first use (see db/engine.py).
    database = Database(str(Config.DATABASE_PATH), Config.DATABASE_MEMORY)
    # The request log, with each input and output kept TRACE_RETENTION_DAYS (its traces), and the audit log, unless
    # their retention is 0.
    monitoring = (MonitoringStore(database, Config.MONITORING_RETENTION_DAYS, Config.TRACE_RETENTION_DAYS,
                                  Config.TRACE_MAX_CHARS) if Config.MONITORING_RETENTION_DAYS > 0 else None)
    # Counts for GET /metrics; hands each record on to the store.
    metrics = Metrics(monitoring)
    # The client keys an admin makes in /admin (access/keys.py), which api/deps.py checks with the settings' keys.
    client_keys = ClientKeys(database)
    # Who changed what (api/audit.py).
    audit_log = AuditLog(database, Config.AUDIT_RETENTION_DAYS) if Config.AUDIT_RETENTION_DAYS > 0 else None
    model_manager = ModelManager(
        max_models=Config.MAX_MODELS,
        max_memory_gb=Config.MAX_MEMORY_GB,
        device=Config.DEFAULT_DEVICE,
        monitor=metrics,
        wait_timeout=Config.MODEL_WAIT_TIMEOUT,
        batch_wait_s=Config.BATCH_WAIT_MS / 1000,
        keep_s=Config.MODEL_KEEP_S,
        turn_s=Config.MODEL_TURN_S,
        # ALLOWED_MODELS, checked on every load and run, so no route checks it.
        allowed=deps.model_allowed,
        # Each request is counted, and kept in the request log with its inputs and outputs.
        sinks=[metrics.on_request],
    )
    # /db. The evals, jobs and the enrichers' cache keep their tables in it, in workspaces /db shows only to the
    # admin, read-only; the evidence ledger in the workspaces, as kept tables, read-only there too. db/ cannot import
    # the models: a vector field with a model embeds (texts of MAX_INPUT_CHARS at most), finds the model's prompts and
    # reranks through the model manager's calls it is given.
    tables = GraphStore(database, vector_max_rows=Config.VECTOR_MAX_ROWS, embed_max_chars=Config.MAX_INPUT_CHARS,
                        embed=model_manager.embed, prompts=model_manager.prompts, rerank=model_manager.rerank)
    # The bytes of the buckets' files, in DATA_DIR/files (their rows are /db tables); kept until deleted, so no prune.
    # db/ cannot import buckets/: the table store asks it before making a bucket and tells it what it drops.
    buckets = BucketFiles(tables, Config.FILES_DIR, Config.MAX_FILE_BYTES)
    tables.files = buckets
    # Views and dashboards: saved reads over a workspace's data, kept beside it, which a client key runs within
    # VIEW_TIME_LIMIT_MS and VIEW_MAX_ROWS. Kept until deleted, so no prune; they go when their workspace is dropped,
    # which the table store tells them (db/ cannot import views/).
    views = Views(database, tables, Config.VIEW_MAX_ROWS, Config.VIEW_TIME_LIMIT_MS)
    tables.on_drop.append(views.forget)
    # The playground's saved chats and presets.
    playground = PlaygroundStore(database, tables)
    # Datasets, runs and scores; a run's outputs are cut to TRACE_MAX_CHARS, as traces are.
    evals = EvalStore(database, tables, Config.TRACE_MAX_CHARS)
    # Background work waits in one task table, each kind with its limit: jobs, eval runs, judging, webhook deliveries,
    # copies of buckets' files, workflow runs (tasks/runner.py).
    tasks = TaskRunner(TaskStore(database, tables), loaded=model_manager.is_loaded)
    # Schedules submit tasks of the kinds that declare themselves to it (tasks/schedule.py): jobs, workflows.
    scheduler = Scheduler(tasks.store)
    # Places outside the service that the buckets' files are copied to (S3, R2, B2, a folder...: buckets/remotes.py),
    # their specs in the database, and the copying, a kind of task (buckets/copies.py).
    namings = Namings(database, tables)
    remotes = Remotes(database, namings, buckets.root)
    copies = Copies(buckets, remotes, namings, tasks)
    # Each upload and delete asks for its bucket's copy, a file missing from disk is read back from the remote, and
    # saving a remote asks for the copies of the buckets that name it.
    buckets.copies = copies
    remotes.copies = copies
    # Finished jobs and runs are POSTed to WEBHOOK_URLS, each delivery a task, so a restart does not lose it.
    webhooks = Webhooks(Config.WEBHOOK_URLS, Config.WEBHOOK_SECRET, tasks=tasks)
    runs = RunExecutor(evals, model_manager, tasks, webhooks)
    jobs = JobRunner(
        tasks,
        webhooks,
        scheduler,
        jobs_dir=Config.JOBS_DIR,
        envs_dir=Config.JOB_ENVS_DIR,
        data_dir=Config.JOB_DATA_DIR,
        max_upload_bytes=Config.JOB_MAX_UPLOAD_BYTES,
        max_running=Config.MAX_RUNNING_JOBS,
        num_cpus=Config.JOB_NUM_CPUS,
        device=Config.JOB_DEVICE,
    )

    enricher_store = EnricherStore(database)
    enrichers = EnrichService(
        enricher_store,
        ResultCache(database, tables),
        terms_allowed=Config.ENRICHER_TERMS,
        allow_private=Config.ENRICH_ALLOW_PRIVATE,
        max_concurrency=Config.ENRICH_CONCURRENCY,
    )
    # The keys typed into /admin, opened once here: a real environment variable wins over them, they over the .env file.
    # Children (jobs, model servers) never get them, nor any other key: they get only what a program needs to run
    # (core/procs.py, child_env).
    keys = KeyStore(Config.SECRETS_FILE, Config.SECRETS_MASTER_KEY)
    saved = keys.load()
    Config.apply({name: value for name, value in saved.items() if not Config.set_by_environment(name)})

    # Workflows: a table's rows through a graph of models, enrichers and conditions, written back; a kind of task, each
    # trigger a schedule. They read and write tables as the admin does through /db.
    workflows = WorkflowRunner(WorkflowStore(database), tables, model_manager, tasks, scheduler,
                               enrich=enrichers.enrich, debug=Config.DEBUG, keep_days=Config.WORKFLOW_RUN_DAYS)

    # Evidence capture (/evidence) shares the database too; its files and key live in DATA_DIR/evidence.
    evidence_tsa = Tsa(Config.EVIDENCE_TSA_URL, version=APP_VERSION)
    evidence = EvidenceStore(database, tables, CaptureFiles(Config.EVIDENCE_DIR), Signer(Config.EVIDENCE_KEY_FILE),
                             APP_VERSION)

    async def retry_timestamps() -> None:
        """Ask the TSA again for records that have no time stamp yet."""
        ok, failed = await timestamp_pending(evidence, evidence_tsa)
        if ok or failed:
            logger.info("Evidence time stamps retried: %d granted, %d failed", ok, failed)

    # What lifespan prunes hourly, and the background writers it closes at shutdown: kept on app.state too, where
    # tests/test_api.py finds every store with a retention (a prune()) or a writer that is missing from them.
    pruned = [(store, what) for store, what in (
        (monitoring, "request history and traces"), (audit_log, "the audit log"),
        (enrichers.cache, "the enrichers' cache"), (tasks, "finished webhook deliveries, copies and workflow runs"),
    ) if store is not None]
    writers = [writer for writer in (monitoring, audit_log) if writer is not None]

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Read before the first request, so none waits on the database to check its key; a service that cannot read
        # them does not start.
        await client_keys.load()
        for warning in Config.warnings(client_keys=client_keys.made_any()):
            logger.warning(warning)
        # Periodic work, each an every() loop (core/background.py) cancelled at shutdown.
        loops = [every(60, client_keys.flush, "Writing when the client keys were last used", first=60)]
        if evidence_tsa.enabled:
            loops.append(every(3600, retry_timestamps, "Retrying evidence time stamps", first=60))
        # Hourly, a copy for each bucket whose copy is behind its remote: one whose retries all failed while the remote
        # was down, say (its first is asked for at startup, below, once the queue runs).
        loops.append(every(COPY_CHECK_S, copies.start, "Queueing the copies of buckets behind their remotes",
                           first=COPY_CHECK_S))
        loops.append(every(POLL_S, scheduler.tick, "The scheduler"))
        if Config.MODEL_IDLE_TIMEOUT > 0:
            idle = Config.MODEL_IDLE_TIMEOUT
            loops.append(every(min(30.0, idle), lambda: model_manager.cache.unload_idle(idle), "Unloading idle models",
                               first=min(30.0, idle)))
        # Hourly, what is past its retention goes: traces also with capture off (TRACE_RETENTION_DAYS=0), so those
        # kept before are dropped.
        for store, what in pruned:
            loops.append(every(3600, store.prune, f"Pruning {what}"))
        background = [asyncio.create_task(loop) for loop in loops]
        # In the background: the service answers while they load.
        preload = asyncio.create_task(model_manager.preload(Config.PRELOAD_MODELS, Config.PINNED_MODELS))
        # After the kinds' own start: queued tasks start as their kinds have room.
        await tasks.start()
        # Once the queue runs: a copy for each bucket whose files wait for its remote (uploaded while it failed).
        try:
            await copies.start()
        except Exception:
            logger.exception("Could not queue the buckets' copies to their remotes")
        yield
        # Running tasks are stopped and marked failed (interrupted).
        await tasks.stop()
        # Cancelling a loop does not stop the call it has running on the database's pool (a prune's first run making
        # its store's types, say): database.close() below waits for those before it closes.
        for task in (*background, preload):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await webhooks.close()
        await model_manager.clear_all_models()
        # Model servers that are asleep rather than loaded, too.
        await servers.stop_all()
        await enrichers.close()
        await evidence_tsa.close()
        try:
            await client_keys.flush()
        except Exception:
            logger.exception("Could not write when the client keys were last used")
        # The writers finish their queues (the unloads above included) before the database closes.
        for writer in writers:
            writer.close()
        database.close()

    app = FastAPI(
        title="Core AI & Data toolkit",
        version=APP_VERSION,
        description="A self-hosted service for small AI models and the data work around them",
        lifespan=lifespan,
    )

    # Each request's own handling (Requests, above), inside CORS, so its refusals and internal errors carry the CORS
    # headers as every other answer does. Browsers reject credentials with a wildcard origin, so only enable them when
    # explicit origins are configured.
    app.add_middleware(Requests)
    allow_all = "*" in Config.CORS_ORIGINS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=Config.CORS_ORIGINS,
        allow_credentials=not allow_all,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Until setup runs, saving it needs this code from the console's log, so nobody who merely reaches the server can
    # claim it: never in /admin/logs, which needs no key until then. main.py picks the reloader's child the same code.
    app.state.setup_code = None
    if Config.needs_setup():
        code = Config.SETUP_CODE or secrets.token_urlsafe(12)
        app.state.setup_code = code
        host = "localhost" if Config.HOST in ("0.0.0.0", "::", "127.0.0.1") else Config.HOST
        logger.warning(
            "Not set up yet. Open http://%s:%s/admin/setup#code=%s to choose API keys and allowed models "
            "(setup code: %s)", host, Config.PORT, code, code, extra=CONSOLE_ONLY,
        )

    app.state.manager = model_manager
    app.state.keys = keys
    app.state.client_keys = client_keys
    app.state.monitoring = monitoring
    app.state.audit = audit_log
    app.state.metrics = metrics
    app.state.logs = log_buffer
    app.state.evals = evals
    app.state.runs = runs
    # The judge is a model this service serves, asked through its own /v1 in-process, with the admin's key; those requests
    # are ordinary ones, tagged "judge" in Activity & logs.
    runs.local_client = lambda priority: httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), headers={"X-Trace-Tag": "judge", "X-Priority": priority},
        auth=_AdminKey())

    app.state.database = database
    app.state.pruned = [store for store, _ in pruned]
    app.state.writers = writers
    app.state.tables = tables
    app.state.buckets = buckets
    app.state.remotes = remotes
    app.state.copies = copies
    app.state.views = views
    app.state.playground = playground
    app.state.jobs = jobs
    app.state.tasks = tasks
    app.state.webhooks = webhooks
    app.state.enrichers = enrichers
    app.state.workflows = workflows
    app.state.evidence = evidence
    app.state.evidence_tsa = evidence_tsa
    # Every refusal the service makes (core/errors.py) is answered with its status; a plain ValueError is a 400.
    # (Starlette types a handler as taking any exception; each of these takes the one it is added for.)
    app.add_exception_handler(ServiceError, cast(Any, service_error_response))
    app.add_exception_handler(ValueError, cast(Any, value_error_response))
    # So the schema says what every route may answer with besides its own: a client's validator (and
    # tests/test_schemathesis.py) then holds each error body to {"detail": message}. Any status a route does not
    # declare is a refusal (core/errors.py: 400, 401, 403, 404, 409, 413, 503) or an internal error (500): one default
    # response, not the same seven on every operation.
    refusals = {"default": {"model": ErrorDetail, "description": "A refusal (core/errors.py: its status says which) "
                                                                 "or an internal error (500): {detail}"},
                # Declared, since FastAPI leaves its own out once there is a default.
                422: {"model": ValidationProblem, "description": "The body or a parameter does not fit"}}
    # In this order: a path two routers could match goes to the first.
    # The views' routes come before the files' and /db's, which would take _views for a table's name; the file routes
    # before /db's, which would take a bucket's _remote for a row's id.
    for module in (public_routes, inference_routes, openai_routes, decision_routes, admin_routes, setup_routes,
                   key_routes, playground_routes, view_routes, file_routes, remote_routes, table_routes, job_routes,
                   eval_routes, enrich_routes, model_server_routes, metrics_routes, evidence_routes, workflow_routes,
                   queue_routes):
        # A module with routes of both levels has a router for each (api/deps.py): the admin's first, as its paths
        # (/db/{workspace}/_query) would otherwise go to the client's (/db/{workspace}/{table}).
        for r in (getattr(module, "admin_router", None), module.router):
            if r is not None:
                app.include_router(r, responses=refusals)
    # The built pages' scripts and styles; missing until frontend/ is built (see api/pages.py).
    app.mount("/admin/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="admin-static")
    return app


app = create_app()
