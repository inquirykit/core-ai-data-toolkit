"""
The /playground page: a tab per category of task. Chat talks to one model, or
sends the same chat to several side by side to compare their replies; the
other tabs run one model of a task through /inference.

Like /admin, the page (see api/pages.py) needs no key. It talks to the existing
routes (/models/catalog, /tasks, /inference and the OpenAI-compatible ones)
with the client key (API_KEY, or the admin key), and
reads the limits below so it never offers more columns than the model cache
holds; past MAX_MODELS each turn would evict and reload the others.

Saved conversations and prompt presets are kept by playground/store.py, in
the /db tables. A saved conversation can become an eval dataset example (admin
key): the chat up to the last reply as the input, that reply as the reference.
"""

from typing import List, Literal, Optional

from fastapi import APIRouter, Response
from pydantic import BaseModel, ConfigDict, Field

from core.config import Config
from playground import CONVERSATIONS, PRESETS, example_from
from . import deps
from . import responses

router = APIRouter(prefix="/playground", dependencies=deps.CLIENT)


@router.get("/limits", **responses.answering(responses.Limits))
async def limits():
    """What the page needs to size its controls."""
    return {
        "max_models": Config.MAX_MODELS,
        "max_generation_length": Config.MAX_GENERATION_LENGTH,
        "max_input_chars": Config.MAX_INPUT_CHARS,
    }


# ------------------------------------------------------------ conversations
class SavedEntry(BaseModel):
    model_config = ConfigDict(extra="ignore", json_schema_serialization_defaults_required=True)

    role: Literal["user", "assistant"]
    content: str = Field(..., max_length=Config.MAX_INPUT_CHARS)
    model: Optional[str] = Field(None, max_length=256, description="The model that answered (assistant turns)")


class SavedColumn(BaseModel):
    model_config = ConfigDict(extra="ignore", json_schema_serialization_defaults_required=True)

    model: str = Field("", max_length=256)
    log: List[SavedEntry] = Field(default_factory=list, max_length=2000)


class SavedSettings(BaseModel):
    model_config = ConfigDict(extra="ignore", json_schema_serialization_defaults_required=True)

    system: str = Field("", max_length=Config.MAX_INPUT_CHARS)
    temperature: float = Field(0.7, ge=0, le=2)
    max_tokens: int = Field(256, ge=1, le=Config.MAX_GENERATION_LENGTH)


class ConversationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1, max_length=200)
    settings: SavedSettings = Field(default_factory=SavedSettings)
    columns: List[SavedColumn] = Field(..., min_length=1, max_length=16)


class Conversation(ConversationBody):
    """A saved conversation, as GET, POST and PUT answer with it."""
    model_config = ConfigDict(**responses.OPEN, json_schema_serialization_defaults_required=True)

    id: str
    created: float
    updated: float


class ExampleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(..., min_length=1, max_length=64)
    column: int = Field(0, ge=0, description="Which column's chat to use")
    reference: Optional[str] = Field(None, max_length=Config.MAX_INPUT_CHARS,
                                     description="The expected answer; the column's last reply when left out")


@router.get("/conversations", **responses.answering(responses.Conversations))
async def list_conversations(saved: deps.Playground):
    """Saved conversations, most recently updated first (titles and models; GET one for its turns)."""
    return {"conversations": await saved.conversations()}


@router.post("/conversations", **responses.answering(Conversation), status_code=201)
async def create_conversation(saved: deps.Playground, body: ConversationBody):
    """Save a conversation."""
    return await saved.create(CONVERSATIONS, body.model_dump())


@router.get("/conversations/{conversation_id}", **responses.answering(Conversation))
async def get_conversation(saved: deps.Playground, conversation_id: str):
    """A saved conversation, with its turns."""
    return await saved.get(CONVERSATIONS, conversation_id)


@router.put("/conversations/{conversation_id}", **responses.answering(Conversation))
async def replace_conversation(saved: deps.Playground, conversation_id: str, body: ConversationBody):
    """Replace a saved conversation (its turns, title and settings); 404 if it was deleted."""
    return await saved.replace(CONVERSATIONS, conversation_id, body.model_dump())


@router.delete("/conversations/{conversation_id}", status_code=204, response_class=Response)
async def delete_conversation(saved: deps.Playground, conversation_id: str):
    """Delete a saved conversation."""
    await saved.delete(CONVERSATIONS, conversation_id)
    return Response(status_code=204)


@router.post("/conversations/{conversation_id}/example", **responses.answering(responses.ExampleAdded), dependencies=deps.ADMIN)
async def conversation_to_example(saved: deps.Playground, evals: deps.Evals, conversation_id: str, body: ExampleBody):
    """Add one column's chat to an eval dataset as an example (admin key)."""
    conversation = await saved.get(CONVERSATIONS, conversation_id)
    example = example_from(conversation, body.column, body.reference)
    added = await evals.add_examples(body.dataset_id, [example])
    return {"added": added, "dataset_id": body.dataset_id, "example": example}


# ------------------------------------------------------------------ presets
class PresetBody(SavedSettings):
    """A named system prompt with its sampling settings."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)


@router.get("/presets", **responses.answering(responses.Presets))
async def list_presets(saved: deps.Playground):
    """Saved prompt presets (a system prompt with its sampling settings), by name."""
    return {"presets": await saved.presets()}


@router.post("/presets", **responses.answering(responses.Preset), status_code=201)
async def create_preset(saved: deps.Playground, body: PresetBody):
    """Save a preset."""
    return await saved.create(PRESETS, body.model_dump())


@router.put("/presets/{preset_id}", **responses.answering(responses.Preset))
async def replace_preset(saved: deps.Playground, preset_id: str, body: PresetBody):
    """Replace a saved preset; 404 if it was deleted."""
    return await saved.replace(PRESETS, preset_id, body.model_dump())


@router.delete("/presets/{preset_id}", status_code=204, response_class=Response)
async def delete_preset(saved: deps.Playground, preset_id: str):
    """Delete a saved preset."""
    await saved.delete(PRESETS, preset_id)
    return Response(status_code=204)
