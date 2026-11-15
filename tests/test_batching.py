"""Micro-batching (models/batching.py): single requests arriving together share one predict_batch() call."""

import asyncio
import time
from collections import Counter

import pytest

from core.errors import Unavailable
from models.events import RequestContext, request_context
from models.manager import ModelManager
from core.priorities import HIGH, LOW, NORMAL
from tests.fakes import FakeModel, registered
from tests.waits import until_async


class BatchingModel(FakeModel):
    """Answers several requests in one call; a request saying "bad" fails."""

    batch_calls = Counter()

    def predict_batch(self, requests):
        BatchingModel.batch_calls["calls"] += 1
        BatchingModel.batch_calls["requests"] += len(requests)
        if any(r == "bad" for r in requests):
            raise RuntimeError("one of them is bad")
        return [f"{r}!" for r in requests]

    async def predict(self, request):
        self.calls["predict"] += 1
        if request == "bad":
            raise RuntimeError("bad request")
        return f"{request}!"


class SlowBatchingModel(BatchingModel):
    """A pass takes ``pass_s`` (in the worker thread, as a real model's would); records each batch's size."""

    pass_s = 0.2
    sizes = []
    seen = []

    def predict_batch(self, requests):
        SlowBatchingModel.sizes.append(len(requests))
        SlowBatchingModel.seen.append(list(requests))
        time.sleep(self.pass_s)
        return super().predict_batch(requests)


@pytest.fixture(autouse=True)
def fake_registry():
    BatchingModel.batch_calls.clear()
    SlowBatchingModel.sizes = []
    SlowBatchingModel.seen = []
    with registered(BatchingModel, task="t", fallback=True), registered(SlowBatchingModel, task="slow", fallback=True):
        yield


def make_manager(**kw):
    return ModelManager(device="cpu", batch_wait_s=0.02, **kw)


def test_requests_arriving_together_share_one_call():
    async def scenario():
        mgr = make_manager()
        results = await asyncio.gather(*[mgr.run_inference("m", "t", f"r{i}") for i in range(6)])
        assert [r["result"] for r in results] == [f"r{i}!" for i in range(6)]
        assert BatchingModel.batch_calls == {"calls": 1, "requests": 6}
        # Each request got its own trace.
        assert len({r["trace_id"] for r in results}) == 6

    asyncio.run(scenario())


def test_a_request_spoiling_the_batch_fails_alone():
    async def scenario():
        mgr = make_manager()
        outcomes = await asyncio.gather(
            mgr.run_inference("m", "t", "ok"), mgr.run_inference("m", "t", "bad"), mgr.run_inference("m", "t", "fine"),
            return_exceptions=True,
        )
        assert outcomes[0]["result"] == "ok!" and outcomes[2]["result"] == "fine!"
        assert isinstance(outcomes[1], RuntimeError)
        # The batch failed once, then each request ran on its own.
        assert BatchingModel.batch_calls["calls"] == 1
        assert FakeModel.instances[0].predict_calls == 3

    asyncio.run(scenario())


def test_a_lone_request_waits_the_window_at_most_and_a_full_batch_goes_at_once():
    async def scenario():
        mgr = make_manager()
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        assert (await mgr.run_inference("m", "t", "solo"))["result"] == "solo!"
        assert loop.time() - t0 < 0.5
        BatchingModel.batch_calls.clear()
        # The model's batch_size caps a batch; it is read when the model loads, so a fresh manager.
        BatchingModel.batch_size = 2
        fresh = make_manager()
        try:
            await asyncio.gather(*[fresh.run_inference("m", "t", f"r{i}") for i in range(4)])
        finally:
            del BatchingModel.batch_size
        assert BatchingModel.batch_calls["calls"] == 2 and BatchingModel.batch_calls["requests"] == 4

    asyncio.run(scenario())


def test_requests_waiting_for_the_next_batch_are_reported_as_waiting():
    async def scenario():
        mgr = make_manager()
        details = lambda: next(iter(mgr.get_cache_status()["model_details"].values()))
        first = asyncio.create_task(mgr.run_inference("m", "slow", "first"))
        await until_async(lambda: SlowBatchingModel.sizes, what="the first batch to start")
        assert details()["waiting"] == 0
        rest = [asyncio.create_task(mgr.run_inference("m", "slow", f"r{i}")) for i in range(3)]
        await until_async(lambda: details()["waiting"] == 3, what=lambda: f"3 waiting, not {details()['waiting']}")
        await asyncio.gather(first, *rest)
        assert details()["waiting"] == 0 and SlowBatchingModel.sizes == [1, 3]

    asyncio.run(scenario())


