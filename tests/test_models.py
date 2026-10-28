import asyncio
import threading
import time

import pytest

from config import Config
from models import BaseModel
from models.generic.transformers_model import TransformersModel, fold_system
from models.vision.fastvlm import FastVLMModel


class Tiny(BaseModel):
    """A model written the way model files are: plain blocking methods."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.weights = None

    def load(self):
        self.load_thread = threading.current_thread()
        self.weights = [1, 2, 3]
        self.tokenizer = "tok"

    def predict(self, text, temperature=0.5):
        return {"text": text, "temperature": temperature, "thread": threading.current_thread()}


def test_blocking_methods_run_off_the_event_loop():
    async def scenario():
        model = Tiny("tiny", "t")
        await model.start()
        out = await model.run("hi")
        assert model.load_thread is not threading.main_thread()
        assert out["thread"] is not threading.main_thread()

    asyncio.run(scenario())


class Busy(Tiny):
    """Records how many predicts run at the same moment."""

    def predict(self, text):
        with Busy.guard:
            Busy.running += 1
            Busy.peak = max(Busy.peak, Busy.running)
        time.sleep(0.05)
        with Busy.guard:
            Busy.running -= 1


@pytest.mark.parametrize("device, peak", [("mps", 1), ("cpu", 2)])
def test_models_take_turns_only_on_mps(device, peak):
    Busy.guard, Busy.running, Busy.peak = threading.Lock(), 0, 0

    async def scenario():
        models = [Busy(name, "t", device=device) for name in ("a", "b")]
        for model in models:
            await model.start()
        await asyncio.gather(*(model.run("hi") for model in models))

    asyncio.run(scenario())
    assert Busy.peak == peak

def test_unload_drops_everything_load_created():
    async def scenario():
        model = Tiny("tiny", "t")
        await model.start()
        assert model.is_loaded and model.weights == [1, 2, 3]
        await model.stop()
        assert not model.is_loaded
        assert model.weights is None  # set in __init__: restored
        assert not hasattr(model, "tokenizer")  # created by load(): deleted
        with pytest.raises(RuntimeError):
            await model.run("hi")

    asyncio.run(scenario())


def test_task_defaults_to_the_registered_one():
    assert FastVLMModel("apple/FastVLM-1.5B").task == "vision-language"


def test_auto_dtype_is_half_on_mps_only_for_text_generators():
    torch = pytest.importorskip("torch")

    assert FastVLMModel("apple/FastVLM-1.5B", device="mps").dtype == torch.float16
    assert TransformersModel("m", "text-generation", "mps").dtype == torch.float16
    assert TransformersModel("m", "text-classification", "mps").dtype == torch.float32
    assert FastVLMModel("apple/FastVLM-1.5B", device="cpu").dtype == torch.float32
    # An explicit choice still wins.
    assert FastVLMModel("apple/FastVLM-1.5B", device="mps", options={"torch_dtype": "float32"}).dtype == torch.float32


def test_fastvlm_remote_code_only_for_allowlisted_repos(monkeypatch):
    assert FastVLMModel("apple/FastVLM-1.5B").trust_remote_code is True

    untrusted = FastVLMModel("evil/pwn-fastvlm")
    assert untrusted.trust_remote_code is False
    with pytest.raises(ValueError, match="TRUST_REMOTE_CODE_MODELS"):
        asyncio.run(untrusted.start())
    assert not untrusted.is_loaded

    monkeypatch.setattr(Config, "TRUST_REMOTE_CODE", False)
    assert FastVLMModel("apple/FastVLM-1.5B").trust_remote_code is False


class TemplateTokenizer:
    """Stands in for a tokenizer with a chat template; can refuse system turns like Gemma's."""

    chat_template = "{{ messages }}"

    def __init__(self, allow_system=True):
        self.allow_system = allow_system
        self.seen = None

    def apply_chat_template(self, messages, add_generation_prompt, tokenize, **kwargs):
        if not self.allow_system and any(m["role"] == "system" for m in messages):
            raise ValueError("System role not supported")
        self.seen = messages
        self.kwargs = kwargs
        return "|".join(f"{m['role']}:{m['content']}" for m in messages) + "|assistant:"


def chat_model(tokenizer):
    model = TransformersModel("chat/model", "text-generation")
    model.tokenizer = tokenizer
    return model


CHAT = [{"role": "system", "content": "Be brief."}, {"role": "user", "content": "hi"}]
TOOLS = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]


def generation(**fields):
    from models.tasks import TextGenerationInput

    return TextGenerationInput(text="x", messages=CHAT, **fields)


def test_chat_is_rendered_with_the_tokenizer_template():
    assert chat_model(TemplateTokenizer())._render_chat(generation()) == "system:Be brief.|user:hi|assistant:"


def test_system_turn_is_folded_in_when_the_template_refuses_it():
    tokenizer = TemplateTokenizer(allow_system=False)
    assert chat_model(tokenizer)._render_chat(generation()) == "user:Be brief.\n\nhi|assistant:"
    assert CHAT[1]["content"] == "hi", "the caller's messages must not change"


def test_no_template_means_the_plain_transcript_is_used():
    tokenizer = TemplateTokenizer()
    tokenizer.chat_template = None
    assert chat_model(tokenizer)._render_chat(generation()) is None


def test_tools_go_to_a_template_that_takes_them():
    tokenizer = TemplateTokenizer()
    tokenizer.chat_template = "{{ messages }}{{ tools }}"
    rendered = chat_model(tokenizer)._render_chat(generation(tools=TOOLS, tool_choice="required"))
    assert tokenizer.kwargs["tools"] == TOOLS
    # The template lists the functions; only the must-call instruction is added.
    assert rendered == "system:Be brief.\n\nYou must call at least one function now.|user:hi|assistant:"


