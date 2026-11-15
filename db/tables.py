"""
Tables of JSON rows grouped in workspaces, in the embedded DuckDB database
every store shares (see engine.py; one process can open it).

A workspace is a DuckDB schema of its name and a record in
``main.tk_workspace``, maybe with a ``label`` (free text);
names the database keeps (main, toolkit, fts_...: schema.RESERVED) and its
own name (its file's, which SQL could not tell from a schema's) are
refused, and two that differ only by case are one to DuckDB. Only the admin makes
or drops one. ``default`` always exists: it is made when the workspaces are
first loaded, and cannot be dropped (its tables can, one by one). The
service's own workspaces (evals, tasks, enrichers, the playground:
``manage``) are managed: the list leaves them out unless asked.

What the service keeps is written only by the part of it that keeps it. A
*managed* workspace (``manage``) is its store's, every table of it. A *kept*
table (``keep``) is one whose name starts with a prefix its owner declared
(evidence's ``evidence_``), in any workspace, and is made by that owner
(``ensure_tables``); an edge type so named is the owner's too, since tables
and edge types share a workspace's names. Every write here (rows, edges, a
table, a schema, a label, a drop) takes the writer as ``owner`` and is
refused (Forbidden, naming the keeper) when it touches either and ``owner``
is not its keeper; dropping a workspace that holds kept tables or edge types
is refused too, whole, before anything is dropped. /db passes no owner, so
there they are read-only, and reading them needs the admin key
(api/table_routes.py).
Stores that write their own types in SQL (DocumentStore) are not checked.

A table is a DuckDB table ``"<workspace>"."<table>"`` with a unique
``id``, made by an admin or on its first write (ddl.py says its columns).
Names are letters, digits and single underscores (never two in a row, at
either end); a table and an edge type of a workspace never share a name,
case aside, and no two tables share the name DuckDB gives their full-text
index (fts_<workspace>_<table>: a_b/c and a/b_c would).

A table is a plain one unless it is a *graph* table (``graph`` in its
schema, chosen when it is created and never changed): its rows can then be
linked by edges. The workspace's edges, of every type, are rows of its
``_edges`` table; an edge type is recorded when it is made, or with its
first edge; at most one edge of a type joins a given pair of rows, so
putting an edge again replaces its properties. Edges go with the rows they
join: deleting a row deletes its edges, and so does dropping a table.
Neighbors, traversals and read-only SQL queries read the graph; see graph.py
(GraphStore).

Each table has a schema, empty unless an admin declares one: fields with a
type (a list of it, if ``list``), maybe required, indexed or unique, and
``strict`` to refuse fields not declared. A field of type ``ref`` holds row
ids of a table of the workspace, checked on every write, followed by a
delete (restrict, unset, cascade) and by a read's ``_expand``: refs.py. Rows are checked against it here,
on every write; DuckDB would quietly convert (2.7 into an integer column is
3). The schema is a record in ``main.tk_table``; a declared field is a typed
column, and an indexed one has an index on it too (but a list: DuckDB indexes
none, so its filters scan). Changing the schema checks the rows already
there first.

Filters are equality on top-level fields. On a declared field the value is
read as the field's type and the filter is the one predicate it needs
(``=``, or ``list_contains`` for a list), which its index serves when it has
one. On any other field (a JSON value in ``_rest``) a list matches when it
holds the value, else the value must equal it. Field
names and sort keys are checked against FIELD before they reach SQL, and
values always go in as parameters.

A string field can be ``fulltext`` instead of indexed: the table's full-text
fields share one index (DuckDB's fts, BM25), and a search (``_search``, read
by schema.search_query) finds the rows that hold its words in any of them,
the best match first unless sorted otherwise; rows.py tells how the index,
which does not follow writes, and the rows written since it was built are
read together. The index does not keep where words are, so a phrase of
several words is checked in the rows, as written (case aside).

A table may have one vector field (schema.py): a FLOAT[dimensions] column,
made when the field is made (a schema put or a new table) and never on a
write; removing the field drops the column and the rows' vectors.
``search`` finds the ``k`` rows nearest a vector, or a text embedded with
the field's model, among those the filters and the full-text words select,
nearest first, by computing every such row's distance; ranked by the
text's words too (hybrid), or read again with a reranker (``rerank``, also
built on the model manager), best first. A field with a
``model`` is embedded from its ``from`` text by the async writes, before
their blocking part runs on the pool: one batch per write (``embed``, which
api/app.py builds on the model manager, since db/ cannot import models/),
once the owner, the rows and the table's room are checked, so a write
refused for any of those spends no model time (``_embedded``); a model
outside ALLOWED_MODELS is refused there, as when the field is made and its
dimensions are found by embedding a probe text. The field's document prompt
goes before each row's text and its query prompt before a search's; made
without them, it takes the model's own (``prompts``, which api/app.py also
builds on the model manager). A table with a vector field holds
``vector_max_rows`` rows at most (VECTOR_MAX_ROWS: a search computes every
row's distance, so its time grows with them), past which a write is refused
(TooLarge). Row lists, searches, bulk reads and the graph view leave the
vectors out (hundreds of numbers a row); reading one row gives it whole.

A *bucket* is a table of files (schema.py's ``BUCKET``: ``bucket`` set when
an admin makes it, its fields fixed, a plain table). Its rows are read as
any table's, with the client key, but only the file routes write them
(buckets/, passing ``owner=FILES``), so each row has its bytes on disk: a
row write through /db (a row, a bulk write, a transaction's puts or
deletes, an edge at one) is refused (Forbidden), as for a kept table, and a
schema put is refused (TableError). Unlike a kept table, a bucket is the
admin's to drop, alone or with its workspace. Its bytes are in a folder
named after its workspace and itself, kept by ``files`` (buckets/; db/
cannot import it, so api/app.py gives it, as it gives ``embed``): asked
before a bucket is made (Unavailable when the store was given none;
Conflict when the folder already holds files), and told after the
async drops have dropped buckets, which removes their folders. A bucket
whose folder would be another bucket's when case is ignored (on a disk that
ignores it) is refused too.

Which remote a bucket names, and which of its files are copied there, is
buckets/'s (naming.py), not this store's.

What is kept beside a workspace rather than in it (views/'s views and
dashboards) goes when it is dropped: ``on_drop`` holds what to tell, as
``files`` is for buckets, since db/ cannot import those packages.
"""

