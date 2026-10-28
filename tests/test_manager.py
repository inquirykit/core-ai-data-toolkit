import asyncio

import pytest

from errors import NotFound
from models import UnsupportedModel
from models.manager import ModelManager
from tests.fakes import FakeModel, registered


@pytest.fixture(autouse=True)
def fake_registry():
    FakeModel.instances.clear()
    FakeModel.load_delay = 0.0
    FakeModel.predict_delay = 0.0
    with registered(FakeModel, task="t", fallback=True):
        yield


def make_manager(**kw):
    return ModelManager(device="cpu", **kw)


def test_concurrent_requests_load_model_once():
    async def scenario():
        FakeModel.load_delay = 0.05
        mgr = make_manager()
        results = await asyncio.gather(*[mgr.load_model("x", "t") for _ in range(5)])
        assert len({id(r) for r in results}) == 1
        assert len(FakeModel.instances) == 1
        assert FakeModel.instances[0].load_calls == 1

    asyncio.run(scenario())


def test_inference_is_serialized_per_model_and_concurrent_across_models():
    async def scenario():
        FakeModel.predict_delay = 0.05
        mgr = make_manager()
        await mgr.load_model("a", "t")
        await mgr.load_model("b", "t")
        loop = asyncio.get_running_loop()
        t0 = loop.time()
        await asyncio.gather(
            mgr.run_inference("a", "t", "1"),
            mgr.run_inference("a", "t", "2"),
            mgr.run_inference("b", "t", "3"),
            mgr.run_inference("b", "t", "4"),
        )
        elapsed = loop.time() - t0
        # Two per model serialized (~0.1s), models overlap (< 0.2s total).
        assert 0.09 <= elapsed < 0.19

    asyncio.run(scenario())


def test_a_request_that_would_wait_too_long_for_a_slot_is_refused():
    """A model answering one request at a time (generation, a model server) is held to the batcher's rule."""
    from models.cache import Overloaded

    async def scenario():
        FakeModel.predict_delay = 0.2
        mgr = make_manager(wait_timeout=0.3)
        await mgr.run_inference("a", "t", "warm")  # the entry learns how long a request takes
        # One runs at once, so a request with two or more ahead of it would wait 0.4 s or more.
        return await asyncio.gather(*[mgr.run_inference("a", "t", str(i)) for i in range(6)], return_exceptions=True)

    outcomes = asyncio.run(scenario())
    refused = [o for o in outcomes if isinstance(o, Exception)]
    assert len(refused) == 4 and all(isinstance(o, Overloaded) for o in refused)
    assert all(int(o.headers["Retry-After"]) >= 1 for o in refused)
    assert not isinstance(outcomes[0], Exception) and not isinstance(outcomes[1], Exception)


def test_model_in_use_is_not_evicted_mid_request():
    async def scenario():
        FakeModel.predict_delay = 0.05
        mgr = make_manager(max_models=1)
        await mgr.load_model("a", "t")
        a = FakeModel.instances[0]
        infer = asyncio.create_task(mgr.run_inference("a", "t", "hello"))
        await asyncio.sleep(0.01)
        # Loading b must evict a, but only after a's request has finished.
        await mgr.load_model("b", "t")
        result = await infer
        assert result["result"]["echo"] == "hello"
        assert a.unload_calls == 1
        assert list(mgr.cache.entries) == ["b:t"]

    asyncio.run(scenario())


def test_failed_load_is_not_cached():
    class Exploding(FakeModel):
        async def load(self):
            self.half_loaded = object()
            raise RuntimeError("no weights")

    async def scenario():
        mgr = make_manager()
        with pytest.raises(RuntimeError):
            await mgr.load_model("z", "boom")
        assert mgr.get_cache_status()["cache_size"] == 0
        model = FakeModel.instances[0]
        assert model.unload_calls == 1
        assert not hasattr(model, "half_loaded")

    with registered(Exploding, task="boom", fallback=True):
        asyncio.run(scenario())


def test_load_options_reach_the_model():
    async def scenario():
        mgr = make_manager()
        m = await mgr.load_model("x", "t", torch_dtype="float16", max_memory={"cpu": "1GB"})
        assert m.options == {"torch_dtype": "float16", "max_memory": {"cpu": "1GB"}}
        assert m.device == "cpu" and m.task == "t"
        info = mgr.get_cache_status()["model_details"]["x:t"]
        assert info["task"] == "t"

    asyncio.run(scenario())


def test_unsupported_task_is_rejected_before_loading():
    async def scenario():
        mgr = make_manager()
        with pytest.raises(UnsupportedModel):
            await mgr.run_inference("x", "nope", "hi")
        assert FakeModel.instances == []

    asyncio.run(scenario())


def test_unload_and_clear():
    async def scenario():
        mgr = make_manager()
        await mgr.load_model("x", "t")
        await mgr.unload_model("x", "t")
        assert FakeModel.instances[0].unload_calls == 1
        with pytest.raises(NotFound):
            await mgr.unload_model("x", "t")
        await mgr.load_model("y", "t")
        await mgr.clear_all_models()
        assert mgr.get_cache_status()["cache_size"] == 0

    asyncio.run(scenario())


def test_available_models_come_from_the_registry():
    mgr = make_manager()
    listed = mgr.get_available_models()
    assert "Qwen/Qwen2.5-0.5B-Instruct" in listed["text-generation"]
    assert "LiquidAI/LFM2.5-230M" in listed["text-generation"]
    assert "vega-4b" in listed["decision"]
    assert listed["t"] == []

    allowed = mgr.get_available_models(["gpt2", "org/vega-custom", "some/new-model"])
    assert allowed["text-generation"] == ["gpt2", "some/new-model"]
    assert allowed["decision"] == ["org/vega-custom"]


