"""
/db: namespaced tables of JSON rows, in the embedded ArcadeDB database (see
vectorstore/tables.py).

Namespaces, tables and their schemas are made and changed with the admin key
(the /admin dashboard's Data section does it), and so are raw queries
(``_query``), which are not confined to their namespace; rows need the client
key. The service's own namespaces (evals, jobs, enrichers, evidence, the
playground) need the admin key for everything, and are read-only here. A
table also appears on its first write, with an empty schema. Writes that do
not fit the schema get 400 (see vectorstore/tables.py). A table made with
``graph`` is a graph table: its rows can be joined by edges
(``{type, from: "table/id", to: "table/id", ...properties}``), read back as a
row's edges or neighbors, by traversal, or by a read-only SQL or Cypher query. Path segments and query parameters starting with ``_`` belong to the
API (``_count``, ``_bulk``, ``_limit``...), which is why names and ids never
start with one.

    GET    /db                                    namespaces, their tables, row counts and sizes on disk
    POST   /db                     {name}         create a namespace (admin)
    GET    /db/{ns}                               its tables, row counts and sizes on disk
    POST   /db/{ns}                {name, fields, strict, graph}  create a table (admin)
    DELETE /db/{ns}                               drop it with every table (admin)
    POST   /db/{ns}/_transaction   {puts, deletes, edges, delete_edges}  all applied, or none
    POST   /db/{ns}/_query         {language, query, params, limit}  {rows}: read-only SQL or Cypher (admin)
    GET    /db/{ns}/_graph?_table=&_type=&_limit=&_edge_limit=  {nodes, edges, tables, outside, more}: rows and the edges among them, to draw
    GET    /db/{ns}/_edges                        its edge types with their counts and the tables they join
    POST   /db/{ns}/_edges         {name}         create an empty edge type (admin)
    PUT    /db/{ns}/_edges         {edges}        204: store the edges (all or none)
    POST   /db/{ns}/_edges/_delete {edges}        204: delete the edges named by type, from and to
    DELETE /db/{ns}/_edges/{type}                 drop the edge type with its edges (admin)
    GET    /db/{ns}/{table}/{id}/_edges?_type=&_direction=&_limit=&_cursor=      {edges, next}
    GET    /db/{ns}/{table}/{id}/_neighbors?_type=&_direction=&_limit=&_cursor=  {neighbors, next}
    GET    /db/{ns}/{table}/{id}/_traverse?_type=&_direction=&_depth=&_limit=    {rows}
    GET    /db/{ns}/{table}?f=v&_sort=-f&_limit=&_cursor=   {rows, next}
    GET    /db/{ns}/{table}/_count?f=v            {count}
    POST   /db/{ns}/{table}        row            201 + row (409 if the id exists)
    DELETE /db/{ns}/{table}                       drop the table (admin)
    GET    /db/{ns}/{table}/_schema?_sample=500   {fields, strict, observed}
    PUT    /db/{ns}/{table}/_schema   {fields, strict}  replace it, after checking the rows (admin)
    POST   /db/{ns}/{table}/_bulk-get   {ids}     {rows}
    PUT    /db/{ns}/{table}/_bulk       {rows}    204 (upsert, atomic)
    POST   /db/{ns}/{table}/_bulk-delete {ids}    204 (atomic)
    GET    /db/{ns}/{table}/{id}                  row or 404
    PUT    /db/{ns}/{table}/{id}   row            201 created / 200 replaced, + row
    PATCH  /db/{ns}/{table}/{id}   merge patch    200 + row, or 404
    DELETE /db/{ns}/{table}/{id}                  204
"""

import os
import time
import uuid
from typing import Any, Dict, Iterable, List, Literal, Optional, Tuple

from fastapi import APIRouter, Body, Depends, Query, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from vectorstore.tables import (DIRECTIONS, GRAPH_MAX_EDGES, GRAPH_MAX_NODES, MAX_DEPTH, QUERY_LANGUAGES, TYPES,
                                Field as SchemaField, TableSchema, check_id, decode_cursor, make_schema)
