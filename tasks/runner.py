"""
TaskRunner: the one queue for the service's background work.

Each piece of work is a task: a row of the tasks table (tasks/store.py) with its
kind, its priority and, when it needs one, the model it runs on. A kind is
declared once (``Kind``): how many of its tasks run at once, how it runs and
how it is stopped. Jobs (scripts in their own process, jobs/runner.py), eval
runs and judging (evals/runner.py), webhook deliveries (tasks/webhooks.py), copies of buckets' files to their remotes
(buckets/copies.py) and workflow runs (workflows/runner.py) are kinds. A finished task is kept until it is
deleted, or for its kind's ``keep_s`` (``prune``).

Each kind has its own limit, so a training job that takes hours does not hold
up an eval run. When a kind has room it starts its queued task that is most
urgent (``priority``: high, normal, low), then one whose model is loaded
already, then the oldest: so the work for the models in memory finishes
before another model has to be loaded (the model cache, models/cache.py,
decides when one is). A task queued for LOADED_FIRST_S counts as one on a
loaded model whatever its model, so a steady stream of work for the models in
memory cannot keep it waiting for ever.

The table is the queue: a task queued when the service stopped is simply
there at the next start. A running one the service stops under is marked
failed (interrupted), unless its kind can go on where it stopped
(``on_interrupt="resume"``: an eval run skips the examples it answered, a
workflow run reads on from the page its
task's state names, which it keeps with ``checkpoint``): then it goes back
in the queue. One left
running by a service that died is resumed too, but at most MAX_RESUMES times,
so a task that brings the service down cannot do it at every start.

A task can be given ``retries``, ``retry_delay_s`` and ``timeout_s`` when it
is submitted (jobs and schedules take them). One that runs longer than
``timeout_s`` is stopped and fails. One that fails, by a timeout or
otherwise, goes back in the queue until it has been retried ``retries``
times: it waits ``retry_delay_s`` before the first retry, twice that before
the second, and so on up to MAX_RETRY_DELAY_S (``not_before`` on its row).
Neither a cancel nor the service stopping under it is retried, nor a failure
its run says retrying cannot fix (``TaskFailed(retry=False)``), and the kind
hears only of the last outcome (``finished``).

A kind whose tasks on one ``ref`` must not overlap (a bucket's copies) says
``one_per_ref``: a task submitted while another of its ref waits in the queue
is that one, and a queued task does not start while another of its ref runs.
It waits in the queue meanwhile, holding none of its kind's places, and runs
once that one ends: so a change made during a run is taken by the task after
it.
"""

import asyncio
import logging
import math
import time
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from pydantic import BaseModel, Field

from db.tables import decode_cursor, encode_cursor
from core.errors import NotFound

from core.priorities import PRIORITIES
from .store import CANCELLED, FAILED, FINISHED, QUEUED, RETRY_DELAY_S, RUNNING, SUCCEEDED, TaskStore
# Also importable from here: what the packages above use of store.py, which they do not import (tests/test_layers.py,
# PUBLIC).
from .store import Status, public  # noqa: F401

logger = logging.getLogger(__name__)

INTERRUPTED = "Interrupted: the service stopped while it ran"
# Seconds a cancel waits for a running task to end.
STOP_WAIT = 15.0
# Times a resumable task is resumed after the service died while it ran.
MAX_RESUMES = 2
# Seconds after which a queued task goes in its turn by age, its model loaded or not.
LOADED_FIRST_S = 600.0
# The most retries a task can be given, and the longest wait before one (the delay doubles at each).
MAX_RETRIES = 10
MAX_RETRY_DELAY_S = 3600.0


class Retries(BaseModel):
    """What happens to a task that fails or runs too long: the fields ``submit`` takes, with the bounds
    ``retry_policy`` holds them to."""

    retries: int = Field(0, ge=0, le=MAX_RETRIES, description="Times a task that fails (a timeout too) is queued again")
    retry_delay_s: float = Field(RETRY_DELAY_S, ge=1, le=MAX_RETRY_DELAY_S, allow_inf_nan=False,
                                 description="Seconds before the first retry; doubled before each next one, up to an hour")
    timeout_s: Optional[float] = Field(None, ge=1, allow_inf_nan=False, description="Seconds a run may take; past them it is stopped and "
                                                               "fails. Unset: no limit")


