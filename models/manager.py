"""
Model manager for handling model loading, caching, and inference
"""

import asyncio
import logging
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple
from errors import Forbidden, NotFound
from . import memory, registry, tasks
from .base import BaseModel, NullMonitor, TryNextBackend, resolve_device
from .cache import CapacityError, Entry, ModelCache
from .events import HIGH, Call, RequestContext, RequestEvent, Sink, request_context

logger = logging.getLogger(__name__)

# Where an allowed model ID that no class claims is listed.
DEFAULT_TASK = "text-generation"
# Loads of one model a request tries, moving to the next backend each time one fails to start.
MAX_BACKEND_TRIES = 8


def _ms_since(started: float) -> float:
    return (time.perf_counter() - started) * 1000


def _parse_inputs(contract: tasks.TaskContract, requests: List[Any], batch: bool) -> List[Any]:
    """Each request validated into the task's input model; in a batch, an error names the input it is about."""
    if not batch:
        return [contract.parse_input(request) for request in requests]
    parsed = []
    for index, request in enumerate(requests):
        try:
            parsed.append(contract.parse_input(request))
        except ValueError as e:
            raise ValueError(f"inputs[{index}]: {e}") from e
    return parsed


def _total(results: List[Any], name: str) -> Optional[int]:
    """The sum of the ``name`` counts over ``results``; None when none of them has one."""
    counts = [count for count in (getattr(result, name, None) for result in results) if count is not None]
    return sum(counts) if counts else None


