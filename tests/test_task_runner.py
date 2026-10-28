"""The task runner (jobs/tasks.py): which queued task of a kind starts next, and what a restart makes of them."""

import asyncio

import pytest

from jobs.store import CANCELLED, FAILED, QUEUED, SUCCEEDED, JobStore
from jobs.tasks import Kind, TaskCancelled, TaskRunner
from models.manager import ModelManager
from tests.fakes import FakeModel, registered
from vectorstore.tables import TableStore


@pytest.fixture
def store(arcade):
    return JobStore(arcade, TableStore(arcade))


class Recorder:
    """A kind whose tasks note their ref as they run; ``hold`` keeps each running until released."""

    def __init__(self, hold: bool = False):
        self.order = []
        self.hold = hold
        self.release = None
        self.stopped = []

    async def run(self, task):
        self.order.append(task["ref"])
        if self.hold:
            await self.release.wait()
            if task["ref"] in self.stopped:
                raise TaskCancelled()
        return {"ref": task["ref"]}

    async def stop(self, task):
        self.stopped.append(task["ref"])
        self.release.set()


async def until(predicate, timeout=5.0):
    for _ in range(int(timeout / 0.01)):
        if await predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("timed out")


def test_a_kind_starts_the_most_urgent_then_a_loaded_model_then_the_oldest(store):
    async def scenario():
        recorder = Recorder()
        tasks = TaskRunner(store, loaded=lambda model_id: model_id == "A")
        tasks.add(Kind("thing", run=recorder.run, stop=recorder.stop))
        # Submitted before the runner starts, oldest first.
        for ref, priority, model in [("old-normal-C", "normal", "C"), ("normal-A", "normal", "A"),
                                     ("low-A", "low", "A"), ("high-C", "high", "C"), ("normal-none", "normal", None)]:
            await tasks.submit("thing", ref=ref, priority=priority, model_id=model)
        await tasks.start()
        await until(lambda: asyncio.sleep(0, len(recorder.order) == 5))
        await tasks.stop()
        return recorder.order

    assert asyncio.run(scenario()) == ["high-C", "normal-A", "normal-none", "old-normal-C", "low-A"]


def test_each_kind_runs_up_to_its_own_limit_whatever_the_others_do(store):
    async def scenario():
        slow, quick = Recorder(hold=True), Recorder(hold=True)
        slow.release, quick.release = asyncio.Event(), asyncio.Event()
        tasks = TaskRunner(store)
        tasks.add(Kind("slow", run=slow.run, stop=slow.stop))
        tasks.add(Kind("quick", run=quick.run, stop=quick.stop, concurrency=2))
        for i in range(3):
            await tasks.submit("slow", ref=f"s{i}")
            await tasks.submit("quick", ref=f"q{i}")
        await tasks.start()
        # One slow one holds its kind; two quick ones run beside it, the third waits for one of them.
        await until(lambda: asyncio.sleep(0, len(slow.order) == 1 and len(quick.order) == 2))
        await asyncio.sleep(0.1)
        running = (list(slow.order), list(quick.order))
        quick.release.set()
        await until(lambda: asyncio.sleep(0, len(quick.order) == 3))
        stuck = list(slow.order)
        await tasks.stop()
        return running, stuck

    (slow_running, quick_running), slow_after = asyncio.run(scenario())
    assert slow_running == ["s0"] and quick_running == ["q0", "q1"]
    assert slow_after == ["s0"], "the quick ones finishing frees no room for a slow one"


class Slow(FakeModel):
    load_delay = 0.02
    predict_delay = 0.005


def owners_case(store, prefer_loaded):
    """5 tasks for model A, 10 for B, 7 for C, submitted interleaved; room for two models, A and B loaded."""
    async def scenario():
        mgr = ModelManager(device="cpu", max_models=2)
        started = []

        async def run(task):
            started.append(task["model_id"])
            return await mgr.run_inference(task["model_id"], "t", task["ref"])

        tasks = TaskRunner(store, loaded=mgr.is_loaded if prefer_loaded else None)
        tasks.add(Kind("infer", run=run, stop=lambda task: asyncio.sleep(0)))
        await mgr.load_model("A", "t")
        await mgr.load_model("B", "t")
        counts = {"A": 5, "B": 10, "C": 7}
        n = 0
        while any(counts.values()):
            for model in "ABC":
                if counts[model]:
                    counts[model] -= 1
                    n += 1
                    await tasks.submit("infer", ref=f"{model}{n}", model_id=model)
        await tasks.start()
        await until(lambda: asyncio.sleep(0, len(started) == 22 and not tasks._running))
        await tasks.stop()
        return started, [model for kind, model in FakeModel.log if kind == "load"]

    FakeModel.log.clear()
    with registered(Slow, task="t", fallback=True):
        return asyncio.run(scenario())