def test_invalid_request_is_rejected_before_loading():
    from pydantic import Field
    from models import tasks

    class Input(tasks.TaskInput):
        text: str = Field(..., max_length=3)

    class Output(tasks.TaskOutput):
        echo: str

    tasks.define_task("strict", Input, Output)
    try:
        async def scenario():
            mgr = make_manager()
            with registered(FakeModel, task="strict", match="*"):
                with pytest.raises(ValueError):
                    await mgr.run_inference("x", "strict", {"text": "too long"})
            # The bad request never loaded the model.
            assert FakeModel.instances == []

        asyncio.run(scenario())
    finally:
        tasks._contracts.pop("strict", None)


def test_preload_loads_and_pins_and_survives_failures():
    async def scenario():
        mgr = make_manager()
        # "x:nope" has no class for its task: logged and skipped, the rest still load.
        await mgr.preload(["a:t", "x:nope"], pinned=["b:t"])
        assert sorted(mgr.cache.entries) == ["a:t", "b:t"]
        assert mgr.cache.entries["b:t"].pinned is True
        assert mgr.cache.entries["a:t"].pinned is False
        with pytest.raises(NotFound):
            mgr.set_pinned("zzz", "t", True)
        mgr.set_pinned("a", "t", True)
        assert mgr.cache.entries["a:t"].pinned is True

    asyncio.run(scenario())


def test_model_specs_name_a_task_or_get_the_claimed_one():
    mgr = make_manager()
    with registered(FakeModel, task="t", ids=["org/model:v2"]):
        assert mgr.parse_model_spec("org/model:v2:t") == ("org/model:v2", "t")
        assert mgr.parse_model_spec("org/model:v2") == ("org/model:v2", "t")
    assert mgr.parse_model_spec("any/model") == ("any/model", "text-generation")


class Batcher(FakeModel):
    """A FakeModel that answers a batch in one predict_batch() call."""

    batches = []
    drop_one = False

    def predict_batch(self, requests):
        Batcher.batches.append(list(requests))
        results = [{"echo": r} for r in requests]
        return results[:-1] if Batcher.drop_one else results


def test_a_failing_sink_is_logged_and_the_others_still_hear(caplog):
    def broken(event):
        raise RuntimeError("sink down")

    async def scenario():
        events = []
        out = await make_manager(sinks=[broken, events.append]).run_inference("a", "t", "hi")
        assert out["result"] == {"echo": "hi"}
        assert [e.status for e in events] == ["ok"]

    asyncio.run(scenario())
    assert "A request sink failed" in caplog.text


def test_batch_answers_in_order_with_one_trace_per_input():
    async def scenario():
        events = []
        mgr = make_manager(sinks=[events.append])
        out = await mgr.run_batch("a", "t", ["1", "2", "3"])
        assert out["results"] == [{"echo": "1"}, {"echo": "2"}, {"echo": "3"}]
        assert out["model_used"] == "a:t" and out["cold_start"] is True
        # No predict_batch(): predict() answered each in turn, holding one slot.
        assert FakeModel.instances[0].predict_calls == 3
        # One request; a call (a trace) per input, each with its own result.
        [event] = events
        assert event.status == "ok" and event.cold is True
        assert [(c.input, c.output) for c in event.calls] == [("1", {"echo": "1"}), ("2", {"echo": "2"}), ("3", {"echo": "3"})]
        assert [c.trace_id for c in event.calls] == out["trace_ids"] and len(set(out["trace_ids"])) == 3

    asyncio.run(scenario())


def test_batch_goes_to_predict_batch_in_one_call():
    Batcher.batches, Batcher.drop_one = [], False

    async def scenario():
        with registered(Batcher, task="b", fallback=True):
            out = await make_manager().run_batch("m", "b", ["x", "y"])
        assert out["results"] == [{"echo": "x"}, {"echo": "y"}]
        assert Batcher.batches == [["x", "y"]]
        assert Batcher.instances[-1].predict_calls == 0

    asyncio.run(scenario())


def test_batch_with_a_result_missing_is_a_contract_violation():
    from models.tasks import ContractViolation

    Batcher.batches, Batcher.drop_one = [], True

    async def scenario():
        with registered(Batcher, task="b", fallback=True):
            with pytest.raises(ContractViolation, match="2 results for 3 requests"):
                await make_manager().run_batch("m", "b", ["x", "y", "z"])

    try:
        asyncio.run(scenario())
    finally:
        Batcher.drop_one = False


def test_batch_names_the_bad_input_and_loads_nothing():
    from pydantic import Field
    from models import tasks

    class Input(tasks.TaskInput):
        text: str = Field(..., max_length=3)

    class Output(tasks.TaskOutput):
        echo: str

    tasks.define_task("strict", Input, Output)
    try:
        async def scenario():
            mgr = make_manager()
            with registered(FakeModel, task="strict", match="*"):
                with pytest.raises(ValueError, match=r"inputs\[1\]"):
                    await mgr.run_batch("x", "strict", [{"text": "ok"}, {"text": "too long"}])
                with pytest.raises(ValueError, match="at least one"):
                    await mgr.run_batch("x", "strict", [])
            assert FakeModel.instances == []

        asyncio.run(scenario())
    finally:
        tasks._contracts.pop("strict", None)
