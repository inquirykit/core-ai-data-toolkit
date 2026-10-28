import asyncio

import pytest

from models import BaseModel, registry, tasks
from models.tasks import ContractViolation, TaskInput, TaskOutput, define_task
from tests.fakes import registered


class ShoutInput(TaskInput):
    text: str
    times: int = 1


class ShoutOutput(TaskOutput):
    text: str


class Shouter(BaseModel):
    result = None  # what predict() returns; None means a valid result
    seen = []

    def load(self):
        pass

    def predict(self, request):
        Shouter.seen.append(request)
        if Shouter.result is not None:
            return Shouter.result
        return {"text": request.text.upper() * request.times}


def shout(**fields):
    """A request parsed the way the manager does it before run()."""
    return tasks.get("shout").parse_input(fields)


@pytest.fixture
def shouter():
    define_task("shout", ShoutInput, ShoutOutput)
    Shouter.result, Shouter.seen = None, []
    try:
        model = Shouter("s", "shout")
        asyncio.run(model.start())
        yield model
    finally:
        tasks._contracts.pop("shout", None)


def test_predict_gets_the_input_model_and_its_result_becomes_the_output_model(shouter):
    out = asyncio.run(shouter.run(shout(text="hi", times=2)))
    assert isinstance(Shouter.seen[0], ShoutInput)
    assert out == ShoutOutput(text="HIHI")
    # Defaults come from the input model.
    asyncio.run(shouter.run(shout(text="hi")))
    assert Shouter.seen[1].times == 1


@pytest.mark.parametrize("bad", [{"text": "hi", "loud": True}, {"times": 2}, "hi"])
def test_a_bad_request_is_a_value_error(shouter, bad):
    with pytest.raises(ValueError):
        tasks.get("shout").parse_input(bad)


def test_run_takes_only_parsed_requests(shouter):
    # Validation happens once, in the manager, before the model is loaded.
    with pytest.raises(TypeError):
        asyncio.run(shouter.run({"text": "hi"}))
    assert Shouter.seen == []


@pytest.mark.parametrize("result", [{"txt": "oops"}, "HI", {"text": None}])
def test_a_result_that_breaks_the_contract_is_not_a_client_error(shouter, result):
    Shouter.result = result
    with pytest.raises(ContractViolation) as caught:
        asyncio.run(shouter.run(shout(text="hi")))
    # Routes answer ValueError with 400; a broken model must be a 500.
    assert not isinstance(caught.value, ValueError)


def test_fields_beyond_the_contract_are_dropped(shouter):
    Shouter.result = {"text": "HI", "debug": "internal"}
    assert asyncio.run(shouter.run(shout(text="hi"))).model_dump() == {"text": "HI"}


def test_a_task_without_a_contract_passes_everything_through():
    model = Shouter("s", "no-contract")
    asyncio.run(model.start())
    Shouter.result, Shouter.seen = ["anything"], []
    assert asyncio.run(model.run("raw")) == ["anything"]
    assert Shouter.seen == ["raw"]


def test_on_text_reaches_only_a_predict_that_names_it(shouter):
    class Streaming(Shouter):
        def predict(self, request, on_text=None):
            on_text("piece")
            return {"text": "done"}

    pieces = []
    model = Streaming("s", "shout")
    asyncio.run(model.start())
    asyncio.run(model.run(shout(text="hi"), on_text=pieces.append))
    assert pieces == ["piece"]
    # Shouter.predict has no on_text: it is simply not passed.
    asyncio.run(shouter.run(shout(text="hi"), on_text=pieces.append))


def test_every_registered_task_has_a_contract():
    registry.discover()
    assert set(registry.known_tasks()) <= set(tasks.contracts())


def test_a_model_file_can_define_its_task():
    registry.discover()
    assert tasks.get("custom").input.__name__ == "EchoInput"
    with registered(Shouter, task="custom", ids=["echo-1"]):
        assert registry.resolve("echo-1", "custom") is Shouter


def test_classification_label_must_be_scored():
    contract = tasks.get("text-classification")
    with pytest.raises(ContractViolation, match="missing from scores"):
        contract.parse_output({"label": "POSITIVE", "confidence": 0.9, "scores": {"NEGATIVE": 0.1}})


def test_describe_gives_json_schema():
    described = tasks.get("question-answering").describe()
    assert described["input"]["required"] == ["question", "context"]
    assert described["input"]["additionalProperties"] is False
    assert "answer" in described["output"]["properties"]


def test_audio_decodes_wav_without_soundfile(monkeypatch):
    import io
    import sys
    import wave

    import numpy as np

    from models.media import decode_audio, resample

    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(2)
        w.setsampwidth(2)
        w.setframerate(8000)
        w.writeframes(np.array([[16384, -16384]] * 80, dtype=np.int16).tobytes())
    monkeypatch.setitem(sys.modules, "soundfile", None)  # as if not installed
    samples, rate = decode_audio(buf.getvalue())
    assert rate == 8000 and samples.shape == (80,) and abs(samples).max() < 1e-6  # the channels cancel out
    assert resample(np.ones(80, np.float32), 8000, 16000).shape == (160,)
    with pytest.raises(ValueError, match="only PCM WAV"):
        decode_audio(b"ID3 not a wav")
