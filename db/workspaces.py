"""
The workspaces, and what the service keeps among them: a part of TableStore
(tables.py, where the model is told), which reads the workspaces and their
tables' schemas once into memory (``_load``), says who may write what
(``manage``, ``keep``, ``_check_owner``), lists the workspaces from memory
(``listing``), describes one with its tables' counts and sizes
(``workspace``), and makes, labels and drops them.
"""

import asyncio
import functools
import logging
import re
import time
from dataclasses import dataclass, replace
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple

from core.errors import Forbidden

from .engine import on_pool, quote
from .schema import (edges_of, row_type,
    check_name, check_workspace_name, Conflict, EDGES, EMPTY,
    encode_cursor, Field, WORKSPACES, NotFound,
    TableError, TABLES, TableSchema)

logger = logging.getLogger(__name__)

# The workspace that always exists.
DEFAULT = "default"


# What writes a bucket's rows, as the ``owner`` of its writes: its file routes (buckets/), which keep each row's bytes.
FILES = "its file routes (buckets/)"


# workspace -> table -> its schema
Schemas = Dict[str, Dict[str, TableSchema]]


@dataclass(frozen=True)
class Managed:
    """A workspace the service keeps its own data in: what keeps it, and its tables with their schemas."""
    owner: str
    tables: Dict[str, TableSchema]


# What keeps a workspace the database marks as managed when no store of this run declares it (one a store of an
# earlier version kept, say). It stays admin-only and read-only through /db all the same.
UNDECLARED = "a part of the service this run does not have"


# What a kept table's prefix may be: the start of a table name.
KEPT_PREFIX = re.compile(r"[A-Za-z][A-Za-z0-9_]*")


class FileKeeper(Protocol):
    """What keeps the buckets' bytes on disk (buckets/, which db/ cannot import: api/app.py gives it to the table
    store as ``files``). Its calls block."""

    def check_new(self, workspace: str, bucket: str) -> None:
        """Before a bucket is made: Conflict when its folder already holds files."""

    def dropped(self, workspace: str, buckets: Sequence[str]) -> None:
        """After these buckets of the workspace were dropped: remove their folders, and the workspace's own when
        that leaves it empty."""


