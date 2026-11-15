"""
The native endpoints: /inference (one input or a batch) and /vision-language
run a model; /tasks and /models/* list, load, pin, unload and estimate models.

``POST /inference`` takes ``{model_id, task, input}`` and returns the task's
result; ``POST /inference/batch`` takes ``{model_id, task, inputs}`` and
returns ``results``, one per input. These shapes come from the task contracts
in models/contracts.py: there is one request and one response variant per task,
chosen by ``task``, so the OpenAPI docs show exactly what each task takes and
returns. A task without a contract gets the untyped variant, whose input and
result are anything.
"""

import asyncio
import logging
from typing import Annotated, Any, Dict, Generic, List, Literal, Optional, TypeVar, Union

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, create_model

from core.config import Config
from core.errors import NotFound, Refused
from models import ModelManager, registry, contracts
from . import deps, responses

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
    revision: Optional[str] = Field(None, description="The Hub commit of the weights that answered; null when they "
                                                      "are not from the Hub cache")
    trace_id: Optional[str] = Field(None, description="This request's trace (GET /admin/traces/{id}), when tracing is on")


class BatchInferenceResponse(BaseModel, Generic[T]):
    model_config = ConfigDict(protected_namespaces=())

    task: str
    results: List[T] = Field(..., description="One result per input, in the same order")
    model_used: str
    processing_time: float
    cold_start: bool = Field(False, description="True when this request had to load the model first")
    backend: Optional[str] = Field(None, description="Which backend of a model server answered, e.g. \"ollama: qwen3.5:4b\"")
    revision: Optional[str] = Field(None, description="The Hub commit of the weights that answered; null when they "
                                                      "are not from the Hub cache")
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


def _request_variant(contract: contracts.TaskContract, batch: bool) -> type:
    inputs = {"inputs": (List[contract.input], Inputs)} if batch else {"input": (contract.input, ...)}
    return create_model(
        f"{_class_name(contract.name)}{'Batch' if batch else ''}InferenceRequest",
        __config__=ConfigDict(extra="forbid", protected_namespaces=()),
        __doc__=contract.description or None,
        model_id=(str, ModelId),
        task=(Literal[contract.name], ...),
        **inputs,
    )


def _response_variant(contract: contracts.TaskContract, batch: bool) -> type:
    return create_model(
        f"{_class_name(contract.name)}{'Batch' if batch else ''}InferenceResponse",
        __base__=(BatchInferenceResponse if batch else InferenceResponse)[contract.output],
        task=(Literal[contract.name], ...),
    )


def build_inference_models(batch: bool = False) -> tuple:
    """(request type, response type) from the task contracts defined so far; /inference/batch's with ``batch``."""
    # Model files may define their own tasks; import them first.
    registry.discover()
    defined = contracts.defined()
    # The untyped variants' task is any string but a contract's: in the schema too (``not``), so a typed body fits
    # exactly one member of the union and a client's validator agrees with ours.
    other_task = (str, Field(..., min_length=1, max_length=64, json_schema_extra={"not": {"enum": sorted(defined)}}))
    untyped_request = create_model(
        f"Untyped{'Batch' if batch else ''}InferenceRequest",
        __base__=UntypedBatchInferenceRequest if batch else UntypedInferenceRequest, task=other_task,
    )
    untyped_response = create_model(
        f"Untyped{'Batch' if batch else ''}InferenceResponse",
        __base__=(BatchInferenceResponse if batch else InferenceResponse)[Any], task=other_task,
    )
    request = _by_task({name: _request_variant(c, batch) for name, c in defined.items()}, untyped_request)
    response = _by_task({name: _response_variant(c, batch) for name, c in defined.items()}, untyped_response)
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


logger = logging.getLogger(__name__)

router = APIRouter(dependencies=deps.CLIENT)
# This module's routes that only the admin's key opens.
admin_router = APIRouter(dependencies=deps.ADMIN)


def _resolve_image_path(image_path: Optional[str]) -> Optional[str]:
    """Confine user-supplied image paths to Config.IMAGE_DIR."""
    if image_path is None:
        return None
    base = Config.IMAGE_DIR
    candidate = (base / image_path).resolve()
    try:
        candidate.relative_to(base)
    except ValueError:
        raise Refused("image_path must be inside the configured IMAGE_DIR")
    if not candidate.is_file():
        raise NotFound("Image not found")
    return str(candidate)


async def _infer(manager: ModelManager, model_id: str, task: str, request: Any) -> Dict[str, Any]:
    """Run ``request`` and wrap the result in the inference response."""
    output = await manager.run_inference(model_id, task, request)
    return {"task": task, **output}


@router.post("/inference", response_model=InferenceResult)
async def run_inference(manager: deps.Manager, request: InferenceRequest):
    """Run a model. ``input`` and ``result`` follow the task's contract (see GET /tasks)."""
    return await _infer(manager, request.model_id, request.task, request.input)


@router.post("/inference/batch", response_model=BatchInferenceResult)
async def run_batch_inference(manager: deps.Manager, request: BatchInferenceRequest):
    """Run a model on several inputs at once. Each of ``inputs`` and ``results`` follows the task's contract, in the
    same order. Models that can run several to a forward pass (the generic classifiers and question-answering
    models) do; others answer them one by one."""
    output = await manager.run_batch(request.model_id, request.task, request.inputs)
    return {"task": request.task, **output}


