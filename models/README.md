# Models

To add a model, add one file to a subfolder of `models/`. Nothing else needs editing: the service finds the file when it first needs a model, and the model appears in `/models/list` and `/v1/models`.

## Adding a model

```python
# models/custom/my_model.py
from models import BaseModel, register
from models.tasks import GeneratedText, TextGenerationInput


@register(task="text-generation", ids=["my-org/my-model"])
class MyModel(BaseModel):
    def load(self):
        from transformers import AutoModelForCausalLM, AutoTokenizer

        self.tokenizer = AutoTokenizer.from_pretrained(self.model_id)
        self.model = AutoModelForCausalLM.from_pretrained(self.model_id, dtype=self.dtype)
        self.model.to(self.device).eval()

    def predict(self, request: TextGenerationInput) -> GeneratedText:
        inputs = self.tokenizer(request.text, return_tensors="pt").to(self.device)
        out = self.model.generate(
            **inputs,
            max_new_tokens=request.max_length or 100,
            temperature=request.temperature,
            do_sample=request.do_sample,
        )
        return GeneratedText(text=self.tokenizer.decode(out[0][inputs.input_ids.shape[1]:], skip_special_tokens=True))
```

A request such as `POST /inference {"model_id": "my-org/my-model", "task": "text-generation", "input": {"text": "Hi"}}` now uses this class.

## Task contracts

What `predict()` receives and returns is set by the task, not the model: `models/tasks.py` has an input and an output Pydantic model for each task. Every model for a task follows the same contract, so clients get the same `result` shape whichever model answers.

