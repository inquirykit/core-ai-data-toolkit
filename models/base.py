"""
BaseModel and the torch helpers model files share.

A model file subclasses BaseModel, writes ``load()`` and ``predict()`` as
plain blocking methods, and registers the class with ``@register`` (see
models/registry.py and models/README.md). Everything else (worker threads,
unloading, parameter filtering) is handled here.
"""

import asyncio
import functools
import inspect
import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Tuple, List

from .tasks import ContractViolation, GeneratedText, get as task_contract

logger = logging.getLogger(__name__)

_MISSING = object()

# PyTorch's MPS backend is not thread-safe: two models using an Apple GPU at
# once (generating, or one loading while another generates) crash the process
# with a failed Metal assertion. Work on MPS therefore takes turns; CPU and
# CUDA models still run in parallel.
_MPS_LOCK = threading.Lock()


class TryNextBackend(RuntimeError):
    """The model's backend cannot serve it, but another may: a model server that failed to start, or one that
    went away before a request reached it. The manager drops the instance and tries again with a fresh one,
    which picks the next backend (see models/model_server.py)."""


def _one_at_a_time(fn: Callable, *args, **kwargs) -> Any:
    with _MPS_LOCK:
        return fn(*args, **kwargs)


def resolve_device(requested: str = "auto") -> str:
    """Resolve 'auto' to the best available torch device, else honor the request."""
    if requested and requested != "auto":
        return requested
    try:
        import torch
    except ImportError:
        return "cpu"
    if torch.cuda.is_available():
        return "cuda"
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available():
        return "mps"
    return "cpu"


def resolve_torch_dtype(requested: Any, device: str, half_on_mps: bool = False) -> Any:
    """Map a dtype name to a torch dtype.

    ``None``/``"auto"`` picks float16 on CUDA, float16 on MPS when
    ``half_on_mps`` (MPS has patchy fp16 support, so only models that opt in)
    and float32 elsewhere. Explicit names such as ``"bfloat16"`` are looked up
    on the torch module.
    """
    import torch

    if requested is None or requested == "auto":
        return torch.float16 if device == "cuda" or (device == "mps" and half_on_mps) else torch.float32
    if isinstance(requested, str):
        dtype = getattr(torch, requested, None)
        if not isinstance(dtype, torch.dtype):
            raise ValueError(f"Unknown torch dtype: {requested}")
        return dtype
    return requested


def answer_questions(
    model: Any, tokenizer: Any, pairs: List[Tuple[str, str]], max_length: int = 512
) -> List[Dict[str, Any]]:
    """Extractive QA with a *ForQuestionAnswering model: every (question, context) pair in one forward pass.

    Runs the model directly rather than through ``pipeline("question-answering")``,
    which newer transformers releases no longer ship. Several pairs are padded
    to the longest; padding is masked out, so each pair gets the answer it
    would get alone.
    """
    import torch

    inputs = tokenizer(
        [question for question, _ in pairs],
        [context for _, context in pairs],
        max_length=max_length,
        truncation=True,
        padding=len(pairs) > 1,
        return_tensors="pt",
    ).to(model.device)

    with torch.no_grad():
        outputs = model(**inputs)
        start_scores = outputs.start_logits.float()
        end_scores = outputs.end_logits.float()
        mask = inputs.get("attention_mask")
        if mask is not None:
            start_scores = start_scores.masked_fill(mask == 0, float("-inf"))
            end_scores = end_scores.masked_fill(mask == 0, float("-inf"))
        start_probs = torch.softmax(start_scores, dim=1)
        end_probs = torch.softmax(end_scores, dim=1)

    answers = []
    for row in range(len(pairs)):
        start_idx = int(torch.argmax(start_scores[row]))
        end_idx = max(start_idx, int(torch.argmax(end_scores[row])))
        answer_tokens = inputs["input_ids"][row][start_idx:end_idx + 1]
        answers.append({
            "answer": tokenizer.decode(answer_tokens, skip_special_tokens=True),
            "confidence": (float(start_probs[row, start_idx]) + float(end_probs[row, end_idx])) / 2,
            "start_position": start_idx,
            "end_position": end_idx,
        })
    return answers


