"""/playground: saved conversations and presets."""

from typing import List


from .common import Json, Out


class ConversationSummary(Out):
    id: str
    title: str
    created: float
    updated: float
    models: List[str]
    turns: int


class Conversations(Out):
    conversations: List[ConversationSummary]


class Preset(Out):
    id: str
    name: str
    system: str
    temperature: float
    max_tokens: int
    created: float
    updated: float


class Presets(Out):
    presets: List[Preset]


class ExampleAdded(Out):
    added: int
    dataset_id: str
    example: Json
