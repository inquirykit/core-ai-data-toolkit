"""
What a route depends on: who may call it, and the services it uses.

Access. A router states its level once: ``APIRouter(dependencies=deps.CLIENT)``
or ``deps.ADMIN``, and its routes add none; a module with routes of both has
two routers. CLIENT takes API_KEY, a live client key (made in /admin:
access/keys.py) or the admin key; ADMIN the admin key (or API_KEY when no admin
key is set), never a client key. CLIENT lets everything through while API_KEY
is unset and no client key was ever made, ADMIN while neither setting is set.
The two are security schemes, so the API's schema says which key each route
takes. A client key's grants (access/grants.py) narrow it further: the client
guard checks its tables once a request, from the route alone, and its models
go on the request's context, where ``model_allowed`` holds the request to
them; a key granted ``manage_models`` also opens the admin's routes that load,
unload and pin models. Only api/public_routes.py and the first-run wizard
(api/setup_routes.py) have routes without a key; tests/test_access.py fails on
any other, and lists the routes a client key opens.

Services. ``Annotated`` types for what api/app.py keeps on app.state, so a
route's signature says what it uses (``runner: deps.Jobs``); monitoring and the
audit log are None while their retention is 0.
"""

import secrets
from typing import Annotated, Any, NamedTuple, Optional

from fastapi import Depends, Query, Request, Security
from fastapi.security import APIKeyHeader, HTTPBearer

import buckets
import tasks.webhooks
import monitoring.metrics
from access import ClientKeys as ClientKeyStore, Grants, grants
from buckets import BucketFiles
from core.config import Config
from enrichers import EnrichService
from core.errors import Forbidden, Unauthorized
from core.keystore import KeyStore
from evals import EvalStore, RunExecutor
from evidence import EvidenceStore, Tsa
from jobs.runner import JobRunner
from tasks.runner import TaskRunner
from models import ModelManager, registry
from models.events import request_context
from monitoring import AuditLog, LogBuffer, MonitoringStore
from playground import PlaygroundStore
from views import Views as ViewStore
from workflows import WorkflowRunner
from db.graph import GraphStore


# ------------------------------------------------------------------- access
class Caller(NamedTuple):
    """Who sent a request. ``level``: "admin" (the admin's key, as require_admin_key takes it), "client" (API_KEY or a
    live client key), "none" (no key), "wrong" (a key that opens nothing: unknown, expired or revoked), or "open"
    while there is no key of any kind. ``key`` is the client key's id, when one was sent (live or not); ``problem``
    the 401's message for a key that opens nothing; ``grants`` a live client key's (access/grants.py), none for any
    other caller."""
    level: str
    key: Optional[str] = None
    problem: str = "Invalid or missing API key"
    grants: Grants = Grants()


def _supplied(authorization: Optional[str], x_api_key: Optional[str]) -> Optional[str]:
    """The key a request carries, as X-API-Key or a Bearer token."""
    if x_api_key is None and authorization and authorization.lower().startswith("bearer "):
        return authorization[len("bearer "):].strip()
    return x_api_key


def _matches(supplied: str, key: Optional[str]) -> bool:
    return bool(key) and secrets.compare_digest(supplied.encode(), key.encode())


def clients_need_a_key(request: Request) -> bool:
    """Whether the client routes need a key: API_KEY is set, or a client key was ever made. Revoking every client key
    (or their expiring) never opens them again."""
    return bool(Config.API_KEY) or request.app.state.client_keys.made_any()


def caller(request: Request) -> Caller:
    """Who sent ``request`` (``Caller``), worked out once and kept on it: the guards, the audit log and the evidence
    routes all ask."""
    known = getattr(request.state, "caller", None)
    if known is not None:
        return known
    request.state.caller = found = _identify(request)
    return found


def _identify(request: Request) -> Caller:
    if not (Config.ADMIN_API_KEY or clients_need_a_key(request)):
        return Caller("open")
    supplied = _supplied(request.headers.get("authorization"), request.headers.get("x-api-key"))
    if supplied is None:
        return Caller("none")
    if _matches(supplied, Config.ADMIN_API_KEY or Config.API_KEY):  # the admin's key, as require_admin_key takes it
        return Caller("admin")
    if _matches(supplied, Config.API_KEY):
        return Caller("client")
    key = request.app.state.client_keys.find(supplied)
    if key is None:
        return Caller("wrong")
    state = key.state()
    if state == "live":
        return Caller("client", key.id, grants=key.grants)
    return Caller("wrong", key.id, f"API key {state}")


def check_client(request: Request) -> None:
    """Unauthorized unless the request carries API_KEY, a live client key or the admin key (none needed while clients
    need no key)."""
    who = caller(request)
    if clients_need_a_key(request) and who.level not in ("admin", "client"):
        raise Unauthorized(who.problem)
    grants.check(who.grants, request.method, request.scope["route"].path, request.path_params, request.query_params)
    _hold_to_its_models(who.grants)
    _by_the_admin(who)


def check_admin(request: Request) -> None:
    """Unauthorized unless the request carries Config.ADMIN_API_KEY, or API_KEY when no admin key is set (none needed
    when neither is). A client key never passes, but one granted manage_models on the routes that load, unload and
    pin models, for the models it may use."""
    if Config.ADMIN_API_KEY or Config.API_KEY:
        who = caller(request)
        if who.level != "admin":
            if not grants.manages_models(who.grants, request.scope["route"].path):
                raise Unauthorized(who.problem)
            _hold_to_its_models(who.grants)
            if "model_id" in request.path_params:  # an unload or a pin, which load and run nothing
                check_model_allowed(request.path_params["model_id"])
        _by_the_admin(who)