def test_the_next_batch_takes_the_most_urgent_requests_first():
    async def scenario():
        SlowBatchingModel.batch_size = 2
        mgr = make_manager()

        async def ask(text, priority):
            request_context.set(RequestContext(priority=priority))
            return (await mgr.run_inference("m", "slow", text))["result"]

        try:
            first = asyncio.create_task(ask("first", HIGH))
            await until_async(lambda: SlowBatchingModel.seen, what="the first batch to start")
            rest = [asyncio.create_task(ask(text, priority))
                    for text, priority in (("low1", LOW), ("low2", LOW), ("normal", NORMAL), ("high", HIGH))]
            assert await asyncio.gather(first, *rest) == ["first!", "low1!", "low2!", "normal!", "high!"]
        finally:
            del SlowBatchingModel.batch_size
        # Waiting together behind the first batch: the urgent ones go next, each priority oldest first.
        assert SlowBatchingModel.seen == [["first"], ["high", "normal"], ["low1", "low2"]]

    asyncio.run(scenario())


def test_models_without_predict_batch_and_the_batch_route_are_untouched():
    async def scenario():
        with registered(FakeModel, task="plain", fallback=True):
            mgr = make_manager()
            await asyncio.gather(*[mgr.run_inference("p", "plain", f"r{i}") for i in range(3)])
            entry = next(iter(mgr.cache.entries.values()))
            assert entry.batcher is None
        # POST /inference/batch's inputs already share a pass: one call, not a batch of a batch.
        BatchingModel.batch_calls.clear()
        mgr = make_manager()
        out = await mgr.run_batch("m", "t", ["a", "b"])
        assert out["results"] == ["a!", "b!"] and BatchingModel.batch_calls == {"calls": 1, "requests": 2}
        # With the wait at 0 (BATCH_WAIT_MS=0) there is no batcher at all.
        mgr = ModelManager(device="cpu", batch_wait_s=0)
        await mgr.run_inference("m", "t", "x")
        assert next(iter(mgr.cache.entries.values())).batcher is None

    asyncio.run(scenario())


def test_requests_arriving_while_the_model_is_busy_go_out_together():
    # Spaced wider than the wait, as steady traffic is: a timer would send each alone and line them up.
    async def scenario():
        mgr = ModelManager(device="cpu", batch_wait_s=0.005)
        first = asyncio.create_task(mgr.run_inference("m", "slow", "r0"))
        await asyncio.sleep(0.03)  # r0's pass is under way
        later = []
        for i in range(1, 5):
            later.append(asyncio.create_task(mgr.run_inference("m", "slow", f"r{i}")))
            await asyncio.sleep(0.015)
        results = await asyncio.gather(first, *later)
        assert [r["result"] for r in results] == [f"r{i}!" for i in range(5)]
        assert SlowBatchingModel.sizes == [1, 4]

    asyncio.run(scenario())


def test_a_request_whose_caller_left_is_not_answered():
    async def scenario():
        mgr = ModelManager(device="cpu", batch_wait_s=0.005)
        first = asyncio.create_task(mgr.run_inference("m", "slow", "r0"))
        await asyncio.sleep(0.03)
        gone = asyncio.create_task(mgr.run_inference("m", "slow", "gone"))
        stays = asyncio.create_task(mgr.run_inference("m", "slow", "stays"))
        await asyncio.sleep(0.01)
        gone.cancel()
        assert (await stays)["result"] == "stays!"
        await first
        assert gone.cancelled()
        assert SlowBatchingModel.sizes == [1, 1]

    asyncio.run(scenario())


def test_a_request_that_would_wait_too_long_is_refused():
    async def scenario():
        SlowBatchingModel.batch_size = 1
        try:
            # A pass takes 0.2 s, so with one request a batch, a request with two or more ahead would wait past 0.3 s.
            mgr = ModelManager(device="cpu", batch_wait_s=0.005, wait_timeout=0.3)
            await mgr.run_inference("m", "slow", "warm")  # the batcher learns how long a pass takes
            outcomes = await asyncio.gather(*[mgr.run_inference("m", "slow", f"r{i}") for i in range(6)],
                                            return_exceptions=True)
        finally:
            del SlowBatchingModel.batch_size
        refused = [o for o in outcomes if isinstance(o, Unavailable)]
        assert refused and len(refused) < 6
        assert all(int(o.headers["Retry-After"]) >= 1 for o in refused)
        assert all(o["result"] == f"r{i}!" for i, o in enumerate(outcomes) if not isinstance(o, Exception))

    asyncio.run(scenario())


