"""
API routes for the model service
"""

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query
from errors import NotFound, Refused
from . import deps, responses
from .models import (
    BatchInferenceRequest,
    BatchInferenceResult,
    InferenceRequest,
    InferenceResult,
    LoadPlan,
    ModelCatalog,
    ModelLoadRequest,
    PinRequest,
    VisionLanguageRequest,
)
from models import ModelManager, registry, tasks
from config import Config

logger = logging.getLogger(__name__)

router = APIRouter(dependencies=deps.CLIENT)


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
    """Run vision-language inference on a FastVLM model"""
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
    """Every task with the JSON Schema of its input and result (null for a task without a contract)

    ``limits`` are the caps the schemas cannot show (media sizes are checked
    by validators), so clients can check a file before sending it.
    """
    contracts = tasks.contracts()
    names = sorted(set(registry.known_tasks()) | set(contracts))
    untyped = {"description": "", "input": None, "output": None}
    return {
        "tasks": {name: contracts[name].describe() if name in contracts else untyped for name in names},
        "limits": {
            "max_input_chars": Config.MAX_INPUT_CHARS,
            "max_generation_length": Config.MAX_GENERATION_LENGTH,
            "max_media_bytes": Config.MAX_MEDIA_BYTES,
            "max_media_parts": Config.MAX_MEDIA_PARTS,
            "max_audio_bytes": Config.MAX_AUDIO_BYTES,
            "max_embedding_inputs": Config.MAX_EMBEDDING_INPUTS,
        },
    }


@router.post("/models/load", **responses.answering(responses.ModelMessage), dependencies=deps.ADMIN)
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


@router.delete("/models/clear", **responses.answering(responses.ModelMessage), dependencies=deps.ADMIN)
async def clear_all_models(manager: deps.Manager):
    """Clear all models from cache"""
    await manager.clear_all_models()
    return {"message": "All models cleared from cache"}


@router.delete("/models/{model_id:path}/{task}", **responses.answering(responses.ModelMessage), dependencies=deps.ADMIN)
async def unload_model(manager: deps.Manager, model_id: str, task: str):
    """Unload a specific model from cache (model_id may contain a slash); 404 if it is not loaded"""
    await manager.unload_model(model_id, task)
    return {"message": f"Model {model_id}:{task} unloaded successfully"}


@router.put("/models/{model_id:path}/{task}/pin", **responses.answering(responses.ModelMessage), dependencies=deps.ADMIN)
async def pin_model(manager: deps.Manager, model_id: str, task: str, request: PinRequest):
    """Pin a loaded model so it is never evicted, or unpin it (model_id may contain a slash); 404 if it is not loaded"""
    manager.set_pinned(model_id, task, request.pinned)
    return {"message": f"Model {model_id}:{task} {'pinned' if request.pinned else 'unpinned'}"}


@router.get("/models/status", dependencies=deps.ADMIN)
async def get_models_status(manager: deps.Manager):
    """Cache contents, memory use, loads pending, and per model its queue and expected wait"""
    return manager.get_cache_status()


@router.get("/models/list", **responses.answering(responses.Catalog))
async def list_available_models(manager: deps.Manager):
    """List available models for different tasks"""
    return manager.get_available_models(Config.ALLOWED_MODELS)


@router.get("/models/catalog", response_model=ModelCatalog)
async def model_catalog(manager: deps.Manager):
    """Every task with its models, cards, status and what is on disk: what the model browser shows"""
    return {"tasks": await manager.get_catalog(Config.ALLOWED_MODELS)}


@router.get("/models/estimate", response_model=LoadPlan, dependencies=deps.ADMIN)
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
    """Every registered model class with its task, IDs and patterns"""
    return {"model_types": manager.get_available_model_types()}
