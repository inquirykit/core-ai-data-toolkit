"""Setup, models and tasks: /tasks, /models/*, /v1/models, /playground/limits."""

from typing import Dict, List, Literal, Optional

from pydantic import ConfigDict

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