def length_batches(lengths: List[int], size: int, budget: int) -> List[List[int]]:
    """Indices into ``lengths`` grouped into forward passes: similar lengths together, shortest first.

    A pass holds at most ``size`` items, and at most ``budget`` once every item
    is padded to the pass's longest, so one long text neither pads a pass of
    short ones nor makes a huge one; an item over the budget goes alone.
    """
    batches: List[List[int]] = []
    for i in sorted(range(len(lengths)), key=lambda i: lengths[i]):
        batch = batches[-1] if batches else None
        # Sorted, so lengths[i] is the longest in the pass once added.
        if batch and len(batch) < size and (len(batch) + 1) * lengths[i] <= budget:
            batch.append(i)
        else:
            batches.append([i])
    return batches


def text_streaming(tokenizer: Any, on_text: Optional[Callable[[str], Any]]) -> Dict[str, Any]:
    """generate() arguments that hand ``on_text`` each new piece of text as it is
    produced, and stop generating once it returns False. Empty without ``on_text``.

    ``on_text`` is called from the worker thread running generate().
    """
    if on_text is None:
        return {}
    import torch
    from transformers import StoppingCriteria, StoppingCriteriaList, TextStreamer

    class Streamer(TextStreamer):
        keep_going = True

        def on_finalized_text(self, text: str, stream_end: bool = False) -> None:
            if text and self.keep_going:
                self.keep_going = on_text(text) is not False

    class StopWhenAsked(StoppingCriteria):
        def __call__(self, input_ids, scores, **kwargs):
            return torch.full((input_ids.shape[0],), not streamer.keep_going, dtype=torch.bool, device=input_ids.device)

    streamer = Streamer(tokenizer, skip_prompt=True, skip_special_tokens=True)
    return {"streamer": streamer, "stopping_criteria": StoppingCriteriaList([StopWhenAsked()])}


def with_stop_strings(generate_kwargs: Dict[str, Any], tokenizer: Any, stop: Optional[List[str]]) -> Dict[str, Any]:
    """``generate_kwargs`` (text_streaming()'s, say) plus a criterion that stops at any of ``stop``.

    The text may still end with the stop string itself; the route cuts it.
    Without ``stop`` the arguments are returned unchanged.
    """
    if not stop:
        return generate_kwargs
    from transformers import StoppingCriteriaList
    from transformers.generation import StopStringCriteria

    criteria = generate_kwargs.get("stopping_criteria")
    if criteria is None:
        criteria = generate_kwargs["stopping_criteria"] = StoppingCriteriaList()
    criteria.append(StopStringCriteria(tokenizer, list(stop)))
    return generate_kwargs


def fit_max_length(tokenizer: Any, model: Any) -> None:
    """Cap the tokenizer's length at the model's position embeddings: tokenizers sometimes report a larger
    (or sentinel) limit, and truncation relies on it."""
    positions = getattr(model.config, "max_position_embeddings", None)
    if positions:
        tokenizer.model_max_length = min(tokenizer.model_max_length, positions)


def generated_text(decoder: Any, sequence: Any, prompt_length: int) -> GeneratedText:
    """The text generate() added after the prompt, decoded by ``decoder`` (a tokenizer or processor), with counts."""
    new_tokens = sequence[prompt_length:]
    return GeneratedText(
        text=decoder.decode(new_tokens, skip_special_tokens=True).strip(),
        prompt_tokens=int(prompt_length),
        completion_tokens=int(new_tokens.shape[0]),
    )


def release_torch_memory() -> None:
    """Run the GC, drop cached device allocations and hand freed heap back to the OS."""
    import gc

    from .memory import trim_heap

    gc.collect()
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
    mps = getattr(torch.backends, "mps", None)
    if mps is not None and mps.is_available() and hasattr(torch, "mps"):
        torch.mps.empty_cache()
    trim_heap()


@dataclass
class ModelInfo:
    """Information about a cached model"""
    model_id: str
    task: str
    device: str
    max_memory: Optional[Dict] = None
    torch_dtype: str = "auto"
    loaded_at: Optional[float] = None
    last_used: Optional[float] = None
    memory_usage: Optional[float] = None


class NullMonitor:
    """Where the manager and cache report model events (loads, evictions, pins).

    This default records nothing; the app passes monitoring.Metrics, which has
    the same method. Requests go to the manager's sinks instead (models/events.py).
    """

    def record_event(self, kind: str, model_key: str, duration_ms: Optional[float] = None,
                     detail: Optional[str] = None) -> None:
        pass


async def _call(fn: Callable, *args, **kwargs) -> Any:
    """Await ``fn`` if it is async, else run it in a worker thread."""
    if inspect.iscoroutinefunction(fn):
        return await fn(*args, **kwargs)
    return await asyncio.to_thread(fn, *args, **kwargs)