def _hold_to_its_models(granted: Grants) -> None:
    """A client key's models go on the request's context, where model_allowed holds every load and run to them."""
    context = request_context.get()
    if context is not None:
        context.models = granted.models


# The two ways a key may come, as the schema shows them: the guards read the headers themselves (caller()).
_CLIENT_KEY = APIKeyHeader(name="X-API-Key", scheme_name="ClientKey", auto_error=False,
                           description="API_KEY, a client key made in /admin, or the admin key")
_CLIENT_BEARER = HTTPBearer(scheme_name="ClientBearer", auto_error=False, description="The same keys, as a Bearer token")
_ADMIN_KEY = APIKeyHeader(name="X-API-Key", scheme_name="AdminKey", auto_error=False,
                          description="ADMIN_API_KEY (API_KEY when no admin key is set); never a client key, but one "
                                      "granted manage_models on the routes that load, unload and pin models")
_ADMIN_BEARER = HTTPBearer(scheme_name="AdminBearer", auto_error=False, description="The same key, as a Bearer token")


async def require_api_key(request: Request, _key: Any = Security(_CLIENT_KEY), _bearer: Any = Security(_CLIENT_BEARER)) -> None:
    """Client routes: ``check_client``."""
    check_client(request)


async def require_admin_key(request: Request, _key: Any = Security(_ADMIN_KEY), _bearer: Any = Security(_ADMIN_BEARER)) -> None:
    """Admin routes: ``check_admin``."""
    check_admin(request)


def _by_the_admin(who: Caller) -> None:
    """A request the admin's key sent (the playground, /admin) is the admin's in the request log, not an app's. The
    guards say so, as they know the caller: the middleware only knows the path (api/app.py)."""
    context = request_context.get()
    if who.level == "admin" and context is not None and context.source == "api":
        context.source = "admin"


CLIENT = [Depends(require_api_key)]
ADMIN = [Depends(require_admin_key)]

# Where a page of a list starts: the previous page's ``next`` (db/schema.py, encode_cursor), the first page without.
# Every list outside /db pages with ``limit`` and ``cursor``; /db's routes call them _limit and _cursor, since its other
# parameters are a table's fields.
Cursor = Annotated[Optional[str], Query(description="The previous page's next")]

# The largest integer the database takes (DuckDB's BIGINT): the bound of a query's integer, which past it would be a
# 500.
INT64_MAX = 2**63 - 1


def model_allowed(model_id: str) -> bool:
    """Whether clients may use ``model_id``: Config.ALLOWED_MODELS is empty, or names it, or the class serving it
    says the operator chose it (``registry.operator_chose``: a model server's entry, a predictor a job trained here,
    which only an admin can start). The model manager checks this on every load and run (api/app.py gives it this),
    so clients cannot make the server fetch arbitrary repos whichever route they use. A client key granted
    ``models`` (access/grants.py) is held to those too: its guard puts them on the request's context."""
    context = request_context.get()
    if context is not None and context.models is not None and model_id not in context.models:
        return False
    return not Config.ALLOWED_MODELS or model_id in Config.ALLOWED_MODELS or registry.operator_chose(model_id)


def check_model_allowed(model_id: str) -> None:
    """403 unless ``model_allowed``: for a route that should refuse before doing other work (queueing an eval run)."""
    if not model_allowed(model_id):
        raise Forbidden("model_id is not allowed: not in ALLOWED_MODELS, or not one of this key's models")


# ----------------------------------------------------------------- services
def _state(name: str) -> Any:
    """A dependency on ``app.state.<name>``."""
    async def get(request: Request) -> Any:
        return getattr(request.app.state, name)
    get.__name__ = f"app_state_{name}"
    return Depends(get)


Jobs = Annotated[JobRunner, _state("jobs")]
Tasks = Annotated[TaskRunner, _state("tasks")]
Tables = Annotated[GraphStore, _state("tables")]
Buckets = Annotated[BucketFiles, _state("buckets")]
Remotes = Annotated[buckets.Remotes, _state("remotes")]
Copies = Annotated[buckets.Copies, _state("copies")]
Views = Annotated[ViewStore, _state("views")]
Workflows = Annotated[WorkflowRunner, _state("workflows")]
Evals = Annotated[EvalStore, _state("evals")]
Runs = Annotated[RunExecutor, _state("runs")]
Enrichers = Annotated[EnrichService, _state("enrichers")]
Evidence = Annotated[EvidenceStore, _state("evidence")]
Playground = Annotated[PlaygroundStore, _state("playground")]
ClientKeys = Annotated[ClientKeyStore, _state("client_keys")]
Manager = Annotated[ModelManager, _state("manager")]
Keys = Annotated[KeyStore, _state("keys")]
Webhooks = Annotated[tasks.webhooks.Webhooks, _state("webhooks")]
Metrics = Annotated[monitoring.metrics.Metrics, _state("metrics")]
Logs = Annotated[LogBuffer, _state("logs")]
# None while MONITORING_RETENTION_DAYS is 0: the routes then show the live state.
Monitoring = Annotated[Optional[MonitoringStore], _state("monitoring")]
# None while AUDIT_RETENTION_DAYS is 0.
Audit = Annotated[Optional[AuditLog], _state("audit")]
EvidenceTsa = Annotated[Tsa, _state("evidence_tsa")]
