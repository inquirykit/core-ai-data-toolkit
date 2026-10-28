"""/db: namespaces, tables, schemas, rows, edges and graph views."""

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict

from .common import Json, Out


class Row(BaseModel):
    """A table's row: any JSON object, whose id is a string (every write checks it). Its schema stays open."""
    model_config = ConfigDict(extra="allow")

    id: str


class FieldInfo(Out):
    """A declared field: rows hold ``type`` there (a list of it when ``list``), or leave it out unless ``required``."""
    name: str
    type: Literal["string", "integer", "number", "boolean", "object", "any"]
    list: bool
    required: bool
    indexed: bool
    unique: bool


class TableInfo(Out):
    name: str
    count: int
    # On disk: its bucket file and its indexes' files
    size_bytes: int
    strict: bool
    # A graph table: its rows can be joined by edges
    graph: bool
    fields: List[FieldInfo]


class EdgeTypeInfo(Out):
    name: str
    count: int
    size_bytes: int


class Namespace(Out):
    name: str
    tables: List[TableInfo]
    edges: List[EdgeTypeInfo]
    # The sum of its tables' and edge types' sizes
    size_bytes: int
    # Kept by the service (the evals, the evidence ledger), and what keeps it: read-only through /db
    managed: Optional[str]
    created: Optional[float] = None


class Namespaces(Out):
    namespaces: List[Namespace]


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
    fields: List[FieldInfo]
    observed: Observed


class TableSchemaOut(Out):
    """A table's schema, as PUT .../_schema saved it."""
    strict: bool
    graph: bool
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
