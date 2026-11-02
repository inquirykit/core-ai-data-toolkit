"""The task runner (tasks/runner.py): which queued task of a kind starts next, what a restart makes of them, and
retries and timeouts."""

import asyncio
import time

import pytest

from tasks import runner as tasks_module
from tasks.store import CANCELLED, FAILED, QUEUED, SUCCEEDED, TaskStore
from tasks.runner import Kind, TaskCancelled, TaskFailed, TaskRunner
from models.manager import ModelManager
from tests.fakes import FakeModel, registered
from tests.waits import until_async
from db.tables import TableStore


@pytest.fixture
def store(arcade):
    return TaskStore(arcade, TableStore(arcade))


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
        await until_async(lambda: asyncio.sleep(0, len(recorder.order) == 5))
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
        await until_async(lambda: asyncio.sleep(0, len(slow.order) == 1 and len(quick.order) == 2))
        await asyncio.sleep(0.1)
        running = (list(slow.order), list(quick.order))
        quick.release.set()
        await until_async(lambda: asyncio.sleep(0, len(quick.order) == 3))
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
        await until_async(lambda: asyncio.sleep(0, len(started) == 22 and not tasks._running))
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
        await until_async(lambda: asyncio.sleep(0, recorder.order == ["was-queued"]))
        await until_async(lambda: _status(store, queued["id"], SUCCEEDED))
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
        await until_async(lambda: asyncio.sleep(0, recorder.order == ["long"]))
        assert (await tasks.get(waiting["id"]))["status"] == QUEUED  # one at a time
        cancelled = await tasks.cancel(running["id"])
        await until_async(lambda: _status(store, waiting["id"], SUCCEEDED))
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
        await until_async(lambda: asyncio.sleep(0, resumable.order == ["a"] and other.order == ["b"]))
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
        await until_async(lambda: _status(store, fresh["id"], SUCCEEDED))
        await tasks.stop()
        return recorder.order, resumed, await tasks.get(fresh["id"]), await tasks.get(tired["id"])

    order, resumed, fresh, tired = asyncio.run(scenario())
    assert order == ["died once"] and resumed == [("died once", 1)]
    assert fresh["attempts"] == 1
    assert tired["status"] == FAILED and "resumed 2 times already" in tired["error"]


class Flaky:
    """A kind whose tasks fail ``failures`` times, then succeed; it notes when each run starts and what ended."""

    def __init__(self, failures: int):
        self.failures = failures
        self.starts = []
        self.ended = []

    async def run(self, task):
        self.starts.append(time.monotonic())
        if len(self.starts) <= self.failures:
            raise TaskFailed(f"failure {len(self.starts)}")
        return {"runs": len(self.starts)}

    async def stop(self, task):
        pass

    async def finished(self, task):
        self.ended.append(task["status"])


def test_a_failed_task_is_retried_after_a_delay_that_doubles_and_the_kind_hears_only_the_end(store, monkeypatch):
    delays = []

    def short(row):
        delays.append(tasks_module.retry_delay.__wrapped__(row))
        return 0.3

    short.__wrapped__ = tasks_module.retry_delay
    monkeypatch.setattr(tasks_module, "retry_delay", short)

    async def scenario():
        flaky = Flaky(failures=2)
        tasks = TaskRunner(store)
        tasks.add(Kind("thing", run=flaky.run, stop=flaky.stop, finished=flaky.finished))
        await tasks.start()
        task = await tasks.submit("thing", ref="x", retries=3, retry_delay_s=10)
        await until_async(lambda: asyncio.sleep(0, len(flaky.starts) == 1))
        await until_async(lambda: _status(store, task["id"], QUEUED))
        waiting = await tasks.get(task["id"])
        await until_async(lambda: _status(store, task["id"], SUCCEEDED))
        await tasks.stop()
        return flaky, waiting, await tasks.get(task["id"])

    flaky, waiting, done = asyncio.run(scenario())
    # Queued again with the error kept, until its not_before.
    assert waiting["retried"] == 1 and waiting["error"] == "failure 1" and waiting["not_before"] > time.time() - 5
    assert "retry 1 of 3" in waiting["message"]
    # 10 s before the first retry, 20 s before the second (shortened here to 0.3 s, which each retry waited).
    assert delays == [10, 20]
    assert all(b - a >= 0.29 for a, b in zip(flaky.starts, flaky.starts[1:]))
    assert done["status"] == SUCCEEDED and done["retried"] == 2 and done["error"] is None
    assert flaky.ended == ["succeeded"]


