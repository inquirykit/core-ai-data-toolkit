"""
TaskRunner: the one queue for the service's background work.

Each piece of work is a task: a row of the jobs table (jobs/store.py) with its
kind, its priority and, when it needs one, the model it runs on. A kind is
declared once (``Kind``): how many of its tasks run at once, how it runs and
how it is stopped. Jobs (scripts in their own process, jobs/runner.py), eval
runs and judging (evals/runner.py) and Hub downloads (models/hub.py) are
kinds.

Each kind has its own limit, so a training job that takes hours does not hold
up an eval run. When a kind has room it starts its queued task that is most
urgent (``priority``: high, normal, low), then one whose model is loaded
already, then the oldest: so the work for the models in memory finishes
before another model has to be loaded (the model cache, models/cache.py,
decides when one is).

The table is the queue: a task queued when the service stopped is simply
there at the next start. A running one the service stops under is marked
failed (interrupted), unless its kind can go on where it stopped
(``on_interrupt="resume"``: an eval run skips the examples it answered, a
download keeps the files it has): then it goes back in the queue. One left
running by a service that died is resumed too, but at most MAX_RESUMES times,
so a task that brings the service down cannot do it at every start.
"""

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, Optional, Tuple

from models.events import PRIORITIES

from .store import CANCELLED, FAILED, FINISHED, QUEUED, RUNNING, SUCCEEDED, JobNotFound, JobStore

logger = logging.getLogger(__name__)

INTERRUPTED = "Interrupted: the service stopped while it ran"
# Seconds a cancel waits for a running task to end.
STOP_WAIT = 15.0
# Times a resumable task is resumed after the service died while it ran.
MAX_RESUMES = 2


class TaskFailed(Exception):
    """A task's work ended in failure; the message is the task's error."""


class TaskCancelled(Exception):
    """A task's work stopped because it was cancelled."""


@dataclass
class Kind:
    """One kind of task, declared once.

    ``run(task)`` does the work of a claimed task (its row) and returns its
    result; it raises TaskFailed (or anything else) to fail, and
    TaskCancelled when it stopped for a cancel. ``stop(task)`` asks a running
    one to end early. ``concurrency`` is how many run at once.
    ``finished(task)`` hears of each task that ended, as stored; not of those
    cut short by the service stopping.

    ``on_interrupt`` is what a task the service stopped under becomes:
    "fail", or "resume" for a kind that can go on where it stopped; then
    ``resumed(task)`` hears of it, back in the queue.
    """

    name: str
    run: Callable[[Dict[str, Any]], Awaitable[Any]]
    stop: Callable[[Dict[str, Any]], Awaitable[None]]
    concurrency: int = 1
    finished: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
    on_interrupt: str = "fail"
    resumed: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None

    def __post_init__(self):
        self.concurrency = max(1, self.concurrency)


