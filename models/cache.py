"""
Model cache: LRU eviction against a model count and a memory budget.

Each loaded model is an ``Entry`` with its size, counted against
``max_memory_gb``. The size is the model's own estimate, made before loading,
or what loading took when it has none. Room is made *before* a model loads,
so the old and new models never sit in memory together, and only one model
loads at a time, so two loads cannot both decide they fit.

A model is never evicted while a request is using it: requests check an
entry out and back in, and eviction only takes entries nobody holds. Nor is
one evicted the moment it goes idle: it keeps its place for ``keep_s``, since
its client's next request is usually on the way, and swapping two models on
every request costs a load each time. When no model can go, a load waits up
to ``wait_timeout`` seconds from when it was asked for, then fails with
``CapacityError`` (routes answer 503). A pinned entry is never evicted at
all; only an explicit unload removes it.

A waiting load may take a busy model's place when it outranks that model's
work (models/events.py): its priority is higher, or the same and it has waited
``turn_s``. The model is then drained: its requests finish, new ones wait,
and it is evicted once idle. Without draining, the busy model's next request
would always check it out before the waiting load woke up. Loads waiting for
room take turns the same way, by priority, then age, and none holds the load
slot while it waits: so an urgent load that comes later goes first, instead
of waiting behind one that may never get room.

Each entry also keeps a running average of its request times, which the
stats turn into the wait a queued request can expect. A request that would
wait for its model longer than ``wait_timeout`` is refused (``Entry.admit``):
the same rule whether it waits for a slot or for a batch (models/batching.py).
"""

import asyncio
import logging
import math
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Sequence, Tuple

from errors import Unavailable

from . import memory
from .base import BaseModel, ModelInfo, NullMonitor
from .batching import Batcher
from .events import HIGH

logger = logging.getLogger(__name__)


class CapacityError(Unavailable):
    """No room for another model right now: every loaded model is busy.

    ``retry_after`` is a guess, in seconds, at when a slot frees up; the reply's Retry-After says it.
    """

    def __init__(self, message: str, retry_after: float = 5.0):
        super().__init__(message, headers={"Retry-After": str(math.ceil(retry_after))})
        self.retry_after = retry_after


class Overloaded(Unavailable):
    """More is waiting for the model than a new request should wait for; Retry-After says when to try again."""


@dataclass
class Entry:
    model: BaseModel
    info: ModelInfo
    size: int  # bytes counted against the budget
    inflight: int = 0  # requests holding this entry, waiting or running
    slots: asyncio.Semaphore = field(default=None)  # requests running at once
    pinned: bool = False  # never evicted (idle timeout and room-making skip it)
    avg_ms: Optional[float] = None  # running average request time; None until one finishes
    # Seconds a single request waits for others to share a forward pass with; 0 answers each alone.
    batch_wait_s: float = 0.0
    # The longest a request is expected to wait for the model before it is refused (the cache's wait_timeout); None: never.
    max_wait_s: Optional[float] = None
    # Groups single requests for a model that answers several in one call (models/batching.py); None otherwise.
    batcher: Optional[Batcher] = None
    # How urgent its work is: the highest priority among the requests since it was last idle, kept while it is idle.
    priority: int = HIGH
    # When its last request finished (time.monotonic()).
    idle_since: float = 0.0
    # Taken by a load that outranks its work: new requests wait, and it is evicted once idle.
    draining: bool = False

    def __post_init__(self):
        self.slots = asyncio.Semaphore(max(1, self.model.max_concurrency))
        if self.batch_wait_s > 0 and self.model.batches:
            self.batcher = Batcher(self.model, self.slots, self.note, self.batch_wait_s,
                                   max_size=getattr(self.model, "batch_size", 32))

    @property
    def max_concurrency(self) -> int:
        return max(1, self.model.max_concurrency)

    @property
    def queued(self) -> int:
        """Requests waiting for a slot on this model."""
        return max(0, self.inflight - self.max_concurrency)

    @property
    def estimated_wait_ms(self) -> Optional[float]:
        """How long the next request would wait for a slot; None before any request has finished."""
        if self.avg_ms is None:
            return None
        return (self.queued + 1) * self.avg_ms / self.max_concurrency if self.inflight >= self.max_concurrency else 0.0

    def note(self, ms: float) -> None:
        """Fold one request's time into the running average (recent ones weigh more)."""
        self.avg_ms = ms if self.avg_ms is None else 0.8 * self.avg_ms + 0.2 * ms

    def admit(self, batched: bool) -> None:
        """Refuse a request checked out of this entry that would wait longer than ``max_wait_s``: Overloaded (503),
        with a Retry-After. ``batched``: it joins the batcher's queue, which empties a batch at a time; else it waits
        for a slot behind the requests ahead of it. Before a request has finished nothing says how long one takes, so
        nothing is refused."""
        if batched and self.batcher is not None:
            ahead, expected = self.batcher.waiting, self.batcher.expected_wait_s()
        else:
            # Checked out already, so ``queued`` counts it: each max_concurrency of them ahead is a request's time.
            ahead = self.inflight - 1
            expected = None if self.avg_ms is None else self.queued * self.avg_ms / self.max_concurrency / 1000
        if self.max_wait_s and expected is not None and expected > self.max_wait_s:
            raise Overloaded(
                f"{ahead} requests are ahead of this one for the model, about {expected:.0f} s of work",
                headers={"Retry-After": str(max(1, math.ceil(expected - self.max_wait_s)))},
            )


