"""
Micro-batching: single requests that reach a model together share one
forward pass.

An in-process model answers one request at a time (one slot), and a
classifier's forward pass over 32 texts costs about a ninth per text of 32
passes over one. So when a model can answer several requests in one call
(``predict_batch``), its single requests wait in one queue, and one worker
answers them: it takes the model's slot, then takes everything waiting (up
to ``max_size``) as one ``run_batch()``. Requests that arrive while the
model is busy go out together in the next batch, so batches grow with the
load. Before taking the slot the worker waits for company only while the
oldest request is younger than ``wait_s`` (``BATCH_WAIT_MS``) and the batch
is not full, which in practice means only when the model was idle. This is
how Triton, TF Serving and TEI form batches; forming the batch on a timer
first and then waiting for the slot lines up batches of one under load.

Each request keeps its own result, its own trace and its own count; a batch
one request spoils is answered again one by one, so only that request fails.
A request whose caller stopped waiting is left out of the next batch. How long
a request joining now would wait (``expected_wait_s``) is what the cache's
entry refuses one by (models/cache.py, ``Entry.admit``), like any request
that waits for the model. ``POST /inference/batch`` is unchanged: its inputs
already share a pass.

Both wait for the model's slots (``Slots``), the requests it runs at once,
most urgent first: a request someone waits on (HIGH) goes before an eval
run's or a bulk client's already waiting, and those go as soon as nothing
more urgent does. The batcher takes the most urgent waiting requests first
too, and asks for the slot at the most urgent priority among them, moving up
when a more urgent one joins while it waits. A request is refused only for
the wait of the requests that go before it (``expected_wait_s`` of its
priority).
"""

import asyncio
import heapq
import itertools
import logging
import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, NamedTuple, Optional, Tuple

from core.errors import Unavailable
from core.priorities import HIGH, LOW

logger = logging.getLogger(__name__)


class Slots:
    """A model's slots: a semaphore whose waiters go most urgent first (core/priorities.py: HIGH, NORMAL, LOW), then in the
    order they asked. While requests someone waits on keep coming, less urgent ones wait; that is what they
    are for."""

    def __init__(self, size: int):
        self.size = max(1, size)
        self._free = self.size
        # (-priority, order asked, the future set when the slot is handed over)
        self._waiters: List[Tuple[int, int, "asyncio.Future[None]"]] = []
        self._asked = itertools.count()

    def take(self, priority: int = HIGH) -> "Ask":
        """One slot, held for an ``async with`` block, waited for behind the more urgent and the earlier requests."""
        return Ask(self, priority)

    @property
    def full(self) -> bool:
        """Whether every slot is held: a request asking now waits."""
        return self._free == 0

    def ahead(self, priority: int) -> int:
        """The requests waiting for a slot that go before one asking at ``priority`` now: those as urgent or more."""
        return len({id(handed) for p, _, handed in self._waiters if -p >= priority and not handed.done()})

    def _wait(self, priority: int, handed: "asyncio.Future[None]") -> None:
        heapq.heappush(self._waiters, (-priority, next(self._asked), handed))

    def _release(self) -> None:
        while self._waiters:
            handed = heapq.heappop(self._waiters)[2]
            if not handed.done():  # not one that stopped waiting
                handed.set_result(None)
                return
        self._free += 1


class Ask:
    """A wait for one of a model's slots (``Slots.take``), then the slot held for an ``async with`` block. ``hurry``
    makes the wait more urgent while it lasts: the batcher's, as a more urgent request joins those it waits for."""

    def __init__(self, slots: Slots, priority: int):
        self.slots, self.priority = slots, priority
        self._handed: "Optional[asyncio.Future[None]]" = None

    def hurry(self, priority: int) -> None:
        """Wait from now on as a request of ``priority`` would, if that is more urgent: it is asked for again at that
        priority, and whichever of its places in the line comes first is handed the slot."""
        if priority > self.priority:
            self.priority = priority
            if self._handed is not None and not self._handed.done():
                self.slots._wait(priority, self._handed)

    async def __aenter__(self) -> None:
        slots = self.slots
        # A free slot means nobody waits: a slot that frees goes straight to the next waiter.
        if slots._free > 0:
            slots._free -= 1
            return
        self._handed = handed = asyncio.get_running_loop().create_future()
        slots._wait(self.priority, handed)
        try:
            await handed
        except asyncio.CancelledError:
            if handed.done() and not handed.cancelled():
                slots._release()  # handed the slot just as it stopped waiting: the next one gets it
            raise

    async def __aexit__(self, *_: Any) -> None:
        self.slots._release()


class Waiting(NamedTuple):
    request: Any
    future: "asyncio.Future[Any]"
    arrived: float  # time.monotonic()
    priority: int