async def at(priority, call):
    """``call()`` as a request of ``priority`` makes it."""
    request_context.set(RequestContext(priority=priority, skip=True))
    return await call()


def test_a_request_is_refused_only_for_the_requests_that_go_before_it():
    """Low-priority requests waiting do not hold up a high one, which goes before them: it is not refused for them."""
    async def scenario():
        SlowBatchingModel.batch_size = 1
        try:
            # A pass takes 0.2 s: a request with two or more ahead of it would wait past 0.5 s. Before the first pass
            # nothing says how long one takes, so the low ones all wait.
            mgr = ModelManager(device="cpu", batch_wait_s=0.005, wait_timeout=0.5)
            low = [asyncio.create_task(at(LOW, lambda i=i: mgr.run_inference("m", "slow", f"low{i}"))) for i in range(5)]
            await asyncio.sleep(0.45)  # two passes on: three low ones wait, and a pass is known to take 0.2 s
            high = await at(HIGH, lambda: mgr.run_inference("m", "slow", "high"))
            await asyncio.gather(*low, return_exceptions=True)
        finally:
            del SlowBatchingModel.batch_size
        assert high["result"] == "high!"

    asyncio.run(scenario())


def test_a_request_joining_the_batcher_hurries_its_wait_for_the_model():
    """The batcher asks for the model at the priority of the most urgent request waiting; one more urgent that joins
    while it waits moves it up, so it does not wait behind less urgent callers of the model."""
    async def scenario():
        mgr = make_manager()
        await mgr.run_inference("m", "slow", "warm")
        SlowBatchingModel.seen.clear()
        first = asyncio.create_task(at(NORMAL, lambda: mgr.run_batch("m", "slow", ["n1a", "n1b"])))
        await asyncio.sleep(0.03)  # it holds the model
        low = asyncio.create_task(at(LOW, lambda: mgr.run_inference("m", "slow", "low")))
        await asyncio.sleep(0.05)  # the batcher waits for the model, as low
        second = asyncio.create_task(at(NORMAL, lambda: mgr.run_batch("m", "slow", ["n2a", "n2b"])))
        await asyncio.sleep(0.01)
        high = asyncio.create_task(at(HIGH, lambda: mgr.run_inference("m", "slow", "high")))
        await asyncio.gather(first, low, second, high)
        return [r for batch in SlowBatchingModel.seen for r in batch]

    order = asyncio.run(scenario())
    assert order.index("high") < order.index("n2a"), order


def test_a_running_batch_is_one_pass_to_wait_for_not_a_pass_per_request():
    async def scenario():
        mgr = make_manager()
        details = lambda: next(iter(mgr.get_cache_status()["model_details"].values()))
        await mgr.run_inference("m", "slow", "warm")  # one pass timed
        batch = [asyncio.create_task(mgr.run_inference("m", "slow", f"r{i}")) for i in range(16)]
        await until_async(lambda: SlowBatchingModel.sizes == [1, 16], what="the batch of 16 to start")
        shown = details()
        # 16 running in one pass, none waiting: the next request waits about that pass, not 16 of them.
        assert shown["in_use"] == 16 and shown["waiting"] == 0 and shown["batch_size"] == 32
        assert shown["estimated_wait_ms"] < 2 * shown["avg_request_ms"]
        await asyncio.gather(*batch)
        assert details()["estimated_wait_ms"] == 0.0

    asyncio.run(scenario())


def test_requests_waiting_for_a_slot_of_their_own_count_on_a_model_that_batches():
    # The batch route's requests skip the batcher and wait for the model's slot: each is a pass to wait for.
    async def scenario():
        mgr = make_manager()
        details = lambda: next(iter(mgr.get_cache_status()["model_details"].values()))
        await mgr.run_batch("m", "slow", ["warm"])  # one pass timed
        calls = [asyncio.create_task(mgr.run_batch("m", "slow", [f"r{i}", f"s{i}"])) for i in range(6)]
        await until_async(lambda: len(SlowBatchingModel.sizes) >= 2 and details()["waiting"] == 5,
                          what=lambda: f"one running and 5 waiting, not {details()}")
        shown = details()
        assert shown["in_use"] == 6 and shown["estimated_wait_ms"] > 5 * shown["avg_request_ms"]
        await asyncio.gather(*calls)

    asyncio.run(scenario())
