"""
/admin/model-servers: the model servers, their providers and backends, edited from /admin.

    GET    /admin/model-servers                      what is in effect, where each entry comes from, problems
    PUT    /admin/model-servers/providers/{name}     create or replace a provider
    DELETE /admin/model-servers/providers/{name}     remove the saved one (the file's comes back)
    PUT    /admin/model-servers/models/{model_id}    create or replace a model's entry
    DELETE /admin/model-servers/models/{model_id}    remove the saved one (the file's comes back)
    POST   /admin/model-servers/probe                {model_id, entry?}: the backend a load would use now, and why
                                                     not the ones before it

All need the admin key. Changes go to MODEL_SERVERS_SAVED_FILE, never to
MODEL_SERVERS_FILE, and take effect at once: a model whose entry changed is
unloaded once its requests finish and loads from the new entry on its next
request. Commands run on this host, so a change may only keep the ``cmd``
and ``env`` of the file's entry and of the one in effect, unless
MODEL_SERVERS_ALLOW_COMMANDS is on. The dashboard's Model servers section is
a client of these routes.
"""

from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field

from config import Config
from models import ModelManager, model_server
from models.server_specs import (
    MODEL_ID, PROVIDER_NAME, TASKS, ModelEntry, Provider, ServerSpec, ServerStore, dump_entry, new_commands,
)
from errors import Conflict, Forbidden, NotFound, Refused, ServiceError, Unavailable
from . import deps, responses

router = APIRouter(prefix="/admin/model-servers", tags=["model servers"], dependencies=deps.ADMIN)


class ProbeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model_id: str = Field(..., min_length=1, max_length=200)
    entry: Optional[ModelEntry] = Field(None, description="A draft to probe instead of the entry in effect")


def _store() -> ServerStore:
    return ServerStore.from_config()


def _writable(store: ServerStore) -> None:
    if store.saved is None:
        raise Unavailable("MODEL_SERVERS_SAVED_FILE is empty: model servers are read-only here")


def _listing(manager: ModelManager, store: ServerStore, notes: List[str] = ()) -> Dict[str, Any]:
    """Everything the Model servers section shows."""
    effective = store.effective()
    serving = {e.info.model_id: e.model.backend for e in manager.cache.entries.values() if e.model.backend}
    providers = []
    for name, provider in sorted(effective.providers.items()):
        origin = effective.origin("providers", name)
        file_provider = effective.file.providers.get(name)
        providers.append({
            "name": name, **provider.model_dump(mode="json"), "origin": origin, "used_by": effective.users(name),
            "file_provider": file_provider.model_dump(mode="json") if origin == "replaced" else None,
        })
    models = []
    for model_id, spec in sorted(effective.models.items()):
        origin = effective.origin("models", model_id)
        file_spec = effective.file.models.get(model_id)
        models.append({
            "model_id": model_id, **dump_entry(spec), "origin": origin,
            "file_entry": dump_entry(file_spec) if origin == "replaced" else None,
            "status": manager.model_state(model_id, spec.task),
            "serving": serving.get(model_id),
        })
    return {
        "file": str(store.file),
        "file_exists": store.file.is_file(),
        "saved_file": str(store.saved) if store.saved else None,
        "editable": store.saved is not None,
        "commands_allowed": Config.MODEL_SERVERS_ALLOW_COMMANDS,
        "tasks": list(TASKS),
        "providers": providers,
        "models": models,
        "problems": [{"where": where, "message": message} for where, message in effective.problems.items()],
        "notes": list(notes),
    }


async def _apply(manager: ModelManager, store: ServerStore) -> List[str]:
    """Serve what the files say now; notes on what that did."""
    loaded = {e.info.model_id for e in manager.cache.entries.values()}
    changed = await model_server.reload(manager.cache)
    notes = [f"{m} was unloaded; its next request loads it from the new entry" for m in changed if m in loaded]
    if Config.WORKERS > 1:
        notes.append("Only this worker serves the change until the others restart (WORKERS > 1)")
    return notes


def _save(write) -> None:
    try:
        write()
    except OSError as e:
        raise ServiceError(f"Could not write the saved model servers file: {e.strerror or e}", status=500)