class TaskRunner:
    def __init__(self, store: JobStore, loaded: Optional[Callable[[str], bool]] = None):
        self.store = store
        # Whether a model is loaded now (the model cache); tasks on loaded models go first.
        self.loaded = loaded or (lambda model_id: False)
        self.kinds: Dict[str, Kind] = {}
        # Task id -> (its kind, the asyncio task running it).
        self._running: Dict[str, Tuple[str, asyncio.Task]] = {}
        # Made in start(), since an asyncio event belongs to one event loop.
        self._wake: Optional[asyncio.Event] = None
        self._dispatcher: Optional[asyncio.Task] = None

    def add(self, kind: Kind) -> None:
        """Declare a kind."""
        self.kinds[kind.name] = kind

    # ------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        """Resume or fail the tasks a service that died left running, then start the queued ones as kinds have room."""
        for row in await self.store.with_status(RUNNING):
            await self._interrupted(row, died=True)
        self._wake = asyncio.Event()
        self._wake.set()
        self._dispatcher = asyncio.create_task(self._dispatch(), name="task-dispatcher")

    async def stop(self) -> None:
        """Start no more tasks; running ones are cancelled and marked failed (interrupted)."""
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)
            self._dispatcher = None
        running = [task for _, task in self._running.values()]
        for task in running:
            task.cancel()
        await asyncio.gather(*running, return_exceptions=True)
        self._wake = None

    # -------------------------------------------------------------- queries
    async def get(self, id: str) -> Dict[str, Any]:
        row = await self.store.get(id)
        if row is None:
            raise JobNotFound("Job not found")
        return row

    async def find(self, kind: str, ref: str) -> Optional[Dict[str, Any]]:
        """The newest task of ``kind`` working on ``ref``."""
        return await self.store.latest(kind, ref)

    # ------------------------------------------------------------- commands
    async def submit(self, kind: str, *, id: Optional[str] = None, job: Optional[str] = None,
                     params: Optional[Dict[str, Any]] = None, priority: str = "normal", ref: Optional[str] = None,
                     model_id: Optional[str] = None, schedule_id: Optional[str] = None,
                     webhook_url: Optional[str] = None) -> Dict[str, Any]:
        """Queue a task of ``kind``; it starts when its kind has room and nothing more urgent waits."""
        if kind not in self.kinds:
            raise KeyError(f"No kind of task {kind!r} is declared")
        row = await self.store.insert(id or uuid.uuid4().hex[:16], job, params or {}, schedule_id, webhook_url,
                                      kind=kind, priority=priority, ref=ref, model_id=model_id)
        self._poke()
        return row

    async def cancel(self, id: str) -> Dict[str, Any]:
        """Cancel a queued task, or stop a running one and wait for it to end. A finished one is left as is."""
        row = await self.get(id)
        if row["status"] in FINISHED:
            return row
        await self.store.request_cancel(id)
        if await self.store.finish(id, CANCELLED, only_if=(QUEUED,)):
            await self._finished(id)
            return await self.get(id)
        running = self._running.get(id)
        if running is not None:
            await self.kinds[row["kind"]].stop(row)
            await asyncio.wait({running[1]}, timeout=STOP_WAIT)
        # A task claimed but not started yet sees the cancel as it starts.
        return await self.get(id)

    # ------------------------------------------------------------ execution
    def _poke(self) -> None:
        if self._wake is not None:
            self._wake.set()

    def _busy(self, kind: str) -> int:
        return sum(1 for running_kind, _ in self._running.values() if running_kind == kind)

    def _order(self, row: Dict[str, Any]) -> Tuple[int, bool, float]:
        """Most urgent first, then a task whose model is loaded (or that needs none), then the oldest."""
        ready = row["model_id"] is None or self.loaded(row["model_id"])
        return -PRIORITIES.get(row["priority"], PRIORITIES["normal"]), not ready, row["created"]

    async def _dispatch(self) -> None:
        """Start queued tasks while their kinds have room; woken by a submit and by each task that ends."""
        assert self._wake is not None
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                for kind in self.kinds.values():
                    while self._busy(kind.name) < kind.concurrency:
                        waiting = await self.store.queued(kind.name)
                        if not waiting:
                            break
                        row = min(waiting, key=self._order)
                        if await self.store.claim(row["id"]):
                            self._start(row)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Starting queued tasks failed; trying again shortly")
                await asyncio.sleep(1.0)
                self._poke()

    def _start(self, row: Dict[str, Any]) -> None:
        kind = self.kinds[row["kind"]]
        task = asyncio.create_task(self._execute(kind, row["id"]), name=f"task-{row['id']}")
        self._running[row["id"]] = (kind.name, task)

    async def _execute(self, kind: Kind, id: str) -> None:
        started = time.perf_counter()
        try:
            row = await self.get(id)
            if row["cancel"]:  # cancelled between the claim and now
                raise TaskCancelled()
            result = await kind.run(row)
        except asyncio.CancelledError:
            # The service is stopping.
            await asyncio.shield(self._interrupted(await self.get(id), died=False))
            raise
        except TaskCancelled:
            await self.store.finish(id, CANCELLED)
        except Exception as e:
            # Stopped for a cancel, its work fails as it goes: that is a cancel, not a failure.
            if (await self.get(id))["cancel"]:
                await self.store.finish(id, CANCELLED)
            else:
                if not isinstance(e, TaskFailed):
                    logger.exception("Task %s (%s) failed", id, kind.name)
                error = str(e) if isinstance(e, TaskFailed) else f"{type(e).__name__}: {e}"
                await self.store.finish(id, FAILED, error=error[:2000])
        else:
            await self.store.progress(id, 1.0, "done")
            await self.store.finish(id, SUCCEEDED, result=result)
        finally:
            self._running.pop(id, None)
            self._poke()
        logger.info("Task %s (%s) %s after %.1fs", id, kind.name, (await self.get(id))["status"],
                    time.perf_counter() - started)
        await self._finished(id)

    async def _interrupted(self, row: Dict[str, Any], died: bool) -> None:
        """A running task the service stopped under (or, ``died``, left running when it died): back in the queue if its
        kind resumes, at most MAX_RESUMES times after a death, which is counted; else failed (interrupted)."""
        kind = self.kinds.get(row["kind"])
        resumes = kind is not None and kind.on_interrupt == "resume"
        attempts = row["attempts"] + (1 if died else 0)
        if resumes and attempts <= MAX_RESUMES:
            if await self.store.requeue(row["id"], attempts):
                logger.info("Task %s (%s) is queued again: the service stopped while it ran", row["id"], kind.name)
                if kind.resumed is not None:
                    try:
                        await kind.resumed(await self.get(row["id"]))
                    except Exception:
                        logger.exception("Telling %s that task %s resumes failed", kind.name, row["id"])
            return
        error = INTERRUPTED if not resumes else f"{INTERRUPTED}, and it was resumed {MAX_RESUMES} times already"
        await self.store.finish(row["id"], FAILED, error=error, only_if=(RUNNING,))

    async def _finished(self, id: str) -> None:
        row = await self.store.get(id)
        kind = self.kinds.get(row["kind"]) if row is not None else None
        if kind is None or kind.finished is None:
            return
        try:
            await kind.finished(row)
        except Exception:
            logger.exception("Telling %s that task %s ended failed", kind.name, id)
