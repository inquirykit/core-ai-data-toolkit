"""
FastAPI application with CORS support
"""

import asyncio
import contextlib
import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Dict, Optional
import httpx
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from .public_routes import router as public_router
from .routes import router
from .openai_routes import router as openai_router
from .decision_routes import router as decision_router
from .admin_routes import router as admin_router
from .setup_routes import router as setup_router
from .playground_routes import router as playground_router
from . import playground_routes
from .collection_routes import router as collection_router
from .table_routes import router as table_router
from .job_routes import router as job_router
from .eval_routes import router as eval_router
from .enrich_routes import router as enrich_router
from .model_server_routes import router as model_server_router
from .metrics_routes import router as metrics_router
from .hub_routes import router as hub_router
from .evidence_routes import router as evidence_router
from .pages import STATIC_DIR
import procs
from background import every
from models import ModelManager, model_server
from models.hub import Downloads
from evals import EvalStore, RunExecutor
from models.events import HIGH, PRIORITIES, RequestContext, request_context
from monitoring import LogBuffer, Metrics, MonitoringStore
from vectorstore import CollectionStore, Database
from vectorstore.graph import GraphStore
from jobs.runner import JobRunner
from jobs.store import JobStore
from jobs.tasks import TaskRunner
from jobs.webhooks import Webhooks
from enrichers import EnrichService, EnricherStore, MemoryCache, ResultCache
from evidence import CaptureFiles, EvidenceStore, Signer, Tsa, timestamp_pending
from config import Config
from errors import ServiceError, describe
from .responses.common import ErrorDetail
from . import deps

logging.basicConfig(level=getattr(logging, Config.LOG_LEVEL.upper(), logging.INFO))
logger = logging.getLogger(__name__)
# The dashboard's "Server log" tab reads this. Uvicorn's loggers do not
# propagate to the root, so they are attached too.
log_buffer = LogBuffer().install(None, "uvicorn", "uvicorn.access")
# The service's version, as the API schema and each evidence record state it.
APP_VERSION = "0.2alpha22"


def _every_error(cls: type) -> list:
    """``cls`` and every subclass of it, however deep (the stores' own errors included)."""
    return [cls, *(sub for direct in cls.__subclasses__() for sub in _every_error(direct))]


def _answer(request: Request, message: str, status: int, headers: Optional[Dict[str, str]] = None) -> JSONResponse:
    """An error as FastAPI's look: ``{"detail": message}`` with its status.

    Under /evidence the message is ``error`` too, which the browser extension reads; /v1 keeps ``detail`` alone,
    since an OpenAI client takes ``error`` for an object."""
    body = {"detail": message}
    if request.url.path.startswith("/evidence"):
        body["error"] = message
    return JSONResponse(body, status_code=status, headers=headers)


async def service_error_response(request: Request, exc: ServiceError) -> JSONResponse:
    """A refusal (errors.py): its own status and headers (a full cache's Retry-After)."""
    if exc.status >= 500:
        logger.warning("%s %s: %s", request.method, request.url.path, exc)
    return _answer(request, str(exc), exc.status, exc.headers)