@router.post("/vision-language", response_model=InferenceResult)
async def run_vision_language_inference(manager: deps.Manager, request: VisionLanguageRequest):
    """Run vision-language inference on a FastVLM model: the same as POST /inference with task vision-language, under
    older field names. ``image_path`` is relative to IMAGE_DIR."""
    vision_input = {
        "text": request.input_text,
        "image": _resolve_image_path(request.image_path),
        "max_length": request.max_new_tokens,
        "temperature": request.temperature,
        "top_p": request.top_p,
        "do_sample": request.do_sample,
    }
    return await _infer(manager, request.model_id, request.task, vision_input)


@router.get("/tasks", **responses.answering(responses.Tasks))
async def list_tasks():
    """Every task with the JSON Schema of its input and result (null for a task without a contract), its category,
    label and the task it is a variant of

    ``limits`` are the caps the schemas cannot show (media sizes are checked
    by validators), so clients can check a file before sending it.
    """
    names = contracts.ordered(set(registry.known_tasks()) | set(contracts.defined()))
    return {
        "tasks": {name: contracts.describe(name) for name in names},
        "limits": {
            "max_input_chars": Config.MAX_INPUT_CHARS,
            "max_generation_length": Config.MAX_GENERATION_LENGTH,
            "max_media_bytes": Config.MAX_MEDIA_BYTES,
            "max_media_parts": Config.MAX_MEDIA_PARTS,
            "max_audio_bytes": Config.MAX_AUDIO_BYTES,
            "max_embedding_inputs": Config.MAX_EMBEDDING_INPUTS,
        },
    }


@admin_router.post("/models/load", **responses.answering(responses.ModelMessage))
async def load_model(manager: deps.Manager, request: ModelLoadRequest):
    """Load a model into cache (``pin`` keeps it there until it is unloaded explicitly)"""
    await manager.load_model(
        request.model_id,
        request.task,
        pin=request.pin,
        torch_dtype=request.torch_dtype,
        max_memory=request.max_memory,
    )
    return {"message": f"Model {request.model_id} loaded successfully"}


@admin_router.delete("/models/clear", **responses.answering(responses.ModelMessage))
async def clear_all_models(manager: deps.Manager):
    """Clear all models from cache"""
    await manager.clear_all_models()
    return {"message": "All models cleared from cache"}


@admin_router.delete("/models/{model_id:path}/{task}", **responses.answering(responses.ModelMessage))
async def unload_model(manager: deps.Manager, model_id: str, task: str):
    """Unload a specific model from cache (model_id may contain a slash); 404 if it is not loaded"""
    await manager.unload_model(model_id, task)
    return {"message": f"Model {model_id}:{task} unloaded successfully"}


@admin_router.put("/models/{model_id:path}/{task}/pin", **responses.answering(responses.ModelMessage))
async def pin_model(manager: deps.Manager, model_id: str, task: str, request: PinRequest):
    """Pin a loaded model so it is never evicted, or unpin it (model_id may contain a slash); 404 if it is not loaded"""
    manager.set_pinned(model_id, task, request.pinned)
    return {"message": f"Model {model_id}:{task} {'pinned' if request.pinned else 'unpinned'}"}


@admin_router.get("/models/status")
async def get_models_status(manager: deps.Manager):
    """Cache contents, memory use, loads pending, and per model its queue and expected wait"""
    return manager.get_cache_status()


@router.get("/models/list", **responses.answering(responses.Catalog))
async def list_available_models(manager: deps.Manager):
    """List available models for different tasks (only those in ALLOWED_MODELS when it is set, and those the operator
    chose here: model servers' entries, models jobs here trained)."""
    return await asyncio.to_thread(manager.get_available_models, Config.ALLOWED_MODELS)


@router.get("/models/catalog", response_model=responses.ModelCatalog)
async def model_catalog(manager: deps.Manager):
    """Every task with its models, cards, status and what is on disk: what the model browser shows. Tasks come in the
    order they were defined, with their category and label; only ALLOWED_MODELS when it is set. What is on disk is read
    from the local Hub cache only."""
    return {"tasks": await manager.get_catalog(Config.ALLOWED_MODELS)}


@admin_router.get("/models/estimate", response_model=responses.LoadPlan)
async def estimate_load(
    manager: deps.Manager,
    model_id: str = Query(..., min_length=1, max_length=256),
    task: str = Query(..., min_length=1, max_length=64),
    torch_dtype: str = Query("auto", pattern=r"^(auto|float16|bfloat16|float32)$"),
):
    """The memory loading a model would take and what the cache would unload for it; nothing is loaded"""
    return await manager.plan_load(model_id, task, torch_dtype=torch_dtype)


@router.get("/models/types", **responses.answering(responses.ModelTypes))
async def list_model_types(manager: deps.Manager):
    """Every registered model class with its task, IDs and patterns, and whether it is a fallback (serving any ID no
    other class claims)."""
    return {"model_types": manager.get_available_model_types()}
