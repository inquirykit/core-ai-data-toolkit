"""
RunExecutor: runs a model over a dataset, and judges runs.

A run is a kind of task (jobs/tasks.py): one runs at a time, and the rest wait,
the most urgent first, then one whose model is loaded already, then the
oldest. Each example goes through the model manager like any request (so
the model loads, is cached and evicted as usual), at the task's priority, but
with tracing off: the outputs are the run's items, not traces. Examples go a
page (RUN_PAGE) at a time, as many at once as the model gets through best
(ModelManager.parallelism): a full batch for one that answers several in one
pass (models/batching.py), about nine times cheaper per text for a
classifier; as many as a model server runs at once; one at a time for a model
that answers one at a time, so a live request waits behind one of the run's,
not a page of them. Each still has its own answer, error and timing, and they
are recorded in the dataset's order, a page in one transaction. A request the
service refuses for want of room (errors.Unavailable) is no answer: the run
stops there, failed, and resumes like one.

A run that was interrupted, failed or cancelled resumes where it stopped: the
examples it has items for are skipped. One the service stopped under resumes
by itself (at most twice after the service died, see jobs/tasks.py); after
that, and for a failed or cancelled one, by POST /admin/runs/{id}/resume.
Judging a run is a task too, of the "judge" kind (two at a time; scoring.py
does the work); its API key is kept in memory only, never in the table.
``activity()`` says what is happening to runs (queued, running or being
judged, and how far, or why the judging failed), for the dashboard: from the
runs' rows and their judges' tasks, so it holds across a restart.
"""

import asyncio
import logging
import time
import uuid
from typing import Any, AsyncIterator, Callable, Dict, List, Optional, Tuple

import httpx

from errors import Unavailable
from jobs.store import FAILED, FINISHED
from jobs.tasks import Kind, TaskCancelled, TaskFailed, TaskRunner
from jobs.webhooks import Webhooks
from models.events import NORMAL, PRIORITIES, RequestContext, request_context

from . import scoring
from .scoring import Judge
from .store import EXAMPLE_PAGE, EvalError, EvalStore

logger = logging.getLogger(__name__)

# Examples a run sends in one go, at most: a full batch for a model that answers several in one pass (batch_size 32).
RUN_PAGE = 32


