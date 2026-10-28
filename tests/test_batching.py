"""Micro-batching (models/batching.py): single requests arriving together share one predict_batch() call."""

import asyncio
import time
from collections import Counter

import pytest

from errors import Unavailable
from models.manager import ModelManager
from tests.fakes import FakeModel, registered


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

    def predict_batch(self, requests):
        SlowBatchingModel.sizes.append(len(requests))
        time.sleep(self.pass_s)
        return super().predict_batch(requests)


@pytest.fixture(autouse=True)
def fake_registry():
    FakeModel.instances.clear()
    BatchingModel.batch_calls.clear()
    SlowBatchingModel.sizes = []
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
