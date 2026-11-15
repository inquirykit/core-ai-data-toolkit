"""
RunExecutor: runs a model over a dataset, and judges runs.

A run is a kind of task (tasks/runner.py): one runs at a time, and the rest wait,
the most urgent first, then one whose model is loaded already, then the
oldest. Each example goes through the model manager like any request (so
the model loads, is cached and evicted as usual), at the task's priority, but
with tracing off: the outputs are the run's items, not traces. Examples go a
page (RUN_PAGE) at a time, as many at once as the model gets through best
(ModelManager.width): a full batch for one that answers several in one
pass (models/batching.py), about nine times cheaper per text for a
classifier; as many as a model server runs at once; one at a time for a model
that answers one at a time, so a live request waits behind one of the run's,
not a page of them. Each still has its own answer, error and timing, and they
are recorded in the dataset's order, a page in one transaction. A request the
service refuses for want of room (errors.Unavailable), or for a model no
longer allowed (errors.Forbidden), is no answer: the run stops there, failed,
and resumes like one, as a workflow's Task node does (workflows/nodes.py).

A run's status, when it started and ended, and why it failed are those of its
newest task: the run's row keeps only what it is and its counts. A run that
failed or was cancelled resumes where it stopped: the examples it has items
for are skipped. One the service stopped under resumes by itself (at most
twice after the service died, see tasks/runner.py); after that, and for a
failed or cancelled one, by POST /admin/runs/{id}/resume, a new task.
Judging a run is a task too, of the "judge" kind (two at a time; scoring.py
does the work): the judge is a model ID, asked through this service's own /v1.
``activity()`` says what is happening to runs (queued, running or being
judged, and how far, or why the judging failed), for the dashboard: from the
runs' tasks and their judges', so it holds across a restart.
"""

import asyncio
import logging
import time
import uuid
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

import httpx

from core.errors import Forbidden, Refused, Unavailable
from models import ModelManager
from models.events import RequestContext, request_context
from core.priorities import NORMAL, PRIORITIES
from tasks.runner import FAILED, FINISHED, QUEUED, RUNNING, Kind, TaskCancelled, TaskFailed, TaskRunner
from tasks.webhooks import Webhooks

from . import scoring
from .scoring import Judge
from .store import EXAMPLE_PAGE, EvalStore

logger = logging.getLogger(__name__)