@router.get("", **responses.answering(responses.ModelServers))
async def list_model_servers(manager: deps.Manager) -> Dict[str, Any]:
    """The providers and models in effect: the file's, each replaced by the one saved from /admin."""
    return _listing(manager, _store())


@router.put("/providers/{name}", **responses.answering(responses.ModelServers))
async def put_provider(manager: deps.Manager, name: str, provider: Provider) -> Dict[str, Any]:
    """Create or replace a provider; every backend on it uses the new settings."""
    if not PROVIDER_NAME.match(name):
        raise Refused("A provider name is lower case letters, digits, _ and - (up to 63)")
    store = _store()
    _writable(store)
    _save(lambda: store.save_provider(name, provider))
    return _listing(manager, store, await _apply(manager, store))


@router.delete("/providers/{name}", **responses.answering(responses.ModelServers))
async def delete_provider(manager: deps.Manager, name: str) -> Dict[str, Any]:
    """Remove a saved provider, or the saved changes to one in the file (which comes back)."""
    store = _store()
    _writable(store)
    effective = store.effective()
    origin = effective.origin("providers", name) if name in effective.providers else None
    if origin is None:
        raise NotFound(f"No provider named {name!r}")
    if origin == "file":
        raise Refused(f"{name} is in {store.file}; remove it there")
    users = effective.users(name)
    if origin == "saved" and users:
        raise Conflict(f"{', '.join(users)} use {name}; change their backends first")
    _save(lambda: store.delete_provider(name))
    return _listing(manager, store, await _apply(manager, store))


@router.put("/models/{model_id:path}", **responses.answering(responses.ModelServers))
async def put_model(manager: deps.Manager, model_id: str, entry: ModelEntry) -> Dict[str, Any]:
    """Create or replace a model's entry: its task and its backends, in the order they are tried."""
    if not MODEL_ID.match(model_id):
        raise Refused("A model ID is letters, digits and ._:/@+- (up to 200)")
    store = _store()
    _writable(store)
    effective = store.effective()
    spec = ServerSpec.model_validate({**entry.model_dump(exclude_unset=True), "model_id": model_id})
    missing = sorted({b.provider for b in spec.backends if b.provider and b.provider not in effective.providers})
    if missing:
        raise Refused(f"No provider named {', '.join(missing)}")
    if not Config.MODEL_SERVERS_ALLOW_COMMANDS:
        added = new_commands(spec, effective.file.models.get(model_id), effective.models.get(model_id))
        if added:
            raise Forbidden((
                f"MODEL_SERVERS_ALLOW_COMMANDS is off, so /admin cannot add commands ({'; '.join(added)}). "
                f"Put them in {store.file}, or turn the setting on."
            ))
    _save(lambda: store.save_model(spec))
    return _listing(manager, store, await _apply(manager, store))


@router.delete("/models/{model_id:path}", **responses.answering(responses.ModelServers))
async def delete_model(manager: deps.Manager, model_id: str) -> Dict[str, Any]:
    """Remove a saved entry, or the saved changes to one in the file (which comes back)."""
    store = _store()
    _writable(store)
    effective = store.effective()
    if model_id not in effective.models:
        raise NotFound(f"No model server entry for {model_id}")
    if effective.origin("models", model_id) == "file":
        raise Refused(f"{model_id} is in {store.file}; remove it there")
    _save(lambda: store.delete_model(model_id))
    return _listing(manager, store, await _apply(manager, store))


@router.post("/probe", **responses.answering(responses.ModelServerProbe))
async def probe_model(body: ProbeRequest) -> Dict[str, Any]:
    """Probe every backend of an entry (the one in effect, or a draft) the way a load would. Nothing starts."""
    if body.entry is not None:
        spec = ServerSpec.model_validate({**body.entry.model_dump(exclude_unset=True), "model_id": body.model_id})
    else:
        spec = model_server.specs().get(body.model_id)
        if spec is None:
            raise NotFound(f"No model server entry for {body.model_id}")
    probes, chosen = await model_server.probe(spec)
    return {"model_id": body.model_id, "chosen": chosen, "backends": [p.as_dict() for p in probes]}
