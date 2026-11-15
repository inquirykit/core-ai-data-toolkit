"""
/db: workspaces of tables of JSON rows, in the embedded DuckDB database
(see db/tables.py).

``default`` always exists and cannot be dropped; an admin makes the others,
each maybe with a label. Workspaces, tables and their schemas are made and
changed with the admin key (the /admin dashboard's Data section does it),
and so are raw queries (``_query``), which are not confined to their
workspace; rows need the client key. The service's own workspaces (evals,
tasks, enrichers, the playground) are left out: GET /db lists them only
with ``?all=true``, which needs the admin key. A kept table is a table of a
workspace that a part of the service keeps (its name starts with a prefix it
declared, as the evidence ledger's ``evidence_``), and GET /db names its keeper as
``kept``. Both are read with the admin key only, and are read-only here:
the table store refuses their writes (403) to all but their keeper, so the
check here is only the key's. A table also appears on its first write, with
an empty schema. Writes that do not fit the schema get 400
(see db/tables.py). A table made with ``graph`` is a graph table: its rows
can be joined by edges (``{type, from: "table/id", to: "table/id",
...properties}``), read back as a row's edges or neighbors, by traversal, or
by a read-only SQL query. A field of type ``ref`` joins tables by their
rows' ids (``to``: a table of the workspace): every id written must be one
of its rows (400), deleting a row it names follows its ``on_delete``
(restrict: 409; unset; cascade), and a read's ``_expand`` puts the rows it
names, or those naming the row (``table:field``), under each row's
``_expand``. Path segments and query parameters
starting with ``_`` belong to the API (``_count``, ``_bulk``, ``_limit``...),
which is why names and ids never start with one.

A table may have one vector field (``type: vector``, with ``dimensions`` and
``similarity``, or a ``model`` that embeds the string field ``from`` on
every write): ``_search`` finds the rows nearest a vector, or a text the
model embeds, among those ``filters`` and ``q`` select. A model's dimensions
are found when the field is made, which is why a model outside
ALLOWED_MODELS is refused then (403); lists leave the vectors out, and one row read alone
holds its own. A field with a model has a ``document_prompt`` put before
each row's text and a ``query_prompt`` before a search's, the model's own
unless given.

A table made with ``bucket`` is a bucket of files: its fields are fixed
(name, size, sha256, content_type, created; db/schema.py's BUCKET) and its
rows, read here as any table's with the client key, are written only by its
file routes (api/file_routes.py), which keep each row's bytes: every row
write here is refused (403), and so is a schema put (400). Dropping it, or
its workspace, deletes its files.

    GET    /db?all=                               {workspaces}: their tables, row counts and sizes on disk
    POST   /db                     {name, label}  create a workspace (admin)
    GET    /db/{ws}                               its tables, row counts and sizes on disk
    POST   /db/{ws}                {name, fields, strict, graph, bucket}  create a table, or a bucket (admin)
    PATCH  /db/{ws}                {label}        change its label; null clears it (admin)
    DELETE /db/{ws}                               drop it with every table (admin; never default)
    POST   /db/{ws}/_transaction   {puts, deletes, edges, delete_edges}  all applied, or none
    POST   /db/{ws}/_query         {query, params, limit}  {rows}: one read-only SELECT (admin)
    GET    /db/{ws}/_graph?_table=&_type=&_limit=&_edge_limit=  {nodes, edges, tables, outside, more}: rows and the edges among them, to draw
    GET    /db/{ws}/_edges                        its edge types with their counts and the tables they join
    POST   /db/{ws}/_edges         {name}         create an empty edge type (admin)
    PUT    /db/{ws}/_edges         {edges}        204: store the edges (all or none)
    POST   /db/{ws}/_edges/_delete {edges}        204: delete the edges named by type, from and to
    DELETE /db/{ws}/_edges/{type}                 drop the edge type with its edges (admin)
    GET    /db/{ws}/{table}/{id}/_edges?_type=&_direction=&_limit=&_cursor=      {edges, next}
    GET    /db/{ws}/{table}/{id}/_neighbors?_type=&_direction=&_limit=&_cursor=  {neighbors, next}
    GET    /db/{ws}/{table}/{id}/_traverse?_type=&_direction=&_depth=&_limit=    {rows}
    GET    /db/{ws}/{table}?f=v&_sort=-f&_limit=&_cursor=&_expand=   {rows, next}
    GET    /db/{ws}/{table}/_count?f=v            {count}
    POST   /db/{ws}/{table}        row            201 + row (409 if the id exists)
    DELETE /db/{ws}/{table}                       drop the table (admin)
    GET    /db/{ws}/{table}/_schema?_sample=500   {fields, strict, observed}
    PUT    /db/{ws}/{table}/_schema   {fields, strict}  replace it, after checking the rows (admin)
    POST   /db/{ws}/{table}/_bulk-get   {ids}     {rows}
    PUT    /db/{ws}/{table}/_bulk       {rows}    204 (upsert, atomic)
    POST   /db/{ws}/{table}/_bulk-delete {ids}    204 (atomic)
    POST   /db/{ws}/{table}/_search {vector | text, k, filters, q}  {results: [{row, distance}], tokens}
    GET    /db/{ws}/{table}/{id}?_expand=         row or 404
    PUT    /db/{ws}/{table}/{id}   row            201 created / 200 replaced, + row
    PATCH  /db/{ws}/{table}/{id}   merge patch    200 + row, or 404
    DELETE /db/{ws}/{table}/{id}                  204
"""