class ModelManager:
    """Manages model loading, caching, and inference.

    Concurrency model: a model loads at most once, and only one model loads
    at a time (see models/cache.py). Each loaded model runs up to its
    ``max_concurrency`` requests at once (1 for in-process models, the
    sane default for a single accelerator; more for model servers, which
    batch), and is never evicted while a request is using it. Different
    models run concurrently because the blocking torch work happens in
    worker threads.

    Every request becomes one RequestEvent for each of ``sinks`` (see
    models/events.py); every load, eviction and unload is reported to
    ``monitor`` (monitoring.Metrics). By default nothing is kept.
    """

    def __init__(
        self,
        max_models: int = 3,
        max_memory_gb: float = 8.0,
        device: str = "auto",
        monitor: Any = None,
        wait_timeout: float = 120.0,
        sinks: Sequence[Sink] = (),
        batch_wait_s: float = 0.0,
        allowed: Optional[Callable[[str], bool]] = None,
        keep_s: float = 0.0,
        turn_s: float = 0.0,
    ):
        self.monitor = monitor or NullMonitor()
        self.sinks = list(sinks)
        # Whether clients may use a model ID (the allow-list, api/deps.py); None allows any.
        self.allowed = allowed
        self.device = resolve_device(device)
        self.cache = ModelCache(max_models, max_memory_gb, monitor=self.monitor, device=self.device,
                                wait_timeout=wait_timeout, batch_wait_s=batch_wait_s, keep_s=keep_s, turn_s=turn_s)
        logger.info("Using device: %s", self.device)

    @staticmethod
    def _create_model_key(model_id: str, task: str) -> str:
        """Create unique key for model caching"""
        return f"{model_id}:{task}"

    async def _checkout(self, model_id: str, task: str, priority: int = HIGH, **options) -> Tuple[Entry, bool]:
        """(entry, whether it was loaded now); the caller checks it back in. ``priority``: models/events.py."""
        model_key = self._create_model_key(model_id, task)

        def create() -> BaseModel:
            # Raises UnsupportedModel (a ValueError) before anything is evicted or loaded.
            model_class = registry.resolve(model_id, task)
            logger.info("Loading %s for %s with %s", model_id, task, model_class.__name__)
            return model_class(model_id, task, self.device, options)

        def loaded(model: BaseModel, seconds: float) -> None:
            self.monitor.record_event(
                kind="load", model_key=model_key, duration_ms=seconds * 1000, detail=type(model).__name__
            )

        # A model server's backend that fails to start is skipped by the next instance, so
        # each try uses another backend; the last one's error is raised.
        tries = 0
        while True:
            started = time.perf_counter()
            try:
                return await self.cache.checkout(model_key, create, on_load=loaded, priority=priority)
            except (registry.UnsupportedModel, CapacityError):
                # Nothing was loaded.
                raise
            except Exception as e:
                self.monitor.record_event(
                    kind="load_failed", model_key=model_key, duration_ms=_ms_since(started), detail=type(e).__name__
                )
                tries += 1
                if not isinstance(e, TryNextBackend) or tries == MAX_BACKEND_TRIES:
                    raise
                logger.warning("%s", e)

    def check_allowed(self, model_id: str) -> None:
        """Forbidden (403) unless ``allowed`` lets clients use ``model_id``. Every load, run and estimate asked for
        from outside checks it here, so no route needs to; preloading (the operator's own list) does not."""
        if self.allowed is not None and not self.allowed(model_id):
            raise Forbidden("model_id is not in ALLOWED_MODELS")

    async def load_model(self, model_id: str, task: str, pin: bool = False, **options) -> BaseModel:
        """Load a model with caching. ``options`` (torch_dtype, max_memory) reach the model.

        ``pin`` keeps it loaded until it is unloaded explicitly.
        """
        self.check_allowed(model_id)
        return await self._load(model_id, task, pin, **options)

    async def _load(self, model_id: str, task: str, pin: bool = False, **options) -> BaseModel:
        entry, _ = await self._checkout(model_id, task, **options)
        if pin and not entry.pinned:
            self.cache.set_pinned(self._create_model_key(model_id, task), True)
        await self.cache.checkin(entry)
        return entry.model

    def set_pinned(self, model_id: str, task: str, pinned: bool) -> None:
        """Pin (never evict) or unpin a loaded model. NotFound if it is not loaded."""
        try:
            self.cache.set_pinned(self._create_model_key(model_id, task), pinned)
        except KeyError:
            raise NotFound(f"Model {self._create_model_key(model_id, task)} not found in cache") from None

    def parse_model_spec(self, spec: str) -> Tuple[str, str]:
        """``"model_id:task"`` or ``"model_id"`` (the task a model file claims it for, else text-generation)."""
        model_id, sep, task = spec.rpartition(":")
        if sep and task in registry.known_tasks():
            return model_id, task
        return spec, (registry.claimed_tasks(spec) or [DEFAULT_TASK])[0]

    async def preload(self, specs: List[str], pinned: List[str] = ()) -> None:
        """Load ``specs`` and ``pinned`` (which stay loaded) one by one at startup; a failure is logged, not raised."""
        wanted = [(s, False) for s in specs] + [(s, True) for s in pinned]
        for spec, pin in wanted:
            model_id, task = self.parse_model_spec(spec)
            try:
                await self._load(model_id, task, pin=pin)
                logger.info("Preloaded %s for %s%s", model_id, task, " (pinned)" if pin else "")
            except Exception:
                logger.exception("Could not preload %s for %s", model_id, task)

    async def run_inference(
        self, model_id: str, task: str, request: Any, on_text: Optional[Callable[[str], Any]] = None
    ) -> Dict[str, Any]:
        """Run inference on a model, loading it first if needed.

        ``request`` is the task's input model or a dict it validates; the
        result is the task's output model (see models/tasks.py).
        """
        async def one(model: BaseModel, requests: List[Any]) -> List[Any]:
            return [await model.run(requests[0], on_text)]

        # A request not streamed may share a forward pass with others arriving now (models/batching.py).
        results, trace_ids, info = await self._serve(model_id, task, [request], one, batch=False, grouped=on_text is None)
        # The trace of this request (GET /admin/traces/{id}), when tracing is on.
        return {"result": results[0], **info, "trace_id": trace_ids[0]}

    async def run_batch(self, model_id: str, task: str, requests: List[Any]) -> Dict[str, Any]:
        """Run several requests on a model at once, loading it first if needed.

        They hold one of the model's slots together. A model with
        predict_batch() answers them in one call (several to a forward pass);
        others answer them one by one. Results come back in request order.
        """
        if not requests:
            raise ValueError("Give at least one input")

        async def together(model: BaseModel, parsed: List[Any]) -> List[Any]:
            return await model.run_batch(parsed)

        results, trace_ids, info = await self._serve(model_id, task, list(requests), together, batch=True)
        return {"results": results, **info, "trace_ids": trace_ids}

    async def _serve(
        self, model_id: str, task: str, requests: List[Any],
        call: Callable[[BaseModel, List[Any]], Awaitable[List[Any]]], batch: bool, grouped: bool = False,
    ) -> Tuple[List[Any], List[str], Dict[str, Any]]:
        """Validate ``requests``, check the model out, run ``call(model, requests)`` in one slot, and report it.

        The call is one RequestEvent; each input in it has its own trace id,
        so a trace is always one input with its output. Returns the results,
        their trace IDs, and the response fields both routes share. With
        ``grouped``, the one request goes through the model's batcher when it
        has one, sharing a slot and a forward pass with others arriving now.
        """
        # Before anything is recorded: a model outside the allow-list is no request of this model's.
        self.check_allowed(model_id)
        start_time = time.time()
        started = time.perf_counter()
        model_key = self._create_model_key(model_id, task)
        trace_ids = [uuid.uuid4().hex for _ in requests]
        context = request_context.get() or RequestContext()
        cold = False
        backend = None
        try:
            # Reject a bad request before it loads (or evicts) anything.
            contract = tasks.get(task)
            if contract is not None:
                requests = _parse_inputs(contract, requests, batch)
            for attempt in (1, 2):
                entry, loaded = await self._checkout(model_id, task, priority=context.priority)
                cold = cold or loaded
                backend = entry.model.backend
                try:
                    batched = grouped and entry.batcher is not None
                    # Overloaded (503) if it would wait for the model longer than MODEL_WAIT_TIMEOUT.
                    entry.admit(batched)
                    if batched:
                        results = [await entry.batcher.run(requests[0])]
                    else:
                        async with entry.slots:
                            running = time.perf_counter()
                            results = await call(entry.model, requests)
                            entry.note(_ms_since(running))
                    break
                except TryNextBackend as e:
                    # Its server went away before the request reached it: once more, on the backend chosen next.
                    if attempt == 2:
                        raise
                    logger.warning("%s; trying again", e)
                finally:
                    await self.cache.checkin(entry)
                    if not entry.model.alive:
                        # Its server died: drop it, so the next request starts a new one.
                        await self.cache.discard(model_key, entry, "crashed")
        except Exception as e:
            # A request the client got wrong (a ValueError: a bad input, an unknown model) is theirs; a full cache
            # (errors.Unavailable) or anything else is ours.
            self._report(RequestEvent(
                model_id=model_id, task=task, status="client_error" if isinstance(e, ValueError) else "error",
                duration_ms=_ms_since(started), cold=cold, calls=[Call(t, r) for t, r in zip(trace_ids, requests)],
                backend=backend, error=type(e).__name__, error_message=f"{type(e).__name__}: {str(e)[:500]}",
                context=context,
            ))
            raise
        self._report(RequestEvent(
            model_id=model_id, task=task, status="ok", duration_ms=_ms_since(started), cold=cold,
            calls=[Call(t, r, out) for t, r, out in zip(trace_ids, requests, results)], backend=backend,
            prompt_tokens=_total(results, "prompt_tokens"), completion_tokens=_total(results, "completion_tokens"),
            context=context,
        ))
        return results, trace_ids, {
            "model_used": model_key,
            "processing_time": time.time() - start_time,
            # True when this request had to load the model first.
            "cold_start": cold,
            # Which backend of a model server answered; None for other models.
            "backend": backend,
        }

    def _report(self, event: RequestEvent) -> None:
        """Hand ``event`` to every sink; one that fails is logged, and neither the request nor the others notice."""
        for sink in self.sinks:
            try:
                sink(event)
            except Exception:
                logger.exception("A request sink failed: %r", sink)

    async def unload_model(self, model_id: str, task: str) -> None:
        """Unload specific model"""
        model_key = self._create_model_key(model_id, task)
        try:
            await self.cache.remove_model(model_key)
        except KeyError:
            raise NotFound(f"Model {model_key} not found in cache") from None

    def model_state(self, model_id: str, task: str) -> str:
        """"loaded", "loading" or "unloaded"."""
        return self.cache.state(self._create_model_key(model_id, task))

    def parallelism(self, model_id: str, task: str) -> Optional[int]:
        """How many requests the model gets through best at once: a full batch for one that answers several in one
        pass, else as many as it runs at once (its slots); None while it is not loaded."""
        entry = self.cache.entries.get(self._create_model_key(model_id, task))
        if entry is None:
            return None
        return entry.batcher.max_size if entry.batcher is not None else entry.max_concurrency

    def is_loaded(self, model_id: str) -> bool:
        """Whether ``model_id`` is loaded for any task (the task runner starts work on loaded models first)."""
        return any(info.model_id == model_id for info in self.cache.model_info.values())

    def get_cache_status(self) -> Dict[str, Any]:
        """Get current cache status"""
        return self.cache.get_cache_stats()

    def model_queue(self, model_id: str, task: str) -> Dict[str, Any]:
        """How busy a model is: requests waiting and the wait the next one can expect (zeros when not loaded)."""
        entry = self.cache.entries.get(self._create_model_key(model_id, task))
        if entry is None:
            return {"in_use": 0, "queued": 0, "estimated_wait_ms": None}
        return {"in_use": entry.inflight, "queued": entry.queued, "estimated_wait_ms": entry.estimated_wait_ms}

    async def set_limits(self, max_models: Optional[int] = None, max_memory_gb: Optional[float] = None) -> List[str]:
        """Change MAX_MODELS / MAX_MEMORY_GB live; returns the model keys evicted to meet them."""
        return await self.cache.set_limits(max_models, max_memory_gb)

    async def clear_all_models(self) -> None:
        """Clear all models from cache"""
        await self.cache.clear_cache()

    def get_available_models(self, allowed_models: Optional[List[str]] = None) -> Dict[str, List[str]]:
        """Registered model IDs by task.

        With ``allowed_models``, only those are listed, including allowed IDs
        no class lists (filed under the task a class claims them for by
        pattern, else text-generation).
        """
        models_by_task = registry.catalog()
        if allowed_models:
            listed = {m for ids in models_by_task.values() for m in ids}
            for task in models_by_task:
                models_by_task[task] = [m for m in models_by_task[task] if m in allowed_models]
            for model_id in allowed_models:
                if model_id not in listed:
                    task = (registry.claimed_tasks(model_id) or [DEFAULT_TASK])[0]
                    models_by_task.setdefault(task, []).append(model_id)
        return models_by_task

    async def get_catalog(self, allowed_models: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """Every task with its models, as the model browser shows them (GET /models/catalog).

        The models are the listed ones (see ``get_available_models``) with
        their cards, then, without ``allowed_models``, loaded models nobody
        lists. ``any_id`` says whether the task takes IDs beyond those (a
        fallback class serves any Hub ID); ``patterns`` are the ID patterns
        its classes claim. Nothing here downloads or reaches the network.
        """
        on_disk = await asyncio.to_thread(memory.cached_weights)
        by_task = self.get_available_models(allowed_models)
        if not allowed_models:
            for info in self.cache.model_info.values():
                listed = by_task.setdefault(info.task, [])
                if info.model_id not in listed:
                    listed.append(info.model_id)
        cards = registry.cards()
        contracts = tasks.contracts()
        return [
            {
                "task": task,
                "description": contracts[task].description if task in contracts else "",
                "any_id": not allowed_models and registry.accepts_any_id(task),
                "patterns": [] if allowed_models else registry.patterns(task),
                "models": [
                    self._catalog_model(model_id, task, cards.get(task, {}).get(model_id), on_disk)
                    for model_id in ids
                ],
            }
            for task, ids in sorted(by_task.items())
        ]

    def _catalog_model(
        self, model_id: str, task: str, card: Optional[registry.ModelCard], on_disk: Dict[str, int]
    ) -> Dict[str, Any]:
        try:
            entry: Optional[registry.Entry] = registry.entry_for(model_id, task)
        except registry.UnsupportedModel:
            entry = None
        source = entry.cls.weights_source(model_id) if entry is not None else model_id
        disk = (on_disk.get(source) or memory.local_weights(source)) if source else 0
        return {
            "id": model_id,
            "description": card.description if card else "",
            "tags": list(card.tags) if card else [],
            "served_by": entry.cls.__name__ if entry else None,
            "kind": entry.cls.kind if entry else None,
            "generic": bool(entry and entry.fallback),
            "status": self.model_state(model_id, task),
            "on_disk_gb": disk / memory.GB if disk else None,
        }

    async def plan_load(self, model_id: str, task: str, **options) -> Dict[str, Any]:
        """The memory loading ``model_id`` would take and what the cache would do for it now.

        Nothing is loaded or evicted. The estimate may read safetensors
        headers from the Hub. UnsupportedModel if no class serves the pair.
        """
        self.check_allowed(model_id)
        model = registry.resolve(model_id, task)(model_id, task, self.device, options)
        # A model server probes its backends here, to size the one a load would use.
        need = await model.estimate_bytes()
        plan = self.cache.plan(self._create_model_key(model_id, task), need or 0)
        free = memory.free_bytes(self.device)
        return {
            "model_id": model_id,
            "task": task,
            "estimate_gb": None if need is None else need / memory.GB,
            **plan,
            "memory_used_gb": self.cache.used() / memory.GB,
            "max_memory_gb": self.cache.max_memory_gb,
            "free_memory_gb": None if free is None else free / memory.GB,
            "loaded_models": len(self.cache.entries),
            "max_models": self.cache.max_models,
        }

    def get_available_model_types(self) -> List[Dict[str, Any]]:
        """Every registered model class with the IDs and patterns it serves"""
        return registry.describe()