import threading
from typing import Callable, Dict, List, Optional, Tuple

from .ddl import SCAN_BATCH, Ddl  # noqa: F401
from .engine import Database, Gate, Store
from .refs import BACK_LIMIT, EXPAND_DEPTH, Refs  # noqa: F401
from .rows import Rows, decoded, words_held  # noqa: F401
from .schema import TableSchema  # noqa: F401
from .vectors import PROBE, VECTOR_MAX_ROWS, Embed, Prompts, Rerank, Vectors  # noqa: F401
from .workspaces import (DEFAULT, FILES, KEPT_PREFIX, UNDECLARED, FileKeeper, Managed, Schemas,  # noqa: F401
                         Workspaces)
# Also importable from here: what the packages above use of schema.py, which they do not import (tests/test_layers.py,
# PUBLIC: db/ is theirs as db, db.tables, db.graph and db.specs).
from .schema import (EMPTY, Conflict, Field, TableError, check_id, decode_cursor, encode_cursor, make_schema,  # noqa: F401
                     merge_patch, schema_of, search_query, sort_keys, table_type)
from .schema import (DIRECTIONS, FIELD, GRAPH_MAX_EDGES, GRAPH_MAX_NODES, MAX_DEPTH, MAX_DIMENSIONS, MAX_FILE_NAME,  # noqa: F401
                     MAX_K, MAX_NAME, MAX_PROMPT, MAX_SEARCH, NAME, ON_DELETE, QUANTIZATIONS, SIMILARITIES, TYPES)


class TableStore(Workspaces, Ddl, Refs, Rows, Vectors, Store):
    """The /db tables of every workspace, as the module's docstring tells: its parts are the mixins it is made of,
    which share the state made here."""

    def __init__(self, database: Database, vector_max_rows: int = VECTOR_MAX_ROWS,
                 embed_max_chars: Optional[int] = None, *, embed: Optional[Embed] = None,
                 prompts: Optional[Prompts] = None, rerank: Optional[Rerank] = None):
        super().__init__(database)
        # What vector fields with a model embed with (api/app.py gives the model manager's); None refuses those writes
        # (Unavailable).
        self.embed = embed
        # An embedding model's own prompts, which a vector field made without its prompts takes; None: it has none.
        self.prompts = prompts
        # What a search with a reranker reads its candidates with; None refuses those searches (Unavailable).
        self.rerank = rerank
        self.vector_max_rows = vector_max_rows
        # The longest text the embedder takes (MAX_INPUT_CHARS), so a row with a longer one is refused by name.
        self.embed_max_chars = embed_max_chars
        # What keeps the buckets' bytes; None refuses making a bucket (Unavailable).
        self.files: Optional[FileKeeper] = None
        # Told that a workspace was dropped, while the schema lock is still held (so it is not made again meanwhile):
        # what is kept beside a workspace goes with it (views/, which db/ cannot import: api/app.py adds Views.forget).
        # Blocking; a failure is logged, as the workspace is gone already.
        self.on_drop: List[Callable[[str], None]] = []
        # Loaded once, kept current by this process's schema changes. Once loaded it is read without the lock,
        # which only schema changes take, so a slow one (a scan of every row, an index build, a drop) holds up the
        # other schema changes and not every request.
        self._schemas: Optional[Schemas] = None
        self._loaded = False
        # workspace -> its edge types
        self._edge_types: Dict[str, set] = {}
        # (workspace, table) -> the write its full-text index holds up to (None: no index), when it was last built
        # and how long that took (time.time(), seconds), and a lock per table for its rebuilds (rows.py).
        self._fulltext_seq: Dict[Tuple[str, str], Optional[int]] = {}
        self._fulltext_built: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._rebuild_locks: Dict[Tuple[str, str], threading.Lock] = {}
        self._schema_lock = threading.RLock()
        # (workspace, table) -> its gate: its writes share it, a change of its schema has it alone (ddl.py). Got under
        # a lock of their own, as writes never take the schema lock.
        self._gates: Dict[Tuple[str, str], Gate] = {}
        self._gates_lock = threading.Lock()
        # workspace -> its ref lock (refs.py), got under the gates' lock.
        self._ref_locks: Dict[str, threading.RLock] = {}
        # Workspaces the service keeps its own data in, as its stores declare them: made by the service, written
        # only by their store, admin-only through /db (api/table_routes.py), never made or dropped there.
        self.managed: Dict[str, Managed] = {}
        # The workspaces the database marks as managed, declared by a store of this run or not (UNDECLARED).
        self._marked: set = set()
        # Kept tables: a prefix of table names -> what keeps every table so named, in any workspace (``keep``).
        self.kept: Dict[str, str] = {}
