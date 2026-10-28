"""
Namespaced tables of JSON rows, in the embedded ArcadeDB database every store
shares (see engine.py; one process can open it).

A namespace is a record in the type ``tk__namespace``; only the admin makes
or drops one. A table is an ArcadeDB document type ``ns_<namespace>__<table>``
with a unique ``id``, made by an admin or on its first write. Names are
letters, digits and single underscores (never two in a row, at either end),
so the type name always splits back into one namespace and one table, and
never collides with a collection's ``tk_<name>``.

A table is made as a plain document type unless it is a *graph* table
(``graph`` in its schema, chosen when it is created and never changed): then
it is an ArcadeDB vertex type, which reads and writes exactly like a plain
one but whose rows can be linked by edges. An edge type is an ArcadeDB edge
type ``ne_<namespace>__<name>`` (so it never collides with a table), made on
the first edge of that name; at most one edge of a type joins a given pair
of rows, so putting an edge again replaces its properties. Edges go with the
rows they join: deleting a row deletes its edges, and so does dropping a
graph table (its rows are deleted first, since ArcadeDB would otherwise
leave the edges dangling). Neighbors, traversals and read-only SQL or Cypher
queries read the graph; see graph.py (GraphStore).

Each table has a schema, empty unless an admin declares one: fields with a
type (a list of it, if ``list``), maybe required, indexed or unique, and
``strict`` to refuse fields not declared. Rows are checked against it here,
on every write; ArcadeDB would quietly convert (2.7 into an integer field is
2). The schema is a record in ``tk__table``; an indexed field is also a
typed ArcadeDB property with an index on it (``BY ITEM`` for a list, so each
item is a key). Changing the schema checks the rows already there first.

Filters are equality on top-level fields. On a declared field the value is
read as the field's type and the filter is the one predicate it needs
(``=``, or ``CONTAINS`` for a list), which its index serves when it has one.
On any other field a list matches when it holds the value, both tried. Field
names and sort keys are checked against FIELD before they reach SQL, and
values always go in as parameters.
"""

import functools
import json
import logging
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Tuple

from .engine import Database, retry_concurrent
from .schema import (edge_type_of, edge_type_name_of, FROM_ARCADE, ordered, split_type, row_type, row_type_name,
    check_edge, check_edge_ref, EdgeRef, check_field, check_name, check_row, Conflict, EDGE_PREFIX, EMPTY,
    encode_cursor, Field, filter_value, INDEXABLE, merge_patch, NAMESPACES, NotFound, observe, PREFIX, TableError,
    TABLES, TableSchema)
# Also importable from here: what the routes and the stores that keep /db tables (evals/, jobs/, enrichers/, evidence/) use.
from .schema import check_id, decode_cursor, make_schema, schema_of, table_type  # noqa: F401
from .schema import DIRECTIONS, GRAPH_MAX_EDGES, GRAPH_MAX_NODES, MAX_DEPTH, QUERY_LANGUAGES, TYPES  # noqa: F401

logger = logging.getLogger(__name__)

SCAN_BATCH = 2000
DUPLICATE = re.compile(r"Duplicated key \[(.*?)\] found on index '([^']+)'")

Filters = Sequence[Tuple[str, str]]  # (field, the value as the query string gives it)
Sort = Sequence[Tuple[str, bool]]  # (field, descending)


def _arcade_message(e: Exception) -> str:
    return str(e).split("Exception: ")[-1].split(" (caused by")[0][:400]


# namespace -> table -> its schema
Schemas = Dict[str, Dict[str, TableSchema]]


@dataclass(frozen=True)
class Kept:
    """A namespace the service keeps its own data in: what keeps it, and its tables with their schemas."""
    owner: str
    tables: Dict[str, TableSchema]
    # Take over a namespace of that name made through /db (the playground made its own that way until 2026-10).
    adopt: bool = False


# What keeps a namespace the database marks as kept when no store of this run declares it: its store is switched
# off (EVIDENCE_DIR or ENRICHERS_DIR left empty, say). It stays admin-only and read-only through /db all the same.
SWITCHED_OFF = "a part of the service that is switched off"


def _retried(method: Callable) -> Callable:
    """A write run again, whole, after a concurrent change to the same record: it re-reads what it needs."""
    @functools.wraps(method)
    def wrapper(self, *args: Any) -> Any:
        return retry_concurrent(lambda: method(self, *args))
    return wrapper