import os
import time
import uuid
import json
from typing import Annotated, Any, Dict, Iterable, List, Literal, Optional, Tuple, Union

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from db.tables import (
    BACK_LIMIT, DIRECTIONS, EXPAND_DEPTH, GRAPH_MAX_EDGES, GRAPH_MAX_NODES, MAX_DEPTH, MAX_DIMENSIONS,
    MAX_K, MAX_PROMPT, MAX_SEARCH, ON_DELETE, QUANTIZATIONS, SIMILARITIES, TYPES, Field as SchemaField, TableSchema,
    check_id, decode_cursor, make_schema, sort_keys,
)
from core.config import Config
from core.errors import Refused
from . import audit, deps
from . import responses


async def admin_for_kept(request: Request) -> None:
    """The service's own workspaces (evals, the playground...) and kept tables (evidence's) hold its data: reading
    them here needs the admin key (so does one the database marks as a store's that this run does not have). The
    table store refuses their writes, which change through their own APIs, so a write with the admin key gets 403
    there. The file routes (api/file_routes.py) take it too, so a bucket's files are under the same rules as its
    rows."""
    workspace = request.path_params.get("workspace")
    tables = request.app.state.tables
    if workspace is None:
        return
    # The table a route reads: its path's (a bucket's, for the file routes), or the graph view's _table.
    named = [request.path_params.get("table"), request.path_params.get("bucket"),
             *request.query_params.getlist("_table")]
    if await tables.owner(workspace) or any(table and tables.keeper(table) for table in named):
        deps.check_admin(request)


# The client key first, so a caller without one never reaches the database. Any key the kept check takes also
# passes CLIENT, so either order gives the same answers: 401 without the admin key, then the store's 403 for a write.
router = APIRouter(prefix="/db", tags=["tables"], dependencies=[*deps.CLIENT, Depends(admin_for_kept)])
# This module's routes that only the admin's key opens.
admin_router = APIRouter(prefix="/db", tags=["tables"], dependencies=deps.ADMIN)

# Rows or ids in one bulk request or transaction.
MAX_BATCH = 1000
MAX_LIMIT = 1000
LIST_PARAMS = {"_limit", "_cursor", "_sort", "_search", "_expand"}
EDGE_PARAMS = {"_type", "_direction", "_limit", "_cursor"}
TRAVERSE_PARAMS = {"_type", "_direction", "_depth", "_limit"}
GRAPH_PARAMS = {"_table", "_type", "_limit", "_edge_limit"}

Row = Dict[str, Any]
RowBody = Body(..., description="A JSON object; ``id`` is a string")
Edge = Dict[str, Any]
EDGE_DOC = "``{type, from: 'table/id', to: 'table/id', ...properties}``"


# A workspace's label: free text, trimmed.
Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


class CreateWorkspace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=48, description="Letters, digits and single underscores")
    label: Optional[Label] = Field(None, description="What it holds, in words: free text, 1 to 200 characters")


class SetLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: Optional[Label] = Field(..., description="Free text, 1 to 200 characters; null clears it")


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True, protected_namespaces=())

    name: str = Field(..., pattern=r"^[A-Za-z][A-Za-z0-9_]{0,127}$", description="A top-level field")
    type: Literal[TYPES] = Field("any", description="What it holds; object is a JSON object, any is anything, ref the "
                                                    "id of a row of the table ``to``, vector a list of numbers "
                                                    "searched by nearness (one field a table)")
    list: bool = Field(False, description="A list of that type; field=value matches rows whose list holds it")
    required: bool = Field(False, description="Every row has it, not null")
    indexed: bool = Field(False, description="Filters and sorts on it use an index (string, integer, number, boolean)")
    unique: bool = Field(False, description="No two rows with the same value (rows without one are fine); indexed")
    fulltext: bool = Field(False, description="Its words are searchable with _search (a string field, not a list, "
                                              "and not also indexed)")
    # A ref field's: the table whose rows it names, and what deleting one of them does here.
    to: Optional[str] = Field(None, min_length=1, max_length=48,
                              description="A ref field's table, another of the same workspace: every id "
                                          "written must be one of its rows")
    on_delete: Optional[Literal[ON_DELETE]] = Field(
        None, description="A ref field's: what deleting a row it names does to the rows naming it. restrict (the "
                          "default) refuses the delete, unset takes the id out, cascade deletes them too (not for a "
                          "list)")
    # A vector field's options, set when it is made: changing one means removing the field and adding it again.
    dimensions: Optional[int] = Field(None, ge=1, le=MAX_DIMENSIONS,
                                      description="A vector field's numbers; with a model, left out to take the model's")
    similarity: Optional[Literal[SIMILARITIES]] = Field(None, description="A vector field's comparison (COSINE when unset)")
    quantization: Optional[Literal[QUANTIZATIONS]] = Field(
        None, description="A vector field's storage: NONE, its numbers as 4-byte floats (NONE when unset)")
    model: Optional[str] = Field(None, min_length=1, max_length=256,
                                 description="A vector field's embedding model, which embeds ``from`` on every write")
    from_: Optional[str] = Field(None, alias="from", pattern=r"^[A-Za-z][A-Za-z0-9_]{0,127}$",
                                 description="The string field of the table a vector field with a model embeds")
    query_prompt: Optional[str] = Field(
        None, max_length=MAX_PROMPT, description="With a model: the text put before a search's text (\"\" for none); "
                                                 "left out, the model's own prompt named query, and may change")
    document_prompt: Optional[str] = Field(
        None, max_length=MAX_PROMPT, description="With a model: the text put before each row's text (\"\" for none); "
                                                 "left out, the model's own prompt named document")

    def field(self) -> SchemaField:
        return SchemaField(**self.model_dump(by_alias=False))


class SchemaBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: List[FieldSpec] = Field(default_factory=list, max_length=256)
    strict: bool = Field(False, description="Refuse fields not declared")

    def schema(self) -> TableSchema:
        return make_schema([f.field() for f in self.fields], self.strict, getattr(self, "graph", False))


class CreateTable(SchemaBody):
    name: str = Field(..., min_length=1, max_length=48, description="Letters, digits and single underscores")
    graph: bool = Field(True, description="A graph table, as tables are unless made with false: its rows can be "
                                          "joined by edges. Set for good when the table is made; a plain table cannot "
                                          "become one")
    bucket: bool = Field(False, description="A bucket of files: a plain table whose fields are fixed (name, size, "
                                            "sha256, content_type, created), a row per file, written only by its "
                                            "file routes (.../_files). Give no fields, and graph unset or false")

    def schema(self) -> TableSchema:
        # A bucket is a plain table: graph counts only when it was given.
        graph = self.graph if not self.bucket or "graph" in self.model_fields_set else False
        return make_schema([f.field() for f in self.fields], self.strict, graph, self.bucket)


class CreateEdgeType(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=48, description="Letters, digits and single underscores")


class Edges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edges: List[Edge] = Field(..., max_length=MAX_BATCH, description=f"Each {EDGE_DOC}")


class GraphQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(..., min_length=1, max_length=20000,
                       description="One SELECT in DuckDB's SQL, naming the workspace's tables as the API does")
    params: Dict[str, Any] = Field(default_factory=dict, description="Values for the query's $name placeholders")
    limit: int = Field(100, ge=1, le=MAX_LIMIT, description="Rows returned at most")