class ModelCache:
    def __init__(
        self,
        max_models: int = 3,
        max_memory_gb: float = 8.0,
        monitor: Any = None,
        device: str = "cpu",
        wait_timeout: float = 120.0,
        batch_wait_s: float = 0.0,
        keep_s: float = 0.0,
        turn_s: float = 0.0,
    ):
        self.max_models = max_models
        # 0 or less: no memory budget, only the model count.
        self.max_memory_gb = max_memory_gb
        self.monitor = monitor or NullMonitor()
        self.device = device
        self.wait_timeout = wait_timeout
        # Seconds an idle model keeps its place before another may take it (0: none).
        self.keep_s = keep_s
        # Seconds a load waits before it may drain a model whose work has the same priority (0: never).
        self.turn_s = turn_s
        # Seconds a single request waits for others to share a forward pass with (models/batching.py).
        self.batch_wait_s = batch_wait_s
        self.entries: "OrderedDict[str, Entry]" = OrderedDict()
        # Loads under way or waiting for room (the key's state() is "loading").
        self.loads_pending = 0
        # Per key: one load at a time for that model.
        self._locks: Dict[str, asyncio.Lock] = {}
        # Across keys: one load (and the room-making before it) at a time.
        self._load_lock = asyncio.Lock()
        # Loads waiting for room -> their rank, (-priority, since): the lowest makes room first.
        self._waiting: Dict[object, Tuple[int, float]] = {}
        # Notified whenever an entry is checked in or removed.
        self._changed = asyncio.Condition()

    # ------------------------------------------------------------- budget
    @property
    def budget(self) -> Optional[int]:
        return int(self.max_memory_gb * memory.GB) if self.max_memory_gb > 0 else None

    def used(self) -> int:
        return sum(e.size for e in self.entries.values())

    def _shortfall(self, need: int, count: int, used: int, free: Optional[int]) -> Optional[str]:
        """Why ``need`` more bytes do not fit beside ``count`` models using ``used`` bytes with ``free`` bytes free, or None."""
        if count >= self.max_models:
            return "max_models"
        if self.budget is not None and used + need > self.budget:
            return "max_memory_gb"
        if need and free is not None and need > free:
            return "free_memory"
        return None

    def _victim(self, skip: Sequence[str] = (), keep: bool = True) -> Optional[str]:
        """The model to evict next: idle, not pinned, least recently used, not in ``skip``; None if there is none.

        With ``keep``, a model idle for less than ``keep_s`` is not one yet, unless it is draining.
        """
        now = time.monotonic()
        for key, entry in self.entries.items():
            if entry.inflight or entry.pinned or key in skip:
                continue
            if not keep or entry.draining or now - entry.idle_since >= self.keep_s:
                return key
        return None

    def _drain(self, priority: int, since: float) -> Optional[Tuple[str, Entry]]:
        """Mark the least recently used model whose work a load of ``priority``, waiting since ``since``, outranks.

        It outranks work of a lower priority at once, and work of the same priority once it has waited ``turn_s``,
        so a model with steady work cannot keep its place for ever. None when it outranks no model's work.
        """
        turn = self.turn_s > 0 and time.monotonic() - since >= self.turn_s
        for key, entry in self.entries.items():
            if entry.pinned or entry.draining:
                continue
            if entry.priority < priority or (entry.priority == priority and turn):
                entry.draining = True
                logger.info("Draining %s for a waiting load", key)
                return key, entry
        return None

    def _next_change(self, since: float, now: float) -> float:
        """Seconds until something changes that nobody notifies: an idle model's ``keep_s`` ends, or the load's turn comes."""
        times = [e.idle_since + self.keep_s - now for e in self.entries.values()
                 if not e.inflight and not e.pinned and not e.draining]
        if self.turn_s > 0:
            times.append(since + self.turn_s - now)
        return min((t for t in times if t > 0), default=math.inf)

    def _retry_after(self, since: Optional[float] = None) -> float:
        """Seconds until a busy model is likely free again, from their request times (5 when unknown); for a load waiting
        since ``since``, no sooner than its turn comes."""
        waits = [e.estimated_wait_ms for e in self.entries.values() if e.inflight and e.estimated_wait_ms]
        after = max(1.0, min(waits) / 1000) if waits else 5.0
        if since is not None and self.turn_s > 0:
            after = max(after, since + self.turn_s - time.monotonic())
        return after

    def lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    # ----------------------------------------------------------- check out
    async def checkout(
        self, key: str, create: Callable[[], BaseModel], on_load: Optional[Callable[..., Any]] = None,
        priority: int = HIGH,
    ) -> Tuple[Entry, bool]:
        """The entry for ``key``, loading it first if needed; (entry, whether it loaded now).

        ``create`` builds the unloaded model; it may raise (an unsupported
        model) before anything is evicted. The caller must ``checkin`` the
        entry when done. ``on_load(model, seconds)`` is told about each load.
        ``priority`` (models/events.py) is how urgent the request is when it
        has to wait for room.
        """
        since = time.monotonic()
        async with self.lock_for(key):
            entry = await self._usable(key)
            if entry is not None:
                self._touch(key, entry)
                entry.priority = priority if entry.inflight == 0 else max(entry.priority, priority)
                entry.inflight += 1
                return entry, False

            model = create()
            self.loads_pending += 1
            try:
                # None: unknown, so count what loading takes instead.
                estimate = await model.estimate_bytes()
                async with self._room(estimate or 0, priority, since):
                    before = memory.device_bytes(self.device)
                    started = time.perf_counter()
                    await model.start()
                    took = time.perf_counter() - started
                    size = estimate if estimate is not None else max(0, memory.device_bytes(self.device) - before)
                    info = model.get_model_info()
                    info.loaded_at = info.last_used = time.time()
                    info.memory_usage = size / memory.GB
                    entry = Entry(model, info, size, inflight=1, batch_wait_s=self.batch_wait_s,
                                  max_wait_s=self.wait_timeout, priority=priority)
                    # Entered under the lock, so the next load counts this one.
                    self.entries[key] = entry
            finally:
                self.loads_pending -= 1
            if on_load is not None:
                on_load(model, took)
            logger.info("Loaded %s (%.2f GB)", key, size / memory.GB)
            return entry, True

    async def _usable(self, key: str) -> Optional[Entry]:
        """The loaded entry for ``key``, or None to load it. A draining one is waited out: it is either evicted (and
        loaded again) or kept, when the load that drained it got room elsewhere or gave up."""
        entry = self.entries.get(key)
        if entry is None or not entry.draining:
            return entry
        async with self._changed:
            await self._changed.wait_for(lambda: key not in self.entries or not self.entries[key].draining)
        return self.entries.get(key)

    async def checkin(self, entry: Entry) -> None:
        entry.inflight -= 1
        entry.info.last_used = time.time()
        if entry.inflight == 0:
            entry.idle_since = time.monotonic()
        await self._notify()

    async def _notify(self) -> None:
        """Wake whoever waits for an entry to change: loads waiting for room, requests for a draining model, unloads."""
        async with self._changed:
            self._changed.notify_all()

    def _touch(self, key: str, entry: Entry) -> None:
        self.entries.move_to_end(key)
        entry.info.last_used = time.time()

    # -------------------------------------------------------------- evict
    @asynccontextmanager
    async def _room(self, need: int, priority: int, since: float) -> AsyncIterator[None]:
        """Hold the load slot (``_load_lock``) with room made for ``need`` bytes, for a load of ``priority`` asked for
        at ``since``; CapacityError when there is none ``wait_timeout`` seconds after ``since``.

        The waiting loads take turns by rank (priority, then age): only the first evicts and drains, and none holds
        the slot while it waits, so a more urgent load that comes later goes ahead of them.
        """
        me = object()
        self._waiting[me] = (-priority, since)
        deadline = since + self.wait_timeout
        drained: Optional[Tuple[str, Entry]] = None
        reason: Optional[str] = "more urgent loads are waiting"
        try:
            while True:
                async with self._load_lock:
                    if min(self._waiting, key=self._waiting.__getitem__) is me:
                        reason, drained = await self._evict(need, priority, since, drained)
                        if reason is None:
                            await self._leave(me, drained)
                            yield
                            return
                now = time.monotonic()
                if now >= deadline:
                    raise CapacityError(
                        f"No room for another model ({reason}) and every loaded model is busy", self._retry_after(since)
                    )
                async with self._changed:
                    try:
                        await asyncio.wait_for(self._changed.wait(), min(deadline - now, self._next_change(since, now)))
                    except asyncio.TimeoutError:
                        pass
        finally:
            await self._leave(me, drained)

    async def _leave(self, me: object, drained: Optional[Tuple[str, Entry]]) -> None:
        """A load stops waiting for room (it has it, or gave up): the next may go, and the model it drained, if still
        loaded, takes requests again."""
        self._waiting.pop(me, None)
        if drained is not None and self.entries.get(drained[0]) is drained[1]:
            drained[1].draining = False
        await self._notify()

    async def _evict(self, need: int, priority: int, since: float, drained: Optional[Tuple[str, Entry]]
                     ) -> Tuple[Optional[str], Optional[Tuple[str, Entry]]]:
        """Evict idle models, least recently used first, until ``need`` bytes fit: (None, the model drained) when they
        do, else (the limit in the way, the model drained) with only busy models left.

        Free memory is system-wide, so evicting a model may not raise it
        (the allocator keeps the pages, or another process holds the
        memory). When an eviction gains nothing, evicting more would not
        help: the load goes ahead instead. When nothing can go, the load
        drains a model whose work it outranks (``priority``, waiting since
        ``since``): one at a time for this load, the next once ``drained`` is
        gone. Another waiting load may have drained one of its own.
        """
        while True:
            free = memory.free_bytes(self.device)
            reason = self._shortfall(need, len(self.entries), self.used(), free)
            if reason is None:
                return None, drained
            idle = self._victim()
            if idle is not None:
                await self._unload(idle, "evict", reason)
                if reason == "free_memory":
                    after = memory.free_bytes(self.device)
                    if after is not None and after <= free:
                        logger.warning(
                            "Evicting %s freed no memory; loading a model of %.2f GB with %.2f GB free",
                            idle, need / memory.GB, after / memory.GB,
                        )
                        return None, drained
                continue
            if all(e.pinned for e in self.entries.values()):
                # Nothing left to evict (none, or only pinned models): the
                # model alone exceeds the budget or free memory. Try anyway;
                # loading reports a real failure.
                logger.warning("Loading a model of %.2f GB that does not fit (%s)", need / memory.GB, reason)
                return None, drained
            if drained is None or self.entries.get(drained[0]) is not drained[1]:
                drained = self._drain(priority, since)
                if drained is not None:
                    continue  # an idle one goes at once
            return reason, drained

    async def _unload(self, key: str, kind: str, detail: str) -> None:
        """Remove ``key`` (which must be idle) and free its model."""
        entry = self.entries.pop(key, None)
        if entry is None:
            return
        try:
            await entry.model.stop()
        except Exception:
            logger.exception("Error while unloading model %s", key)
        self.monitor.record_event(kind=kind, model_key=key, detail=detail)
        logger.info("Unloaded model: %s (%s)", key, detail)
        await self._notify()

    async def _wait_idle(self, key: str) -> None:
        async with self._changed:
            await self._changed.wait_for(lambda: key not in self.entries or self.entries[key].inflight == 0)

    async def remove_model(self, key: str, kind: str = "unload", detail: str = "manual") -> None:
        """Unload ``key`` once its requests have finished. KeyError if it is not loaded."""
        if key not in self.entries:
            raise KeyError(f"Model {key} not found in cache")
        while True:
            await self._wait_idle(key)
            async with self._load_lock:
                entry = self.entries.get(key)
                # A request may have checked it out again while we waited for the lock.
                if entry is None or entry.inflight == 0:
                    await self._unload(key, kind, detail)
                    return

    def set_pinned(self, key: str, pinned: bool) -> None:
        """Keep ``key`` loaded until it is unloaded explicitly (or let it be evicted again). KeyError if not loaded."""
        self.entries[key].pinned = pinned
        self.monitor.record_event(kind="pin" if pinned else "unpin", model_key=key, detail="manual")

    async def discard(self, key: str, entry: Entry, detail: str) -> None:
        """Drop ``entry`` without waiting, e.g. after its model server died."""
        if self.entries.get(key) is entry:
            await self._unload(key, "unload", detail)

    async def set_limits(self, max_models: Optional[int] = None, max_memory_gb: Optional[float] = None) -> List[str]:
        """Change the limits now; returns the models evicted to meet them.

        Lower limits evict idle models at once, least recently used first.
        Busy models stay until their requests finish; the next load makes
        room for itself as usual.
        """
        async with self._load_lock:
            if max_models is not None:
                self.max_models = max_models
            if max_memory_gb is not None:
                self.max_memory_gb = max_memory_gb
            evicted = []
            while self.over_limits():
                idle = self._victim(keep=False)
                if idle is None:
                    break
                reason = "max_models" if len(self.entries) > self.max_models else "max_memory_gb"
                await self._unload(idle, "evict", reason)
                evicted.append(idle)
            return evicted

    def plan(self, key: str, need: int) -> Dict[str, Any]:
        """What loading ``key`` (``need`` bytes) would do now, without doing it.

        Follows ``_evict``: idle models go least recently used first until
        the new one fits. ``outcome`` is "loaded" (nothing to do), "fits",
        "evicts" (``evicts`` lists the keys), "waits" (the rest are busy, so
        it would wait for them) or "too_big" (it does not fit even alone;
        loading is still tried). ``reason`` names the limit in the way.
        """
        if key in self.entries:
            return {"outcome": "loaded", "evicts": [], "reason": None}
        count, used, free = len(self.entries), self.used(), memory.free_bytes(self.device)
        evicts: List[str] = []
        while True:
            reason = self._shortfall(need, count, used, free)
            if reason is None:
                return {"outcome": "evicts" if evicts else "fits", "evicts": evicts, "reason": None}
            victim = self._victim(skip=evicts)
            if victim is None:
                outcome = "too_big" if count == 0 else "waits"
                return {"outcome": outcome, "evicts": evicts, "reason": reason}
            size = self.entries[victim].size
            evicts.append(victim)
            count, used = count - 1, used - size
            if free is not None:
                free += size

    def over_limits(self) -> bool:
        return len(self.entries) > self.max_models or (self.budget is not None and self.used() > self.budget)

    async def clear_cache(self) -> None:
        for key in list(self.entries):
            try:
                await self.remove_model(key, detail="clear")
            except KeyError:
                pass  # evicted or unloaded meanwhile
        logger.info("Cleared model cache")

    async def unload_idle(self, idle_seconds: float) -> None:
        """Unload models nobody has used for ``idle_seconds``."""
        cutoff = time.time() - idle_seconds
        for key, entry in list(self.entries.items()):
            if entry.inflight == 0 and not entry.pinned and (entry.info.last_used or 0) < cutoff:
                async with self._load_lock:
                    if self.entries.get(key) is entry and entry.inflight == 0:
                        await self._unload(key, "unload", "idle")

    # -------------------------------------------------------------- stats
    def state(self, key: str) -> str:
        """"loaded", "loading" (or waiting to), or "unloaded"."""
        if key in self.entries:
            return "loaded"
        lock = self._locks.get(key)
        return "loading" if lock is not None and lock.locked() else "unloaded"

    def get_model(self, key: str) -> Optional[BaseModel]:
        entry = self.entries.get(key)
        return entry.model if entry is not None else None

    @property
    def model_info(self) -> Dict[str, ModelInfo]:
        return {key: e.info for key, e in self.entries.items()}

    @property
    def memory_backend(self) -> str:
        return "cuda" if self.device.startswith("cuda") else self.device if self.device == "mps" else "rss"

    def get_cache_stats(self) -> Dict[str, Any]:
        return {
            "cached_models": list(self.entries),
            "cache_size": len(self.entries),
            "max_models": self.max_models,
            "memory_backend": self.memory_backend,
            # The budgeted sizes of the loaded models, what max_memory_gb limits.
            "memory_used_gb": self.used() / memory.GB,
            "max_memory_gb": self.max_memory_gb,
            # Loads under way or waiting for room; a 503 means they could not get it.
            "loads_pending": self.loads_pending,
            **memory.describe(self.device),
            "model_details": {
                key: {
                    "model_id": e.info.model_id,
                    "task": e.info.task,
                    "device": e.info.device,
                    "torch_dtype": e.info.torch_dtype,
                    "loaded_at": e.info.loaded_at,
                    "last_used": e.info.last_used,
                    "memory_usage": e.info.memory_usage,
                    "in_use": e.inflight,
                    "kind": e.model.kind,
                    "backend": e.model.backend,
                    "pinned": e.pinned,
                    "max_concurrency": e.max_concurrency,
                    # Requests waiting for a slot, and the wait the next one can expect.
                    "queued": e.queued,
                    "avg_request_ms": e.avg_ms,
                    "estimated_wait_ms": e.estimated_wait_ms,
                }
                for key, e in self.entries.items()
            },
        }