def test_work_for_the_loaded_models_finishes_before_another_model_loads(store):
    started, loads = owners_case(store, prefer_loaded=True)
    assert loads == ["A", "B", "C"]
    # Every A and B task ran before the first C task.
    assert "".join(started) == "AB" * 5 + "B" * 5 + "C" * 7


def test_without_preferring_loaded_models_the_same_work_swaps_models_back_and_forth(store):
    _, loads = owners_case(store, prefer_loaded=False)
    assert len(loads) > 3


def test_a_restart_fails_running_tasks_and_starts_queued_ones(store):
    async def scenario():
        recorder = Recorder()
        tasks = TaskRunner(store)
        tasks.add(Kind("thing", run=recorder.run, stop=recorder.stop))
        left_running = await tasks.submit("thing", ref="was-running")
        await store.claim(left_running["id"])  # as a service that stopped left it
        queued = await tasks.submit("thing", ref="was-queued")
        await tasks.start()
        await until(lambda: asyncio.sleep(0, recorder.order == ["was-queued"]))
        await until(lambda: _status(store, queued["id"], SUCCEEDED))
        await tasks.stop()
        return await tasks.get(left_running["id"])

    interrupted = asyncio.run(scenario())
    assert interrupted["status"] == FAILED and "Interrupted" in interrupted["error"]


def test_cancelling_a_running_task_asks_its_kind_to_stop_and_waits(store):
    async def scenario():
        recorder = Recorder(hold=True)
        recorder.release = asyncio.Event()
        tasks = TaskRunner(store)
        tasks.add(Kind("thing", run=recorder.run, stop=recorder.stop))
        await tasks.start()
        running = await tasks.submit("thing", ref="long")
        waiting = await tasks.submit("thing", ref="next")
        await until(lambda: asyncio.sleep(0, recorder.order == ["long"]))
        assert (await tasks.get(waiting["id"]))["status"] == QUEUED  # one at a time
        cancelled = await tasks.cancel(running["id"])
        await until(lambda: _status(store, waiting["id"], SUCCEEDED))
        await tasks.stop()
        return cancelled, recorder

    cancelled, recorder = asyncio.run(scenario())
    assert cancelled["status"] == CANCELLED and recorder.stopped == ["long"]
    assert recorder.order == ["long", "next"]


async def _status(store, id, status):
    return (await store.get(id))["status"] == status


def test_stopping_the_service_queues_resumable_tasks_again_and_fails_the_others(store):
    async def scenario():
        resumable, other = Recorder(hold=True), Recorder(hold=True)
        resumable.release = other.release = asyncio.Event()
        tasks = TaskRunner(store)
        tasks.add(Kind("resumable", run=resumable.run, stop=resumable.stop, on_interrupt="resume"))
        tasks.add(Kind("other", run=other.run, stop=other.stop))
        await tasks.start()
        a = await tasks.submit("resumable", ref="a")
        b = await tasks.submit("other", ref="b")
        await until(lambda: asyncio.sleep(0, resumable.order == ["a"] and other.order == ["b"]))
        await tasks.stop()
        return await tasks.get(a["id"]), await tasks.get(b["id"])

    a, b = asyncio.run(scenario())
    # A stop is not the task's doing: it is not counted against it.
    assert a["status"] == QUEUED and a["attempts"] == 0
    assert b["status"] == FAILED and "Interrupted" in b["error"]


def test_a_task_left_running_by_a_service_that_died_resumes_at_most_twice(store):
    async def scenario():
        recorder, resumed = Recorder(), []

        async def heard(task):
            resumed.append((task["ref"], task["attempts"]))

        tasks = TaskRunner(store)
        tasks.add(Kind("thing", run=recorder.run, stop=recorder.stop, on_interrupt="resume", resumed=heard))
        fresh = await tasks.submit("thing", ref="died once")
        tired = await tasks.submit("thing", ref="died thrice")
        for task, attempts in ((fresh, 0), (tired, 2)):
            await store.claim(task["id"])  # as a service that died left them
            if attempts:
                await store.requeue(task["id"], attempts)
                await store.claim(task["id"])
        await tasks.start()
        await until(lambda: _status(store, fresh["id"], SUCCEEDED))
        await tasks.stop()
        return recorder.order, resumed, await tasks.get(fresh["id"]), await tasks.get(tired["id"])

    order, resumed, fresh, tired = asyncio.run(scenario())
    assert order == ["died once"] and resumed == [("died once", 1)]
    assert fresh["attempts"] == 1
    assert tired["status"] == FAILED and "resumed 2 times already" in tired["error"]