@functools.lru_cache(maxsize=None)
def _streams(predict: Callable) -> bool:
    """Whether ``predict`` takes ``on_text`` (by name or through **kwargs)."""
    params = inspect.signature(predict).parameters.values()
    return any(p.name == "on_text" or p.kind is inspect.Parameter.VAR_KEYWORD for p in params)


class BaseModel(ABC):
    """Base class for every model.

    Subclasses write:

    - ``load()``: load weights onto ``self.device`` (``self.dtype`` is the
      requested torch dtype). Every attribute it sets is dropped on unload.
    - ``predict(request)``: answer one request. ``request`` is the task's
      input model and the return value must fit its output model (see
      models/tasks.py; a dict with the same fields is fine). Name ``on_text``
      to stream generated text.
    - ``predict_batch(requests)``: optional. Answer several requests at once
      (in padded forward passes, say), one result per request, in order.
      Without it, a batch runs ``predict()`` on each request in turn.
    - ``unload()``: optional extra cleanup.

    Each may be a plain blocking method (run in a worker thread) or ``async``.
    The manager drives the model through ``start()``, ``run()``,
    ``run_batch()`` and ``stop()``.
    """

    # Tasks this class is registered for; set by @register.
    tasks: Tuple[str, ...] = ()
    # Requests one loaded instance runs at once; more wait their turn.
    max_concurrency = 1
    # What /models/status shows: "in-process", or "server" for a model
    # served by another process (models/servers).
    kind = "in-process"
    # Which of a model server's backends answers, e.g. "ollama: qwen3.5:4b"
    # (see models/model_server.py); None for a model with only one way to run.
    backend: Optional[str] = None
    # Whether dtype "auto" means float16 on MPS. Text generators set it:
    # producing tokens is bound by reading the weights, so half-size weights
    # are up to twice as fast. Scoring and embedding models stay float32.
    half_on_mps = False

    def __init__(
        self,
        model_id: str,
        task: Optional[str] = None,
        device: str = "cpu",
        options: Optional[Dict[str, Any]] = None,
    ):
        self.model_id = model_id
        self.task = task or (self.tasks[0] if self.tasks else "")
        self.device = device
        # Load options from the client, e.g. torch_dtype and max_memory.
        self.options = dict(options or {})
        self._loaded = False
        self._before_load: Dict[str, Any] = {}

    # ------------------------------------------------- written by subclasses
    @abstractmethod
    def load(self) -> None:
        """Load the model. Attributes set here are dropped again on unload."""

    @abstractmethod
    def predict(self, request: Any) -> Any:
        """Answer one request: the task's input model in, its output model (or a dict) out."""

    def predict_batch(self, requests: List[Any]) -> List[Any]:
        """Optional: answer ``requests`` together, one result each, in order (see ``run_batch()``)."""
        raise NotImplementedError

    @property
    def batches(self) -> bool:
        """Whether single requests arriving together should share one predict_batch() call (models/batching.py):
        when the class has one. A class whose predict_batch() gains nothing for some task says so here."""
        return type(self).predict_batch is not BaseModel.predict_batch

    def unload(self) -> None:
        """Optional cleanup, run before the attributes load() set are dropped."""

    # --------------------------------------------------------------- helpers
    @property
    def dtype(self) -> Any:
        """The requested torch dtype ("auto" resolves per device). Imports torch."""
        return resolve_torch_dtype(self.options.get("torch_dtype"), self.device, self.half_on_mps)

    def load_pretrained(self, auto_class: Any, trust_remote_code: bool = False) -> Any:
        """``auto_class.from_pretrained`` for this model, on its device, in eval mode.

        On CUDA accelerate places the weights (within the load's ``max_memory``).
        Elsewhere they load on the CPU and then move: not device_map on MPS,
        because Transformers loads weights from several threads and concurrent
        MPS use crashes the process (see _MPS_LOCK).
        """
        kwargs: Dict[str, Any] = {"dtype": self.dtype, "trust_remote_code": trust_remote_code}
        if self.device == "cuda":
            kwargs["device_map"] = "auto"
            if self.options.get("max_memory"):
                kwargs["max_memory"] = self.options["max_memory"]
        model = auto_class.from_pretrained(self.model_id, **kwargs)
        if self.device != "cuda":
            model = model.to(self.device)
        return model.eval()

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def alive(self) -> bool:
        """False once the model can no longer answer (its server died); the cache then drops it."""
        return True

    @classmethod
    def weights_source(cls, model_id: str) -> Optional[str]:
        """The Hub ID or local path this model loads its files from, or None if it loads none here."""
        return model_id

    async def estimate_bytes(self) -> Optional[int]:
        """Memory this model will need once loaded, asked before loading so the cache can make room.

        The default reads the safetensors headers of ``model_id`` (a local
        path, the Hub cache, or the Hub) and sizes them in ``self.dtype``.
        None means unknown: the cache then counts what loading took.
        """
        from .memory import estimate_bytes

        def element_size() -> Optional[int]:
            try:
                return self.dtype.itemsize
            except Exception:
                return None

        return await asyncio.to_thread(lambda: estimate_bytes(self.model_id, element_size()))

    def get_model_info(self) -> ModelInfo:
        return ModelInfo(
            model_id=self.model_id,
            task=self.task,
            device=self.device,
            max_memory=self.options.get("max_memory"),
            torch_dtype=str(self.options.get("torch_dtype") or "auto"),
        )

    # ------------------------------------------------ called by the manager
    async def start(self) -> None:
        """Run load(). If it fails, undo whatever it got done and re-raise."""
        before = dict(vars(self))
        try:
            await _call(self._on_device(self.load))
        except Exception:
            await self._teardown(before)
            raise
        self._before_load = before
        self._loaded = True

    async def run(self, request: Any, on_text: Optional[Callable[[str], Any]] = None) -> Any:
        """Run predict() on ``request`` and return its result, checked against the task's contract.

        ``request`` must be the task's input model already (the manager
        validates requests before a model is loaded; see
        ``TaskContract.parse_input``). A result that breaks the contract
        raises ContractViolation. Tasks without a contract pass both through.
        ``on_text`` reaches predict() only if it names it.
        """
        if not self._loaded:
            raise RuntimeError(f"Model {self.model_id} is not loaded")
        contract = task_contract(self.task)
        if contract is not None and not isinstance(request, contract.input):
            raise TypeError(f"{self.task} requests must be parsed into {contract.input.__name__} before run()")
        kwargs = {"on_text": on_text} if on_text is not None and _streams(type(self).predict) else {}
        result = await _call(self._on_device(self.predict), request, **kwargs)
        return contract.parse_output(result) if contract is not None else result

    async def run_batch(self, requests: List[Any]) -> List[Any]:
        """run() for several requests: one predict_batch() call if the class has one, else predict() on each in turn.

        The results come back in request order, each checked against the
        task's contract; predict_batch() returning a different number of
        results than it got requests is a ContractViolation.
        """
        if not self._loaded:
            raise RuntimeError(f"Model {self.model_id} is not loaded")
        contract = task_contract(self.task)
        if contract is not None and not all(isinstance(request, contract.input) for request in requests):
            raise TypeError(f"{self.task} requests must be parsed into {contract.input.__name__} before run_batch()")
        if type(self).predict_batch is BaseModel.predict_batch:
            results = [await _call(self._on_device(self.predict), request) for request in requests]
        else:
            results = list(await _call(self._on_device(self.predict_batch), list(requests)))
            if len(results) != len(requests):
                raise ContractViolation(
                    f"{type(self).__name__}.predict_batch() returned {len(results)} results for {len(requests)} requests"
                )
        return [contract.parse_output(result) for result in results] if contract is not None else results

    async def stop(self) -> None:
        """Run unload() and drop everything load() created."""
        if self._loaded:
            await self._teardown(self._before_load)

    async def _teardown(self, before: Dict[str, Any]) -> None:
        try:
            await _call(self._on_device(self.unload))
        except Exception:
            logger.exception("unload() failed for %s", self.model_id)
        # Put the instance back the way it was before load(): attributes it
        # created are deleted and ones it replaced get their old value back.
        # That drops every reference to the weights, so memory can be freed.
        current = vars(self)
        for name in [n for n in current if n not in before]:
            delattr(self, name)
        for name, value in before.items():
            if current.get(name, _MISSING) is not value:
                setattr(self, name, value)
        await _call(self._on_device(release_torch_memory))

    def _on_device(self, fn: Callable) -> Callable:
        """``fn``, made to wait for other models' work when the device is MPS."""
        if self.device != "mps" or inspect.iscoroutinefunction(fn):
            return fn
        return functools.partial(_one_at_a_time, fn)
