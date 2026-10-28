"""
What a route depends on: who may call it, and the services it uses.

Access. A router states its level once: ``APIRouter(dependencies=deps.CLIENT)``
or ``deps.ADMIN``; a route that needs more than its router adds
``dependencies=deps.ADMIN``. CLIENT takes API_KEY or the admin key, ADMIN the
admin key (or API_KEY when no admin key is set); each lets everything through
while its key is unset. Only api/public_routes.py and the first-run wizard
(api/setup_routes.py) have routes without a key, and tests/test_access.py fails
on any other.

Services. ``Annotated`` types for what api/app.py keeps on app.state, so a
route's signature says what it uses (``runner: deps.Jobs``); one that can be
off answers 503 (errors.Unavailable) while it is.
"""

import secrets
from typing import Annotated, Any, Optional

from fastapi import Depends, Header, Request

import jobs.webhooks
import monitoring.metrics
from config import Config
from enrichers import EnrichService
from errors import Forbidden, Unauthorized, Unavailable
from evals import EvalStore, RunExecutor
from evidence import EvidenceStore, Tsa
from jobs.runner import JobRunner
from models import ModelManager, model_server
from models.autogluon import predictor as autogluon_predictor
from models.hub import Downloads
from monitoring import LogBuffer, MonitoringStore
from vectorstore import CollectionStore
from vectorstore.graph import GraphStore


# ------------------------------------------------------------------- access
def check_key(authorization: Optional[str], x_api_key: Optional[str], *accepted: Optional[str]) -> None:
    """Raise 401 unless the request carries one of the accepted keys (as X-API-Key, or a Bearer token)."""
    supplied = x_api_key
    if supplied is None and authorization and authorization.lower().startswith("bearer "):
        supplied = authorization[len("bearer "):].strip()
    if supplied is not None:
        for key in accepted:
            if key and secrets.compare_digest(supplied.encode(), key.encode()):
                return
    raise Unauthorized("Invalid or missing API key")


def require_api_key(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> None:
    """Client routes: Config.API_KEY or the admin key (no-op when API_KEY is unset)."""
    if Config.API_KEY:
        check_key(authorization, x_api_key, Config.API_KEY, Config.ADMIN_API_KEY)


def require_admin_key(
    authorization: Optional[str] = Header(None),
    x_api_key: Optional[str] = Header(None),
) -> None:
    """Admin routes: Config.ADMIN_API_KEY, or API_KEY when no admin key is set (no-op when neither is)."""
    expected = Config.ADMIN_API_KEY or Config.API_KEY
    if expected:
        check_key(authorization, x_api_key, expected)


CLIENT = [Depends(require_api_key)]
ADMIN = [Depends(require_admin_key)]


def model_allowed(model_id: str) -> bool:
    """Whether clients may use ``model_id``: Config.ALLOWED_MODELS is empty, or names it, or the operator configured
    it (a model server's entry, a predictor a job trained here, which only an admin can start). The model manager
    checks this on every load and run (api/app.py gives it this), so clients cannot make the server fetch arbitrary
    repos whichever route they use."""
    return (
        not Config.ALLOWED_MODELS or model_id in Config.ALLOWED_MODELS
        or model_server.configured(model_id) or autogluon_predictor.trained(model_id)
    )


def check_model_allowed(model_id: str) -> None:
    """403 unless ``model_allowed``: for a route that should refuse before doing other work (queueing an eval run)."""
    if not model_allowed(model_id):
        raise Forbidden("model_id is not in ALLOWED_MODELS")


# ----------------------------------------------------------------- services
def _state(name: str, off: Optional[str] = None) -> Any:
    """A dependency on ``app.state.<name>``; with ``off``, a 503 saying it while that is None."""
    def get(request: Request) -> Any:
        value = getattr(request.app.state, name)
        if value is None and off is not None:
            raise Unavailable(off)
        return value
    get.__name__ = f"app_state_{name}"
    return Depends(get)


_EVALS_OFF = "Traces and evals are off: ARCADEDB_PATH is empty"

Jobs = Annotated[JobRunner, _state("jobs", "Jobs are off: JOBS_DIR or ARCADEDB_PATH is empty")]
Tables = Annotated[GraphStore, _state("tables", "Tables are off: ARCADEDB_PATH is empty")]
Collections = Annotated[CollectionStore, _state("collections", "Collections are off: ARCADEDB_PATH is empty")]
Evals = Annotated[EvalStore, _state("evals", _EVALS_OFF)]
Runs = Annotated[RunExecutor, _state("runs", _EVALS_OFF)]
Enrichers = Annotated[EnrichService, _state("enrichers", "Enrichers are off: ENRICHERS_DIR is empty")]
Evidence = Annotated[EvidenceStore, _state("evidence", "Evidence capture is off: EVIDENCE_DIR or ARCADEDB_PATH is empty")]
# Always there:
Manager = Annotated[ModelManager, _state("manager")]
Webhooks = Annotated[jobs.webhooks.Webhooks, _state("webhooks")]
HubDownloads = Annotated[Downloads, _state("hub_downloads", "Hub downloads are off: they need the database (ARCADEDB_PATH)")]
Metrics = Annotated[monitoring.metrics.Metrics, _state("metrics")]
Logs = Annotated[LogBuffer, _state("logs")]
# None while monitoring is off (ARCADEDB_PATH empty, or MONITORING_RETENTION_DAYS 0): the routes then show the live state.
Monitoring = Annotated[Optional[MonitoringStore], _state("monitoring")]
# None with the enrichers off: a capture is then stored without lookups.
OptionalEnrichers = Annotated[Optional[EnrichService], _state("enrichers")]
EvidenceTsa = Annotated[Tsa, _state("evidence_tsa")]