def retry_policy(retries: Optional[int] = 0, retry_delay_s: Optional[float] = None,
                 timeout_s: Optional[float] = None) -> Dict[str, Any]:
    """The retry and timeout fields of a task, checked: ValueError for any out of range."""
    retries = retries or 0
    retry_delay_s = RETRY_DELAY_S if retry_delay_s is None else float(retry_delay_s)
    if not 0 <= retries <= MAX_RETRIES:
        raise ValueError(f"retries must be 0 to {MAX_RETRIES}")
    if not 1 <= retry_delay_s <= MAX_RETRY_DELAY_S:
        raise ValueError(f"retry_delay_s must be 1 to {MAX_RETRY_DELAY_S:.0f}")
    if timeout_s is not None and not (timeout_s >= 1 and math.isfinite(timeout_s)):
        raise ValueError("timeout_s must be at least 1")
    return {"retries": int(retries), "retry_delay_s": retry_delay_s, "timeout_s": timeout_s}


def retry_delay(row: Dict[str, Any]) -> float:
    """Seconds before a task's next retry: its retry_delay_s, doubled for each retry it has had."""
    return min(row["retry_delay_s"] * 2 ** row["retried"], MAX_RETRY_DELAY_S)


class TaskFailed(Exception):
    """A task's work ended in failure; the message is the task's error. ``retry=False`` when trying again cannot help
    (a webhook's receiver answered that it is gone): the task fails whatever retries it has left."""

    def __init__(self, message: str = "", retry: bool = True):
        super().__init__(message)
        self.retry = retry


class TaskCancelled(Exception):
    """A task's work stopped because it was cancelled."""


@dataclass
class Kind:
    """One kind of task, declared once.

    ``run(task)`` does the work of a claimed task (its row) and returns its
    result; it raises TaskFailed (or anything else) to fail, and
    TaskCancelled when it stopped for a cancel. A stop (a cancel, a timeout)
    cancels the work it awaits through ``TaskRunner.stoppable``; a kind whose
    work is elsewhere (a process, a thread) is asked by its ``stop(task)``
    too. ``concurrency`` is how many run at once.
    ``finished(task)`` hears of each task that ended, as stored, one failed
    because the service stopped under it too; not of one queued again (to
    resume, or for a retry).

    ``on_interrupt`` is what a task the service stopped under becomes:
    "fail", or "resume" for a kind that can go on where it stopped: what its
    run keeps with ``TaskRunner.checkpoint`` is its ``state`` when it starts
    again, as after a retry.

    A task past its ``timeout_s`` is stopped with ``stop`` too, and its run
    cancelled if it has not ended STOP_WAIT seconds later.

    ``keep_s`` is how long a task of the kind is kept once it has ended, for a
    kind that makes a task per event (a webhook delivery); ``prune`` deletes
    it then. None keeps it until it is deleted.

    With ``one_per_ref``, its tasks on one ``ref`` neither wait in the queue
    two at a time nor run two at a time (see the module's docstring).
    """

    name: str
    run: Callable[[Dict[str, Any]], Awaitable[Any]]
    stop: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
    concurrency: int = 1
    finished: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None
    on_interrupt: str = "fail"
    keep_s: Optional[float] = None
    one_per_ref: bool = False

    def __post_init__(self):
        self.concurrency = max(1, self.concurrency)


class Running(NamedTuple):
    """A task running here: its kind, its ref, and the asyncio task running it."""
    kind: str
    ref: Optional[str]
    task: asyncio.Task


