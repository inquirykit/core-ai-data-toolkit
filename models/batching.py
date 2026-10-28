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
"""

import asyncio
import logging
import time
from collections import deque
from typing import Any, Callable, Deque, List, NamedTuple, Optional

from errors import Unavailable

logger = logging.getLogger(__name__)


class Waiting(NamedTuple):
    request: Any
    future: "asyncio.Future[Any]"
    arrived: float  # time.monotonic()


class Batcher:
    """The requests waiting on one loaded model, answered a batch at a time by one worker task."""

    def __init__(self, model: Any, slots: asyncio.Semaphore, note: Callable[[float], None], wait_s: float,
                 max_size: int = 32):
        self.model = model
        self.slots = slots
        self.note = note
        self.wait_s = wait_s
        self.max_size = max(1, max_size)
        self._pending: Deque[Waiting] = deque()
        self._worker: Optional["asyncio.Task[None]"] = None
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
        return len(self._pending)

    def expected_wait_s(self) -> Optional[float]:
        """How long a request joining now would wait: the batches ahead of it, at the average batch time."""
        if self.avg_ms is None:
            return None
        return (len(self._pending) // self.max_size + 1) * self.avg_ms / 1000

    async def run(self, request: Any) -> Any:
        """The result of ``request``, answered in the next batch the model runs."""
        future: "asyncio.Future[Any]" = asyncio.get_running_loop().create_future()
        self._pending.append(Waiting(request, future, time.monotonic()))
        if len(self._pending) >= self.max_size:
            self._full.set()
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._work())
        return await future

    async def _work(self) -> None:
        """Answer what is waiting, a batch at a time, until nothing is."""
        batch: List[Waiting] = []
        try:
            while self._pending:
                await self._linger()
                async with self.slots:
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
            for waiting in [*batch, *self._pending]:
                _settle(waiting.future, error=Unavailable("The model stopped answering"))
            self._pending.clear()
            raise

    async def _linger(self) -> None:
        """Wait for company until the oldest request has waited ``wait_s``, or a full batch is waiting."""
        if len(self._pending) >= self.max_size:
            return
        left = self._pending[0].arrived + self.wait_s - time.monotonic()
        if left <= 0:
            return
        self._full.clear()
        try:
            await asyncio.wait_for(self._full.wait(), left)
        except asyncio.TimeoutError:
            pass

    def _take(self) -> List[Waiting]:
        """Up to ``max_size`` of the oldest requests, leaving out those whose caller stopped waiting."""
        batch: List[Waiting] = []
        while self._pending and len(batch) < self.max_size:
            waiting = self._pending.popleft()
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
                async with self.slots:
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
