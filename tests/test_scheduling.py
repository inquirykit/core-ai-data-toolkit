"""Which model gets the cache's room when several want it (models/cache.py): keep_s, turns, priority, draining."""

import asyncio
import time

import pytest

from models.cache import CapacityError
from models.events import HIGH, LOW, NORMAL, RequestContext, request_context
from models.manager import ModelManager
from tests.fakes import FakeModel, registered


class Slow(FakeModel):
    load_delay = 0.05
    predict_delay = 0.01


@pytest.fixture(autouse=True)
def fake_registry():
    FakeModel.instances.clear()
    FakeModel.log.clear()
    with registered(Slow, task="t", fallback=True):
        yield


def loads():
    return [model for kind, model in FakeModel.log if kind == "load"]


def manager(**kw):
    """Room for one model."""
    return ModelManager(device="cpu", max_models=1, **kw)


async def client(mgr, model, n, gap=0.0, priority=HIGH, start=0.0):
    """``n`` requests to ``model`` one after another (run it as a task: it sets its own priority); how long each took."""
    request_context.set(RequestContext(priority=priority))
    await asyncio.sleep(start)
    took = []
    for i in range(n):
        t0 = time.monotonic()
        await mgr.run_inference(model, "t", f"{model}{i}")
        took.append(time.monotonic() - t0)
        await asyncio.sleep(gap)
    return took


def test_without_keep_two_clients_swap_models_on_every_request():
    # The old behaviour, kept with keep_s=0: what the next test fixes.
    async def scenario():
        mgr = manager()
        await asyncio.gather(client(mgr, "A", 10, gap=0.005), client(mgr, "B", 10, gap=0.005, start=0.001))
        assert len(loads()) > 10

    asyncio.run(scenario())


def test_a_model_between_requests_keeps_its_place():
    # Two clients pausing 5 ms between requests (an HTTP round trip) share room for one model: A finishes, then B loads.
    async def scenario():
        mgr = manager(keep_s=0.2)
        await asyncio.gather(client(mgr, "A", 30, gap=0.005), client(mgr, "B", 30, gap=0.005, start=0.001))
        assert loads() == ["A", "B"]

    asyncio.run(scenario())


def test_a_load_that_waited_its_turn_drains_a_busy_model():
    async def scenario():
        mgr = manager(keep_s=0.2, turn_s=0.3)
        # B's few requests take far less than A's turn, so A does not take the place back while B still works.
        _, b = await asyncio.gather(client(mgr, "A", 100), client(mgr, "B", 3, start=0.05))
        # B waited its turn (0.3 s) and a load, not A's 100 requests (over 1 s); then A came back for the rest.
        assert b[0] < 0.8
        assert loads() == ["A", "B", "A"]

    asyncio.run(scenario())


def test_higher_priority_takes_the_place_without_waiting_a_turn():
    async def scenario():
        mgr = manager(keep_s=0.2, turn_s=10)
        _, c = await asyncio.gather(client(mgr, "A", 60, priority=NORMAL), client(mgr, "C", 1, start=0.1))
        # One of A's requests finishing, then C's load (about 0.06 s): not the 10 s turn.
        assert c[0] < 1.0
        assert loads() == ["A", "C", "A"]

    asyncio.run(scenario())


def test_a_load_of_lower_priority_never_drains_and_gives_up():
    async def scenario():
        mgr = manager(keep_s=0.2, turn_s=0.1, wait_timeout=0.3)
        outcomes = await asyncio.gather(client(mgr, "A", 60), client(mgr, "B", 1, priority=LOW, start=0.05),
                                        return_exceptions=True)
        assert isinstance(outcomes[1], CapacityError)
        assert loads() == ["A"]
        assert not mgr.cache.entries["A:t"].draining

    asyncio.run(scenario())


def test_retry_after_counts_the_turn_still_to_come():
    async def scenario():
        mgr = manager(keep_s=0.2, turn_s=3, wait_timeout=0.2)
        outcomes = await asyncio.gather(client(mgr, "A", 40), client(mgr, "B", 1, start=0.05), return_exceptions=True)
        refused = outcomes[1]
        # No sooner than the turn (about 2.8 s off); the busy model's own estimate may say later.
        assert isinstance(refused, CapacityError) and refused.retry_after >= 2.5
        assert int(refused.headers["Retry-After"]) >= 3

    asyncio.run(scenario())


def test_a_pinned_model_is_never_drained():
    async def scenario():
        mgr = manager(keep_s=0.2, turn_s=0.05)
        await mgr.load_model("A", "t", pin=True)
        await asyncio.gather(client(mgr, "A", 20, priority=LOW), client(mgr, "C", 1, start=0.02))
        entry = mgr.cache.entries["A:t"]
        assert entry.pinned and not entry.draining
        # Only a pinned model is in the way, so C loads beside it, as before.
        assert loads() == ["A", "C"]

    asyncio.run(scenario())


def test_an_urgent_load_goes_ahead_of_one_still_waiting_for_room():
    async def scenario():
        mgr = manager(keep_s=0.2, turn_s=10, wait_timeout=2)
        # B can never drain A (lower priority) and waits out its timeout; C, which outranks A, must not wait behind it.
        outcomes = await asyncio.gather(client(mgr, "A", 400, priority=NORMAL), client(mgr, "B", 1, priority=LOW, start=0.05),
                                        client(mgr, "C", 1, start=0.1), return_exceptions=True)
        assert isinstance(outcomes[1], CapacityError)
        # One of A's requests and C's load; behind B, C would have waited out B's 2 s.
        assert outcomes[2][0] < 1.2
        assert loads()[:2] == ["A", "C"]

    asyncio.run(scenario())


def test_requests_for_a_model_with_no_room_give_up_at_their_own_deadline():
    async def scenario():
        mgr = manager(keep_s=0.2, turn_s=10, wait_timeout=0.3)
        busy = asyncio.create_task(client(mgr, "A", 100))
        await asyncio.sleep(0.05)
        started = time.monotonic()
        # They wait for B's load one after another; each gives up 0.3 s after it asked, not 0.3 s after its turn.
        outcomes = await asyncio.gather(*(mgr.run_inference("B", "t", "b") for _ in range(4)), return_exceptions=True)
        assert all(isinstance(o, CapacityError) for o in outcomes)
        assert time.monotonic() - started < 0.6
        busy.cancel()

    asyncio.run(scenario())