class TaskRunner:
    def __init__(self, store: TaskStore, loaded: Optional[Callable[[str], bool]] = None):
        self.store = store
        # Whether a model is loaded now (the model cache); tasks on loaded models go first.
        self.loaded = loaded or (lambda model_id: False)
        self.kinds: Dict[str, Kind] = {}
        # Task id -> what runs it.
        self._running: Dict[str, Running] = {}
        # Held while a task of a one_per_ref kind is looked for and queued, so two submits at once queue one.
        self._submitting = asyncio.Lock()
        # Made in start(), since an asyncio event belongs to one event loop.
        self._wake: Optional[asyncio.Event] = None
        self._dispatcher: Optional[asyncio.Task] = None
        # Running tasks stopped for running past their timeout_s.
        self._timed_out: Set[str] = set()
        # The asyncio tasks whose run has ended, storing how: neither a timeout nor a stop cancels them any more.
        self._ending: Set[asyncio.Task] = set()
        # Task id -> the work its run awaits through stoppable(), which a stop cancels; and the running tasks asked to
        # stop, whose next stoppable work is cancelled before it starts.
        self._stoppable: Dict[str, asyncio.Future] = {}
        self._stopped: Set[str] = set()

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
        """Start no more tasks; running ones are cancelled and marked failed (interrupted), and those whose run has
        ended store how first."""
        if self._dispatcher is not None:
            self._dispatcher.cancel()
            await asyncio.gather(self._dispatcher, return_exceptions=True)
            self._dispatcher = None
        running = [r.task for r in self._running.values()]
        for task in running:
            if task not in self._ending:
                task.cancel()
        # Those whose run ended are waited for too, out of _running or not yet: their kind still hears how they ended
        # (a job's webhook), which a stop must not cut short.
        await asyncio.gather(*{*running, *self._ending}, return_exceptions=True)
        self._wake = None

    # -------------------------------------------------------------- queries
    # What a kind's owner reads and notes of its tasks, rather than reaching into the store, whose other writes
    # (claim, finish, retry) are the runner's alone.
    async def get(self, id: str) -> Dict[str, Any]:
        row = await self.store.get(id)
        if row is None:
            raise NotFound("No such task")
        return row

    async def lookup(self, id: str) -> Optional[Dict[str, Any]]:
        """The task, or None."""
        return await self.store.get(id)

    async def find(self, kind: str, ref: str) -> Optional[Dict[str, Any]]:
        """The newest task of ``kind`` working on ``ref``."""
        return await self.store.latest(kind, ref)

    async def latest_of(self, kind: str, refs: List[str]) -> Dict[str, Dict[str, Any]]:
        """The newest task of ``kind`` working on each of ``refs`` that has one: ref -> task."""
        return await self.store.latest_of(kind, refs)

    async def list(self, limit: int = 50, *, kind: Optional[str] = None, status: Optional[str] = None,
                   statuses: Sequence[str] = (), ref: Optional[str] = None) -> List[Dict[str, Any]]:
        """The newest ``limit`` tasks, every kind or one, of ``status`` (or any of ``statuses``) and ``ref``."""
        return await self.store.list(limit, [status] if status else statuses, kind, ref)

    async def page(self, limit: int, cursor: Optional[str] = None, *, kind: Optional[str] = None,
                   statuses: Sequence[str] = (), ref: Optional[str] = None,
                   jobs: Sequence[str] = ()) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """A page of the tasks, newest first, as ``list`` filters them (and of any of ``jobs``, when given), after the
        page whose ``next`` is ``cursor``; and this page's ``next`` (None on the last)."""
        before = decode_cursor(cursor, "s") if cursor else None
        rows = await self.store.list(limit + 1, statuses, kind, ref, before, jobs)
        return rows[:limit], encode_cursor(rows[limit - 1]["_seq"], "s") if len(rows) > limit else None

    async def progress(self, id: str, fraction: Optional[float], message: str) -> None:
        """Note how far a running task has got: ``fraction`` 0..1 or None, and what it is doing."""
        await self.store.progress(id, fraction, message)

    async def checkpoint(self, id: str, state: Any) -> None:
        """Keep a task's ``state`` (any JSON) on its row, which its run reads again as ``task["state"]`` when it
        resumes or is retried: how far it got, so it goes on from there. Kept as long as the task."""
        await self.store.checkpoint(id, state)

    async def set_pid(self, id: str, pid: int) -> None:
        """Note the process (its group) a running task started."""
        await self.store.set_pid(id, pid)

    async def delete(self, id: str) -> None:
        """Forget a task: the owner says when (a job deleted)."""
        await self.store.delete(id)

    async def delete_of(self, kind: str, ref: str) -> int:
        """Forget the kind's ended tasks of ``ref`` (a workflow's runs, as it is deleted); how many."""
        return await self.store.delete_of(kind, ref)

    def running_on(self, kind: str, ref: str) -> Optional[str]:
        """The id of a task of ``kind`` running here on ``ref``, if one is."""
        return next((id for id, r in self._running.items() if r.kind == kind and r.ref == ref), None)

    async def prune(self) -> int:
        """Delete the tasks that ended longer ago than their kind keeps them (``keep_s``); how many. Hourly, in
        api/app.py's lifespan."""
        now, deleted = time.time(), 0
        for kind in self.kinds.values():
            if kind.keep_s is not None:
                deleted += await self.store.prune(kind.name, now - kind.keep_s)
        return deleted

    # ------------------------------------------------------------- commands
    async def submit(self, kind: str, *, id: Optional[str] = None, job: Optional[str] = None,
                     params: Optional[Dict[str, Any]] = None, priority: str = "normal", ref: Optional[str] = None,
                     model_id: Optional[str] = None, schedule_id: Optional[str] = None,
                     webhook_url: Optional[str] = None, retries: int = 0, retry_delay_s: Optional[float] = None,
                     timeout_s: Optional[float] = None) -> Dict[str, Any]:
        """Queue a task of ``kind``; it starts when its kind has room and nothing more urgent waits. For a
        ``one_per_ref`` kind, the task of ``ref`` queued already, if there is one, rather than another.
        ValueError for retries, retry_delay_s or timeout_s out of range (``retry_policy``)."""
        if kind not in self.kinds:
            raise KeyError(f"No kind of task {kind!r} is declared")
        policy = retry_policy(retries, retry_delay_s, timeout_s)
        if not (self.kinds[kind].one_per_ref and ref is not None):
            return await self._insert(id, job, params, schedule_id, webhook_url, kind, priority, ref, model_id, policy)
        async with self._submitting:
            # The newest of its ref: one that waits in the queue (for a retry too) is the newest.
            latest = await self.store.latest(kind, ref)
            if latest is not None and latest["status"] == QUEUED and not latest["cancel"]:
                return latest
            return await self._insert(id, job, params, schedule_id, webhook_url, kind, priority, ref, model_id, policy)

    async def _insert(self, id, job, params, schedule_id, webhook_url, kind, priority, ref, model_id,
                      policy) -> Dict[str, Any]:
        row = await self.store.insert(id or uuid.uuid4().hex[:16], job, params or {}, schedule_id, webhook_url,
                                      kind=kind, priority=priority, ref=ref, model_id=model_id, **policy)
        self._poke()
        return row

    async def due_now(self, id: str) -> bool:
        """Start a queued task that waits for its retry as soon as its kind has room, not at its ``not_before``
        (something its owner was told has changed, so it may work now); False when it is not queued."""
        if not await self.store.due_now(id):
            return False
        self._poke()
        return True

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
            await self._stop(self.kinds[row["kind"]], row)
            await asyncio.wait({running.task}, timeout=STOP_WAIT)
        # A task claimed but not started yet sees the cancel as it starts.
        return await self.get(id)

    async def stoppable(self, task: Dict[str, Any], work: Awaitable[Any]) -> Any:
        """Await ``work`` for ``task``'s run so that a stop of the task (a cancel, its timeout) cancels it, and
        TaskCancelled is raised: what the run does in its ``finally`` (record what is done, keep a checkpoint) is not
        cancelled. The service stopping cancels it too, and that goes on as a cancel. A task asked to stop before
        ``work`` starts gets TaskCancelled at once."""
        id = task["id"]
        if id in self._stopped:
            if asyncio.iscoroutine(work):
                work.close()
            raise TaskCancelled()
        child = asyncio.ensure_future(work)
        self._stoppable[id] = child
        try:
            return await child
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # the service is stopping
            raise TaskCancelled() from None
        finally:
            if self._stoppable.get(id) is child:
                del self._stoppable[id]

    async def _stop(self, kind: Kind, row: Dict[str, Any]) -> None:
        """Ask a running task to stop: the work it awaits through ``stoppable`` is cancelled, and its kind's ``stop``
        asked."""
        self._stopped.add(row["id"])
        child = self._stoppable.get(row["id"])
        if child is not None:
            child.cancel()
        if kind.stop is not None:
            await kind.stop(row)

    # ------------------------------------------------------------ execution
    def _poke(self) -> None:
        if self._wake is not None:
            self._wake.set()

    def _busy(self, kind: str) -> int:
        return sum(1 for r in self._running.values() if r.kind == kind)

    def _order(self, row: Dict[str, Any]) -> Tuple[int, bool, float]:
        """Most urgent first, then a task whose model is loaded (or that needs none, or that has waited
        LOADED_FIRST_S), then the oldest."""
        ready = (row["model_id"] is None or time.time() - row["created"] >= LOADED_FIRST_S
                 or self.loaded(row["model_id"]))
        return -PRIORITIES.get(row["priority"], PRIORITIES["normal"]), not ready, row["created"]

    async def _dispatch(self) -> None:
        """Start queued tasks while their kinds have room; woken by a submit, by each task that ends, and when the
        first task waiting to be retried comes due."""
        assert self._wake is not None
        due_in: Optional[float] = None
        while True:
            try:
                await asyncio.wait_for(self._wake.wait(), due_in)
            except asyncio.TimeoutError:
                pass
            self._wake.clear()
            due_in = None
            try:
                for kind in self.kinds.values():
                    if self._busy(kind.name) >= kind.concurrency:
                        continue
                    # The kind's queue, read once a pass: as many start from it as the kind has room for.
                    now = time.time()
                    waiting = await self.store.queued(kind.name)
                    later = [row["not_before"] - now for row in waiting if (row["not_before"] or 0) > now]
                    if later:
                        due_in = min(due_in or math.inf, *later)
                    waiting = [row for row in waiting if (row["not_before"] or 0) <= now]
                    while waiting and self._busy(kind.name) < kind.concurrency:
                        if kind.one_per_ref:
                            # Not while another of its ref runs: its end wakes this loop again.
                            busy = {r.ref for r in self._running.values() if r.kind == kind.name}
                            waiting = [row for row in waiting if row["ref"] is None or row["ref"] not in busy]
                            if not waiting:
                                break
                        row = min(waiting, key=self._order)
                        waiting.remove(row)
                        # False when it is no longer queued (cancelled, or claimed): the next one goes.
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
        task.add_done_callback(self._ending.discard)
        self._running[row["id"]] = Running(kind.name, row["ref"], task)

    async def _execute(self, kind: Kind, id: str) -> None:
        started = time.perf_counter()
        this = asyncio.current_task()
        row: Dict[str, Any] = {}  # as claimed; a timeout's error names its timeout_s
        watchdog = None
        try:
            row = await self.get(id)
            if row["cancel"]:  # cancelled between the claim and now
                raise TaskCancelled()
            if row["timeout_s"]:
                watchdog = asyncio.create_task(self._watch(kind, row, this), name=f"timeout-{id}")
            result = await kind.run(row)
        except asyncio.CancelledError:
            if id not in self._timed_out:  # the service is stopping
                await asyncio.shield(self._interrupted(await self.get(id), died=False))
                raise
            # It ran past its timeout and did not stop when asked: the watchdog cancelled it.
            this.uncancel()
            self._ending.add(this)
            ended = await self._fail(id, self._timeout_error(row))
        except Exception as e:
            self._ending.add(this)
            # Stopped for a cancel, its work fails as it goes: that is a cancel, not a failure.
            if (await self.get(id))["cancel"] or (isinstance(e, TaskCancelled) and id not in self._timed_out):
                await self.store.finish(id, CANCELLED)
                ended = True
            elif id in self._timed_out:
                ended = await self._fail(id, self._timeout_error(row))
            elif isinstance(e, TaskFailed):
                ended = await self._fail(id, str(e), retry=e.retry)
            else:
                logger.exception("Task %s (%s) failed", id, kind.name)
                ended = await self._fail(id, f"{type(e).__name__}: {e}")
        else:
            self._ending.add(this)
            await self.store.progress(id, 1.0, "done")
            await self.store.finish(id, SUCCEEDED, result=result)
            ended = True
        finally:
            if watchdog is not None and id not in self._timed_out:
                watchdog.cancel()
            self._timed_out.discard(id)
            # Its retry may be running already, under the same id: its entry, and a stop asked of it, are its own.
            running = self._running.get(id)
            if running is None or running.task is this:
                self._running.pop(id, None)
                self._stopped.discard(id)
            self._poke()
        logger.info("Task %s (%s) %s after %.1fs", id, kind.name, (await self.get(id))["status"],
                    time.perf_counter() - started)
        if ended:
            await self._finished(id)

    async def _fail(self, id: str, error: str, retry: bool = True) -> bool:
        """Fail a task, or queue it again when it has retries left (and ``retry``); whether it ended. One cancelled
        since its run failed is not retried: it ends cancelled."""
        error = error[:2000]
        row = await self.get(id)
        if retry and row["retried"] < row["retries"]:
            delay, retry = retry_delay(row), row["retried"] + 1
            message = f"failed; retry {retry} of {row['retries']} is queued"
            if await self.store.retry(id, retry, time.time() + delay, error, message):
                logger.info("Task %s (%s) failed; retry %d of %d in %.0fs", id, row["kind"], retry, row["retries"], delay)
                return False
            if (await self.get(id))["cancel"]:
                await self.store.finish(id, CANCELLED)
                return True
        await self.store.finish(id, FAILED, error=error)
        return True

    @staticmethod
    def _timeout_error(row: Dict[str, Any]) -> str:
        return f"Timed out: still running after its timeout_s of {row['timeout_s']:g} seconds"

    async def _watch(self, kind: Kind, row: Dict[str, Any], running: asyncio.Task) -> None:
        """Stop a task once it has run for its timeout_s; cancel it if it has not ended STOP_WAIT seconds later."""
        await asyncio.sleep(row["timeout_s"])
        self._timed_out.add(row["id"])
        logger.warning("Task %s (%s) ran past its timeout of %gs; stopping it", row["id"], kind.name, row["timeout_s"])
        try:
            await self._stop(kind, row)
        except Exception:
            logger.exception("Stopping task %s (%s) after its timeout failed", row["id"], kind.name)
        await asyncio.wait({running}, timeout=STOP_WAIT)
        if not running.done() and running not in self._ending:
            running.cancel()

    async def _interrupted(self, row: Dict[str, Any], died: bool) -> None:
        """A running task the service stopped under (or, ``died``, left running when it died): back in the queue if its
        kind resumes, at most MAX_RESUMES times after a death, which is counted; else failed (interrupted), which the
        kind hears of (``finished``)."""
        kind = self.kinds.get(row["kind"])
        resumes = kind is not None and kind.on_interrupt == "resume"
        attempts = row["attempts"] + (1 if died else 0)
        if resumes and attempts <= MAX_RESUMES:
            if await self.store.requeue(row["id"], attempts):
                logger.info("Task %s (%s) is queued again: the service stopped while it ran", row["id"], kind.name)
            return
        error = INTERRUPTED if not resumes else f"{INTERRUPTED}, and it was resumed {MAX_RESUMES} times already"
        if await self.store.finish(row["id"], FAILED, error=error, only_if=(RUNNING,)):
            await self._finished(row["id"])

    async def _finished(self, id: str) -> None:
        row = await self.store.get(id)
        kind = self.kinds.get(row["kind"]) if row is not None else None
        if kind is None or kind.finished is None:
            return
        try:
            await kind.finished(row)
        except Exception:
            logger.exception("Telling %s that task %s ended failed", kind.name, id)
