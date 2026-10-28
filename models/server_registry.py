"""
The model server entries in effect in this process.

load_configured() serves what MODEL_SERVERS_FILE and the saved file say (see
models/server_specs.py), once, when registry.discover() runs; reload() reads
them again after an edit in /admin, and tests add entries with
register_specs(). Each time, the entries are registered with ServerModel
(models/model_server.py) last, after registry.unregister dropped the old
registrations, so they win over model files claiming the same IDs. The
backends that failed to start are kept here too (ServerModel skips one for a
while), and forgotten when their entry changes.
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from . import server_process
from .server_specs import TASKS, Provider, ServerSpec, ServerStore

logger = logging.getLogger(__name__)

_providers: Dict[str, Provider] = {}
_specs: Dict[str, ServerSpec] = {}
# (model ID, backend index) -> when it failed to start (time.monotonic()) and why.
_failed: Dict[Tuple[str, int], Tuple[float, str]] = {}


def configured(model_id: str) -> bool:
    """Whether a model server entry lists ``model_id`` (the operator chose it, so it is allowed)."""
    from . import registry

    registry.discover()
    return model_id in _specs


def providers() -> Dict[str, Provider]:
    return dict(_providers)


def specs() -> Dict[str, ServerSpec]:
    return dict(_specs)


def _register() -> None:
    """Serve ``_specs`` with ServerModel, registered last, so they win over model files claiming the same IDs."""
    from .model_server import ServerModel
    from .registry import ModelCard, register, unregister

    unregister(ServerModel)
    for task in TASKS:
        cards = [ModelCard(s.model_id, s.description, s.tags) for s in _specs.values() if s.task == task]
        if cards:
            register(task=task, ids=cards)(ServerModel)


def register_specs(specs: Dict[str, ServerSpec], providers: Optional[Dict[str, Provider]] = None) -> None:
    """Serve ``specs`` too, beside those already served (tests add theirs this way)."""
    _specs.update(specs)
    _providers.update(providers or {})
    _register()


def apply(providers: Dict[str, Provider], specs: Dict[str, ServerSpec]) -> List[str]:
    """Make exactly these the model servers in effect. Returns the model IDs whose serving changed:
    added, edited, removed, or on a provider that was."""
    edited = {name for name in {*_providers, *providers} if _providers.get(name) != providers.get(name)}
    changed = sorted(
        model_id for model_id in {*_specs, *specs}
        if _specs.get(model_id) != specs.get(model_id)
        or any(b.provider in edited for b in (specs.get(model_id) or _specs[model_id]).backends)
    )
    _providers.clear()
    _providers.update(providers)
    _specs.clear()
    _specs.update(specs)
    for key in [k for k in _failed if k[0] in changed]:
        del _failed[key]
    _register()
    return changed


def load_configured() -> None:
    """Serve what MODEL_SERVERS_FILE and the saved file say (registry.discover() calls this once)."""
    store = ServerStore.from_config()
    effective = store.effective()
    for where, problem in effective.problems.items():
        logger.error("Model servers, %s: %s", where, problem)
    apply(effective.providers, effective.models)
    if effective.models:
        logger.info("Model servers: %s", ", ".join(effective.models))


async def reload(cache: Any) -> List[str]:
    """Read the files again and serve what they say now. The changed models' loaded instances are unloaded
    (once their requests finish) and their sleeping servers stopped, so the next request uses the new entry."""
    effective = ServerStore.from_config().effective()
    changed = apply(effective.providers, effective.models)
    for model_id in changed:
        await server_process.stop_sleeping(model_id)
        for key in [k for k, e in list(cache.entries.items()) if e.info.model_id == model_id]:
            try:
                await cache.remove_model(key, detail="reconfigured")
            except KeyError:
                pass  # unloaded meanwhile
    return changed