async def value_error_response(request: Request, exc: ValueError) -> JSONResponse:
    """A bad value that got past validation (a model refusing its input, a bad option) is the client's: 400.

    Its text reaches the client only with DEBUG on, as an internal error's does, since nobody wrote it for them."""
    logger.info("%s %s refused: %s: %s", request.method, request.url.path, type(exc).__name__, exc)
    return _answer(request, describe(exc, "Invalid request", Config.DEBUG), 400)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application"""
    # One ArcadeDB database holds everything the service stores, and every store below shares it; opened on
    # first use (see vectorstore/engine.py).
    database = Database(Config.ARCADEDB_PATH, Config.ARCADEDB_HEAP) if Config.ARCADEDB_PATH else None
    if database is not None:
        # Its JVM may only start on the main thread: here, at import (uvicorn's workers, tests and scripts alike).
        try:
            database.start_jvm()
        except RuntimeError as e:
            logger.warning("The database waits for its JVM: %s", e)
    collections = CollectionStore(database) if database is not None else None
    monitoring = (
        MonitoringStore(database, Config.MONITORING_RETENTION_DAYS)
        if database is not None and Config.MONITORING_RETENTION_DAYS > 0 else None
    )
    # Counts for GET /metrics; hands each record on to the store.
    metrics = Metrics(monitoring)
    # /db. The evals, jobs, the enrichers' cache and the evidence ledger keep their tables in it, in namespaces
    # /db shows only to the admin, read-only.
    tables = GraphStore(database) if database is not None else None
    if tables is not None:
        # The playground's saved chats and presets; it made its namespace through /db before it was kept.
        tables.manage(playground_routes.NAMESPACE, playground_routes.OWNER, playground_routes.TABLES, adopt=True)
    # Traces, datasets, runs and scores.
    evals = EvalStore(database, tables, Config.TRACE_RETENTION_DAYS, Config.TRACE_MAX_CHARS) if database is not None else None
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
        # Each request is counted (and kept for the dashboard), and traced while traces are kept.
        sinks=[metrics.on_request] + ([evals.on_request] if evals is not None and evals.capturing else []),
    )
    webhooks = Webhooks(Config.WEBHOOK_URLS, Config.WEBHOOK_SECRET)
    # Background work waits in one task table, each kind with its limit: jobs, eval runs, judging, downloads (jobs/tasks.py).
    tasks = TaskRunner(JobStore(database, tables), loaded=model_manager.is_loaded) if database is not None else None
    runs = RunExecutor(evals, model_manager, tasks, webhooks) if evals is not None else None
    jobs = None
    if Config.JOBS_DIR and tasks is not None:
        jobs_dir = Path(Config.JOBS_DIR).resolve()
        jobs = JobRunner(
            tasks,
            webhooks,
            jobs_dir=jobs_dir,
            envs_dir=Config.JOB_ENVS_DIR,
            data_dir=Config.JOB_DATA_DIR,
            max_running=Config.MAX_RUNNING_JOBS,
            num_cpus=Config.JOB_NUM_CPUS,
            device=Config.JOB_DEVICE,
        )

    # Hub downloads started from the dashboard, a child process each: a kind of task, so they need the database.
    hub_downloads = Downloads(tasks, max_running=Config.MAX_HUB_DOWNLOADS) if tasks is not None else None

    enrichers = None
    if Config.ENRICHERS_DIR:
        enrichers_dir = Path(Config.ENRICHERS_DIR).resolve()
        enricher_store = EnricherStore(enrichers_dir)
        enrichers = EnrichService(
            enricher_store,
            ResultCache(database, tables) if tables is not None else MemoryCache(),
            terms_allowed=Config.ENRICHER_TERMS,
            allow_private=Config.ENRICH_ALLOW_PRIVATE,
            max_concurrency=Config.ENRICH_CONCURRENCY,
        )
        # The enrichers' keys stay with the service, like its own (below).
        procs.hide(*(entry.spec.secret for entry in enricher_store.all().values() if entry.spec.secret))
    # Jobs and model servers run as children of this process: without its keys and secrets.
    procs.hide(*Config.secret_names())

    # Evidence capture (/evidence) shares the database too; its files and key live in EVIDENCE_DIR.
    evidence = None
    evidence_tsa = Tsa(Config.EVIDENCE_TSA_URL, version=APP_VERSION)
    if database is not None and Config.EVIDENCE_DIR:
        evidence = EvidenceStore(
            database, tables, CaptureFiles(Path(Config.EVIDENCE_DIR).resolve()), Signer(Config.EVIDENCE_KEY_FILE),
            APP_VERSION,
        )

    async def retry_timestamps() -> None:
        """Ask the TSA again for records that have no time stamp yet."""
        ok, failed = await timestamp_pending(evidence, evidence_tsa)
        if ok or failed:
            logger.info("Evidence time stamps retried: %d granted, %d failed", ok, failed)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        # Periodic work, each an every() loop (background.py) cancelled at shutdown.
        loops = []
        if evidence is not None and evidence_tsa.enabled:
            loops.append(every(3600, retry_timestamps, "Retrying evidence time stamps", first=60))
        if Config.MODEL_IDLE_TIMEOUT > 0:
            idle = Config.MODEL_IDLE_TIMEOUT
            loops.append(every(min(30.0, idle), lambda: model_manager.cache.unload_idle(idle), "Unloading idle models",
                               first=min(30.0, idle)))
        if evals is not None:
            # Also with capture off (TRACE_RETENTION_DAYS=0): traces kept before are dropped.
            loops.append(every(3600, evals.prune, "Pruning traces"))
        if monitoring is not None:
            loops.append(every(3600, monitoring.prune, "Pruning request history"))
        if enrichers is not None:
            loops.append(every(3600, enrichers.cache.prune, "Pruning the enrichers' cache"))
        background = [asyncio.create_task(loop) for loop in loops]
        if runs is not None:
            await runs.start()
        # In the background: the service answers while they load.
        preload = asyncio.create_task(model_manager.preload(Config.PRELOAD_MODELS, Config.PINNED_MODELS))
        if jobs is not None:
            await jobs.start()
        if tasks is not None:
            # After the kinds' own start: queued tasks start as their kinds have room.
            await tasks.start()
        yield
        if jobs is not None:
            await jobs.stop()
        if tasks is not None:
            # Running tasks are stopped and marked failed (interrupted).
            await tasks.stop()
        for task in (*background, preload):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await webhooks.close()
        await model_manager.clear_all_models()
        # Model servers that are asleep rather than loaded, too.
        await model_server.stop_all()
        if enrichers is not None:
            await enrichers.close()
        await evidence_tsa.close()
        # The writers finish their queues (the unloads above included) before the database closes.
        for writer in (evals, monitoring):
            if writer is not None:
                writer.close()
        if database is not None:
            database.close()

    app = FastAPI(
        title="Core AI & Data toolkit",
        version=APP_VERSION,
        description="A self-hosted service for small AI models and the data work around them",
        lifespan=lifespan,
    )

    # Browsers reject credentials with a wildcard origin, so only enable them
    # when explicit origins are configured.
    allow_all = "*" in Config.CORS_ORIGINS
    app.add_middleware(
        CORSMiddleware,
        allow_origins=Config.CORS_ORIGINS,
        allow_credentials=not allow_all,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    for warning in Config.warnings():
        logger.warning(warning)

    # Until setup runs, saving it needs this code from the log, so nobody who
    # merely reaches the server can claim it. main.py picks one code for all
    # workers.
    app.state.setup_code = None
    if Config.needs_setup():
        code = Config.SETUP_CODE or secrets.token_urlsafe(12)
        app.state.setup_code = code
        host = "localhost" if Config.HOST in ("0.0.0.0", "::", "127.0.0.1") else Config.HOST
        logger.warning(
            "Not set up yet. Open http://%s:%s/admin/setup#code=%s to choose API keys and allowed models "
            "(setup code: %s)", host, Config.PORT, code, code,
        )

    app.state.manager = model_manager
    app.state.monitoring = monitoring
    app.state.metrics = metrics
    app.state.hub_downloads = hub_downloads
    app.state.logs = log_buffer
    app.state.evals = evals
    app.state.runs = runs
    if runs is not None:
        # A judge with no base URL is a model this service runs, asked through its own /v1 in-process; those
        # requests are ordinary ones, tagged "judge" in Activity & logs.
        runs.local_client = lambda priority: httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), headers={"X-Trace-Tag": "judge", "X-Priority": priority})

    @app.middleware("http")
    async def internal_errors(request: Request, call_next):
        """Anything a route did not expect is a 500 naming the error's type (its text too with DEBUG on), logged with
        its traceback. So no route maps exceptions to statuses; only a reply streamed already reports its own failure
        (api/streaming.py), since its status has gone out."""
        try:
            return await call_next(request)
        except Exception as e:
            logger.exception("%s %s failed", request.method, request.url.path)
            return _answer(request, describe(e, "Internal error", Config.DEBUG), 500)

    @app.middleware("http")
    async def tag_requests(request: Request, call_next):
        """Tag this request's trace from X-Trace-Tag (comma-separated) and X-Session-Id. X-Priority (high, normal or
        low; high when not given) says how urgent it is when models compete for room (models/cache.py)."""
        tags = [t.strip()[:64] for t in request.headers.get("x-trace-tag", "").split(",") if t.strip()][:8]
        session = (request.headers.get("x-session-id") or "").strip()[:128] or None
        named = (request.headers.get("x-priority") or "").strip().lower()
        if named and named not in PRIORITIES:
            return _answer(request, "X-Priority must be high, normal or low", 400)
        if tags or session or named:
            request_context.set(RequestContext(tags=tags, session=session, priority=PRIORITIES.get(named, HIGH)))
        return await call_next(request)

    app.state.database = database
    app.state.collections = collections
    app.state.tables = tables
    app.state.jobs = jobs
    app.state.tasks = tasks
    app.state.webhooks = webhooks
    app.state.enrichers = enrichers
    app.state.evidence = evidence
    app.state.evidence_tsa = evidence_tsa
    # Every refusal the service makes (errors.py) is answered with its status; a plain ValueError is a 400.
    app.add_exception_handler(ServiceError, service_error_response)
    app.add_exception_handler(ValueError, value_error_response)
    # So the schema says what every route may answer with besides its own: a client's validator (and
    # tests/test_schemathesis.py) then holds each error body to {"detail": message}.
    refusals = {cls.status: {"model": ErrorDetail, "description": (cls.__doc__ or "").strip().split("\n")[0] or cls.__name__}
                for cls in _every_error(ServiceError)}
    refusals[400] = {"model": ErrorDetail, "description": "Refused: the request is wrong (a bad value, an unknown name)"}
    refusals[500] = {"model": ErrorDetail, "description": "Internal error: its type, and its text with DEBUG on"}
    # In this order: a path two routers could match goes to the first.
    for r in (public_router, router, openai_router, decision_router, admin_router, setup_router, playground_router,
              collection_router, table_router, job_router, eval_router, enrich_router, model_server_router, metrics_router,
              hub_router, evidence_router):
        app.include_router(r, responses=refusals)
    # The built pages' scripts and styles; missing until frontend/ is built (see api/pages.py).
    app.mount("/admin/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="admin-static")
    return app


app = create_app()