from errors import Forbidden, Refused
from . import deps
from . import responses

# POST routes that only read.
_READING = ("/_query", "/_bulk-get")


async def _unless_managed(request: Request) -> None:
    """The service's own namespaces (evals, evidence, the playground...) hold admin data: here they need the admin
    key, and are read-only, since they change through their own APIs; so are those of a store switched off now.
    The key is checked first, so a caller without it gets 401."""
    namespace = request.path_params.get("namespace")
    tables = request.app.state.tables
    owner = await tables.owner(namespace) if namespace is not None and tables is not None else None
    if not owner:
        return
    deps.require_admin_key(request.headers.get("authorization"), request.headers.get("x-api-key"))
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    if request.method == "POST" and request.url.path.endswith(_READING):
        return
    raise Forbidden(f"The namespace {namespace!r} is read-only here: it is kept by the "
                                                f"service ({owner})")


# The client key first, so a caller without one never reaches the database. Any key the managed check takes also
# passes CLIENT, so either order gives the same answers: 401 without the admin key, then 403 for a write.
router = APIRouter(prefix="/db", tags=["tables"], dependencies=[*deps.CLIENT, Depends(_unless_managed)])

# Rows or ids in one bulk request or transaction.
MAX_BATCH = 1000
MAX_LIMIT = 1000
LIST_PARAMS = {"_limit", "_cursor", "_sort"}
EDGE_PARAMS = {"_type", "_direction", "_limit", "_cursor"}
TRAVERSE_PARAMS = {"_type", "_direction", "_depth", "_limit"}
GRAPH_PARAMS = {"_table", "_type", "_limit", "_edge_limit"}

Row = Dict[str, Any]
RowBody = Body(..., description="A JSON object; ``id`` is a string")
Edge = Dict[str, Any]
EDGE_DOC = "``{type, from: 'table/id', to: 'table/id', ...properties}``"


class CreateNamespace(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=48, description="Letters, digits and single underscores")


class FieldSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., pattern=r"^[A-Za-z][A-Za-z0-9_]{0,127}$", description="A top-level field")
    type: Literal[TYPES] = Field("any", description="What it holds; object is a JSON object, any is anything")
    list: bool = Field(False, description="A list of that type; field=value matches rows whose list holds it")
    required: bool = Field(False, description="Every row has it, not null")
    indexed: bool = Field(False, description="Filters and sorts on it use an index (string, integer, number, boolean)")
    unique: bool = Field(False, description="No two rows with the same value (rows without one are fine); indexed")


class SchemaBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fields: List[FieldSpec] = Field(default_factory=list, max_length=256)
    strict: bool = Field(False, description="Refuse fields not declared")

    def schema(self) -> TableSchema:
        return make_schema([SchemaField(**f.model_dump()) for f in self.fields], self.strict,
                        getattr(self, "graph", False))


class CreateTable(SchemaBody):
    name: str = Field(..., min_length=1, max_length=48, description="Letters, digits and single underscores")
    graph: bool = Field(True, description="A graph table, as tables are unless made with false: its rows can be "
                                          "joined by edges. Set for good when the table is made; a plain table cannot "
                                          "become one")


class CreateEdgeType(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=48, description="Letters, digits and single underscores")