class RunExecutor:
    def __init__(self, store: EvalStore, manager: Any, tasks: TaskRunner, webhooks: Optional[Webhooks] = None):
        self.store = store
        self.manager = manager
        self.tasks = tasks
        tasks.add(Kind("run", run=self._run, stop=self._stop, finished=self._finished, on_interrupt="resume",
                       resumed=self._resumed))
        tasks.add(Kind("judge", run=self._judge_task, stop=self._stop_judging, concurrency=2, finished=self._judged))
        # POSTed to when a run finishes (jobs/webhooks.py), with the run's own webhook_url.
        self.webhooks = webhooks
        # Run id -> the scoring under way for it, which a cancel stops.
        self._judging: Dict[str, asyncio.Task] = {}
        # Judge task id -> its API key, until it starts: a key is never written to the table.
        self._judge_keys: Dict[str, Optional[str]] = {}
        # Run id -> the requests of the page under way, which a cancel stops.
        self._page: Dict[str, List[asyncio.Task]] = {}
        # Makes the HTTP client that reaches this service's own /v1 in-process, for a judge with no base URL (api/app.py);
        # given the judge's priority, which its requests carry (X-Priority).
        self.local_client: Optional[Callable[[str], httpx.AsyncClient]] = None

    async def start(self) -> None:
        """Mark the runs a stopped service left running as interrupted; queued ones keep their place in the queue."""
        self._judging = {}
        for run in await self.store.runs():
            if run["status"] == "running" or (run["status"] == "queued" and not await self._queued(run["id"])):
                await self.store.update_run(run["id"], status="interrupted", finished=time.time())

    async def _queued(self, run_id: str) -> bool:
        """Whether the run has a task waiting or under way."""
        task = await self.tasks.find("run", run_id)
        return task is not None and task["status"] not in FINISHED

    async def submit(self, run_id: str, priority: str = "normal") -> None:
        """Queue a run (marked queued already)."""
        run = await self.store.run(run_id)
        await self.tasks.submit("run", ref=run_id, priority=priority, model_id=run.get("model_id"))

    async def activity(self, runs: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
        """What is happening to each of ``runs`` (rows), for the dashboard: queued or running, with how far it got; else
        being judged, with how far, or why its last judging failed. Runs with none of these are left out."""
        judges = await self.tasks.store.latest_of("judge", [run["id"] for run in runs])
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

    async def _notify(self, run_id: str) -> None:
        """POST the finished run to the webhooks: WEBHOOK_URLS and the run's own URL."""
        if self.webhooks is None:
            return
        try:
            run = await self.store.run(run_id)
        except Exception:
            return
        self.webhooks.send("run.finished", run, extra_urls=[run.get("webhook_url")])

    async def cancel(self, run_id: str) -> None:
        """Cancel the run's judging and the run itself, where queued or running (waiting for them to stop)."""
        for kind in ("judge", "run"):
            task = await self.tasks.find(kind, run_id)
            if task is not None and task["status"] not in FINISHED:
                await self.tasks.cancel(task["id"])

    # --------------------------------------------------------- the run kind
    async def _run(self, task: Dict[str, Any]) -> None:
        """A run's task: the run over its dataset, its requests at the task's priority (models/cache.py)."""
        run_id = task["ref"]
        # The runner's own requests leave no traces; by default they give way to requests someone is waiting on.
        request_context.set(RequestContext(skip=True, priority=PRIORITIES.get(task["priority"], NORMAL)))
        try:
            status = await self._execute(task)
        except asyncio.CancelledError:
            # The service is stopping: the run can be resumed later, and nobody is told it finished.
            await asyncio.shield(self.store.update_run(run_id, status="interrupted", finished=time.time()))
            raise
        except Exception as e:
            if isinstance(e, Unavailable):
                error = f"The service was too busy for the model ({e}); resume the run to go on from here"
                logger.warning("Run %s stopped: %s", run_id, error)
            else:
                error = str(e)
                logger.exception("Run %s failed", run_id)
            await self.store.update_run(run_id, status="failed", error=error[:500], finished=time.time())
            raise TaskFailed(error[:500]) from e
        if status == "cancelled":
            raise TaskCancelled()

    async def _resumed(self, task: Dict[str, Any]) -> None:
        """The service stopped under the run, and its task is queued again: so is the run, which skips what it answered."""
        await self.store.update_run(task["ref"], status="queued", error=None, finished=None)

    async def _stop(self, task: Dict[str, Any]) -> None:
        """Stop the run under way: its page's requests still waiting are dropped (a resume asks them again), and the
        run, seeing the cancel on its task, stops there."""
        for request in self._page.get(task["ref"], []):
            request.cancel()

    async def _finished(self, task: Dict[str, Any]) -> None:
        """A run's task ended. One cancelled before it started leaves the run queued: it is marked cancelled."""
        run_id = task["ref"]
        try:
            run = await self.store.run(run_id)
        except Exception:
            return  # deleted
        if run["status"] == "queued":
            await self.store.update_run(run_id, status="cancelled", finished=time.time())
        await self._notify(run_id)

    async def _execute(self, task: Dict[str, Any]) -> str:
        """Run the task's run over its dataset; how it ended: "done", "cancelled", or "skipped" (no longer queued)."""
        run_id = task["ref"]
        run = await self.store.run(run_id)
        if run["status"] != "queued":
            return "skipped"
        total = (await self.store.dataset(run["dataset_id"]))["examples"]
        # A resumed run keeps its items and goes on from there.
        answered = await self.store.answered(run_id)
        done, failed = len(answered), (run.get("failed") or 0) if answered else 0
        await self.store.update_run(run_id, status="running", started=run.get("started") or time.time(), total=total, done=done, failed=failed)
        cancelled = False
        async for page in self._pages(run["dataset_id"], answered):
            done, failed = await self._answer_page(run_id, run, page, done, failed, total)
            cancelled = (await self.tasks.get(task["id"]))["cancel"]
            if cancelled:
                break
        if cancelled:
            await self.store.update_run(run_id, status="cancelled", finished=time.time())
            return "cancelled"
        await self.store.update_run(run_id, status="done", finished=time.time())
        return "done"

    async def _answer_page(self, run_id: str, run: Dict[str, Any], page: List[Dict[str, Any]], done: int, failed: int,
                           total: int) -> Tuple[int, int]:
        """Send a page of examples at once and record their answers; the run's counts after it.

        A cancel drops the requests still waiting. The service stopping records the answers already in, then stops.
        """
        task = run.get("task") or page[0].get("task") or "text-generation"
        width = self.manager.parallelism(run["model_id"], task)
        # Not loaded yet: one at a time until an answer has loaded it, then as many as the model says it takes at once.
        gate = asyncio.Semaphore(width or 1)
        widened = width is not None

        async def answer(example: Dict[str, Any]) -> Dict[str, Any]:
            nonlocal widened
            async with gate:
                result = await self._answer(run, example)
            width = None if widened else self.manager.parallelism(run["model_id"], task)
            if width is not None:
                widened = True
                for _ in range(width - 1):
                    gate.release()
            return result

        requests = [asyncio.create_task(answer(example)) for example in page]
        self._page[run_id] = requests
        try:
            # Until every one is answered, or one is refused for want of room (errors.Unavailable).
            await asyncio.wait(requests, return_when=asyncio.FIRST_EXCEPTION)
        finally:
            self._page.pop(run_id, None)
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

        Unavailable (no room for the model, or more waiting for it than it gets through) is the service's, not the
        example's: it is raised, and stops the run (see _run).
        """
        # The run's task when it names one, else the example's (a copied trace knows its task), else text.
        task = run.get("task") or example.get("task") or "text-generation"
        request = {**example["input"], **(run.get("params") or {})}
        started = time.perf_counter()
        try:
            output = await self.manager.run_inference(run["model_id"], task, request)
        except (asyncio.CancelledError, Unavailable):
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
            raise EvalError("This run is being judged already")
        if not judge.base_url and self.local_client is None:
            raise EvalError("The judge needs a base URL: this service cannot ask itself here")
        id = uuid.uuid4().hex[:16]
        self._judge_keys[id] = judge.api_key
        params = {"model": judge.model, "base_url": judge.base_url, "rubric": judge.rubric, "against": against,
                  "concurrency": concurrency, "needs_key": judge.api_key is not None}
        # A judge this service runs is a model it loads: on one loaded already, it goes before other judges.
        await self.tasks.submit("judge", id=id, ref=run_id, params=params, priority=priority,
                                model_id=None if judge.base_url else judge.model)

    async def _judge_task(self, task: Dict[str, Any]) -> None:
        """A judge's task: the run's items scored; asking this service's own /v1 at the task's priority."""
        run_id, p = task["ref"], task["params"]
        key = self._judge_keys.pop(task["id"], None)
        if p.get("needs_key") and key is None:
            raise TaskFailed("The judge's API key is kept in memory only and the service restarted: judge the run again")
        judge = Judge(model=p["model"], base_url=p["base_url"], api_key=key, rubric=p["rubric"])
        local = (lambda: self.local_client(task["priority"])) if self.local_client is not None else None
        work = asyncio.create_task(self._judge(task, judge, local), name=f"judge-{run_id}")
        self._judging[run_id] = work
        try:
            error = await work
        except asyncio.CancelledError:
            if asyncio.current_task().cancelling():
                raise  # the service is stopping
            raise TaskCancelled()  # a cancel stopped the scoring (_stop_judging)
        finally:
            self._judging.pop(run_id, None)
        if error:
            raise TaskFailed(error)

    async def _stop_judging(self, task: Dict[str, Any]) -> None:
        work = self._judging.get(task["ref"])
        if work is not None:
            work.cancel()

    async def _judged(self, task: Dict[str, Any]) -> None:
        """A judge's task ended; one cancelled before it started leaves its key behind."""
        self._judge_keys.pop(task["id"], None)

    async def _judge(self, task: Dict[str, Any], judge: Judge, local: Optional[Callable[[], httpx.AsyncClient]]
                     ) -> Optional[str]:
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
                await self.tasks.store.progress(task["id"], done / len(items), "judging")

            rows = await scoring.score_items(items, evaluator, others=others, concurrency=task["params"].get("concurrency") or 4,
                                             retry=scoring.JUDGE_RETRY, progress=progress)
            for row in rows:
                row.update(judge=judge.name, against=against)
            await self.store.replace_scores(run_id, metric, rows)
        except Exception as e:
            logger.exception("Judging run %s failed", run_id)
            return f"{type(e).__name__}: {str(e)[:300]}"
        return None
