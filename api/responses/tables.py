"""/db: workspaces, tables, schemas, rows, edges and graph views (a bucket's files: files.py)."""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from db.tables import ON_DELETE, QUANTIZATIONS, SIMILARITIES, TYPES

from .common import OPEN, Json, Out


class Row(BaseModel):
    """A table's row: any JSON object, whose id is a string (every write checks it). Its schema stays open."""
    model_config = ConfigDict(extra="allow")

    id: str


class FieldInfo(Out):
    """A declared field: rows hold ``type`` there (a list of it when ``list``), or leave it out unless ``required``.
    A vector field also has its options; ``model`` and ``from`` only when it is embedded. A ref field has ``to`` and
    ``on_delete``."""
    name: str
    type: Literal[TYPES]
    list: bool
    required: bool
    indexed: bool
    unique: bool
    # Its words are searchable with _search (a string field)
    fulltext: bool
    # A vector field's: how many numbers each vector holds
    dimensions: Optional[int] = None
    # A vector field's: how a search compares vectors
    similarity: Optional[Literal[SIMILARITIES]] = None
    # A vector field's storage: NONE, its numbers as 4-byte floats
    quantization: Optional[Literal[QUANTIZATIONS]] = None
    # A vector field's embedding model, which embeds the text of ``from`` on every write that sets it
    model: Optional[str] = None
    # "from" is a Python keyword: the field is read from and written as "from".
    from_: Optional[str] = Field(None, alias="from", description="The string field a vector is embedded from")
    # A vector field with a model's: the text put before a search's text, and before each row's ("" for none)
    query_prompt: Optional[str] = None
    document_prompt: Optional[str] = None
    # A ref field's: the table of the workspace whose row ids it holds, and what deleting one of those rows does here
    to: Optional[str] = None
    on_delete: Optional[Literal[ON_DELETE]] = None

    model_config = ConfigDict(**OPEN, populate_by_name=True, protected_namespaces=())


class TableEntry(Out):
    """A table as the workspaces' list (GET /db) names it: its kind and what keeps it."""
    name: str
    # A graph table: its rows can be joined by edges
    graph: bool
    # A bucket: a table of files, its fields fixed, its rows written by its file routes (.../_files)
    bucket: bool
    # The name of its vector field; null for none
    vector: Optional[str]
    # Null for a table of the workspace's; for a kept table (the evidence ledger's), what keeps it: read-only here,
    # and read with the admin key
    kept: Optional[str]


class TableInfo(TableEntry):
    """A table with its rows' count, its size and its schema (GET /db/{workspace})."""
    count: int
    # About how many bytes it takes in the database (an estimate: DuckDB compresses and shares blocks); for a bucket
    # of files, its rows only, as its files' bytes are kept in DATA_DIR/files
    size_bytes: int
    strict: bool
    fields: List[FieldInfo]


class EdgeTypeEntry(Out):
    name: str


class EdgeTypeInfo(EdgeTypeEntry):
    count: int
    size_bytes: int


class WorkspaceEntry(Out):
    """A workspace as GET /db lists it, its tables and edge types by name and kind; or, listed with ?all=true, one
    of the service's own."""
    name: str
    # What it holds, in words; null when it has none
    label: Optional[str]
    tables: List[TableEntry]
    edges: List[EdgeTypeEntry]
    # Null for a workspace; for the service's own (the evals, the jobs), what keeps it: read-only here
    managed: Optional[str]
    # When it was made (Unix time)
    created: Optional[float]


class Workspace(WorkspaceEntry):
    """A workspace with its tables' and edge types' counts, sizes and schemas (GET /db/{workspace})."""
    tables: List[TableInfo]  # type: ignore[assignment]
    edges: List[EdgeTypeInfo]  # type: ignore[assignment]
    # The sum of its tables' and edge types' sizes
    size_bytes: int


class Workspaces(Out):
    workspaces: List[WorkspaceEntry]
    # The next page's cursor; absent on the last
    next: Optional[str] = None


class ObservedField(Out):
    """A field the sampled rows hold: in how many, and with which types ("list<string>" for a list of strings)."""
    name: str
    present: int
    types: Dict[str, int]


class Observed(Out):
    sampled: int
    fields: List[ObservedField]


class SchemaInfo(Out):
    strict: bool
    graph: bool
    bucket: bool
    fields: List[FieldInfo]
    observed: Observed


class TableSchemaOut(Out):
    """A table's schema, as PUT .../_schema saved it."""
    strict: bool
    graph: bool
    bucket: bool
    fields: List[FieldInfo]


class RowsPage(Out):
    rows: List[Row]
    # The next page's cursor; absent on the last page
    next: Optional[str] = None


class RowCount(Out):
    count: int


class GraphEdge(BaseModel):
    """An edge: its type, both ends as ``table/id``, and its properties (as further fields)."""
    type: str
    # "from" is a Python keyword: the field is read from and written as "from".
    from_: str
    to: str

    # Open in the schema too, like Row: the properties are the edge's own.
    model_config = ConfigDict(extra="allow", populate_by_name=True,
                              alias_generator=lambda name: "from" if name == "from_" else name)


class GraphNode(Out):
    table: str
    row: Row


class GraphMore(Out):
    nodes: bool
    edges: bool


class GraphView(Out):
    nodes: List[GraphNode]
    edges: List[GraphEdge]
    # The graph tables the rows come from
    tables: List[str]
    # Edges leading from a loaded row to one not loaded
    outside: int
    # Whether the node or edge limit cut the result
    more: GraphMore


class EdgesPage(Out):
    edges: List[GraphEdge]
    next: Optional[str] = None


class Neighbor(Out):
    table: str
    # Null when the row is gone
    row: Optional[Row]
    edge: GraphEdge


class NeighborsPage(Out):
    neighbors: List[Neighbor]
    next: Optional[str] = None


class RowList(Out):
    # Rows, a traversal's {table, depth, row}, or what a query selects: JSON objects, not always with an id
    rows: List[Json]


class EdgeJoin(BaseModel):
    """A pair of tables an edge type joins: the edges of it that leave a row of ``from`` for a row of ``to``."""
    # "from" is a Python keyword, as in GraphEdge.
    from_: str
    to: str
    count: int

    model_config = ConfigDict(populate_by_name=True, alias_generator=lambda name: "from" if name == "from_" else name)


class EdgeTypeDetail(EdgeTypeInfo):
    # Most edges first
    joins: List[EdgeJoin]


class EdgeTypes(Out):
    edges: List[EdgeTypeDetail]


class VectorHit(Out):
    # The row, without its vector
    row: Row
    # How far it is from the vector searched for, by the field's similarity (lower is nearer): the route says how.
    # Absent for a row a hybrid search found by its words only
    distance: Optional[float] = None
    # A hybrid or reranked search's, higher is better: the reranker's score (0 to 1) when there is one, else the fused
    # rank score
    score: Optional[float] = None


class VectorSearch(Out):
    # Nearest first; best first for a hybrid search
    results: List[VectorHit]
    # The tokens the text took to embed, and to rerank with; absent when a vector was given
    tokens: Optional[int] = None