class TableStore:
    def __init__(self, database: Database):
        self.database = database
        # Loaded once, kept current by this process's schema changes. Once loaded it is read without the lock,
        # which only schema changes take, so a slow one (a scan of every row, an index build, a drop) holds up the
        # other schema changes and not every request.
        self._schemas: Optional[Schemas] = None
        self._loaded = False
        # namespace -> its edge types
        self._edge_types: Dict[str, set] = {}
        self._schema_lock = threading.RLock()
        # Namespaces the service keeps its own data in, and prefixes of such namespaces (an evidence workspace's),
        # as its stores declare them: made by the service, admin-only and read-only through /db
        # (api/table_routes.py), never made or dropped there.
        self.managed: Dict[str, Kept] = {}
        self.managed_prefixes: Dict[str, Kept] = {}
        # The namespaces the database marks as kept, declared by a store of this run or not (SWITCHED_OFF).
        self._kept: set = set()

    def manage(self, namespace: str, owner: str, tables: Dict[str, TableSchema], adopt: bool = False) -> None:
        """Keep ``namespace`` for a store of the service's, with these tables: made, or brought up to date, when
        the namespaces are first loaded (``ensure_managed``)."""
        self.managed[check_name("namespace", namespace)] = Kept(owner, tables, adopt)

    def manage_prefix(self, prefix: str, owner: str, tables: Dict[str, TableSchema]) -> None:
        """Keep every namespace starting with ``prefix`` (``evidence_``) for a store of the service's, each with
        these tables, made when the store calls ``ensure_managed`` for it."""
        self.managed_prefixes[prefix] = Kept(owner, tables)

    def _declared(self, namespace: str) -> Optional[Kept]:
        if namespace in self.managed:
            return self.managed[namespace]
        return next((kept for prefix, kept in self.managed_prefixes.items() if namespace.startswith(prefix)), None)

    def owner_of(self, namespace: str) -> Optional[str]:
        """What keeps ``namespace`` when the service does (see ``manage``), else None. One the database marks as
        kept stays kept when no store declares it now (SWITCHED_OFF), which needs the namespaces loaded: ``owner``."""
        kept = self._declared(namespace)
        if kept is not None:
            return kept.owner
        return SWITCHED_OFF if namespace in self._kept else None

    async def owner(self, namespace: str) -> Optional[str]:
        """``owner_of``, once the namespaces are loaded (the first time, on the database's pool)."""
        if not self._loaded or self._schemas is None:
            await self._run(self._load)
        return self.owner_of(namespace)

    def has_namespace(self, namespace: str) -> bool:
        """Blocking."""
        return namespace in self._load()

    def ensure_managed(self, namespace: str) -> None:
        """Make the kept namespace and its tables (as ``manage`` or ``manage_prefix`` declared them) where missing,
        and give a table made by an earlier version the schema declared now (when its rows fit it; else that is
        logged). A namespace of that name an admin made through /db is refused, not taken over, unless the store
        declared ``adopt``. Blocking."""
        kept = self._declared(namespace)
        if kept is None:
            raise ValueError(f"No store keeps the namespace {namespace!r}: declare it with manage()")
        with self._schema_lock:
            db = self._db()
            if namespace not in self._load():
                with db.transaction():
                    db.command("sql", f"INSERT INTO {NAMESPACES} CONTENT {{name: :name, created: :created, managed: true}}",
                               {"name": namespace, "created": time.time()})
                self._schemas[namespace] = {}
            elif namespace not in self._kept:
                if not kept.adopt:
                    raise RuntimeError(f"The namespace {namespace!r} was made through /db before the service reserved it "
                                       f"for {kept.owner}: rename or drop it")
                with db.transaction():
                    db.command("sql", f"UPDATE {NAMESPACES} SET managed = true WHERE name = :n", {"n": namespace})
                logger.info("The namespace %s, made through /db, is kept by the service from now on", namespace)
            self._kept.add(namespace)
            for table, schema in kept.tables.items():
                if not self._has(namespace, table):
                    self._create_table(namespace, table, schema)
                elif replace(self._schema(namespace, table), graph=schema.graph) != schema:
                    try:
                        self._set_schema(namespace, table, schema)
                        logger.info("Schema of %s/%s brought up to date", namespace, table)
                    except TableError as e:
                        logger.error("Could not bring the schema of %s/%s up to date: %s", namespace, table, e)

    async def _run(self, fn, *args):
        return await self.database.run(fn, *args)

    def _db(self):
        return self.database.open()

    def _query(self, sql: str, params: Optional[Dict[str, Any]] = None, db=None) -> List[Dict[str, Any]]:
        return self.database.rows((db or self._db()).query("sql", sql, params or {}))

    def _type_indexes(self, type_name: str) -> List[Tuple[str, str, bool]]:
        """(index name, field, unique) of the type's own indexes, not their per-bucket parts."""
        found = []
        for r in self._query("SELECT FROM schema:indexes WHERE typeName = :t", {"t": type_name}):
            props = [p for group in r.get("properties") or [] for p in group]
            if r.get("associatedBucketId") is None and len(props) == 1:
                found.append((r["name"], props[0].split(" by item")[0], bool(r.get("unique"))))
        return found

    @contextmanager
    def _writing(self) -> Iterator[Any]:
        """A transaction whose unique-key clashes (within it, or with a concurrent writer) are Conflicts."""
        db = self._db()
        try:
            with db.transaction():
                yield db
        except TableError:
            raise
        except Exception as e:
            found = DUPLICATE.search(str(e))
            if not found:
                raise
            value, index = found.groups()
            # The error names an index, often a per-bucket part of one: find its field.
            rows = self._query("SELECT FROM schema:indexes WHERE name = :n", {"n": index})
            props = [p for group in (rows[0].get("properties") or [] if rows else []) for p in group]
            name = props[0] if len(props) == 1 else None
            if name == "id":
                raise Conflict(f"Row {value!r} already exists") from None
            if name:
                raise Conflict(f"Another row already has {name} = {value!r}, and {name} is unique") from None
            raise Conflict(f"Another row already has the unique value {value!r}") from None

    # ---------------------------------------------------------------- schema
    def _load(self) -> Schemas:
        schemas = self._schemas
        if self._loaded and schemas is not None:
            return schemas
        with self._schema_lock:
            if self._schemas is None:
                db = self._db()
                if not self.database.has_type(NAMESPACES):
                    db.command("sql", f"CREATE DOCUMENT TYPE {NAMESPACES}")
                    db.command("sql", f"CREATE PROPERTY {NAMESPACES}.name STRING")
                    db.command("sql", f"CREATE INDEX ON {NAMESPACES} (name) UNIQUE")
                if not self.database.has_type(TABLES):
                    db.command("sql", f"CREATE DOCUMENT TYPE {TABLES}")
                    db.command("sql", f"CREATE PROPERTY {TABLES}.type STRING")
                    db.command("sql", f"CREATE INDEX ON {TABLES} (type) UNIQUE")
                saved = {r["type"]: r for r in self._query(f"SELECT type, strict, fields FROM {TABLES}")}
                names = self._query(f"SELECT name, managed FROM {NAMESPACES}")
                schemas: Schemas = {r["name"]: {} for r in names}
                self._kept = {r["name"] for r in names if r.get("managed")}
                edge_types: Dict[str, set] = {n: set() for n in schemas}
                for r in self._query("SELECT name, type FROM schema:types"):
                    name = r["name"]
                    if (edge := split_type(name, EDGE_PREFIX)) and r.get("type") == "edge":
                        if edge[0] in edge_types:
                            edge_types[edge[0]].add(edge[1])
                        continue
                    split = split_type(name, PREFIX)
                    if split is None or split[0] not in schemas:
                        continue
                    namespace, table = split
                    # Whether it is a vertex type is the database's word, not the saved schema's.
                    graph = r.get("type") == "vertex"
                    if name in saved:
                        fields = [Field(**f) for f in json.loads(saved[name]["fields"] or "[]")]
                        schemas[namespace][table] = TableSchema({f.name: f for f in fields},
                                                                bool(saved[name]["strict"]), graph)
                    else:
                        schemas[namespace][table] = replace(self._schema_from_indexes(name), graph=graph)
                self._schemas = schemas
                self._edge_types = edge_types
                # The service's namespaces exist from the start, so /db lists them before their stores are used.
                for namespace in self.managed:
                    try:
                        self.ensure_managed(namespace)
                    except Exception as e:
                        logger.error("Could not make the namespace %s: %s", namespace, e)
                self._loaded = True
            return self._schemas

    def _schema_from_indexes(self, type_name: str) -> TableSchema:
        """A table with no saved schema: its indexed properties, as declared fields."""
        indexed = {name: unique for _, name, unique in self._type_indexes(type_name)}
        fields = {}
        for p in self._query("SELECT expand(properties) FROM schema:types WHERE name = :n", {"n": type_name}):
            is_list = p.get("type") == "LIST"
            kind = FROM_ARCADE.get(p.get("ofType") if is_list else p.get("type"))
            if p["name"] != "id" and kind and p["name"] in indexed:
                fields[p["name"]] = Field(p["name"], kind, is_list, indexed=True, unique=indexed[p["name"]])
        return TableSchema(fields)

    def _tables(self, namespace: str) -> Dict[str, TableSchema]:
        tables = self._load().get(namespace)
        if tables is None:
            raise NotFound(f"No namespace named {namespace!r}; an admin creates namespaces")
        return tables

    def _has(self, namespace: str, table: str) -> bool:
        return check_name("table", table) in self._tables(namespace)

    def _schema(self, namespace: str, table: str) -> TableSchema:
        return self._tables(namespace).get(table, EMPTY)

    def _ensure(self, namespace: str, tables: Sequence[str], graph: Optional[bool] = None) -> None:
        """Make the tables missing among ``tables``, as graph tables (vertex types) if ``graph``. Unsaid, a table is
        a graph table, which edges can join, unless the service keeps the namespace (its stores say what they
        make). Schema changes run outside transactions, so before them."""
        if graph is None:
            graph = self.owner_of(namespace) is None
        # Tables already there need no lock, so a write to one never waits for a schema change.
        if all(check_name("table", table) in self._tables(namespace) for table in tables):
            return
        with self._schema_lock:
            known = self._tables(namespace)
            for table in tables:
                if check_name("table", table) in known:
                    continue
                # A query names tables and edge types alike by their bare names, so one name is never both.
                if table in self._edges_of(namespace):
                    raise Conflict(f"{namespace!r} already has a relationship (edge type) named {table!r}; pick another name")
                db = self._db()
                t = row_type(namespace, table)
                db.command("sql", f"CREATE {'VERTEX' if graph else 'DOCUMENT'} TYPE {t}")
                db.command("sql", f"CREATE PROPERTY {t}.id STRING")
                db.command("sql", f"CREATE INDEX ON {t} (id) UNIQUE")
                known[table] = replace(EMPTY, graph=graph)

    @contextmanager
    def _making(self, namespace: str, tables: Sequence[str], edge_types: Sequence[str]) -> Iterator[None]:
        """Make the missing tables and edge types a write needs, before its transaction (schema changes are not part
        of one); if the write fails, drop the ones made here again, unless another writer has filled them since."""
        # What is there already needs no lock (see _schemas).
        tables = [t for t in dict.fromkeys(tables) if check_name("table", t) not in self._tables(namespace)]
        edge_types = [t for t in dict.fromkeys(edge_types) if check_name("edge type", t) not in self._edges_of(namespace)]
        made_tables: List[str] = []
        made_types: List[str] = []
        try:
            if tables or edge_types:
                with self._schema_lock:
                    for table in tables:
                        if table not in self._tables(namespace):
                            self._ensure(namespace, [table])
                            made_tables.append(table)
                    for edge_type in edge_types:
                        if edge_type not in self._edges_of(namespace):
                            self._ensure_edge_type(namespace, edge_type)
                            made_types.append(edge_type)
            yield
        except BaseException:
            try:
                with self._schema_lock:
                    for edge_type in made_types:
                        if edge_type in self._edges_of(namespace) and not self._edge_info(namespace, edge_type)["count"]:
                            self._drop_edge_type(namespace, edge_type)
                    for table in made_tables:
                        if self._has(namespace, table) and not self._count(namespace, table):
                            self._drop_table(namespace, table)
            except Exception:
                logger.exception("Could not drop what a failed write made in %s", namespace)
            raise

    def _edges_of(self, namespace: str) -> set:
        self._tables(namespace)
        return self._edge_types.setdefault(namespace, set())

    def _ensure_edge_type(self, namespace: str, edge_type: str) -> None:
        with self._schema_lock:
            known = self._edges_of(namespace)
            if check_name("edge type", edge_type) in known:
                return
            if edge_type in self._tables(namespace):
                raise Conflict(f"{namespace!r} already has a table named {edge_type!r}; pick another name")
            self._db().command("sql", f"CREATE EDGE TYPE {edge_type_of(namespace, edge_type)}")
            known.add(edge_type)

    # ------------------------------------------------------------ namespaces
    def _count(self, namespace: str, table: str, filters: Filters = ()) -> int:
        if not self._has(namespace, table):
            return 0
        where, params = self._where(filters, self._schema(namespace, table))
        rows = self._query(f"SELECT count(*) AS n FROM {row_type(namespace, table)}{where}", params)
        return rows[0]["n"] if rows else 0

    def _size(self, namespace: str, table: str) -> int:
        """Bytes the table takes on disk: its bucket files, its indexes' files and, for a graph table, the files
        of its rows' edge lists (ArcadeDB keeps a vertex type's in and out edges beside each bucket).

        Files grow a page at a time (64 KB for a bucket, 256 KB for an index),
        so an empty table already has a few hundred KB, and deleting rows
        frees space for later writes but never shrinks a file.
        """
        if not self._has(namespace, table):
            return 0
        return self._type_size(row_type_name(namespace, table))

    def _type_size(self, type_name: str) -> int:
        java = self._db().get_java_database()
        kind = java.getSchema().getType(type_name)
        buckets = [str(b.getName()) for b in kind.getBuckets(False)]
        # A vertex type's buckets each have an in and an out edge list of their own.
        names = {*buckets, *(f"{b}_{way}_edges" for b in buckets for way in ("in", "out"))}
        for index in kind.getAllIndexes(False):
            parts = index.getSubIndexes() if hasattr(index, "getSubIndexes") else [index]
            names.update(str(part.getName()) for part in parts)
        # A file is named <bucket or index>.<file id>.<page size>.v<n>.<ext>.
        prefixes = tuple(f"{n}." for n in names)
        return sum(int(f.getSize()) for f in java.getFileManager().getFiles()
                   if f is not None and str(f.getFileName()).startswith(prefixes))

    def _table_info(self, namespace: str, table: str) -> Dict[str, Any]:
        return {"name": table, "count": self._count(namespace, table), "size_bytes": self._size(namespace, table),
                **self._schema(namespace, table).to_dict()}

    def _edge_info(self, namespace: str, edge_type: str) -> Dict[str, Any]:
        t = edge_type_name_of(namespace, edge_type)
        rows = self._query(f"SELECT count(*) AS n FROM `{t}`")
        return {"name": edge_type, "count": rows[0]["n"] if rows else 0, "size_bytes": self._type_size(t)}

    def _describe(self, namespace: str) -> Dict[str, Any]:
        tables = [self._table_info(namespace, t) for t in sorted(self._tables(namespace))]
        edges = [self._edge_info(namespace, e) for e in sorted(self._edges_of(namespace))]
        return {"name": namespace, "tables": tables, "edges": edges,
                "size_bytes": sum(t["size_bytes"] for t in tables) + sum(e["size_bytes"] for e in edges),
                "managed": self.owner_of(namespace)}

    def _namespaces(self) -> List[Dict[str, Any]]:
        # Makes the namespace type on a fresh database, before it is read.
        self._load()
        created = {r["name"]: r.get("created") for r in self._query(f"SELECT name, created FROM {NAMESPACES}")}
        return [{**self._describe(n), "created": created.get(n)} for n in sorted(self._load())]

    async def namespaces(self) -> List[Dict[str, Any]]:
        """Every namespace with its tables, their row counts, sizes on disk and schemas."""
        return await self._run(self._namespaces)

    async def namespace(self, namespace: str) -> Dict[str, Any]:
        return await self._run(self._describe, namespace)

    def _create_namespace(self, namespace: str) -> None:
        check_name("namespace", namespace)
        with self._schema_lock:
            known = self._load()
            owner = self.owner_of(namespace)
            if owner:
                raise Conflict(f"The namespace {namespace!r} is kept by the service ({owner})")
            if namespace in known:
                raise Conflict(f"A namespace named {namespace!r} already exists")
            db = self._db()
            with db.transaction():
                db.command("sql", f"INSERT INTO {NAMESPACES} CONTENT {{name: :name, created: :created}}",
                           {"name": namespace, "created": time.time()})
            self._schemas[namespace] = {}

    async def create_namespace(self, namespace: str) -> None:
        await self._run(self._create_namespace, namespace)

    def _drop_table(self, namespace: str, table: str) -> None:
        with self._schema_lock:
            if not self._has(namespace, table):
                raise NotFound(f"No table {table!r} in {namespace!r}")
            db = self._db()
            # Deleting the rows deletes their edges, which dropping the type alone would leave dangling. Edges join
            # rows of one namespace, so where it has no edge type no row has an edge.
            if self._schema(namespace, table).graph and self._edges_of(namespace):
                self._delete_rows(db, namespace, table)
            db.command("sql", f"DROP TYPE {row_type(namespace, table)} UNSAFE")
            with db.transaction():
                db.command("sql", f"DELETE FROM {TABLES} WHERE type = :t", {"t": row_type_name(namespace, table)})
            del self._schemas[namespace][table]

    def _delete_rows(self, db, namespace: str, table: str) -> None:
        """Delete every row, a batch per transaction along the id index, so a big table needs no huge transaction;
        the last one catches rows written meanwhile."""
        after = ""
        while True:
            ids = [r["id"] for r in self._query(f"SELECT id FROM {row_type(namespace, table)} WHERE id > :last "
                                                f"ORDER BY id LIMIT :n", {"last": after, "n": SCAN_BATCH})]
            if not ids:
                break
            with db.transaction():
                self._delete_ids(db, namespace, table, ids)
            after = ids[-1]
        with db.transaction():
            db.command("sql", f"DELETE FROM {row_type(namespace, table)}")

    def _drop_edge_type(self, namespace: str, edge_type: str) -> None:
        with self._schema_lock:
            if check_name("edge type", edge_type) not in self._edges_of(namespace):
                raise NotFound(f"No edge type {edge_type!r} in {namespace!r}")
            self._db().command("sql", f"DROP TYPE {edge_type_of(namespace, edge_type)} UNSAFE")
            self._edge_types[namespace].discard(edge_type)

    async def drop_edge_type(self, namespace: str, edge_type: str) -> None:
        """Delete the edge type with every edge of it."""
        await self._run(self._drop_edge_type, namespace, edge_type)

    async def drop_table(self, namespace: str, table: str) -> None:
        await self._run(self._drop_table, namespace, table)

    def _drop_namespace(self, namespace: str) -> None:
        with self._schema_lock:
            for table in list(self._tables(namespace)):
                self._drop_table(namespace, table)
            for edge_type in list(self._edges_of(namespace)):
                self._drop_edge_type(namespace, edge_type)
            db = self._db()
            with db.transaction():
                db.command("sql", f"DELETE FROM {NAMESPACES} WHERE name = :name", {"name": namespace})
            del self._schemas[namespace]
            self._edge_types.pop(namespace, None)
            self._kept.discard(namespace)

    async def drop_namespace(self, namespace: str) -> None:
        """Delete a namespace with all its tables and rows."""
        await self._run(self._drop_namespace, namespace)

    # ---------------------------------------------------------------- tables
    def _create_table(self, namespace: str, table: str, schema: TableSchema) -> Dict[str, Any]:
        with self._schema_lock:
            if self._has(namespace, table):
                raise Conflict(f"A table named {table!r} already exists in {namespace!r}")
            self._ensure(namespace, [table], schema.graph)
            try:
                self._set_schema(namespace, table, schema)
            except Exception:
                # All of it, or no table.
                self._drop_table(namespace, table)
                raise
            return self._table_info(namespace, table)

    async def create_table(self, namespace: str, table: str, schema: TableSchema = EMPTY) -> Dict[str, Any]:
        """Make an empty table with this schema; Conflict if it exists."""
        return await self._run(self._create_table, namespace, table, schema)

    def _scan(self, namespace: str, table: str) -> Iterator[Dict[str, Any]]:
        """Every row, in pages along the id index."""
        after = ""
        while True:
            rows = self._query(f"SELECT * FROM {row_type(namespace, table)} WHERE id > :last ORDER BY id LIMIT :n",
                               {"last": after, "n": SCAN_BATCH})
            yield from rows
            if len(rows) < SCAN_BATCH:
                return
            after = rows[-1]["id"]

    def _check_rows(self, namespace: str, table: str, old: TableSchema, new: TableSchema) -> None:
        """TableError, naming a few rows, if rows there now break what the new schema adds."""
        changed = [f for name, f in new.fields.items()
                   if (o := old.fields.get(name)) is None or (o.type, o.list, o.required) != (f.type, f.list, f.required)]
        # Strict refuses undeclared fields: check when it comes on, or fields leave the schema.
        check_extra = new.strict and (not old.strict or not set(new.fields) >= set(old.fields))
        if not changed and not check_extra:
            return
        bad = []
        for row in self._scan(namespace, table):
            try:
                for f in changed:
                    f.check(row)
                extra = [k for k in row if k != "id" and k not in new.fields] if check_extra else []
                if extra:
                    raise TableError(f"{', '.join(extra)} not declared")
            except TableError as e:
                bad.append(f"{row.get('id')} ({e})")
                if len(bad) == 3:
                    break
        if bad:
            raise TableError(f"Rows there now do not fit the new schema: {'; '.join(bad)}")

    def _create_index(self, namespace: str, table: str, f: Field) -> None:
        db, t, col = self._db(), row_type(namespace, table), f"`{f.name}`"
        kind = INDEXABLE[f.type]
        db.command("sql", f"CREATE PROPERTY {t}.{col} {'LIST OF ' + kind if f.list else kind}")
        try:
            db.command("sql", f"CREATE INDEX ON {t} ({col}{' BY ITEM' if f.list else ''}) "
                              f"{'UNIQUE' if f.unique else 'NOTUNIQUE'}")
        except Exception as e:
            db.command("sql", f"DROP PROPERTY {t}.{col}")
            if type(e).__name__ == "ArcadeDBError":
                raise TableError(f"Could not index {f.name}: {_arcade_message(e)}") from None
            raise

    def _drop_index(self, namespace: str, table: str, name: str) -> None:
        db = self._db()
        for index, indexed, _ in self._type_indexes(row_type_name(namespace, table)):
            if indexed == name:
                db.command("sql", f"DROP INDEX `{index}`")
        db.command("sql", f"DROP PROPERTY {row_type(namespace, table)}.`{name}`")

    def _set_schema(self, namespace: str, table: str, new: TableSchema) -> TableSchema:
        with self._schema_lock:
            # A table made here (a schema put before any row) is of the namespace's kind: graph says nothing yet.
            self._ensure(namespace, [table])
            old = self._schema(namespace, table)
            # Whether a table is a graph table is settled when it is made.
            new = replace(new, graph=old.graph)
            self._check_rows(namespace, table, old, new)
            # Indexes whose field changed are dropped and made anew.
            keys = lambda s: {n: f.index_key for n, f in s.fields.items() if f.indexed}  # noqa: E731
            before, after = keys(old), keys(new)
            dropped = [n for n, k in before.items() if after.get(n) != k]
            created: List[str] = []
            try:
                for name in dropped:
                    self._drop_index(namespace, table, name)
                for name, k in after.items():
                    if before.get(name) != k:
                        self._create_index(namespace, table, new.fields[name])
                        created.append(name)
            except Exception:
                # Put the old indexes back; the rows were never touched.
                for name in created:
                    self._drop_index(namespace, table, name)
                for name in dropped:
                    self._create_index(namespace, table, old.fields[name])
                raise
            db = self._db()
            with db.transaction():
                db.command("sql", f"UPDATE {TABLES} CONTENT :r UPSERT WHERE type = :t", {
                    "t": row_type_name(namespace, table),
                    "r": {"type": row_type_name(namespace, table), "strict": new.strict,
                          "fields": json.dumps([asdict(f) for f in new.fields.values()])},
                })
            self._schemas[namespace][table] = new
            return new

    async def set_schema(self, namespace: str, table: str, schema: TableSchema) -> TableSchema:
        """Replace the table's schema (making the table if needed), after checking the rows there fit it.
        ``graph`` only counts when this makes the table; an existing table keeps its kind."""
        return await self._run(self._set_schema, namespace, table, schema)

    def _describe_schema(self, namespace: str, table: str, sample: int) -> Dict[str, Any]:
        rows = []
        if self._has(namespace, table):
            rows = self._query(f"SELECT * FROM {row_type(namespace, table)} ORDER BY id DESC LIMIT :n", {"n": sample})
        return {**self._schema(namespace, table).to_dict(), "observed": observe(rows)}

    async def schema(self, namespace: str, table: str, sample: int = 500) -> Dict[str, Any]:
        """The declared schema, and the fields the newest ``sample`` rows hold."""
        return await self._run(self._describe_schema, namespace, table, sample)

    # ----------------------------------------------------------------- reads
    @staticmethod
    def _where(filters: Filters, schema: TableSchema) -> Tuple[str, Dict[str, Any]]:
        terms, params = [], {}
        for i, (name, raw) in enumerate(filters):
            col = f"`{check_field(name)}`"
            declared = schema.fields.get(name)
            value = declared.parse(raw) if declared else filter_value(raw)
            if value is None:
                terms.append(f"{col} IS NULL")
                continue
            params[f"v{i}"] = value
            if declared and declared.list:
                terms.append(f"{col} CONTAINS :v{i}")
            elif declared and declared.type != "any":
                # The one predicate its index (if any) serves.
                terms.append(f"{col} = :v{i}")
            else:
                terms.append(f"({col} = :v{i} OR {col} CONTAINS :v{i})")
        return (" WHERE " + " AND ".join(terms) if terms else ""), params

    def _list(self, namespace: str, table: str, filters: Filters, sort: Sort, limit: int, offset: int):
        if not self._has(namespace, table):
            return [], None
        where, params = self._where(filters, self._schema(namespace, table))
        keys = [f"`{check_field(f)}`{' DESC' if desc else ''}" for f, desc in sort if f != "id"]
        # id last breaks ties, so pages never overlap or skip a row.
        id_desc = next((desc for f, desc in sort if f == "id"), False)
        keys.append("id DESC" if id_desc else "id")
        params.update(_skip=offset, _limit=limit + 1)
        rows = self._query(f"SELECT * FROM {row_type(namespace, table)}{where} ORDER BY {', '.join(keys)} "
                           f"SKIP :_skip LIMIT :_limit", params)
        more = len(rows) > limit
        return [ordered(r) for r in rows[:limit]], (encode_cursor(offset + limit) if more else None)

    async def list(self, namespace: str, table: str, filters: Filters = (), sort: Sort = (),
                   limit: int = 100, offset: int = 0) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """One page of rows the filters select, and the cursor of the next page (None on the last)."""
        return await self._run(self._list, namespace, table, filters, sort, limit, offset)

    async def count(self, namespace: str, table: str, filters: Filters = ()) -> int:
        return await self._run(self._count, namespace, table, filters)

    def _get(self, namespace: str, table: str, row_id: str, db=None) -> Optional[Dict[str, Any]]:
        if not self._has(namespace, table):
            return None
        rows = self._query(f"SELECT * FROM {row_type(namespace, table)} WHERE id = :id", {"id": row_id}, db)
        return ordered(rows[0]) if rows else None

    async def get(self, namespace: str, table: str, row_id: str) -> Dict[str, Any]:
        row = await self._run(self._get, namespace, table, row_id)
        if row is None:
            raise NotFound(f"No row {row_id!r} in {namespace}/{table}")
        return row

    def _bulk_get(self, namespace: str, table: str, ids: List[str]) -> List[Dict[str, Any]]:
        if not self._has(namespace, table) or not ids:
            return []
        rows = self._query(f"SELECT * FROM {row_type(namespace, table)} WHERE id IN :ids", {"ids": ids})
        by_id = {r["id"]: ordered(r) for r in rows}
        return [by_id[i] for i in dict.fromkeys(ids) if i in by_id]

    async def bulk_get(self, namespace: str, table: str, ids: List[str]) -> List[Dict[str, Any]]:
        """The rows with these ids, in the order asked; missing ones are left out."""
        return await self._run(self._bulk_get, namespace, table, ids)

    # ---------------------------------------------------------------- writes
    @staticmethod
    def _put(db, namespace: str, table: str, row: Dict[str, Any]) -> None:
        db.command("sql", f"UPDATE {row_type(namespace, table)} CONTENT :row UPSERT WHERE id = :id",
                   {"row": row, "id": row["id"]})

    @staticmethod
    def _delete_ids(db, namespace: str, table: str, ids: List[str]) -> None:
        db.command("sql", f"DELETE FROM {row_type(namespace, table)} WHERE id IN :ids", {"ids": ids})

    @_retried
    def _insert(self, namespace: str, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
        check_row(row, self._schema(namespace, table) if self._has(namespace, table) else EMPTY)
        self._ensure(namespace, [table])
        with self._writing() as db:
            if self._get(namespace, table, row["id"], db) is not None:
                raise Conflict(f"Row {row['id']!r} already exists in {namespace}/{table}; PUT replaces it")
            db.command("sql", f"INSERT INTO {row_type(namespace, table)} CONTENT :row", {"row": row})
        return ordered(row)

    async def insert(self, namespace: str, table: str, row: Dict[str, Any]) -> Dict[str, Any]:
        """Add a new row; Conflict if its id (or a unique field's value) is taken."""
        return await self._run(self._insert, namespace, table, row)

    @_retried
    def _upsert(self, namespace: str, table: str, row: Dict[str, Any]) -> bool:
        check_row(row, self._schema(namespace, table) if self._has(namespace, table) else EMPTY)
        self._ensure(namespace, [table])
        with self._writing() as db:
            created = self._get(namespace, table, row["id"], db) is None
            self._put(db, namespace, table, row)
        return created

    async def upsert(self, namespace: str, table: str, row: Dict[str, Any]) -> bool:
        """Store ``row``, replacing any with its id. True when it was new."""
        return await self._run(self._upsert, namespace, table, row)

    @_retried
    def _patch(self, namespace: str, table: str, row_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        if "id" in patch and patch["id"] != row_id:
            raise TableError("A patch cannot change a row's id")
        if not self._has(namespace, table):
            raise NotFound(f"No row {row_id!r} in {namespace}/{table}")
        with self._writing() as db:
            current = self._get(namespace, table, row_id, db)
            if current is None:
                raise NotFound(f"No row {row_id!r} in {namespace}/{table}")
            row = check_row({**merge_patch(current, patch), "id": row_id}, self._schema(namespace, table))
            self._put(db, namespace, table, row)
        return ordered(row)

    async def patch(self, namespace: str, table: str, row_id: str, patch: Dict[str, Any]) -> Dict[str, Any]:
        """Merge ``patch`` into the row (RFC 7396) and return the result; NotFound if there is none."""
        return await self._run(self._patch, namespace, table, row_id, patch)

    @_retried
    def _transaction(self, namespace: str, puts: Dict[str, List[Dict[str, Any]]], deletes: Dict[str, List[str]],
                     edges: List[Dict[str, Any]] = (), delete_edges: List[Dict[str, Any]] = ()) -> None:
        tables = self._tables(namespace)
        for table, rows in puts.items():
            schema = tables.get(check_name("table", table), EMPTY)
            for row in rows:
                check_row(row, schema)
        for table in deletes:
            check_name("table", table)
        checked_edges = [check_edge(e) for e in edges]
        edge_refs = [check_edge_ref(e) for e in delete_edges]
        # Deleting from a table that does not exist yet deletes nothing: only puts need tables.
        with self._making(namespace, [t for t, rows in puts.items() if rows], [ref.type for ref, _ in checked_edges]), \
                self._writing() as db:
            for table, rows in puts.items():
                for row in rows:
                    self._put(db, namespace, table, row)
            # Edges after the rows they may join, and before the rows they may hang on go.
            if checked_edges or edge_refs:
                self._apply_edges(db, namespace, checked_edges, edge_refs)
            for table, ids in deletes.items():
                if ids and table in tables:
                    self._delete_ids(db, namespace, table, ids)

    def _apply_edges(self, db, namespace: str, puts: List[Tuple[EdgeRef, Dict[str, Any]]], deletes: List[EdgeRef]) -> None:
        """Store and delete edges within a transaction: GraphStore's (graph.py); a plain table store has none."""
        raise TableError("Edges need the graph store")

    async def transaction(self, namespace: str, puts: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                          deletes: Optional[Dict[str, List[str]]] = None, edges: Sequence[Dict[str, Any]] = (),
                          delete_edges: Sequence[Dict[str, Any]] = ()) -> None:
        """Upsert ``puts``, store ``edges``, delete ``delete_edges`` and then ``deletes`` (table -> rows or ids),
        all at once or none of it."""
        await self._run(self._transaction, namespace, puts or {}, deletes or {}, list(edges), list(delete_edges))

    async def delete(self, namespace: str, table: str, ids: List[str]) -> None:
        """Delete the rows with these ids; ids with no row are ignored."""
        await self.transaction(namespace, deletes={table: ids})

    async def bulk_put(self, namespace: str, table: str, rows: List[Dict[str, Any]]) -> None:
        await self.transaction(namespace, puts={table: rows})

    # ----------------------------------------------------------------- edges
