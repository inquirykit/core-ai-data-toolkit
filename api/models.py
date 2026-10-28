"""
Pydantic models for API requests and responses

``POST /inference`` takes ``{model_id, task, input}`` and returns the task's
result; ``POST /inference/batch`` takes ``{model_id, task, inputs}`` and
returns ``results``, one per input. These shapes come from the task contracts
in models/tasks.py: there is one request and one response variant per task,
chosen by ``task``, so the OpenAPI docs show exactly what each task takes and
returns. A task without a contract gets the untyped variant, whose input and
result are anything.
"""

from typing import Annotated, Any, Dict, Generic, List, Literal, Optional, TypeVar, Union

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, create_model

from config import Config
from models import registry, tasks
from .responses import ModelStatus

UNTYPED = "untyped"

ModelId = Field(..., min_length=1, max_length=256)
Inputs = Field(..., min_length=1, max_length=Config.MAX_BATCH_INPUTS, description="Run together; results come back in this order")

T = TypeVar("T")


class UntypedInferenceRequest(BaseModel):
    """A task without a contract: ``input`` reaches the model as sent."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model_id: str = ModelId
    task: str = Field(..., min_length=1, max_length=64)
    input: Any


class UntypedBatchInferenceRequest(BaseModel):
    """A task without a contract: each of ``inputs`` reaches the model as sent."""

    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    model_id: str = ModelId
    task: str = Field(..., min_length=1, max_length=64)
    inputs: List[Any] = Inputs


class InferenceResponse(BaseModel, Generic[T]):
    model_config = ConfigDict(protected_namespaces=())

    task: str
    result: T
    model_used: str
    processing_time: float
    cold_start: bool = Field(False, description="True when this request had to load the model first")
    backend: Optional[str] = Field(None, description="Which backend of a model server answered, e.g. \"ollama: qwen3.5:4b\"")
    trace_id: Optional[str] = Field(None, description="This request's trace (GET /admin/traces/{id}), when tracing is on")


class BatchInferenceResponse(BaseModel, Generic[T]):
    model_config = ConfigDict(protected_namespaces=())

    task: str
    results: List[T] = Field(..., description="One result per input, in the same order")
    model_used: str
    processing_time: float
    cold_start: bool = Field(False, description="True when this request had to load the model first")
    backend: Optional[str] = Field(None, description="Which backend of a model server answered, e.g. \"ollama: qwen3.5:4b\"")
    trace_ids: List[str] = Field(
        default_factory=list, description="Each input's trace (GET /admin/traces/{id}), in order, when tracing is on"
    )


def _class_name(task: str) -> str:
    return "".join(part.capitalize() for part in task.replace("_", "-").split("-"))


def _by_task(variants: Dict[str, type], untyped: type) -> Any:
    """A union of ``variants`` (task -> model) plus ``untyped``, picked by the ``task`` field."""

    def pick(value: Any) -> str:
        task = value.get("task") if isinstance(value, dict) else getattr(value, "task", None)
        # A task that is no string (a list, say) is nobody's: the untyped variant then refuses it as a 422.
        return task if isinstance(task, str) and task in variants else UNTYPED

    members = [Annotated[model, Tag(task)] for task, model in variants.items()]
    members.append(Annotated[untyped, Tag(UNTYPED)])
    return Annotated[Union[tuple(members)], Discriminator(pick)]


def _request_variant(contract: tasks.TaskContract, batch: bool) -> type:
    inputs = {"inputs": (List[contract.input], Inputs)} if batch else {"input": (contract.input, ...)}
    return create_model(
        f"{_class_name(contract.name)}{'Batch' if batch else ''}InferenceRequest",
        __config__=ConfigDict(extra="forbid", protected_namespaces=()),
        __doc__=contract.description or None,
        model_id=(str, ModelId),
        task=(Literal[contract.name], ...),
        **inputs,
    )


def _response_variant(contract: tasks.TaskContract, batch: bool) -> type:
    return create_model(
        f"{_class_name(contract.name)}{'Batch' if batch else ''}InferenceResponse",
        __base__=(BatchInferenceResponse if batch else InferenceResponse)[contract.output],
        task=(Literal[contract.name], ...),
    )


def build_inference_models(batch: bool = False) -> tuple:
    """(request type, response type) from the task contracts defined so far; /inference/batch's with ``batch``."""
    # Model files may define their own tasks; import them first.
    registry.discover()
    contracts = tasks.contracts()
    # The untyped variants' task is any string but a contract's: in the schema too (``not``), so a typed body fits
    # exactly one member of the union and a client's validator agrees with ours.
    other_task = (str, Field(..., min_length=1, max_length=64, json_schema_extra={"not": {"enum": sorted(contracts)}}))
    untyped_request = create_model(
        f"Untyped{'Batch' if batch else ''}InferenceRequest",
        __base__=UntypedBatchInferenceRequest if batch else UntypedInferenceRequest, task=other_task,
    )
    untyped_response = create_model(
        f"Untyped{'Batch' if batch else ''}InferenceResponse",
        __base__=(BatchInferenceResponse if batch else InferenceResponse)[Any], task=other_task,
    )
    request = _by_task({name: _request_variant(c, batch) for name, c in contracts.items()}, untyped_request)
    response = _by_task({name: _response_variant(c, batch) for name, c in contracts.items()}, untyped_response)
    return request, response


