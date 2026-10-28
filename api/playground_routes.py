"""
The /playground page: a tab per category of task. Chat talks to one model, or
sends the same chat to several side by side to compare their replies; the
other tabs run one model of a task through /inference.

Like /admin, the page (see api/pages.py) needs no key. It talks to the existing
routes (/models/catalog, /tasks, /inference and the OpenAI-compatible ones)
with the client key (API_KEY, or the admin key), and
reads the limits below so it never offers more columns than the model cache
holds; past MAX_MODELS each turn would evict and reload the others.

Saved conversations and prompt presets live in the ``playground`` namespace
of the /db tables (vectorstore/tables.py), so they survive the browser and are
browsable in the dashboard's Data section. A saved conversation can become an
eval dataset example (admin key): the chat up to the last reply as the input,
that reply as the reference.
"""

import time
import uuid
from typing import Annotated, Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from config import Config
from models.chat import ChatMessage, build_prompt
from vectorstore.tables import NotFound as RowNotFound, TableSchema, TableStore
from errors import NotFound, Refused, Unavailable
from . import deps
from . import responses

router = APIRouter(prefix="/playground", dependencies=deps.CLIENT)

NAMESPACE = "playground"
CONVERSATIONS = "conversations"
PRESETS = "presets"
# A namespace the service keeps (api/app.py declares it): admin-only and read-only through /db, so a row written
# there cannot break the lists. It was an ordinary namespace until 2026-10, so the service adopts that one.
OWNER = "saved chats and presets: change them in the playground"
TABLES = {CONVERSATIONS: TableSchema(), PRESETS: TableSchema()}
MAX_SAVED = 1000


@router.get("/limits", **responses.answering(responses.Limits))
async def limits():
    """What the page needs to size its controls."""
    return {
        "max_models": Config.MAX_MODELS,
        "max_generation_length": Config.MAX_GENERATION_LENGTH,
        "max_input_chars": Config.MAX_INPUT_CHARS,
    }


# ------------------------------------------------------------------ storage
async def _tables(request: Request) -> TableStore:
    """The tables; the playground namespace is made with the others the service keeps. Not deps.Tables: while the
    database is off, the 503 says what saving needs."""
    tables = request.app.state.tables
    if tables is None:
        raise Unavailable("Saving needs the database: ARCADEDB_PATH is empty")
    return tables


Saved = Annotated[TableStore, Depends(_tables)]


async def _rows(tables: TableStore, table: str) -> List[Dict[str, Any]]:
    try:
        rows, _ = await tables.list(NAMESPACE, table, limit=MAX_SAVED)
    except RowNotFound:
        return []
    return rows


async def _row(tables: TableStore, table: str, row_id: str) -> Dict[str, Any]:
    try:
        return await tables.get(NAMESPACE, table, row_id)
    except RowNotFound:
        raise NotFound(f"No saved {table[:-1]} {row_id!r}")


def _check_id(value: str) -> str:
    if not value or len(value) > 64 or "/" in value:
        raise Refused("bad id")
    return value


async def _create(tables: TableStore, table: str, body: BaseModel) -> Dict[str, Any]:
    now = time.time()
    row = {"id": uuid.uuid4().hex[:16], "created": now, "updated": now, **body.model_dump()}
    await tables.upsert(NAMESPACE, table, row)
    return row


async def _replace(tables: TableStore, table: str, row_id: str, body: BaseModel) -> Dict[str, Any]:
    """Replace a saved row's fields with ``body``'s; 404 if it was deleted."""
    current = await _row(tables, table, _check_id(row_id))
    row = {**current, **body.model_dump(), "id": row_id, "updated": time.time()}
    await tables.upsert(NAMESPACE, table, row)
    return row


async def _delete(tables: TableStore, table: str, row_id: str) -> Response:
    await tables.delete(NAMESPACE, table, [_check_id(row_id)])
    return Response(status_code=204)


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


def _summary(row: Dict[str, Any]) -> Dict[str, Any]:
    columns = row.get("columns") or []
    first = columns[0]["log"] if columns else []
    return {
        "id": row["id"], "title": row["title"], "created": row["created"], "updated": row["updated"],
        "models": [c.get("model") or "" for c in columns],
        "turns": sum(1 for e in first if e.get("role") == "user"),
    }