class Ids(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ids: List[str] = Field(..., max_length=MAX_BATCH)


class Rows(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: List[Row] = Field(..., max_length=MAX_BATCH, description="Each with its id")


class Transaction(BaseModel):
    model_config = ConfigDict(extra="forbid")

    puts: Dict[str, List[Row]] = Field(default_factory=dict, description="table -> rows to upsert")
    deletes: Dict[str, List[str]] = Field(default_factory=dict, description="table -> ids to delete")
    edges: List[Edge] = Field(default_factory=list, description=f"Edges to store, after the puts: each {EDGE_DOC}")
    delete_edges: List[Edge] = Field(default_factory=list,
                                     description="Edges to delete, before the deletes: each ``{type, from, to}``")


# A filter's value: what the list's field=value says in its query string, here as JSON (a number, true or null as such).
FilterValue = Union[str, int, float, bool, None]


class VectorQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    vector: Optional[List[float]] = Field(None, min_length=1, max_length=MAX_DIMENSIONS,
                                          description="The vector to find the nearest rows to: as many numbers as the "
                                                      "table's vector field holds")
    text: Optional[str] = Field(None, min_length=1, max_length=Config.MAX_INPUT_CHARS,
                                description="A text to embed with the vector field's model, in place of a vector")
    k: int = Field(10, ge=1, le=MAX_K, description="How many rows, nearest first")
    filters: Dict[str, FilterValue] = Field(
        default_factory=dict, max_length=64,
        description="field: value, all of which must hold, as the list's field=value: a declared field's value is "
                    "read as its type, a list field matches when it holds the value")
    q: Optional[str] = Field(None, min_length=1, max_length=MAX_SEARCH,
                             description="Words the full-text fields must hold, as the list's _search")
    hybrid: bool = Field(False, description="Rank by the text's words too: the nearest rows and those the full-text "
                                            "fields hold the text's words best, fused by rank, each with a score")
    rerank: Optional[str] = Field(None, min_length=1, max_length=256,
                                  description="A rerank model that reads the text against four times k rows found, "
                                              "each row's embedded text, and keeps the k it scores best")

    @model_validator(mode="after")
    def _one(self) -> "VectorQuery":
        if (self.vector is None) == (self.text is None):
            raise ValueError("give a vector or a text, one of them")
        return self


def _refuse_unknown(keys: Iterable[str], allowed: set) -> None:
    for key in keys:
        if key not in allowed:
            raise Refused(f"Unknown parameter {key!r}; "
                                                        f"this route takes {', '.join(sorted(allowed)) or 'none'}")


def _filters(request: Request, allowed: set) -> List[Tuple[str, str]]:
    """The query's field=value filters, as text (the store reads them by field); refuses API parameters
    (``_``) this route does not take."""
    items = request.query_params.multi_items()
    _refuse_unknown([key for key, _ in items if key.startswith("_")], allowed)
    return [(key, raw) for key, raw in items if not key.startswith("_")]


def _offset(cursor: Optional[str]) -> int:
    return 0 if cursor is None else decode_cursor(cursor)


def _uuid7() -> str:
    """A UUIDv7 as 32 hex digits: Python 3.14's, or one made the same way on an older Python (RFC 9562: the time
    in milliseconds, then random bits), where rows made within the same millisecond may sort either way."""
    if hasattr(uuid, "uuid7"):
        return uuid.uuid7().hex
    raw = bytearray((time.time_ns() // 1_000_000).to_bytes(6, "big") + os.urandom(10))
    raw[6] = 0x70 | (raw[6] & 0x0F)  # the version, 7
    raw[8] = 0x80 | (raw[8] & 0x3F)  # the RFC's variant
    return raw.hex()


def _with_id(row: Row, row_id: str) -> Row:
    if "id" in row and row["id"] != row_id:
        raise Refused(f"The row's id {row['id']!r} is not the path's {row_id!r}")
    return {**row, "id": row_id}


# ------------------------------------------------------------- workspaces
@router.get("", **responses.answering(responses.Workspaces))
async def list_workspaces(
    store: deps.Tables, request: Request,
    everything: bool = Query(False, alias="all", description="Add the service's own data (evals, jobs, enrichers, "
                                                              "the playground): needs the admin key"),
    limit: int = Query(MAX_LIMIT, alias="_limit", ge=1, le=MAX_LIMIT, description="Workspaces per page"),
    cursor: Optional[str] = Query(None, alias="_cursor", description="The previous page's 'next'"),
):
    """A page of the workspaces, by name, each with its label and its tables and edge types by name: a table's
    kind (``graph``, ``bucket``, its ``vector`` field), and ``kept`` naming what keeps a kept table (null for the
    others: one kept is read-only here, and needs the admin key). Their row counts,
    sizes and schemas are GET /db/{workspace}'s, so this list costs the same however many tables there are.
    With ``all``, the service's own come too, each with ``managed`` naming what keeps it (null for a workspace).
    ``next`` is the next page's cursor, absent on the last page."""
    _refuse_unknown(request.query_params, {"all", "_limit", "_cursor"})
    if everything:
        deps.check_admin(request)
    listed, after = await store.listing(managed=everything, offset=_offset(cursor), limit=limit)
    return responses.page("workspaces", listed, after)


@admin_router.post("", **responses.answering(responses.Workspace), status_code=201)
async def create_workspace(store: deps.Tables, body: CreateWorkspace):
    """Create an empty workspace, maybe with a label; its tables appear as clients write to them."""
    await store.create_workspace(body.name, body.label)
    return await store.workspace(body.name)


@router.get("/{workspace}", **responses.answering(responses.Workspace))
async def get_workspace(store: deps.Tables, workspace: str):
    """The workspace's label, its tables and edge types, their row counts and their size on disk
    (``size_bytes``); a kept table names what keeps it as ``kept``."""
    return await store.workspace(workspace)


@admin_router.patch("/{workspace}", **responses.answering(responses.Workspace))
async def set_label(store: deps.Tables, workspace: str, body: SetLabel):
    """Change the workspace's label; null clears it."""
    await store.set_label(workspace, body.label)
    return await store.workspace(workspace)


@admin_router.post("/{workspace}", **responses.answering(responses.TableInfo), status_code=201)
async def create_table(store: deps.Tables, workspace: str, body: CreateTable):
    """Create an empty table with this schema; 409 if it exists. A vector field with a model embeds a probe text
    with it now, which takes its dimensions when they are left out (403 for a model outside ALLOWED_MODELS).
    With ``bucket``, a bucket of files, whose fields are fixed (400 for fields given, or graph true); 409 when the
    folder its files would go to holds files already or is another bucket's on a disk that ignores case."""
    return await store.create_table(workspace, body.name, body.schema())


@admin_router.delete("/{workspace}", status_code=204)
async def drop_workspace(store: deps.Tables, workspace: str):
    """Delete the workspace with all its tables and rows, and its buckets' files. ``default`` always exists: 403
    (drop its tables one by one instead), and 403 too for one that holds kept tables (or edge types named like
    them), naming them and what keeps them; nothing is dropped then."""
    await store.drop_workspace(workspace)
    return Response(status_code=204)


@router.post("/{workspace}/_transaction", status_code=204)
async def transaction(store: deps.Tables, workspace: str, body: Transaction):
    """Upsert ``puts`` and delete ``deletes`` across the workspace's tables: all of it, or none. Vector fields with
    a model are embedded from the rows' texts first, every row's in one batch per model."""
    size = sum(map(len, body.puts.values())) + sum(map(len, body.deletes.values())) + len(body.edges) + len(body.delete_edges)
    if size > MAX_BATCH:
        raise Refused(f"At most {MAX_BATCH} rows, ids and edges in one transaction")
    await store.transaction(workspace, body.puts, body.deletes, body.edges, body.delete_edges)
    return Response(status_code=204)


@admin_router.post("/{workspace}/_query", **responses.answering(responses.RowList))
@audit.reads
async def graph_query(store: deps.Tables, workspace: str, body: GraphQuery):
    """Run one read-only SELECT (DuckDB's SQL) over the workspace's tables and return up to ``limit`` rows.

    Tables are named as the API names them: the workspace is the schema a bare name is read in. Its edges, of every
    type, are rows of its ``_edges`` table (type, from_table, from_id, to_table, to_id, props); a table's rows hold
    their declared fields as columns, the others in ``_rest`` (JSON). Pass values through ``params`` as ``$name``.
    Anything but one SELECT (a WITH ... SELECT among them) is refused, and DuckDB here reads no files.

    Needs the admin key: the workspace is where names resolve, not a boundary. A query can name another schema
    (``evals.datasets``, ``main.tk_audit``), so it can read everything the service stores.
    """
    return {"rows": await store.sql_query(workspace, body.query, body.params, body.limit)}


@router.get("/{workspace}/_graph", **responses.answering(responses.GraphView))
async def graph_view(
    store: deps.Tables, request: Request, workspace: str,
    limit: int = Query(500, alias="_limit", ge=1, le=GRAPH_MAX_NODES, description="Rows at most, shared evenly among the tables"),
    edge_limit: int = Query(5000, alias="_edge_limit", ge=1, le=GRAPH_MAX_EDGES, description="Edges at most"),
):
    """Rows of the workspace's graph tables (the ``_table``s named, repeatable; all of them when none, but kept
    tables, which are drawn when named, with the admin key) and the edges of the ``_type``s named (all when none)
    that join two of them: what a view of the graph draws. Each table
    contributes its first rows by id, the node budget shared evenly. ``nodes`` are ``{table, row}``, ``edges``
    ``{type, from, to, ...properties}``; ``outside`` counts edges that lead from a loaded row to one not loaded,
    and ``more`` says whether either limit cut the result. 404 for an unknown table or edge type, 400 for a table
    that is not a graph table.
    """
    _refuse_unknown(request.query_params, GRAPH_PARAMS)
    tables = request.query_params.getlist("_table")
    types = request.query_params.getlist("_type")
    return await store.graph(workspace, tables, types, limit, edge_limit)


# ------------------------------------------------------------------ edges
@router.get("/{workspace}/_edges", **responses.answering(responses.EdgeTypes))
async def list_edge_types(store: deps.Tables, workspace: str):
    """The workspace's edge types, each with how many edges it holds, its size on disk and the tables it joins
    (``joins``: each pair of the table its edges leave and the one they arrive at, with how many edges, most
    first). The joins are counted by reading every edge, which GET /db/{ws} does not do."""
    return {"edges": await store.edge_types(workspace)}


@admin_router.post("/{workspace}/_edges", **responses.answering(responses.EdgeTypeInfo), status_code=201)
async def create_edge_type(store: deps.Tables, workspace: str, body: CreateEdgeType):
    """Create an empty edge type; 409 if it exists or a table has its name. Its first edge (PUT) makes one too."""
    return await store.create_edge_type(workspace, body.name)


@router.put("/{workspace}/_edges", status_code=204)
async def put_edges(store: deps.Tables, workspace: str, body: Edges):
    """Store the edges, all or none. Each joins two rows of graph tables (``from`` and ``to``, as ``table/id``),
    with the rest of the object as its properties. One edge of a type joins a pair of rows: storing it again
    replaces its properties. An edge type appears on its first edge. 400 if an end is not a row of a graph table."""
    await store.put_edges(workspace, body.edges)
    return Response(status_code=204)


@router.post("/{workspace}/_edges/_delete", status_code=204)
async def delete_edges(store: deps.Tables, workspace: str, body: Edges):
    """Delete the edges each ``{type, from, to}`` names, all or none; ones that are not there are ignored."""
    await store.delete_edges(workspace, body.edges)
    return Response(status_code=204)


@admin_router.delete("/{workspace}/_edges/{edge_type}", status_code=204)
async def drop_edge_type(store: deps.Tables, workspace: str, edge_type: str):
    """Delete the edge type with every edge of it."""
    await store.drop_edge_type(workspace, edge_type)
    return Response(status_code=204)


# ----------------------------------------------------------------- tables
SEARCH = Query(None, alias="_search", max_length=MAX_SEARCH,
               description='Words the full-text fields must hold: every word, a "quoted phrase", -word must not, word* '
                           'for words starting so')
EXPAND = Query(None, alias="_expand", max_length=1000,
               description="Refs to follow, comma-separated: a ref field (the row it names), or table:field (the rows "
                           f"of table whose ref field names this one, {BACK_LIMIT} at most); a dot goes on from there, "
                           f"{EXPAND_DEPTH} steps at most. Found under each row's _expand")


@router.get("/{workspace}/{table}/_count", **responses.answering(responses.RowCount))
async def count_rows(store: deps.Tables, request: Request, workspace: str, table: str, search: Optional[str] = SEARCH):
    """How many rows the filters (``field=value``) and the search (``_search``), as in the list, select."""
    filters = _filters(request, {"_search"})
    return {"count": await store.count(workspace, table, filters, search)}


@router.get("/{workspace}/{table}/_schema", **responses.answering(responses.SchemaInfo))
async def get_schema(
    store: deps.Tables, workspace: str, table: str,
    sample: int = Query(500, alias="_sample", ge=0, le=5000, description="Newest rows to observe"),
):
    """The declared fields, ``strict``, and ``observed``: which fields the newest rows hold, how often, with
    which types. ``id`` is built in: a string, indexed and unique."""
    return await store.schema(workspace, table, sample)


@admin_router.put("/{workspace}/{table}/_schema", **responses.answering(responses.TableSchemaOut))
async def put_schema(store: deps.Tables, workspace: str, table: str, body: SchemaBody):
    """Replace the schema (making the table if needed). 400, naming a few rows, if rows there now break
    what it adds (a type, required, strict), or a unique field finds two alike; nothing changes then.
    Fields left out lose their rules and indexes, never their values, but for a vector field, whose vectors go
    with it. A vector field's options never change (400): remove it and add it again, and rows get new vectors
    as they are written again. Adding one to a table past VECTOR_MAX_ROWS rows is refused (413). A bucket's
    fields are fixed (400)."""
    return (await store.set_schema(workspace, table, body.schema())).to_dict()


@router.post("/{workspace}/{table}/_bulk-get", **responses.answering(responses.RowList))
async def bulk_get(store: deps.Tables, workspace: str, table: str, body: Ids):
    """The rows with these ids, in the order asked; ids with no row are left out."""
    return {"rows": await store.bulk_get(workspace, table, body.ids)}


@router.post("/{workspace}/{table}/_search", **responses.answering(responses.VectorSearch))
@audit.reads
async def vector_search(store: deps.Tables, workspace: str, table: str, body: VectorQuery):
    """The ``k`` rows nearest ``vector``, or ``text`` embedded with the table's vector field's model, nearest first:
    each ``{row, distance}``, the row without its vector, the distance by the field's similarity, lower nearer:
    for COSINE 1 minus the cosine, for EUCLIDEAN the squared distance (0 for both is the same vector), for
    DOT_PRODUCT -(1 + the dot product) / 2 (-1 to 0 for unit vectors). Only rows ``filters`` (field: value, as the
    list's field=value) and ``q`` (words of the full-text fields, as the list's _search) select are searched, so
    ``k`` of them come back when there are. ``tokens`` says what the text took to embed.

    With ``hybrid``, the text's words rank the rows as well as its meaning: the nearest rows and the rows whose
    full-text fields hold the most and rarest of the text's words (any of them, its first 64) are fused by
    reciprocal rank, best first, each with its ``score``; a row only the words found has no ``distance``. A text of
    common words over a table near VECTOR_MAX_ROWS takes about half a second, as every row holding one is scored.

    With a ``rerank`` model (the rerank task's: a cross-encoder), four times ``k`` rows found so (at most 100) are
    read by it, the text against each row's embedded text, and the ``k`` it scores best come back, best first, each
    with its ``score`` (0 to 1, comparable across searches); ``tokens`` counts its tokens too.

    400 for a table without a vector field, a text for one without a model, a vector of the wrong size, hybrid
    with a vector or on a table without full-text fields, or a reranker with a vector; 403 for a model outside
    ALLOWED_MODELS."""
    filters = [(name, value if isinstance(value, str) else json.dumps(value)) for name, value in body.filters.items()]
    return await store.search(workspace, table, vector=body.vector, text=body.text, k=body.k, filters=filters,
                              words=body.q, hybrid=body.hybrid, rerank=body.rerank)


@router.put("/{workspace}/{table}/_bulk", status_code=204)
async def bulk_put(store: deps.Tables, workspace: str, table: str, body: Rows):
    """Upsert every row (each replaces the row with its id), all or none. A vector field with a model is embedded
    from the rows' texts first, in one batch; 413 if the table would pass VECTOR_MAX_ROWS."""
    await store.bulk_put(workspace, table, body.rows)
    return Response(status_code=204)


@router.post("/{workspace}/{table}/_bulk-delete", status_code=204)
async def bulk_delete(store: deps.Tables, workspace: str, table: str, body: Ids):
    """Delete the rows with these ids, all or none; ids with no row are ignored."""
    await store.delete(workspace, table, body.ids)
    return Response(status_code=204)


@router.get("/{workspace}/{table}", **responses.answering(responses.RowsPage))
async def list_rows(
    store: deps.Tables,
    request: Request,
    workspace: str,
    table: str,
    limit: int = Query(100, alias="_limit", ge=1, le=MAX_LIMIT, description="Rows per page"),
    cursor: Optional[str] = Query(None, alias="_cursor", description="The previous page's 'next'"),
    sort: Optional[str] = Query(None, alias="_sort",
                                description="Fields, comma-separated; -field sorts descending. Default: id, "
                                            "or the best match first with _search"),
    search: Optional[str] = SEARCH,
    expand: Optional[str] = EXPAND,
):
    """A page of rows, filtered by ``field=value`` query parameters (all must hold).

    On a declared field the value is read as its type (an index, if it has
    one, finds the rows). On other fields values are JSON when they parse as a number,
    true, false or null (quote a string that looks like one: ``code="30"``),
    else text, and every row is scanned. A list field matches when it holds
    the value. ``_search`` finds the rows whose full-text fields hold its words
    (400 for a table without one): words as the index splits them, case aside,
    a domain such as site.example.com being one word. ``next`` is the next
    page's cursor, absent on the last page. Rows come without their vector
    field (GET one row for it), and so do the rows they expand to.

    ``_expand`` follows refs (fields of type ref): ``customer`` puts the row
    the ref field customer names under each row's ``_expand.customer``;
    ``order_items:order`` puts there the rows of order_items whose ref field
    order names the row (the lowest ids, up to the limit the parameter
    says: ``_expand_more`` names a step that held more, which a filter on
    that field pages); ``order_items:order.product`` goes on to each item's
    product. 400 for a step that is not a ref; 413 when it would read too
    many rows, saying how many it reads at most.
    """
    filters = _filters(request, LIST_PARAMS)
    rows, after = await store.list(workspace, table, filters, sort_keys(sort), limit, _offset(cursor), search,
                                   expand)
    return responses.page("rows", rows, after)


@router.post("/{workspace}/{table}", status_code=201, **responses.answering(responses.Row))
async def insert_row(store: deps.Tables, request: Request, response: Response, workspace: str, table: str,
                     row: Row = RowBody):
    """Add a row; 409 if its id is taken. Without an id it gets a UUIDv7 (32 hex digits): unique, and
    in creation order, so the default sort by id lists rows oldest first and the id index grows at its end.
    A vector field with a model is embedded from the row's text (a row does not send it: 400)."""
    row = {"id": _uuid7(), **row} if "id" not in row else row
    created = await store.insert(workspace, table, row)
    response.headers["Location"] = str(request.url_for("get_row", workspace=workspace, table=table, row_id=created["id"]))
    return created


@admin_router.delete("/{workspace}/{table}", status_code=204)
async def drop_table(store: deps.Tables, workspace: str, table: str):
    """Delete the table and its rows (a bucket's files too). Writing to it again makes it anew."""
    await store.drop_table(workspace, table)
    return Response(status_code=204)


# ------------------------------------------------------------ graph reads
def _edge_types(request: Request, allowed: set) -> List[str]:
    """The ``_type`` parameters (repeatable); parameters the route does not take are refused."""
    _refuse_unknown(request.query_params, allowed)
    return request.query_params.getlist("_type")


@router.get("/{workspace}/{table}/{row_id}/_edges", **responses.answering(responses.EdgesPage))
async def row_edges(
    store: deps.Tables, request: Request, workspace: str, table: str, row_id: str,
    direction: Literal[DIRECTIONS] = Query("both", alias="_direction", description="Edges leaving the row, arriving, or both"),
    limit: int = Query(100, alias="_limit", ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = Query(None, alias="_cursor", description="The previous page's 'next'"),
):
    """A page of the row's edges, of the ``_type``s named (repeatable; all when none), each
    ``{type, from, to, ...properties}``. 404 unless the row is in a graph table."""
    types = _edge_types(request, EDGE_PARAMS)
    check_id(row_id)
    edges, after = await store.edges(workspace, table, row_id, types, direction, limit, _offset(cursor))
    return responses.page("edges", edges, after)


@router.get("/{workspace}/{table}/{row_id}/_neighbors", **responses.answering(responses.NeighborsPage))
async def row_neighbors(
    store: deps.Tables, request: Request, workspace: str, table: str, row_id: str,
    direction: Literal[DIRECTIONS] = Query("both", alias="_direction", description="Along edges leaving the row, arriving, or both"),
    limit: int = Query(100, alias="_limit", ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = Query(None, alias="_cursor", description="The previous page's 'next'"),
):
    """A page of the rows one edge away, along edges of the ``_type``s named (repeatable; all when none): each
    ``{table, row, edge}``, the edge being the one it is reached by. A row joined by several edges appears once
    per edge."""
    types = _edge_types(request, EDGE_PARAMS)
    check_id(row_id)
    found, after = await store.neighbors(workspace, table, row_id, types, direction, limit, _offset(cursor))
    return responses.page("neighbors", found, after)


@router.get("/{workspace}/{table}/{row_id}/_traverse", **responses.answering(responses.RowList))
async def row_traverse(
    store: deps.Tables, request: Request, workspace: str, table: str, row_id: str,
    direction: Literal[DIRECTIONS] = Query("out", alias="_direction", description="Follow edges leaving each row, arriving, or both"),
    depth: int = Query(2, alias="_depth", ge=1, le=MAX_DEPTH, description="How many edges away at most"),
    limit: int = Query(100, alias="_limit", ge=1, le=MAX_LIMIT),
):
    """The rows reachable within ``_depth`` edges of the ``_type``s named (repeatable; all when none), nearest
    first and each once: ``{table, depth, row}``, the depth being the fewest edges to it."""
    types = _edge_types(request, TRAVERSE_PARAMS)
    check_id(row_id)
    return {"rows": await store.traverse(workspace, table, row_id, types, direction, depth, limit)}


# ------------------------------------------------------------------- rows
@router.get("/{workspace}/{table}/{row_id}", **responses.answering(responses.Row))
async def get_row(store: deps.Tables, workspace: str, table: str, row_id: str, expand: Optional[str] = EXPAND):
    """One row, whole: its vector field too, which lists and searches leave out (and the rows it expands to).
    ``_expand`` follows its refs, as the list's does."""
    check_id(row_id)
    return await store.get(workspace, table, row_id, expand)


@router.put("/{workspace}/{table}/{row_id}", **responses.answering(responses.Row),
            responses={201: {"description": "Created", "model": responses.Row}})
async def put_row(store: deps.Tables, response: Response, workspace: str, table: str, row_id: str, row: Row = RowBody):
    """Store the row under this id: 201 when new, 200 when it replaced one. A vector field with a model is
    embedded from the row's text."""
    row = _with_id(row, row_id)
    if await store.upsert(workspace, table, row):
        response.status_code = 201
    return {"id": row_id, **row}


@router.patch("/{workspace}/{table}/{row_id}", **responses.answering(responses.Row))
async def patch_row(store: deps.Tables, workspace: str, table: str, row_id: str, patch: Row = RowBody):
    """Merge into the row by JSON Merge Patch (RFC 7396): null removes a field, objects merge. 404 if no row. A
    patch that sets the text a vector field is embedded from gets that text's vector."""
    check_id(row_id)
    return await store.patch(workspace, table, row_id, patch)


@router.delete("/{workspace}/{table}/{row_id}", status_code=204)
async def delete_row(store: deps.Tables, workspace: str, table: str, row_id: str):
    """Delete the row; deleting one that is not there is not an error."""
    check_id(row_id)
    await store.delete(workspace, table, [row_id])
    return Response(status_code=204)