def test_retries_run_out_and_the_delay_never_passes_an_hour(store, monkeypatch):
    monkeypatch.setattr(tasks_module, "retry_delay", lambda row: 0.01)

    async def scenario():
        flaky = Flaky(failures=5)
        tasks = TaskRunner(store)
        tasks.add(Kind("thing", run=flaky.run, stop=flaky.stop, finished=flaky.finished))
        await tasks.start()
        task = await tasks.submit("thing", ref="x", retries=2)
        await until_async(lambda: _status(store, task["id"], FAILED))
        await tasks.stop()
        return flaky, await tasks.get(task["id"])

    flaky, failed = asyncio.run(scenario())
    assert len(flaky.starts) == 3 and failed["retried"] == 2 and failed["error"] == "failure 3"
    assert flaky.ended == ["failed"]
    monkeypatch.undo()
    assert tasks_module.retry_delay({"retry_delay_s": 30, "retried": 0}) == 30
    assert tasks_module.retry_delay({"retry_delay_s": 30, "retried": 9}) == tasks_module.MAX_RETRY_DELAY_S


@pytest.mark.parametrize("policy", [{"retries": 11}, {"retries": -1}, {"retry_delay_s": 0.5},
                                    {"retry_delay_s": 7200}, {"timeout_s": 0}, {"timeout_s": float("inf")}])
def test_retries_and_timeouts_out_of_range_are_refused(store, policy):
    tasks = TaskRunner(store)
    tasks.add(Kind("thing", run=Recorder().run, stop=Recorder().stop))
    with pytest.raises(ValueError):
        asyncio.run(tasks.submit("thing", ref="x", **policy))


def test_a_task_past_its_timeout_is_stopped_fails_and_is_retried(store, monkeypatch):
    monkeypatch.setattr(tasks_module, "retry_delay", lambda row: 0.3)  # long enough to see it queued

    class SlowOnce:
        """The first run waits until stopped and ends as a stopped kind's run does; the next finishes at once."""

        def __init__(self):
            self.runs, self.stops, self.stopped = 0, 0, asyncio.Event()

        async def run(self, task):
            self.runs += 1
            if self.runs == 1:
                await self.stopped.wait()
                raise TaskCancelled()
            return {}

        async def stop(self, task):
            self.stops += 1
            self.stopped.set()

    async def scenario():
        slow = SlowOnce()
        tasks = TaskRunner(store)
        tasks.add(Kind("thing", run=slow.run, stop=slow.stop))
        await tasks.start()
        task = await tasks.submit("thing", ref="slow", retries=1, timeout_s=1)
        await until_async(lambda: _status(store, task["id"], QUEUED) if slow.stops else asyncio.sleep(0, False))
        waiting = await tasks.get(task["id"])
        await until_async(lambda: _status(store, task["id"], SUCCEEDED))
        await tasks.stop()
        return slow, waiting, await tasks.get(task["id"])

    slow, waiting, done = asyncio.run(scenario())
    # Stopped for its timeout, not for a cancel: a failure, so retried.
    assert "Timed out" in waiting["error"] and "1 seconds" in waiting["error"]
    assert slow.runs == 2 and slow.stops == 1 and done["retried"] == 1


def test_a_task_that_ignores_the_stop_after_its_timeout_is_cancelled(store, monkeypatch):
    monkeypatch.setattr(tasks_module, "STOP_WAIT", 0.1)

    async def forever(task):
        await asyncio.sleep(3600)

    async def ignore(task):
        pass

    async def scenario():
        tasks = TaskRunner(store)
        tasks.add(Kind("stubborn", run=forever, stop=ignore))
        await tasks.start()
        task = await tasks.submit("stubborn", ref="x", timeout_s=1)
        await until_async(lambda: _status(store, task["id"], FAILED))
        running = dict(tasks._running)
        await tasks.stop()
        return running, await tasks.get(task["id"])

    running, failed = asyncio.run(scenario())
    assert "Timed out" in failed["error"] and "1 seconds" in failed["error"] and running == {}


def test_a_cancel_while_a_retry_waits_ends_it_for_good(store):
    async def scenario():
        flaky = Flaky(failures=5)
        tasks = TaskRunner(store)
        tasks.add(Kind("thing", run=flaky.run, stop=flaky.stop, finished=flaky.finished))
        await tasks.start()
        task = await tasks.submit("thing", ref="x", retries=3, retry_delay_s=60)
        await until_async(lambda: _status(store, task["id"], QUEUED) if flaky.starts else asyncio.sleep(0, False))
        cancelled = await tasks.cancel(task["id"])
        await asyncio.sleep(0.1)
        await tasks.stop()
        return flaky, cancelled

    flaky, cancelled = asyncio.run(scenario())
    assert cancelled["status"] == CANCELLED and len(flaky.starts) == 1 and flaky.ended == ["cancelled"]


