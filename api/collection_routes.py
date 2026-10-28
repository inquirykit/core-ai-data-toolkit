"""
/collections: documents with vector search, in the embedded ArcadeDB database
(see vectorstore/store.py).

A collection belongs to one embedding model. Adding items embeds their text
with it; searching embeds the query with it and returns the nearest items,
optionally only among those an ArcadeDB SQL ``where`` selects (with
``params`` for its :name placeholders). Each collection also has the typed
fields its creator declared, which items carry and ``where`` can test.

Creating and dropping collections needs the admin key; everything else the
client key. ``where`` runs read-only: it can filter, never write. Any client
may read every collection.
"""

import uuid
from typing import Any, Dict, List, Literal, Optional

from fastapi import APIRouter
from pydantic import BaseModel, ConfigDict, Field, constr

from config import Config
from vectorstore import Collection, FIELD_TYPES
from vectorstore.store import BUILT_IN
from errors import Refused
from models import ModelManager
from . import deps
from .openai_routes import EMBEDDING_TASK

router = APIRouter(prefix="/collections", tags=["collections"], dependencies=deps.CLIENT)

Name = constr(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,62}$")
FieldType = Literal[tuple(FIELD_TYPES)]
Params = Dict[constr(pattern=r"^[A-Za-z][A-Za-z0-9_]{0,62}$"), Any]
Where = Field(None, max_length=4000, description="ArcadeDB SQL condition on the fields, e.g. tenant = :t AND year >= 2024")


class CreateCollection(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    name: Name
    model: str = Field(..., min_length=1, max_length=256, description="The embedding model for items and queries")
    fields: Dict[Name, FieldType] = Field(default_factory=dict, max_length=64)
    quantization: Literal["NONE", "INT8"] = Field("NONE", description="INT8 stores vectors in a quarter of the memory")


class Item(BaseModel):
    # Declared fields arrive as extra keys; the collection checks them.
    model_config = ConfigDict(extra="allow")

    id: Optional[str] = Field(None, min_length=1, max_length=256, description="Unset: one is made. Same id: replaced")
    text: str = Field(..., min_length=1, max_length=Config.MAX_INPUT_CHARS)


class AddItems(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: List[Item] = Field(..., min_length=1, max_length=1000)


class Search(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=Config.MAX_INPUT_CHARS)
    k: int = Field(10, ge=1, le=100)
    where: Optional[str] = Where
    params: Params = Field(default_factory=dict)


class Query(BaseModel):
    model_config = ConfigDict(extra="forbid")

    where: Optional[str] = Where
    params: Params = Field(default_factory=dict)
    limit: int = Field(100, ge=1, le=1000)
    skip: int = Field(0, ge=0)


async def _embed(manager: ModelManager, model: str, texts: List[str]) -> Dict[str, Any]:
    """The texts' vectors with ``model``, in batches the embedding task accepts."""
    vectors: List[List[float]] = []
    tokens = 0
    for start in range(0, len(texts), Config.MAX_EMBEDDING_INPUTS):
        batch = texts[start:start + Config.MAX_EMBEDDING_INPUTS]
        output = await manager.run_inference(model, EMBEDDING_TASK, {"texts": batch})
        vectors.extend(output["result"].embeddings)
        tokens += output["result"].tokens
    return {"vectors": vectors, "tokens": tokens}


def _describe(c: Collection, count: Optional[int] = None) -> Dict[str, Any]:
    out = {"name": c.name, "model": c.model, "dimensions": c.dimensions, "fields": c.fields,
           "quantization": c.quantization}
    if count is not None:
        out["count"] = count
    return out


@router.get("")
async def list_collections(store: deps.Collections):
    """Every collection, with its model, fields and item count."""
    found = await store.collections()
    return {"collections": [_describe(c, await store.count(c)) for c in found.values()]}


@router.post("", dependencies=deps.ADMIN)
async def create_collection(store: deps.Collections, manager: deps.Manager, body: CreateCollection):
    """Create a collection; its vector size comes from embedding a probe text with ``model``."""
    clashes = [name for name in body.fields if name.lower() in BUILT_IN]
    if clashes:
        raise Refused(f"Every collection has {', '.join(BUILT_IN[:3])}; rename {clashes}")
    probe = await _embed(manager, body.model, ["dimension probe"])
    collection = Collection(body.name, body.model, len(probe["vectors"][0]), dict(body.fields), body.quantization)
    await store.create(collection)
    return _describe(collection, 0)


@router.get("/{name}")
async def get_collection(store: deps.Collections, name: str):
    collection = await store.get(name)
    return _describe(collection, await store.count(collection))


@router.delete("/{name}", dependencies=deps.ADMIN)
async def drop_collection(store: deps.Collections, name: str):
    """Delete a collection and all its items."""
    await store.drop(name)
    return {"message": f"Collection {name} dropped"}


@router.post("/{name}/items")
async def add_items(store: deps.Collections, manager: deps.Manager, name: str, body: AddItems):
    """Embed and store items; an item with an existing id replaces it."""
    collection = await store.get(name)
    rows = []
    for item in body.items:
        values = dict(item.model_extra or {})
        collection.check_values(values)
        rows.append({"id": item.id or uuid.uuid4().hex, "text": item.text, **values})
    embedded = await _embed(manager, collection.model, [row["text"] for row in rows])
    for row, vector in zip(rows, embedded["vectors"]):
        row["embedding"] = vector
    await store.upsert(collection, rows)
    return {"ids": [row["id"] for row in rows], "count": len(rows), "tokens": embedded["tokens"]}


@router.get("/{name}/items/{item_id}")
async def get_item(store: deps.Collections, name: str, item_id: str):
    return await store.item(await store.get(name), item_id)


@router.delete("/{name}/items/{item_id}")
async def delete_item(store: deps.Collections, name: str, item_id: str):
    await store.delete(await store.get(name), item_id)
    return {"message": f"Item {item_id} deleted"}


@router.post("/{name}/search")
async def search(store: deps.Collections, manager: deps.Manager, name: str, body: Search):
    """The k items nearest to ``query`` (by the collection's model), nearest first; ``distance`` 0 is identical."""
    collection = await store.get(name)
    embedded = await _embed(manager, collection.model, [body.query])
    results = await store.search(collection, embedded["vectors"][0], body.k, body.where, body.params)
    return {"results": results, "model": collection.model, "tokens": embedded["tokens"]}


@router.post("/{name}/query")
async def query(store: deps.Collections, name: str, body: Query):
    """Items ``where`` selects, without vector search, in pages of ``limit``."""
    collection = await store.get(name)
    items = await store.query(collection, body.where, body.params, body.limit, body.skip)
    return {"items": items}