| Task | Input model | Output model |
|---|---|---|
| `text-generation` | `TextGenerationInput`: `text` (or `tokens`, the prompt as token IDs), `messages` (turns may carry `tool_calls`, `tool_call_id`, `name`), `tools`, `tool_choice`, `json_schema`, `stop`, `logprobs` (alternatives per token; None for none), `echo`, `max_length` (0: generate nothing), `temperature`, `top_p`, `do_sample` | `GeneratedText`: `text`, the new text only; `tool_calls` for a model that parses its own (model servers do; for others `/v1/chat/completions` parses `text`, see `models/toolcalls.py`); `prompt_tokens` and `completion_tokens` when the model counts them (OpenAI's `usage` comes from these); `logprobs` (`TokenLogprobs`: `tokens`, `token_logprobs`, `top_logprobs`, `text_offset`, the echoed prompt first) when asked |
| `text-classification` | `TextClassificationInput`: `text` | `TextClassificationOutput`: `label`, `confidence`, `scores` (every label, keyed like `label`) |
| `question-answering` | `QuestionAnsweringInput`: `question`, `context` | `QuestionAnsweringOutput`: `answer`, `confidence`, `start_position`, `end_position` |
| `vision-language` | `VisionLanguageInput`: `text`, `image` (an absolute path, already checked to be inside `IMAGE_DIR`), `messages` (a chat whose content may be OpenAI parts: `text`, `image_url` with a checked base64 data URL, `input_audio`) and the generation fields | `GeneratedText` (count `prompt_tokens` and `completion_tokens` from the tensors, as the built-in ones do) |
| `embedding` | `EmbeddingInput`: `texts` | `EmbeddingOutput`: `embeddings` (one vector per text), `tokens` |
| `rerank` | `RerankInput`: `query`, `documents`, `top_n` | `RerankOutput`: `results` (`index`, `score`; best first), `tokens` |
| `speech-to-text` | `SpeechToTextInput`: `audio` (bytes), `filename`, `language`, `prompt`, `temperature` | `Transcript`: `text`, `language` |
| `decision` | `DecisionInput`: `state` or `image` (bytes), `questions` | `DecisionOutput`: `answers`, `n_tokens` |

The service checks both sides: a request that does not fit is refused before `predict()` runs, and a result that does not fit is a `ContractViolation` (a 500), so a mistake in a model file shows up at once rather than as odd JSON. Returning a dict with the right fields is fine; it is converted. Fields beyond the contract are dropped.

### What you write

| Method | Required | What it does |
|---|---|---|
| `load()` | yes | Loads the model onto `self.device`. Every attribute it sets is dropped again when the model is unloaded, which is what frees its memory. |
| `predict(request)` | yes | Answers one request. `request` is the task's input model, already validated, with its defaults filled in; return the output model. `max_length` is `None` when the client left it out, so apply your model's own default. For `text-generation`, `/v1/chat/completions` sends a plain transcript as `request.text` and also the chat as `request.messages`; use `messages` to format it yourself, for example with the tokenizer's chat template. Add an `on_text` parameter to stream: call it with each new piece of text and stop generating once it returns False; `models.base.text_streaming(tokenizer, on_text)` gives the `generate()` arguments that do both. |
| `unload()` | no | Extra cleanup, for things other than dropping attributes (closing a file or a client, for example). |
| `estimate_bytes()` | no | The memory the model will need, asked before loading so the cache can make room. The default reads the safetensors headers of `model_id` and sizes them in `self.dtype`; return `None` if unknown, and the cache counts what loading took. |

Set the class attribute `max_concurrency` to let one loaded instance run several requests at once (default 1).

Write these as plain blocking methods. They run in a worker thread, so they never block the server. `async def` also works.

Available on `self`:

| Attribute | Meaning |
|---|---|
| `model_id` | The requested ID: a Hub repo, a local path, or any name you register |
| `task` | The requested task |
| `device` | `cpu`, `cuda` or `mps`, already resolved from `DEFAULT_DEVICE` |
| `dtype` | The torch dtype the client asked for (`auto` gives float16 on CUDA, float16 on MPS if the class sets `half_on_mps = True`, float32 elsewhere) |
| `options` | Raw load options from `POST /models/load` (`torch_dtype`, `max_memory`) |

Import heavy libraries (torch, transformers, your SDK) inside `load()` and `predict()`, not at the top of the file. The service imports every model file at startup, and a top-level import of an optional package would stop the file from registering.

### `@register(task=..., ids=..., match=..., fallback=...)`

| Argument | Meaning |
|---|---|
| `task` | The task this class serves. Stack several decorators to serve several tasks. |
| `ids` | Model IDs this class serves. These are also the IDs listed by `/models/list` and `/v1/models`. Each is a string or a `ModelCard` (see below). |
| `match` | Case-insensitive glob patterns for other IDs it serves, e.g. `"*fastvlm*"` or `["vega-*", "*/vega-*"]` |
| `fallback` | Serve any ID for this task that no other class claims. Only the generic Hugging Face model does this. |

Give a listed ID a `ModelCard` to describe it in the model browser on `/admin` and `/playground` (`GET /models/catalog`): a one-line description and tags to filter by. Plain strings and cards mix freely:

```python
from models import BaseModel, ModelCard, register


@register(task="text-generation", ids=[
    ModelCard("my-org/my-model", "Small chat model tuned on support tickets", ["chat", "english"]),
    "my-org/my-model-base",
])
```

The browser works out the rest itself: whether the model is loaded, how much of it is in the local Hub cache, and (when you open it) how much memory it needs and what loading it would unload.

For a request `(model_id, task)` the service picks:

1. a class that lists `model_id` in `ids`; if none,
2. a class whose `match` pattern matches it; if none,
3. a `fallback` class.

Within the first rule that applies, the class registered for the requested task is used. If that rule found classes but none serves the requested task, the request fails with a 400 before anything loads. For example, `vega-0.8b` with task `text-generation` fails, because vega only does `decision`. If two classes claim the same ID for the same task, the file that sorts later wins, so you can override a built-in model with your own file.

### Folders

Folders are for organisation only; put a file wherever it makes sense. Files and folders whose names start with `_` are skipped, so shared helpers can live in e.g. `generic/_utils.py`. A file that fails to import is logged and skipped.

### A new task

Define its contract next to the model, then register for it:

```python
from models.tasks import TaskInput, TaskOutput, define_task


class SpeechInput(TaskInput):
    audio_path: str


class Transcript(TaskOutput):
    text: str


define_task("speech-to-text", SpeechInput, Transcript, "Transcribe an audio file")


@register(task="speech-to-text", ids=["UsefulSensors/moonshine-tiny"])
class Whisper(BaseModel):
    def predict(self, request: SpeechInput) -> Transcript:
        ...
```

`POST /inference` with `"task": "speech-to-text"` and `"input": {"audio_path": ...}` now reaches your model, and `/docs` and `GET /tasks` list the new task's input and result. `models/custom/custom_model.py` is a complete example. Subclass `TaskInput` and `TaskOutput`: the first rejects unknown fields, the second drops them.

`@register` alone also works: the task then has no contract, `input` reaches `predict()` exactly as the client sent it, and the result is returned unchecked. Add a contract before anyone depends on the task.

A dedicated route is only needed to match an external API's request format, as `/v1/decisions` does for vega.

## What's here

```
models/
├── base.py         # BaseModel and shared torch helpers
├── tasks.py        # Task contracts: each task's input and output model
├── registry.py     # @register, discovery and (model_id, task) -> class
├── manager.py      # ModelManager: load, run, unload, list
├── cache.py        # LRU cache with memory-aware eviction and per-model locks
├── generic/        # Fallbacks for any Hub ID: text tasks, vision-language (Qwen3.5, LFM2.5-VL, ...), embeddings, speech
├── vision/         # Apple FastVLM (text + image)
├── decision/       # vegaml typed decisions
└── custom/         # CustomModel: the smallest example, no weights, defines its own task
```

## Testing a model without the server

```python
import asyncio
from models.custom.my_model import MyModel

async def main():
    model = MyModel("my-org/my-model", device="cpu")
    await model.start()
    print(await model.run({"text": "Hello", "max_length": 20}))
    await model.stop()

asyncio.run(main())
```

Unit tests can swap a registered class for a fake with `tests.fakes.swapped(RealClass, FakeClass)`, or add one with `tests.fakes.registered(cls, task=..., ...)`.

## Models with their own server

A model can also be served by a separate OpenAI-compatible server (mlx-vlm,
mlx-lm, vLLM, llama-server) instead of a model file: list it in
`MODEL_SERVERS_FILE` (see `model_servers.example.toml`). `ServerModel` in
`models/model_server.py` starts the server on the first request and stops it
on eviction, so stopping it frees all of its memory and a crash in it affects
only that model. Those entries are registered after every model file, so they
win over a file that claims the same ID.

The generic `ImageTextToTextModel` (`generic/vision_model.py`) serves any
Hub ID for `vision-language` that no file claims, with Transformers'
`AutoModelForImageTextToText` and the model's own chat template. FastVLM's
native checkpoints go there too; `vision/fastvlm.py` is only for Apple's
`apple/FastVLM-*` repos, which need their Hub code.

`EmbeddingModel` (`generic/embedding_model.py`) and `SpeechToTextModel`
(`generic/speech_model.py`) do the same for `embedding` and `speech-to-text`:
any Hub encoder (pooled as its sentence-transformers config says, mean
otherwise, then L2-normalized), and anything Transformers' speech recognition
pipeline loads (Whisper, wav2vec2). `models/media.py` decodes the audio with
soundfile, or PCM WAV alone without it.