class Batcher:
    """The requests waiting on one loaded model, answered a batch at a time by one worker task."""

    def __init__(self, model: Any, slots: Slots, note: Callable[[float], None], wait_s: float,
                 max_size: int = 32):
        self.model = model
        self.slots = slots
        self.note = note
        self.wait_s = wait_s
        self.max_size = max(1, max_size)
        # The requests waiting, by priority, each in the order they came.
        self._pending: Dict[int, Deque[Waiting]] = {}
        self._worker: Optional["asyncio.Task[None]"] = None
        # The worker's wait for a slot, while it waits: a more urgent request joining hurries it.
        self._asking: Optional[Ask] = None
        # Set when a full batch is waiting, so a worker waiting for company stops waiting.
        self._full = asyncio.Event()
        # Running average of a batch's time in ms, for the expected wait; None before the first batch.
        self.avg_ms: Optional[float] = None
        # How many batches went out, and of how many requests: what /models/status could show.
        self.batches = 0
        self.requests = 0

    @property
    def waiting(self) -> int:
        """Requests waiting for a batch."""
        return sum(len(waiting) for waiting in self._pending.values())

    @property
    def asking(self) -> bool:
        """Whether its worker waits for one of the model's slots now."""
        return self._asking is not None

    def waiting_at(self, priority: int) -> int:
        """Requests waiting that go before one of ``priority`` joining now: those as urgent or more."""
        return sum(len(waiting) for p, waiting in self._pending.items() if p >= priority)

    def expected_wait_s(self, priority: int = LOW) -> Optional[float]:
        """How long a request of ``priority`` joining now would wait: the batches ahead of it (of the requests as
        urgent or more), at the average batch time."""
        if self.avg_ms is None:
            return None
        return (self.waiting_at(priority) // self.max_size + 1) * self.avg_ms / 1000

    async def run(self, request: Any, priority: int = HIGH) -> Any:
        """The result of ``request``, answered in the next batch the model runs that has room for it after the
        more urgent requests."""
        future: "asyncio.Future[Any]" = asyncio.get_running_loop().create_future()
        self._pending.setdefault(priority, deque()).append(Waiting(request, future, time.monotonic(), priority))
        if self._asking is not None:
            self._asking.hurry(priority)
        if self.waiting >= self.max_size:
            self._full.set()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())
        return await future

    async def _work(self) -> None:
        """Answer what is waiting, a batch at a time, until nothing is."""
        batch: List[Waiting] = []
        try:
            while self.waiting:
                await self._linger()
                self._asking = self.slots.take(self._most_urgent())
                async with self._asking:
                    self._asking = None
                    batch = self._take()
                    if not batch:
                        continue
                    self.batches += 1
                    self.requests += len(batch)
                    started = time.perf_counter()
                    try:
                        results = await self.model.run_batch([w.request for w in batch])
                        failed = None
                    except Exception as e:
                        failed = e
                    else:
                        ms = (time.perf_counter() - started) * 1000
                        self.note(ms)
                        self.avg_ms = ms if self.avg_ms is None else 0.8 * self.avg_ms + 0.2 * ms
                if failed is None:
                    for waiting, result in zip(batch, results):
                        _settle(waiting.future, result)
                else:
                    await self._one_by_one(batch, failed)
                batch = []
        except BaseException:
            # Stopped by something other than an empty queue (the service stopping): nobody is left waiting.
            self._asking = None
            for waiting in [*batch, *(w for queue in self._pending.values() for w in queue)]:
                _settle(waiting.future, error=Unavailable("The model stopped answering"))
            self._pending.clear()
            raise

    async def _linger(self) -> None:
        """Wait for company until the oldest request has waited ``wait_s``, or a full batch is waiting."""
        if self.waiting >= self.max_size:
            return
        oldest = min(queue[0].arrived for queue in self._pending.values() if queue)
        left = oldest + self.wait_s - time.monotonic()
        if left <= 0:
            return
        self._full.clear()
        try:
            await asyncio.wait_for(self._full.wait(), left)
        except asyncio.TimeoutError:
            pass

    def _most_urgent(self) -> int:
        """The highest priority among the requests waiting."""
        return max((priority for priority, queue in self._pending.items() if queue), default=LOW)

    def _take(self) -> List[Waiting]:
        """Up to ``max_size`` requests, the most urgent first and each priority's oldest first, leaving out those
        whose caller stopped waiting."""
        batch: List[Waiting] = []
        for priority in sorted(self._pending, reverse=True):
            queue = self._pending[priority]
            while queue and len(batch) < self.max_size:
                waiting = queue.popleft()
                if not waiting.future.done():
                    batch.append(waiting)
        return batch

    async def _one_by_one(self, batch: List[Waiting], error: Exception) -> None:
        """A failed batch: one of them may be the one at fault, so each on its own, and the others still get answers."""
        if len(batch) == 1:
            _settle(batch[0].future, error=error)
            return
        logger.info("A batch of %d failed (%s: %s); answering them one by one", len(batch), type(error).__name__, error)
        for waiting in batch:
            if waiting.future.done():
                continue
            try:
                async with self.slots.take(waiting.priority):
                    result = await self.model.run(waiting.request)
            except Exception as one:
                _settle(waiting.future, error=one)
            else:
                _settle(waiting.future, result)


def _settle(future: "asyncio.Future[Any]", result: Any = None, error: Optional[BaseException] = None) -> None:
    """Give the request its answer, unless it stopped waiting (the client left)."""
    if future.done():
        return
    if error is not None:
        future.set_exception(error)
    else:
        future.set_result(result)