def test_tools_and_json_become_an_instruction_for_other_templates():
    tokenizer = TemplateTokenizer()
    rendered = chat_model(tokenizer)._render_chat(generation(tools=TOOLS, json_schema={"type": "object"}))
    assert "tools" not in tokenizer.kwargs
    system = tokenizer.seen[0]["content"]
    assert system.startswith("Be brief.\n\nYou can call these functions")
    assert '"name": "lookup"' in system and "<tool_call>" in system
    assert system.endswith('matches this JSON Schema, and nothing else:\n{"type": "object"}')
    assert rendered.endswith("|user:hi|assistant:")


def test_earlier_tool_calls_reach_the_template_with_object_arguments():
    tokenizer = TemplateTokenizer()
    history = [
        *CHAT,
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": '{"q": "x"}'}},
        ]},
        {"role": "tool", "content": "42", "tool_call_id": "c1"},
    ]
    from models.tasks import TextGenerationInput

    chat_model(tokenizer)._render_chat(TextGenerationInput(text="x", messages=history, tools=TOOLS))
    assert tokenizer.seen[2]["tool_calls"] == [
        {"id": "c1", "type": "function", "function": {"name": "lookup", "arguments": {"q": "x"}}},
    ]
    assert tokenizer.seen[3] == {"role": "tool", "content": "42", "tool_call_id": "c1"}


def test_fold_system_without_a_user_turn():
    assert fold_system([{"role": "system", "content": "S"}]) == [{"role": "user", "content": "S"}]


class _Pretrained:
    """Stands in for a transformers Auto class: records how it was asked to load."""

    calls = []

    def __init__(self):
        self.device, self.evaluated = "cpu", False

    @classmethod
    def from_pretrained(cls, model_id, **kwargs):
        cls.calls.append((model_id, kwargs))
        return cls()

    def to(self, device):
        self.device = device
        return self

    def eval(self):
        self.evaluated = True
        return self


@pytest.mark.parametrize("device", ["cpu", "mps", "cuda"])
def test_weights_are_placed_by_accelerate_only_on_cuda(device):
    pytest.importorskip("torch")
    _Pretrained.calls = []
    model = Tiny("org/tiny", device=device, options={"max_memory": {"0": "4GiB"}})
    loaded = model.load_pretrained(_Pretrained, trust_remote_code=True)
    (model_id, kwargs), = _Pretrained.calls
    assert model_id == "org/tiny" and kwargs["trust_remote_code"] is True and loaded.evaluated
    if device == "cuda":
        assert kwargs["device_map"] == "auto" and kwargs["max_memory"] == {"0": "4GiB"} and loaded.device == "cpu"
    else:
        assert "device_map" not in kwargs and "max_memory" not in kwargs and loaded.device == device


def test_greedy_generation_leaves_out_the_sampling_knobs():
    from models.tasks import TextGenerationInput

    assert TextGenerationInput(text="x", temperature=0.2).sampling() == {"do_sample": True, "temperature": 0.2, "top_p": 0.9}
    assert TextGenerationInput(text="x", do_sample=False).sampling() == {"do_sample": False}


def test_length_batches_group_similar_lengths_within_size_and_budget():
    from models.base import length_batches

    # Shortest first, at most two per pass.
    assert length_batches([5, 100, 6, 7, 300], size=2, budget=400) == [[0, 2], [3, 1], [4]]
    # Padded to the longest, a third 100 would make 300 > 250: it starts a pass of its own.
    assert length_batches([100, 100, 100], size=32, budget=250) == [[0, 1], [2]]
    # An item over the budget still runs, alone.
    assert length_batches([10, 5000], size=32, budget=1000) == [[0], [1]]
    assert length_batches([], size=32, budget=1000) == []


@pytest.mark.parametrize("model_id, task, inputs", [
    ("hf-internal-testing/tiny-random-DistilBertForSequenceClassification", "text-classification",
     [{"text": t} for t in ["short", "a somewhat longer text to classify", "mid length one", "x", "the longest text of them all, by a fair margin"]]),
    ("hf-internal-testing/tiny-random-DistilBertForQuestionAnswering", "question-answering",
     [{"question": q, "context": "Paris is the capital of France. It lies on the Seine and has about two million people."}
      for q in ["What is the capital?", "Which river?", "How many people live there, roughly?"]]),
])
def test_a_batch_gets_the_answers_single_requests_get(model_id, task, inputs):
    """Padding several inputs into one pass must not change any input's result."""
    pytest.importorskip("torch")
    from models import tasks

    model = TransformersModel(model_id, task, "cpu")
    try:
        asyncio.run(model.start())
    except OSError:
        pytest.skip(f"{model_id} is not in the local Hub cache")
    model.batch_size = 2  # several passes, each padded
    contract = tasks.get(task)
    requests = [contract.parse_input(i) for i in inputs]
    singles = [contract.parse_output(model.predict(r)) for r in requests]
    batch = [contract.parse_output(r) for r in model.predict_batch(requests)]
    for one, together in zip(singles, batch):
        if task == "text-classification":
            assert one.scores.keys() == together.scores.keys()
            assert all(abs(one.scores[k] - together.scores[k]) < 1e-4 for k in one.scores)
        else:
            assert (one.answer, one.start_position, one.end_position) == (together.answer, together.start_position, together.end_position)
            assert abs(one.confidence - together.confidence) < 1e-4
    asyncio.run(model.stop())
