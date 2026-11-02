"""Setup, models and tasks: /tasks, /models/*, /v1/models, /playground/limits."""

from typing import Dict, List, Literal, Optional

from pydantic import ConfigDict, Field

from models.contracts import Category

from .common import OPEN, Json, ModelStatus, Out


class SetupStatus(Out):
    needed: bool


class SetupResult(Out):
    env_file: str
    restart_required: bool
    notes: List[str]


class SetupSkipped(Out):
    env_file: str


class ModelMessage(Out):
    """What the routes that load, unload or pin models answer with."""
    message: str


# GET /models/list: task -> model IDs
Catalog = Dict[str, List[str]]


class TaskInfo(Out):
    description: str
    category: Category
    label: str
    # Its entry in the tab's task switch: the task it is a variant of, else itself (as in the catalog)
    group: str
    group_label: str
    # The task this one is the same job for, done by another kind of model (zero-shot for classification)
    variant_of: Optional[str]
    # The JSON Schemas of the task's input and result; null for a task without a contract
    input: Optional[Json]
    output: Optional[Json]


class TaskLimits(Out):
    max_input_chars: int
    max_generation_length: int
    max_media_bytes: int
    max_media_parts: int
    max_audio_bytes: int
    max_embedding_inputs: int


class Tasks(Out):
    """GET /tasks"""
    tasks: Dict[str, TaskInfo]
    limits: TaskLimits


class OpenAIModel(Out):
    id: str
    object: Literal["model"]
    created: int
    owned_by: str
    # Not OpenAI fields
    task: str
    status: ModelStatus


class OpenAIModels(Out):
    """GET /v1/models"""
    object: Literal["list"]
    data: List[OpenAIModel]


class ModelType(Out):
    # "class" is a Python keyword: the field is read from and written as "class".
    class_: str
    task: str
    ids: List[str]
    match: List[str]
    fallback: bool

    model_config = ConfigDict(**OPEN, populate_by_name=True,
                              alias_generator=lambda name: "class" if name == "class_" else name)


class ModelTypes(Out):
    model_types: List[ModelType]


class Limits(Out):
    """GET /playground/limits"""
    max_models: int
    max_generation_length: int
    max_input_chars: int


class CatalogModel(Out):
    """One model as the model browser shows it."""

    id: str
    description: str = Field("", description="One line from the model's card; empty without one")
    tags: List[str] = Field(default_factory=list, description="Labels to filter by, from the card")
    served_by: Optional[str] = Field(None, description="The class that serves it; null if none would")
    kind: Optional[str] = Field(None, description='"in-process", or "server" for a model with its own server')
    generic: bool = Field(False, description="Served by a generic class that takes any ID for the task")
    status: ModelStatus
    on_disk_gb: Optional[float] = Field(None, description="Weights in the local Hub cache; null if not downloaded")


class CatalogTask(Out):
    task: str
    description: str = Field("", description="What the task does, from its contract")
    category: Category = Field(..., description="The tab the playground and the model browser file it under")
    label: str = Field(..., description="Its short name for people; the task's name when it has none")
    group: str = Field(..., description="Its entry in the tab's task switch: the task it is a variant of, else itself")
    group_label: str = Field(..., description="That entry's label")
    any_id: bool = Field(..., description="Whether IDs beyond the listed ones work (any Hub ID)")
    patterns: List[str] = Field(..., description="ID patterns its classes claim, e.g. vega-*")
    models: List[CatalogModel]


class ModelCatalog(Out):
    tasks: List[CatalogTask] = Field(..., description="In the order the tasks were defined; tasks without a contract last")


class LoadPlan(Out):
    """What loading a model would take, and what the cache would do for it now (nothing is loaded)."""

    model_config = ConfigDict(**OPEN, protected_namespaces=())

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