class Edges(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edges: List[Edge] = Field(..., max_length=MAX_BATCH, description=f"Each {EDGE_DOC}")


class GraphQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    language: Literal[QUERY_LANGUAGES] = Field("sql", description="ArcadeDB SQL (MATCH, TRAVERSE, out()...) or Cypher")
    query: str = Field(..., min_length=1, max_length=20000, description="Names tables and edge types as the API does")
    params: Dict[str, Any] = Field(default_factory=dict, description="Values for the query's :name placeholders")
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


def _page(key: str, items: List[Any], after: Optional[str]) -> Dict[str, Any]:
    """A page of ``items`` under ``key``, with ``next`` (the next page's cursor) unless it is the last."""
    return {key: items, **({"next": after} if after else {})}


def _sort_keys(spec: Optional[str]) -> List[Tuple[str, bool]]:
    if not spec:
        return []
    return [(f.lstrip("-"), f.startswith("-")) for f in (s.strip() for s in spec.split(",")) if f.lstrip("-")]


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


# ------------------------------------------------------------- namespaces
@router.get("", **responses.answering(responses.Namespaces))
async def list_namespaces(store: deps.Tables):
    """Every namespace, with its tables, their row counts and their size on disk (``size_bytes``)."""
    return {"namespaces": await store.namespaces()}


@router.post("", **responses.answering(responses.Namespace), status_code=201, dependencies=deps.ADMIN)
async def create_namespace(store: deps.Tables, body: CreateNamespace):
    """Create an empty namespace; its tables appear as clients write to them."""
    await store.create_namespace(body.name)
    return await store.namespace(body.name)


@router.get("/{namespace}", **responses.answering(responses.Namespace))
async def get_namespace(store: deps.Tables, namespace: str):
    """The namespace's tables, their row counts and their size on disk (``size_bytes``)."""
    return await store.namespace(namespace)


@router.post("/{namespace}", **responses.answering(responses.TableInfo), status_code=201, dependencies=deps.ADMIN)
async def create_table(store: deps.Tables, namespace: str, body: CreateTable):
    """Create an empty table with this schema; 409 if it exists."""
    return await store.create_table(namespace, body.name, body.schema())


@router.delete("/{namespace}", status_code=204, dependencies=deps.ADMIN)
async def drop_namespace(store: deps.Tables, namespace: str):
    """Delete the namespace with all its tables and rows."""
    await store.drop_namespace(namespace)
    return Response(status_code=204)


@router.post("/{namespace}/_transaction", status_code=204)
async def transaction(store: deps.Tables, namespace: str, body: Transaction):
    """Upsert ``puts`` and delete ``deletes`` across the namespace's tables: all of it, or none."""
    size = sum(map(len, body.puts.values())) + sum(map(len, body.deletes.values())) + len(body.edges) + len(body.delete_edges)
    if size > MAX_BATCH:
        raise Refused(f"At most {MAX_BATCH} rows, ids and edges in one transaction")
    await store.transaction(namespace, body.puts, body.deletes, body.edges, body.delete_edges)
    return Response(status_code=204)


@router.post("/{namespace}/_query", **responses.answering(responses.RowList), dependencies=deps.ADMIN)
async def graph_query(store: deps.Tables, namespace: str, body: GraphQuery):
    """Run a read-only SQL or Cypher query over the namespace's tables and edges and return up to ``limit`` rows.

    Name tables and edge types as the API does: where the query names a type (SQL: after FROM, a MATCH's
    ``type:``, ``out('bought')``, ``@type = 'people'``; Cypher: ``(n:people)``, ``[e:bought]``, ``type(e) = 'bought'``)
    a name of the namespace's becomes the ArcadeDB type, and anywhere else a field, value or alias keeps its name.
    Pass values through ``params``. Rows and edges in the result come back as the API shows
    them (a row as ``{table, row}``; an edge with ``type``, ``from`` and ``to``). A query that would change data is
    refused.

    Needs the admin key: the namespace is where names resolve, not a boundary. SQL and Cypher can still reach any
    type, record ID or bucket in the database (``MATCH (n)`` alone matches every graph table's rows), so a query
    can read everything the service stores.
    """
    return {"rows": await store.graph_query(namespace, body.language, body.query, body.params, body.limit)}


@router.get("/{namespace}/_graph", **responses.answering(responses.GraphView))
async def graph_view(
    store: deps.Tables, request: Request, namespace: str,
    limit: int = Query(500, alias="_limit", ge=1, le=GRAPH_MAX_NODES, description="Rows at most, shared evenly among the tables"),
    edge_limit: int = Query(5000, alias="_edge_limit", ge=1, le=GRAPH_MAX_EDGES, description="Edges at most"),
):
    """Rows of the namespace's graph tables (the ``_table``s named, repeatable; all of them when none) and the edges
    of the ``_type``s named (all when none) that join two of them: what a view of the graph draws. Each table
    contributes its first rows by id, the node budget shared evenly. ``nodes`` are ``{table, row}``, ``edges``
    ``{type, from, to, ...properties}``; ``outside`` counts edges that lead from a loaded row to one not loaded,
    and ``more`` says whether either limit cut the result. 404 for an unknown table or edge type, 400 for a table
    that is not a graph table.
    """
    _refuse_unknown(request.query_params, GRAPH_PARAMS)
    tables = request.query_params.getlist("_table")
    types = request.query_params.getlist("_type")
    return await store.graph(namespace, tables, types, limit, edge_limit)


# ------------------------------------------------------------------ edges
@router.get("/{namespace}/_edges", **responses.answering(responses.EdgeTypes))
async def list_edge_types(store: deps.Tables, namespace: str):
    """The namespace's edge types, each with how many edges it holds, its size on disk and the tables it joins
    (``joins``: each pair of the table its edges leave and the one they arrive at, with how many edges, most
    first). The joins are counted by reading every edge, which GET /db/{ns} does not do."""
    return {"edges": await store.edge_types(namespace)}


@router.post("/{namespace}/_edges", **responses.answering(responses.EdgeTypeInfo), status_code=201, dependencies=deps.ADMIN)
async def create_edge_type(store: deps.Tables, namespace: str, body: CreateEdgeType):
    """Create an empty edge type; 409 if it exists or a table has its name. Its first edge (PUT) makes one too."""
    return await store.create_edge_type(namespace, body.name)


@router.put("/{namespace}/_edges", status_code=204)
async def put_edges(store: deps.Tables, namespace: str, body: Edges):
    """Store the edges, all or none. Each joins two rows of graph tables (``from`` and ``to``, as ``table/id``),
    with the rest of the object as its properties. One edge of a type joins a pair of rows: storing it again
    replaces its properties. An edge type appears on its first edge. 400 if an end is not a row of a graph table."""
    await store.put_edges(namespace, body.edges)
    return Response(status_code=204)


@router.post("/{namespace}/_edges/_delete", status_code=204)
async def delete_edges(store: deps.Tables, namespace: str, body: Edges):
    """Delete the edges each ``{type, from, to}`` names, all or none; ones that are not there are ignored."""
    await store.delete_edges(namespace, body.edges)
    return Response(status_code=204)


@router.delete("/{namespace}/_edges/{edge_type}", status_code=204, dependencies=deps.ADMIN)
async def drop_edge_type(store: deps.Tables, namespace: str, edge_type: str):
    """Delete the edge type with every edge of it."""
    await store.drop_edge_type(namespace, edge_type)
    return Response(status_code=204)


# ----------------------------------------------------------------- tables
@router.get("/{namespace}/{table}/_count", **responses.answering(responses.RowCount))
async def count_rows(store: deps.Tables, request: Request, namespace: str, table: str):
    """How many rows the filters (``field=value``, as in the list) select."""
    filters = _filters(request, set())
    return {"count": await store.count(namespace, table, filters)}


@router.get("/{namespace}/{table}/_schema", **responses.answering(responses.SchemaInfo))
async def get_schema(
    store: deps.Tables, namespace: str, table: str,
    sample: int = Query(500, alias="_sample", ge=0, le=5000, description="Newest rows to observe"),
):
    """The declared fields, ``strict``, and ``observed``: which fields the newest rows hold, how often, with
    which types. ``id`` is built in: a string, indexed and unique."""
    return await store.schema(namespace, table, sample)


@router.put("/{namespace}/{table}/_schema", **responses.answering(responses.TableSchemaOut), dependencies=deps.ADMIN)
async def put_schema(store: deps.Tables, namespace: str, table: str, body: SchemaBody):
    """Replace the schema (making the table if needed). 400, naming a few rows, if rows there now break
    what it adds (a type, required, strict), or a unique field finds two alike; nothing changes then.
    Fields left out lose their rules and indexes, never their values."""
    return (await store.set_schema(namespace, table, body.schema())).to_dict()


@router.post("/{namespace}/{table}/_bulk-get", **responses.answering(responses.RowList))
async def bulk_get(store: deps.Tables, namespace: str, table: str, body: Ids):
    """The rows with these ids, in the order asked; ids with no row are left out."""
    return {"rows": await store.bulk_get(namespace, table, body.ids)}


@router.put("/{namespace}/{table}/_bulk", status_code=204)
async def bulk_put(store: deps.Tables, namespace: str, table: str, body: Rows):
    """Upsert every row (each replaces the row with its id), all or none."""
    await store.bulk_put(namespace, table, body.rows)
    return Response(status_code=204)


@router.post("/{namespace}/{table}/_bulk-delete", status_code=204)
async def bulk_delete(store: deps.Tables, namespace: str, table: str, body: Ids):
    """Delete the rows with these ids, all or none; ids with no row are ignored."""
    await store.delete(namespace, table, body.ids)
    return Response(status_code=204)


@router.get("/{namespace}/{table}", **responses.answering(responses.RowsPage))
async def list_rows(
    store: deps.Tables,
    request: Request,
    namespace: str,
    table: str,
    limit: int = Query(100, alias="_limit", ge=1, le=MAX_LIMIT, description="Rows per page"),
    cursor: Optional[str] = Query(None, alias="_cursor", description="The previous page's 'next'"),
    sort: Optional[str] = Query(None, alias="_sort",
                                description="Fields, comma-separated; -field sorts descending. Default: id"),
):
    """A page of rows, filtered by ``field=value`` query parameters (all must hold).

    On a declared field the value is read as its type (an index, if it has
    one, finds the rows). On other fields values are JSON when they parse as a number,
    true, false or null (quote a string that looks like one: ``code="30"``),
    else text, and every row is scanned. A list field matches when it holds
    the value. ``next`` is the next page's cursor,
    absent on the last page.
    """
    filters = _filters(request, LIST_PARAMS)
    rows, after = await store.list(namespace, table, filters, _sort_keys(sort), limit, _offset(cursor))
    return _page("rows", rows, after)


@router.post("/{namespace}/{table}", status_code=201, **responses.answering(responses.Row))
async def insert_row(store: deps.Tables, request: Request, response: Response, namespace: str, table: str,
                     row: Row = RowBody):
    """Add a row; 409 if its id is taken. Without an id it gets a UUIDv7 (32 hex digits): unique, and
    in creation order, so the default sort by id lists rows oldest first and the id index grows at its end."""
    row = {"id": _uuid7(), **row} if "id" not in row else row
    created = await store.insert(namespace, table, row)
    response.headers["Location"] = str(request.url_for("get_row", namespace=namespace, table=table, row_id=created["id"]))
    return created


@router.delete("/{namespace}/{table}", status_code=204, dependencies=deps.ADMIN)
async def drop_table(store: deps.Tables, namespace: str, table: str):
    """Delete the table and its rows. Writing to it again makes it anew."""
    await store.drop_table(namespace, table)
    return Response(status_code=204)


# ------------------------------------------------------------ graph reads
def _edge_types(request: Request, allowed: set) -> List[str]:
    """The ``_type`` parameters (repeatable); parameters the route does not take are refused."""
    _refuse_unknown(request.query_params, allowed)
    return request.query_params.getlist("_type")


@router.get("/{namespace}/{table}/{row_id}/_edges", **responses.answering(responses.EdgesPage))
async def row_edges(
    store: deps.Tables, request: Request, namespace: str, table: str, row_id: str,
    direction: Literal[DIRECTIONS] = Query("both", alias="_direction", description="Edges leaving the row, arriving, or both"),
    limit: int = Query(100, alias="_limit", ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = Query(None, alias="_cursor", description="The previous page's 'next'"),
):
    """A page of the row's edges, of the ``_type``s named (repeatable; all when none), each
    ``{type, from, to, ...properties}``. 404 unless the row is in a graph table."""
    types = _edge_types(request, EDGE_PARAMS)
    check_id(row_id)
    edges, after = await store.edges(namespace, table, row_id, types, direction, limit, _offset(cursor))
    return _page("edges", edges, after)


@router.get("/{namespace}/{table}/{row_id}/_neighbors", **responses.answering(responses.NeighborsPage))
async def row_neighbors(
    store: deps.Tables, request: Request, namespace: str, table: str, row_id: str,
    direction: Literal[DIRECTIONS] = Query("both", alias="_direction", description="Along edges leaving the row, arriving, or both"),
    limit: int = Query(100, alias="_limit", ge=1, le=MAX_LIMIT),
    cursor: Optional[str] = Query(None, alias="_cursor", description="The previous page's 'next'"),
):
    """A page of the rows one edge away, along edges of the ``_type``s named (repeatable; all when none): each
    ``{table, row, edge}``, the edge being the one it is reached by. A row joined by several edges appears once
    per edge."""
    types = _edge_types(request, EDGE_PARAMS)
    check_id(row_id)
    found, after = await store.neighbors(namespace, table, row_id, types, direction, limit, _offset(cursor))
    return _page("neighbors", found, after)


@router.get("/{namespace}/{table}/{row_id}/_traverse", **responses.answering(responses.RowList))
async def row_traverse(
    store: deps.Tables, request: Request, namespace: str, table: str, row_id: str,
    direction: Literal[DIRECTIONS] = Query("out", alias="_direction", description="Follow edges leaving each row, arriving, or both"),
    depth: int = Query(2, alias="_depth", ge=1, le=MAX_DEPTH, description="How many edges away at most"),
    limit: int = Query(100, alias="_limit", ge=1, le=MAX_LIMIT),
):
    """The rows reachable within ``_depth`` edges of the ``_type``s named (repeatable; all when none), nearest
    first and each once: ``{table, depth, row}``, the depth being the fewest edges to it."""
    types = _edge_types(request, TRAVERSE_PARAMS)
    check_id(row_id)
    return {"rows": await store.traverse(namespace, table, row_id, types, direction, depth, limit)}


# ------------------------------------------------------------------- rows
@router.get("/{namespace}/{table}/{row_id}", **responses.answering(responses.Row))
async def get_row(store: deps.Tables, namespace: str, table: str, row_id: str):
    check_id(row_id)
    return await store.get(namespace, table, row_id)


@router.put("/{namespace}/{table}/{row_id}", **responses.answering(responses.Row),
            responses={201: {"description": "Created", "model": responses.Row}})
async def put_row(store: deps.Tables, response: Response, namespace: str, table: str, row_id: str, row: Row = RowBody):
    """Store the row under this id: 201 when new, 200 when it replaced one."""
    row = _with_id(row, row_id)
    if await store.upsert(namespace, table, row):
        response.status_code = 201
    return {"id": row_id, **row}


@router.patch("/{namespace}/{table}/{row_id}", **responses.answering(responses.Row))
async def patch_row(store: deps.Tables, namespace: str, table: str, row_id: str, patch: Row = RowBody):
    """Merge into the row by JSON Merge Patch (RFC 7396): null removes a field, objects merge. 404 if no row."""
    check_id(row_id)
    return await store.patch(namespace, table, row_id, patch)


@router.delete("/{namespace}/{table}/{row_id}", status_code=204)
async def delete_row(store: deps.Tables, namespace: str, table: str, row_id: str):
    """Delete the row; deleting one that is not there is not an error."""
    check_id(row_id)
    await store.delete(namespace, table, [row_id])
    return Response(status_code=204)