@router.get("/conversations", **responses.answering(responses.Conversations))
async def list_conversations(tables: Saved):
    """Saved conversations, most recently updated first (titles and models; GET one for its turns)."""
    rows = await _rows(tables, CONVERSATIONS)
    rows.sort(key=lambda r: r.get("updated") or 0, reverse=True)
    return {"conversations": [_summary(r) for r in rows]}


@router.post("/conversations", **responses.answering(Conversation), status_code=201)
async def create_conversation(tables: Saved, body: ConversationBody):
    return await _create(tables, CONVERSATIONS, body)


@router.get("/conversations/{conversation_id}", **responses.answering(Conversation))
async def get_conversation(tables: Saved, conversation_id: str):
    return await _row(tables, CONVERSATIONS, _check_id(conversation_id))


@router.put("/conversations/{conversation_id}", **responses.answering(Conversation))
async def replace_conversation(tables: Saved, conversation_id: str, body: ConversationBody):
    """Replace a saved conversation (its turns, title and settings); 404 if it was deleted."""
    return await _replace(tables, CONVERSATIONS, conversation_id, body)


@router.delete("/conversations/{conversation_id}", status_code=204, response_class=Response)
async def delete_conversation(tables: Saved, conversation_id: str):
    return await _delete(tables, CONVERSATIONS, conversation_id)


def example_from(conversation: Dict[str, Any], column: int, reference: Optional[str]) -> Dict[str, Any]:
    """An eval example from one column: the chat up to its last reply as the input, that reply as the reference.

    The input holds the chat as ``messages`` and as the plain transcript
    (``text``), the two forms the text-generation task takes, so any model
    can run it. Without a reply, the whole chat is the input and the
    reference is what the caller gave.
    """
    columns = conversation.get("columns") or []
    if not 0 <= column < len(columns):
        raise Refused(f"column must be 0 to {len(columns) - 1}")
    log = [e for e in columns[column].get("log") or [] if e.get("content")]
    replies = [i for i, e in enumerate(log) if e.get("role") == "assistant"]
    turns, default_reference = (log[: replies[-1]], log[replies[-1]]["content"]) if replies else (log, None)
    system = (conversation.get("settings") or {}).get("system") or ""
    messages = ([{"role": "system", "content": system}] if system.strip() else []) \
        + [{"role": e["role"], "content": e["content"]} for e in turns]
    if not messages or messages[-1]["role"] != "user":
        raise Refused("The column needs a user turn for the model to answer")
    return {
        "input": {"text": build_prompt([ChatMessage(**m) for m in messages]), "messages": messages},
        "reference": reference if reference is not None else default_reference,
        "task": "text-generation",
        "tags": [t for t in ("playground", columns[column].get("model") or "") if t][:16],
    }


@router.post("/conversations/{conversation_id}/example", **responses.answering(responses.ExampleAdded), dependencies=deps.ADMIN)
async def conversation_to_example(tables: Saved, evals: deps.Evals, conversation_id: str, body: ExampleBody):
    """Add one column's chat to an eval dataset as an example (admin key)."""
    conversation = await _row(tables, CONVERSATIONS, _check_id(conversation_id))
    example = example_from(conversation, body.column, body.reference)
    added = await evals.add_examples(body.dataset_id, [example])
    return {"added": added, "dataset_id": body.dataset_id, "example": example}


# ------------------------------------------------------------------ presets
class PresetBody(SavedSettings):
    """A named system prompt with its sampling settings."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)


@router.get("/presets", **responses.answering(responses.Presets))
async def list_presets(tables: Saved):
    """Saved prompt presets (a system prompt with its sampling settings), by name."""
    rows = await _rows(tables, PRESETS)
    rows.sort(key=lambda r: (r.get("name") or "").lower())
    return {"presets": rows}


@router.post("/presets", **responses.answering(responses.Preset), status_code=201)
async def create_preset(tables: Saved, body: PresetBody):
    return await _create(tables, PRESETS, body)


@router.put("/presets/{preset_id}", **responses.answering(responses.Preset))
async def replace_preset(tables: Saved, preset_id: str, body: PresetBody):
    return await _replace(tables, PRESETS, preset_id, body)


@router.delete("/presets/{preset_id}", status_code=204, response_class=Response)
async def delete_preset(tables: Saved, preset_id: str):
    return await _delete(tables, PRESETS, preset_id)