InferenceRequest, InferenceResult = build_inference_models()
BatchInferenceRequest, BatchInferenceResult = build_inference_models(batch=True)


class VisionLanguageRequest(BaseModel):
    """/vision-language's own field names; the route turns them into the vision-language input."""

    model_config = ConfigDict(protected_namespaces=())

    model_id: str = Field("apple/FastVLM-1.5B", min_length=1, max_length=256)
    task: str = "vision-language"
    input_text: str = Field(..., min_length=1, max_length=Config.MAX_INPUT_CHARS)
    image_path: Optional[str] = Field(None, max_length=1024, description="Path relative to IMAGE_DIR")
    max_new_tokens: int = Field(128, ge=1, le=Config.MAX_GENERATION_LENGTH)
    temperature: float = Field(0.7, gt=0.0, le=5.0)
    top_p: float = Field(0.9, gt=0.0, le=1.0)
    do_sample: bool = True


class ModelLoadRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_id: str = Field(..., min_length=1, max_length=256)
    task: str = Field(..., min_length=1, max_length=64)
    torch_dtype: Optional[str] = Field("auto", pattern=r"^(auto|float16|bfloat16|float32)$")
    max_memory: Optional[Dict[str, Any]] = None
    pin: bool = Field(False, description="Keep it loaded: never evicted, only unloaded explicitly")


class PinRequest(BaseModel):
    pinned: bool = Field(..., description="true: never evict it; false: let the cache evict it again")


# ------------------------------------------------------------ model browser


class CatalogModel(BaseModel):
    """One model as the model browser shows it."""

    id: str
    description: str = Field("", description="One line from the model's card; empty without one")
    tags: List[str] = Field(default_factory=list, description="Labels to filter by, from the card")
    served_by: Optional[str] = Field(None, description="The class that serves it; null if none would")
    kind: Optional[str] = Field(None, description='"in-process", or "server" for a model with its own server')
    generic: bool = Field(False, description="Served by a generic class that takes any ID for the task")
    status: ModelStatus
    on_disk_gb: Optional[float] = Field(None, description="Weights in the local Hub cache; null if not downloaded")


class CatalogTask(BaseModel):
    task: str
    description: str = Field("", description="What the task does, from its contract")
    any_id: bool = Field(..., description="Whether IDs beyond the listed ones work (any Hub ID)")
    patterns: List[str] = Field(..., description="ID patterns its classes claim, e.g. vega-*")
    models: List[CatalogModel]


class ModelCatalog(BaseModel):
    tasks: List[CatalogTask]


class LoadPlan(BaseModel):
    """What loading a model would take, and what the cache would do for it now (nothing is loaded)."""

    model_config = ConfigDict(protected_namespaces=())

    model_id: str
    task: str
    estimate_gb: Optional[float] = Field(None, description="Memory it would need; null when it cannot be told")
    outcome: Literal["loaded", "fits", "evicts", "waits", "too_big"] = Field(
        ...,
        description="loaded: already is. fits: room as is. evicts: idle models unload first. "
        "waits: it waits for busy models. too_big: it does not fit even alone (loading is still tried).",
    )
    evicts: List[str] = Field(..., description="Model keys that would be unloaded, least recently used first")
    reason: Optional[Literal["max_models", "max_memory_gb", "free_memory"]] = None
    memory_used_gb: float
    max_memory_gb: float
    free_memory_gb: Optional[float]
    loaded_models: int
    max_models: int