def _as_of(run: Dict[str, Any], task: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The run with ``task``'s status, times and error. One with no task (the service stopped between making it and
    queueing it) is cancelled: it can be resumed."""
    state = {k: task[k] for k in ("status", "started", "finished", "error")} if task else \
        {"status": "cancelled", "started": None, "finished": None, "error": None}
    return {**run, **state}

# Examples a run sends in one go, at most: a full batch for a model that answers several in one pass (batch_size 32).
RUN_PAGE = 32


class RunExecutor:
    def __init__(self, store: EvalStore, manager: ModelManager, tasks: TaskRunner, webhooks: Optional[Webhooks] = None):
        self.store = store
        self.manager = manager
        self.tasks = tasks
        # A cancel stops a page's requests, or the scoring, under way (their work goes through TaskRunner.stoppable).
        tasks.add(Kind("run", run=self._run, finished=self._finished, on_interrupt="resume"))
        tasks.add(Kind("judge", run=self._judge_task, concurrency=2))
        # POSTed to when a run finishes (tasks/webhooks.py), with the run's own webhook_url.
        self.webhooks = webhooks
        # Held while a resume looks at the run's task and queues another, so resumes at once queue one.
        self._resuming = asyncio.Lock()
        # Makes the HTTP client that reaches this service's own /v1 in-process, which the judge is asked through
        # (api/app.py); given the judge's priority, which its requests carry (X-Priority).
        self.local_client: Optional[Callable[[str], httpx.AsyncClient]] = None

    # ------------------------------------------------------------- the runs
    async def run(self, run_id: str) -> Dict[str, Any]:
        """The run, with its newest task's status, times and error."""
        return (await self.with_status([await self.store.run(run_id)]))[0]

    async def runs(self, dataset_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Every run (of one dataset), newest first, each with its newest task's status, times and error."""
        return await self.with_status(await self.store.runs(dataset_id))

    async def page(self, dataset_id: Optional[str], limit: int, cursor: Optional[str]
                   ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """A page of the runs (EvalStore.run_page), each with its newest task's status, times and error."""
        rows, after = await self.store.run_page(dataset_id, limit, cursor)
        return await self.with_status(rows), after

    async def with_status(self, runs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Each run with its newest task's ``status``, ``started``, ``finished`` and ``error``: what the run is now."""
        tasks = await self.tasks.latest_of("run", [run["id"] for run in runs])
        return [_as_of(run, tasks.get(run["id"])) for run in runs]

    async def submit(self, run_id: str, priority: str = "normal") -> None:
        """Queue a run: a new task of it, which goes on from what it answered."""
        run = await self.store.run(run_id)
        await self.tasks.submit("run", ref=run_id, priority=priority, model_id=run.get("model_id"))

    async def resume(self, run_id: str) -> Dict[str, Any]:
        """Queue a failed or cancelled run again, at its priority; Refused otherwise. One resume at a time looks at
        the run's task, so however many come at once, the run has one task waiting or running."""
        async with self._resuming:
            run = await self.run(run_id)
            if run["status"] not in ("failed", "cancelled"):
                raise Refused(f"Only a failed or cancelled run resumes; this one is {run['status']}")
            await self.submit(run_id, await self.priority(run_id))
        return await self.run(run_id)

    async def activity(self, runs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """What is happening to each of ``runs`` (``with_status``), for the dashboard: queued or running, with how far it got; else
        being judged, with how far, or why its last judging failed. Runs with none of these are left out."""
        judges = await self.tasks.latest_of("judge", [run["id"] for run in runs])
        out = {}
        for run in runs:
            judge = judges.get(run["id"])
            if run["status"] in ("queued", "running"):
                out[run["id"]] = {"kind": run["status"], "done": run["done"], "total": run["total"]}
            elif judge is not None and judge["status"] not in FINISHED:
                # Every item of the run is judged; the judge's task says what share it has done.
                total = run["done"]
                out[run["id"]] = {"kind": "judging", "done": round((judge["progress"] or 0) * total), "total": total}
            elif judge is not None and judge["status"] == FAILED:
                out[run["id"]] = {"kind": "judging", "done": 0, "total": 0, "error": judge["error"]}
        return out

    async def priority(self, run_id: str) -> str:
        """The priority the run was last queued with ("normal" if never)."""
        task = await self.tasks.find("run", run_id)
        return task["priority"] if task is not None else "normal"

    async def cancel(self, run_id: str) -> None:
        """Cancel the run's judging and the run itself, where queued or running (waiting for them to stop)."""
        for kind in ("judge", "run"):
            for task in await self.tasks.list(100, kind=kind, ref=run_id, statuses=(QUEUED, RUNNING)):
                await self.tasks.cancel(task["id"])

    async def delete(self, run_id: str) -> None:
        """Delete the run with its items and scores, and its tasks; one queued or under way is cancelled first."""
        await self.cancel(run_id)
        await self.store.delete_run(run_id)
        for kind in ("judge", "run"):
            await self.tasks.delete_of(kind, run_id)

    # --------------------------------------------------------- the run kind
    async def _run(self, task: Dict[str, Any]) -> None:
        """A run's task: the run over its dataset, its requests at the task's priority (models/cache.py)."""
        run_id = task["ref"]
        # The runner's own requests leave no traces; by default they give way to requests someone is waiting on.
        request_context.set(RequestContext(skip=True, priority=PRIORITIES.get(task["priority"], NORMAL), source="eval"))
        try:
            await self._execute(task)
        except (asyncio.CancelledError, TaskCancelled):
            raise  # the service is stopping (the run resumes later), or a cancel stopped it
        except Exception as e:
            if isinstance(e, Unavailable):
                error = f"The service was too busy for the model ({e}); resume the run to go on from here"
                logger.warning("Run %s stopped: %s", run_id, error)
            elif isinstance(e, Forbidden):
                error = f"The model is refused ({e}); allow it again and resume the run to go on from here"
                logger.warning("Run %s stopped: %s", run_id, error)
            else:
                error = str(e)
                logger.exception("Run %s failed", run_id)
            raise TaskFailed(error[:500]) from e

    async def _finished(self, task: Dict[str, Any]) -> None:
        """A run's task ended: the webhooks are told of the run as it ended, and its own URL too."""
        if self.webhooks is None:
            return
        try:
            run = _as_of(await self.store.run(task["ref"]), task)
        except Exception:
            return  # deleted
        await self.webhooks.send("run.finished", run, extra_urls=[run.get("webhook_url")])

    async def _execute(self, task: Dict[str, Any]) -> None:
        """Run the task's run over its dataset; TaskCancelled when a cancel stopped it."""
        run_id = task["ref"]
        run = await self.store.run(run_id)
        total = (await self.store.dataset(run["dataset_id"]))["examples"]
        # A resumed run keeps its items and goes on from there.
        answered = await self.store.answered(run_id)
        done, failed = len(answered), (run.get("failed") or 0) if answered else 0
        await self.store.update_run(run_id, total=total, done=done, failed=failed)
        async for page in self._pages(run["dataset_id"], answered):
            done, failed = await self._answer_page(task, run, page, done, failed, total)

    async def _answer_page(self, task: Dict[str, Any], run: Dict[str, Any], page: List[Dict[str, Any]], done: int,
                           failed: int, total: int) -> Tuple[int, int]:
        """Send a page of examples at once and record their answers; the run's counts after it.

        A cancel drops the requests still waiting (a resume asks them again), records the answers already in, and
        raises TaskCancelled; the service stopping records them too, then stops.
        """
        run_id = task["ref"]
        model_task = run.get("task") or page[0].get("task") or "text-generation"
        # Not loaded yet: one at a time until an answer has loaded it, then as many as the model takes at once.
        width = self.manager.width(run["model_id"], model_task)

        async def answer(example: Dict[str, Any]) -> Dict[str, Any]:
            async with width:
                return await self._answer(run, example)

        requests = [asyncio.create_task(answer(example)) for example in page]
        try:
            # Until every one is answered, or one is refused for want of room or a refused model (Unavailable, Forbidden).
            await self.tasks.stoppable(task, asyncio.wait(requests, return_when=asyncio.FIRST_EXCEPTION))
        finally:
            for request in requests:
                request.cancel()
            done, failed = await asyncio.shield(self._record(run_id, page, requests, done, failed, total))
        refused = next((r.exception() for r in requests if r.done() and not r.cancelled() and r.exception()), None)
        if refused is not None:
            raise refused
        return done, failed

    async def _record(self, run_id: str, page: List[Dict[str, Any]], requests: List[asyncio.Task], done: int,
                      failed: int, total: int) -> Tuple[int, int]:
        """The page's answers stored as items, in the dataset's order; those dropped, refused or still waiting are
        left out."""
        items = []
        for example, request in zip(page, requests):
            if not request.done() or request.cancelled() or request.exception() is not None:
                continue
            answer = request.result()
            items.append({"example_id": example["id"], "output": answer["output"], "status": answer["status"],
                          "duration_ms": answer["ms"], "cold": answer["cold"], "error": answer["error"]})
            failed += answer["status"] == "error"
        await self.store.add_items(run_id, items)
        done += len(items)
        await self.store.update_run(run_id, done=done, failed=failed)
        return done, failed

    async def _answer(self, run: Dict[str, Any], example: Dict[str, Any]) -> Dict[str, Any]:
        """One example sent to the run's model: its output, or its error, and how long it took (its batch's time).

        Unavailable (no room for the model, or more waiting for it than it gets through) and Forbidden (the model
        taken out of ALLOWED_MODELS since) are the run's, not the example's: they are raised, and stop the run (see
        _run), as they stop a workflow's.
        """
        # The run's task when it names one, else the example's (a copied trace knows its task), else text.
        task = run.get("task") or example.get("task") or "text-generation"
        request = {**example["input"], **(run.get("params") or {})}
        started = time.perf_counter()
        try:
            output = await self.manager.run_inference(run["model_id"], task, request)
        except (asyncio.CancelledError, Unavailable, Forbidden):
            raise
        except Exception as e:
            return {"output": None, "status": "error", "ms": (time.perf_counter() - started) * 1000, "cold": False,
                    "error": f"{type(e).__name__}: {str(e)[:400]}"}
        return {"output": output["result"], "status": "ok", "ms": (time.perf_counter() - started) * 1000,
                "cold": output.get("cold_start", False), "error": None}

    async def _pages(self, dataset_id: str, answered: set) -> AsyncIterator[List[Dict[str, Any]]]:
        """The dataset's examples not answered yet, RUN_PAGE at a time."""
        page: List[Dict[str, Any]] = []
        async for example in self._examples_of(dataset_id):
            if example["id"] in answered:
                continue
            page.append(example)
            if len(page) == RUN_PAGE:
                yield page
                page = []
        if page:
            yield page

    async def _examples_of(self, dataset_id: str):
        """The dataset's examples, a page at a time, so a run holds one page in memory."""
        skip = 0
        while True:
            page = await self.store.examples(dataset_id, limit=EXAMPLE_PAGE, skip=skip)
            for example in page:
                yield example
            if len(page) < EXAMPLE_PAGE:
                return
            skip += EXAMPLE_PAGE

    # ----------------------------------------------------------------- judge
    async def judge(self, run_id: str, judge: Judge, against: Optional[str] = None, concurrency: int = 4,
                    priority: str = "normal") -> None:
        """Queue judging ``run_id``: by rubric, or against another run when ``against`` is set."""
        task = await self.tasks.find("judge", run_id)
        if task is not None and task["status"] not in FINISHED:
            raise Refused("This run is being judged already")
        if self.local_client is None:
            raise Refused("The judge is asked through this service's own /v1, which this executor cannot reach")
        params = {"model": judge.model, "rubric": judge.rubric, "against": against, "concurrency": concurrency}
        # The judge is a model the service loads: on one loaded already, it goes before other judges.
        await self.tasks.submit("judge", id=uuid.uuid4().hex[:16], ref=run_id, params=params, priority=priority,
                                model_id=judge.model)

    async def _judge_task(self, task: Dict[str, Any]) -> None:
        """A judge's task: the run's items scored; asking this service's own /v1 at the task's priority."""
        p = task["params"]
        judge = Judge(model=p["model"], rubric=p["rubric"])
        local = lambda: self.local_client(task["priority"])  # noqa: E731
        error = await self.tasks.stoppable(task, self._judge(task, judge, local))
        if error:
            raise TaskFailed(error)

    async def _judge(self, task: Dict[str, Any], judge: Judge, local: Callable[[], httpx.AsyncClient]) -> Optional[str]:
        """Score the task's run's items with the judge and store the scores, the share done kept on the task; the
        error, if it failed."""
        run_id, against = task["ref"], task["params"].get("against")
        try:
            model = scoring.judge_model(judge, local)
            items = await self.store.items(run_id)
            others = {i["example_id"]: i for i in await self.store.items(against)} if against else None
            metric = f"judge_vs_{against}" if against else "judge"
            evaluator = scoring.PairJudge(judge, model) if against else scoring.RubricJudge(judge, model)

            async def progress(done: int) -> None:
                await self.tasks.progress(task["id"], done / len(items), "judging")

            rows = await scoring.score_items(items, evaluator, others=others, concurrency=task["params"].get("concurrency") or 4,
                                             retry=scoring.JUDGE_RETRY, progress=progress)
            for row in rows:
                row.update(judge=judge.name, against=against)
            await self.store.replace_scores(run_id, metric, rows)
        except Exception as e:
            logger.exception("Judging run %s failed", run_id)
            return f"{type(e).__name__}: {str(e)[:300]}"
        return None