class Workspaces:
    """TableStore's workspaces, owners, listing and descriptions (a mixin: tables.py builds the store)."""

    def manage(self, workspace: str, owner: str, tables: Dict[str, TableSchema]) -> None:
        """Keep ``workspace`` for a store of the service's, with these tables: made, or brought up to date, when
        the workspaces are first loaded (``ensure_managed``)."""
        self.managed[check_name("workspace", workspace)] = Managed(owner, tables)

    def keep(self, prefix: str, owner: str) -> None:
        """Keep every table whose name starts with ``prefix`` (``evidence_``), in any workspace, for ``owner``:
        only a write that passes ``owner=owner`` makes or changes one (a table, or an edge type, of that name),
        and /db reads one with the admin key only."""
        if not KEPT_PREFIX.fullmatch(prefix):
            raise ValueError(f"Bad prefix of kept tables {prefix!r}: letters, digits and underscores, starting "
                             "with a letter")
        self.kept[prefix] = owner

    def keeper(self, table: str) -> Optional[str]:
        """What keeps the table (``keep``), else None."""
        return next((owner for prefix, owner in self.kept.items() if table.startswith(prefix)), None)

    def _check_owner(self, workspace: str, names: Iterable[str], owner: Optional[str], *, rows: bool = True) -> None:
        """Forbidden, naming the keeper, unless ``owner`` may write these tables (or edge types) of the workspace:
        in a managed workspace only its store may, and a kept table only its keeper. With ``rows`` (a write of rows,
        or of edges at them), a bucket's rows too only its file routes (``FILES``), which write nothing else
        (NotFound for a table that is not a bucket); a schema change or a drop passes ``rows=False``, as a bucket
        is the admin's to drop (its schema never changes: ``_set_schema``). Every write asks this before it
        changes anything."""
        self._load()
        managed = self.owner_of(workspace)
        if managed is not None and owner != managed:
            raise Forbidden(f"{workspace!r} is read-only here: it is kept by the service ({managed})")
        for name in dict.fromkeys(names):
            keeper = self.keeper(name)
            if keeper is not None and owner != keeper:
                raise Forbidden(f"{workspace}/{name} is read-only here: it is kept by the service ({keeper})")
            if not rows:
                continue
            bucket = self._is_bucket(workspace, name)
            if bucket and owner != FILES:
                raise Forbidden(f"{workspace}/{name} is a bucket: its rows are its files, written only by {FILES}: "
                                f"POST /db/{workspace}/{name}/_files uploads one, DELETE .../_files/{{id}} deletes one")
            if owner == FILES and not bucket:
                raise NotFound(f"No bucket {name!r} in {workspace!r}")

    def _is_bucket(self, workspace: str, table: str) -> bool:
        """Whether the workspace has a bucket of that name (no NotFound for a workspace that is not there)."""
        schema = (self._schemas or {}).get(workspace, {}).get(table)
        return schema is not None and schema.bucket

    async def check_bucket(self, workspace: str, table: str) -> None:
        """NotFound unless the workspace is there and has the table, TableError unless the table is a bucket: for
        the file routes, before they read or write its files."""
        await self._loaded_now()
        if not self._has(workspace, table):
            raise NotFound(f"No bucket {table!r} in {workspace!r}")
        if not self._schema(workspace, table).bucket:
            raise TableError(f"{workspace}/{table} is a table of rows, not a bucket of files")

    def owner_of(self, workspace: str) -> Optional[str]:
        """What keeps ``workspace`` when the service does (see ``manage``), else None. One the database marks as
        managed stays so when no store declares it now (UNDECLARED), which needs the workspaces loaded: ``owner``."""
        managed = self.managed.get(workspace)
        if managed is not None:
            return managed.owner
        return UNDECLARED if workspace in self._marked else None

    async def owner(self, workspace: str) -> Optional[str]:
        """``owner_of``, once the workspaces are loaded (the first time, on the database's pool)."""
        if not self._loaded or self._schemas is None:
            await self._run(self._load)
        return self.owner_of(workspace)

    def has_workspace(self, workspace: str) -> bool:
        """Blocking."""
        return workspace in self._load()

    workspace_there = on_pool(has_workspace)

    def has_table(self, workspace: str, table: str) -> bool:
        """Whether the workspace holds the table; NotFound for a workspace that is not there. Blocking."""
        return self._has(workspace, table)

    def names(self) -> List[str]:
        """The workspaces' names, by name, not the service's own workspaces: from memory once loaded. Blocking."""
        return [n for n in sorted(self._load()) if self.owner_of(n) is None]

    def records(self, managed: bool = False) -> List[Dict[str, Any]]:
        """Each workspace's name, label and created time (Unix), by name, without its tables (``workspaces``
        describes them); with ``managed``, the service's own workspaces too. Blocking."""
        known = self._load()
        rows = {r["name"]: r for r in self._query(f"SELECT name, created, label FROM {WORKSPACES}")}
        return [{"name": n, "label": rows.get(n, {}).get("label"), "created": rows.get(n, {}).get("created")}
                for n in sorted(known) if managed or self.owner_of(n) is None]

    def reload(self) -> None:
        """Read the workspaces and tables again, as on first use: after a change made in SQL around the store."""
        with self._schema_lock:
            self._schemas, self._loaded = None, False
            self._load()

    def ensure_tables(self, workspace: str, tables: Dict[str, TableSchema], *, owner: Optional[str] = None) -> None:
        """Make the tables missing among ``tables`` with their schemas, and give one made earlier the schema
        declared now (when its rows fit it; else that is logged): for a store whose tables a workspace holds, kept
        for it (``keep``), made on its first write. Blocking."""
        self._check_owner(workspace, tables, owner, rows=False)
        with self._schema_lock:
            for table, schema in tables.items():
                if not self._has(workspace, table):
                    self._create_table(workspace, table, schema, owner=owner)
                elif replace(self._schema(workspace, table), graph=schema.graph) != schema:
                    try:
                        self._set_schema(workspace, table, schema, owner=owner)
                        logger.info("Schema of %s/%s brought up to date", workspace, table)
                    except TableError as e:
                        logger.error("Could not bring the schema of %s/%s up to date: %s", workspace, table, e)

    def ensure_managed(self, workspace: str) -> None:
        """Make the managed workspace and its tables (as ``manage`` declared them) where missing, and give a table
        made by an earlier version the schema declared now (``ensure_tables``). A workspace of that name an admin
        made through /db is refused, not taken over. Blocking."""
        managed = self.managed.get(workspace)
        if managed is None:
            raise ValueError(f"No store keeps the workspace {workspace!r}: declare it with manage()")
        with self._schema_lock:
            if workspace not in self._load():
                self._make_workspace(workspace, managed=True)
            elif workspace not in self._marked:
                raise RuntimeError(f"The workspace {workspace!r} was made through /db before the service reserved it "
                                   f"for {managed.owner}: rename or drop it")
            self._marked.add(workspace)
            self.ensure_tables(workspace, managed.tables, owner=managed.owner)

    def _query(self, sql: str, params: Optional[Dict[str, Any]] = None, db=None) -> List[Dict[str, Any]]:
        return self._rows(db or self._db(), sql, params)

    def _load(self) -> Schemas:
        schemas = self._schemas
        if self._loaded and schemas is not None:
            return schemas
        with self._schema_lock:
            if self._schemas is None:
                db = self._db()
                db.execute(f"CREATE TABLE IF NOT EXISTS {WORKSPACES} (name VARCHAR PRIMARY KEY, created DOUBLE, "
                           "label VARCHAR, managed BOOLEAN DEFAULT false)")
                # A table's schema, or an edge type (kind "edge"), and the write number its full-text index holds up
                # to (rows.py).
                db.execute(f"CREATE TABLE IF NOT EXISTS {TABLES} (workspace VARCHAR, name VARCHAR, kind VARCHAR, "
                           "strict BOOLEAN DEFAULT false, bucket BOOLEAN DEFAULT false, graph BOOLEAN DEFAULT false, "
                           "fields JSON, fts_seq BIGINT, PRIMARY KEY (workspace, name))")
                names = self._query(f"SELECT name, managed FROM {WORKSPACES}")
                self._schemas = {r["name"]: {} for r in names}
                self._marked = {r["name"] for r in names if r.get("managed")}
                self._edge_types = {n: set() for n in self._schemas}
                # The default workspace, on a database that has none yet.
                if DEFAULT not in self._schemas:
                    self._make_workspace(DEFAULT)
                for r in self._query(f"SELECT workspace, name, kind, strict, bucket, graph, fields, fts_seq FROM {TABLES}"):
                    workspace, name = r["workspace"], r["name"]
                    if workspace not in self._schemas:
                        continue
                    if r["kind"] == "edge":
                        self._edge_types[workspace].add(name)
                        continue
                    fields = [Field.from_dict(f) for f in r["fields"] or []]
                    self._schemas[workspace][name] = TableSchema({f.name: f for f in fields}, bool(r["strict"]),
                                                                 bool(r["graph"]), bool(r["bucket"]))
                    self._fulltext_seq[(workspace, name)] = r["fts_seq"]
                # The service's workspaces exist from the start, so /db lists them before their stores are used.
                for workspace in self.managed:
                    try:
                        self.ensure_managed(workspace)
                    except Exception as e:
                        logger.error("Could not make the workspace %s: %s", workspace, e)
                self._loaded = True
            return self._schemas

    def _tables(self, workspace: str) -> Dict[str, TableSchema]:
        tables = self._load().get(workspace)
        if tables is None:
            raise NotFound(f"No workspace named {workspace!r}; an admin creates workspaces")
        return tables

    def _has(self, workspace: str, table: str) -> bool:
        return check_name("table", table) in self._tables(workspace)

    def _schema(self, workspace: str, table: str) -> TableSchema:
        return self._tables(workspace).get(table, EMPTY)

    def _edges_of(self, workspace: str) -> set:
        self._tables(workspace)
        return self._edge_types.setdefault(workspace, set())

    def _size(self, workspace: str, table: str) -> int:
        """About how many bytes the table takes in the database: the file's blocks its columns are stored in, and
        about 8 bytes a value for the rows written since the last checkpoint, which are not in the file yet. An
        estimate: DuckDB compresses columns and shares blocks, and says no table's size. For a bucket of files, that
        is its rows only: the files' bytes are in DATA_DIR/files."""
        if not self._has(workspace, table):
            return 0
        return self._stored_size(row_type(workspace, table))

    def _stored_size(self, table_sql: str) -> int:
        """``_size``'s estimate for a table named in SQL."""
        rows = self._query(f"SELECT count(DISTINCT block_id) FILTER (WHERE persistent AND block_id >= 0) AS blocks, "
                           f"coalesce(sum(count) FILTER (WHERE NOT persistent AND segment_type <> 'VALIDITY'), 0) AS "
                           f"pending FROM pragma_storage_info('{table_sql.replace(chr(39), chr(39) * 2)}')")
        block = self._query("SELECT block_size FROM pragma_database_size() LIMIT 1")
        return int(rows[0]["blocks"] or 0) * int(block[0]["block_size"] if block else 262144) + 8 * int(rows[0]["pending"] or 0)

    def _table_entry(self, table: str, schema: TableSchema) -> Dict[str, Any]:
        """The table as the workspaces' list names it, from memory: its kind (graph, bucket, its vector field's
        name) and what keeps it."""
        return {"name": table, "graph": schema.graph, "bucket": schema.bucket,
                "vector": schema.vector.name if schema.vector is not None else None, "kept": self.keeper(table)}

    def _table_info(self, workspace: str, table: str) -> Dict[str, Any]:
        """The table's entry (``_table_entry``) with its row count, size and schema."""
        schema = self._schema(workspace, table)
        return {**self._table_entry(table, schema), **schema.to_dict(), "count": self._count(workspace, table),
                "size_bytes": self._size(workspace, table)}

    def _edge_info(self, workspace: str, edge_type: str) -> Dict[str, Any]:
        """The edge type with its count, and its share of the size of the workspace's edges (one table holds every
        type's), by count."""
        rows = self._query(f"SELECT count(*) FILTER (WHERE type = $t) AS n, count(*) AS total FROM {edges_of(workspace)}",
                           {"t": edge_type})
        n, total = (rows[0]["n"], rows[0]["total"]) if rows else (0, 0)
        size = self._stored_size(edges_of(workspace)) * n // total if total else 0
        return {"name": edge_type, "count": n, "size_bytes": size}

    def _describe(self, workspace: str) -> Dict[str, Any]:
        """The workspace with its label, its tables and edge types, their row counts, sizes on disk and schemas."""
        names = sorted(self._tables(workspace))
        found = self._query(f"SELECT created, label FROM {WORKSPACES} WHERE name = $n", {"n": workspace})
        record = found[0] if found else {}
        tables = [self._table_info(workspace, t) for t in names]
        edges = [self._edge_info(workspace, e) for e in sorted(self._edges_of(workspace))]
        return {"name": workspace, "label": record.get("label"), "tables": tables, "edges": edges,
                "size_bytes": sum(t["size_bytes"] for t in tables) + sum(e["size_bytes"] for e in edges),
                "managed": self.owner_of(workspace), "created": record.get("created")}

    workspace = on_pool(_describe)

    def _listing(self, managed: bool = False, offset: int = 0,
                 limit: int = 1000) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """A page of the workspaces (those no part of the service keeps; with ``managed``, the service's own
        too), by name: each with its label, when it was made, what keeps it, and its tables (``_table_entry``) and
        edge types by name, as the store holds them in memory, so a page costs one query, however many tables
        there are; and the next page's cursor (None on the last). ``_describe`` counts and sizes one."""
        # Makes the workspace type on a fresh database, before it is read.
        known = self._load()
        names = [n for n in sorted(known) if managed or self.owner_of(n) is None]
        page = names[offset:offset + limit]
        records = {r["name"]: r for r in self._query(f"SELECT name, created, label FROM {WORKSPACES} WHERE "
                                                     "name = ANY($n)", {"n": page})} if page else {}
        listed = [{"name": n, "label": records.get(n, {}).get("label"), "created": records.get(n, {}).get("created"),
                   "managed": self.owner_of(n),
                   "tables": [self._table_entry(t, schema) for t, schema in sorted(known.get(n, {}).items())],
                   "edges": [{"name": e} for e in sorted(self._edges_of(n))]} for n in page]
        return listed, (encode_cursor(offset + limit) if len(names) > offset + limit else None)

    listing = on_pool(_listing)

    def _create_workspace(self, workspace: str, label: Optional[str] = None) -> None:
        check_workspace_name(workspace)
        with self._schema_lock:
            known = self._load()
            owner = self.owner_of(workspace)
            if owner:
                raise Conflict(f"The name {workspace!r} is kept by the service ({owner})")
            if workspace in known:
                raise Conflict(f"A workspace named {workspace!r} already exists")
            same = next((n for n in known if n.lower() == workspace.lower()), None)
            if same is not None:
                raise Conflict(f"A workspace named {same!r} already exists, and the database reads names ignoring "
                               "case")
            self._make_workspace(workspace, label)

    create_workspace = on_pool(_create_workspace)

    def _make_workspace(self, workspace: str, label: Optional[str] = None, managed: bool = False) -> None:
        """The workspace's record, its schema and the table of its edges, in one transaction. Under the schema
        lock."""
        db = self._db()
        with db.transaction():
            db.execute(f"INSERT INTO {WORKSPACES} (name, created, label, managed) VALUES ($n, $c, $l, $m)",
                       {"n": workspace, "c": time.time(), "l": label, "m": managed})
            db.execute(f"CREATE SCHEMA IF NOT EXISTS {quote(workspace)}")
            # Every type's edges, at most one of a type for a pair of rows (graph.py).
            db.execute(f"CREATE TABLE IF NOT EXISTS {edges_of(workspace)} (type VARCHAR, from_table VARCHAR, "
                       "from_id VARCHAR, to_table VARCHAR, to_id VARCHAR, props JSON, _seq BIGINT, "
                       "PRIMARY KEY (type, from_table, from_id, to_table, to_id))")
            db.execute(f"CREATE INDEX IF NOT EXISTS {quote(EDGES + '_to')} ON {edges_of(workspace)} (to_table, to_id)")
            db.execute(f"CREATE INDEX IF NOT EXISTS {quote(EDGES + '_from')} ON {edges_of(workspace)} "
                       "(from_table, from_id)")
        self._schemas[workspace] = {}
        self._edge_types[workspace] = set()
        if managed:
            self._marked.add(workspace)

    def _set_label(self, workspace: str, label: Optional[str], *, owner: Optional[str] = None) -> None:
        """Give the workspace this label, or none (None)."""
        self._tables(workspace)
        self._check_owner(workspace, [], owner)
        self._db().execute(f"UPDATE {WORKSPACES} SET label = $label WHERE name = $n", {"n": workspace, "label": label})

    set_label = on_pool(_set_label)

    async def _files_dropped(self, workspace: str, buckets: Sequence[str]) -> None:
        """Have ``files`` remove the folders of buckets just dropped, on a thread of its own (there may be many
        files), not the database's pool. A failure is logged: the rows are gone already."""
        if not buckets:
            return
        if self.files is None:
            logger.warning("Dropped the buckets %s of %s, but this table store keeps no buckets' files: "
                           "their files stay on disk", ", ".join(buckets), workspace)
            return
        try:
            await asyncio.to_thread(self.files.dropped, workspace, list(buckets))
        except Exception:
            logger.exception("Dropped the buckets %s of %s, but could not remove their files", ", ".join(buckets),
                             workspace)

    def _drop_workspace(self, workspace: str, *, owner: Optional[str] = None) -> List[str]:
        """Delete a workspace with all its tables and rows; Forbidden for the default workspace, and, unless
        ``owner`` keeps them, for a managed one and one that holds kept tables or edge types. The buckets it held,
        whose files ``drop_workspace`` then removes."""
        if workspace == DEFAULT:
            raise Forbidden(f"The workspace {DEFAULT!r} always exists; drop its tables one by one instead")
        with self._schema_lock:
            tables = list(self._tables(workspace))
            self._check_owner(workspace, [], owner)
            # Refused whole, before anything is dropped: an edge type named like a kept table is its keeper's too.
            held: Dict[str, List[str]] = {}
            for name in [*tables, *sorted(self._edges_of(workspace))]:
                keeper = self.keeper(name)
                if keeper is not None and keeper != owner:
                    held.setdefault(keeper, []).append(name)
            if held:
                raise Forbidden(f"{workspace!r} holds tables or edge types the service keeps, so it cannot be "
                                "dropped: " +
                                "; ".join(f"{', '.join(names)} ({keeper})" for keeper, names in held.items()))
            buckets = [table for table in tables if self._drop_table(workspace, table, owner=owner, whole_workspace=True)]
            for edge_type in list(self._edges_of(workspace)):
                self._drop_edge_type(workspace, edge_type, owner=owner)
            db = self._db()
            with db.transaction():
                db.execute(f"DELETE FROM {TABLES} WHERE workspace = $name", {"name": workspace})
                db.execute(f"DELETE FROM {WORKSPACES} WHERE name = $name", {"name": workspace})
                db.execute(f"DROP SCHEMA IF EXISTS {quote(workspace)} CASCADE")
            del self._schemas[workspace]
            self._edge_types.pop(workspace, None)
            self._marked.discard(workspace)
            for forget in self.on_drop:
                try:
                    forget(workspace)
                except Exception:
                    logger.exception("Dropped the workspace %s, but could not remove what was kept beside it",
                                     workspace)
            return buckets

    async def drop_workspace(self, workspace: str, *, owner: Optional[str] = None) -> None:
        """Delete a workspace with all its tables and rows (``_drop_workspace``), its buckets' files too."""
        await self._files_dropped(workspace, await self._run(functools.partial(self._drop_workspace, workspace,
                                                                               owner=owner)))

    async def _loaded_now(self) -> None:
        """Load the workspaces (the first time, on the database's pool), so they can be read on the event loop."""
        if not self._loaded or self._schemas is None:
            await self._run(self._load)