def test_a_retry_waits_out_its_delay_across_a_restart(store):
    async def scenario():
        flaky = Flaky(failures=0)
        tasks = TaskRunner(store)
        tasks.add(Kind("thing", run=flaky.run, stop=flaky.stop))
        task = await tasks.submit("thing", ref="x", retries=1)
        await store.claim(task["id"])
        await store.retry(task["id"], 1, time.time() + 0.5, "failure 1", "failed; retry 1 of 1 is queued")
        started = time.monotonic()
        await tasks.start()
        await asyncio.sleep(0.2)
        early = list(flaky.starts)
        await until_async(lambda: _status(store, task["id"], SUCCEEDED))
        await tasks.stop()
        return early, flaky.starts[0] - started

    early, waited = asyncio.run(scenario())
    assert early == [] and waited >= 0.45


def test_a_cancel_that_comes_as_a_failed_run_is_queued_for_its_retry_ends_it_cancelled(store):
    async def scenario():
        flaky = Flaky(failures=5)
        tasks = TaskRunner(store)
        tasks.add(Kind("thing", run=flaky.run, stop=flaky.stop, finished=flaky.finished))
        retry, cancelling = store.retry, []

        async def cancel_first(id, *args):
            # The run has failed and the runner decided to retry it; a cancel comes in before the retry is queued.
            cancelling.append(asyncio.create_task(tasks.cancel(id)))
            await until_async(lambda: _cancel_asked(store, id))
            return await retry(id, *args)

        store.retry = cancel_first
        await tasks.start()
        task = await tasks.submit("thing", ref="x", retries=3, retry_delay_s=1)
        await until_async(lambda: asyncio.sleep(0, bool(cancelling)))
        cancelled = await cancelling[0]
        await asyncio.sleep(1.2)  # past the retry's delay: it would have started
        await tasks.stop()
        return flaky, cancelled, await tasks.get(task["id"])

    flaky, cancelled, later = asyncio.run(scenario())
    assert cancelled["status"] == later["status"] == CANCELLED
    assert len(flaky.starts) == 1 and flaky.ended == ["cancelled"]


async def _cancel_asked(store, id):
    return (await store.get(id))["cancel"]


def test_a_task_the_service_stops_or_dies_under_is_not_retried_and_its_kind_hears_it_failed(store):
    async def scenario():
        recorder, ended = Recorder(hold=True), []
        recorder.release = asyncio.Event()

        async def finished(task):
            ended.append((task["ref"], task["status"], task["retried"]))

        def runner():
            tasks = TaskRunner(store)
            tasks.add(Kind("thing", run=recorder.run, stop=recorder.stop, finished=finished))
            return tasks

        tasks = runner()
        await tasks.start()
        await tasks.submit("thing", ref="stopped", retries=3)
        await until_async(lambda: asyncio.sleep(0, recorder.order == ["stopped"]))
        await tasks.stop()
        died = await tasks.submit("thing", ref="died", retries=3)
        await store.claim(died["id"])  # as a service that died left it
        again = runner()
        await again.start()
        await again.stop()
        return ended

    assert asyncio.run(scenario()) == [("stopped", FAILED, 0), ("died", FAILED, 0)]


def test_a_run_that_ends_as_its_timeout_runs_out_is_stored_not_cut_short(store, monkeypatch):
    monkeypatch.setattr(tasks_module, "STOP_WAIT", 0.1)

    async def scenario():
        stopped, finish = asyncio.Event(), store.finish

        async def run(task):
            await stopped.wait()
            raise TaskCancelled()

        async def stop(task):
            stopped.set()

        async def slow_finish(*args, **kwargs):
            await asyncio.sleep(0.3)  # longer than STOP_WAIT: a cancel would land here
            return await finish(*args, **kwargs)

        store.finish = slow_finish
        tasks = TaskRunner(store)
        tasks.add(Kind("thing", run=run, stop=stop))
        await tasks.start()
        task = await tasks.submit("thing", ref="x", timeout_s=1)
        await until_async(lambda: _status(store, task["id"], FAILED))
        await tasks.stop()
        return await tasks.get(task["id"])

    assert "Timed out" in asyncio.run(scenario())["error"]
