"""
Making, changing and dropping tables, edge types and their indexes: a part
of TableStore (tables.py, where the model is told). A table is a DuckDB table
in its workspace's schema: ``id``, a column per declared field, ``_rest`` (the
fields it does not declare, as one JSON object) and ``_seq`` (the number of
the write that last changed the row, which the full-text index goes by:
rows.py). Schema changes run under the schema lock, each in one transaction
(DuckDB's DDL is part of one); a table a write needs is made before its
transaction (``_making``) and dropped again if the write fails.

A schema change builds its table again from a copy of the rows, which would
miss a row committed while it runs (DuckDB does not refuse that commit), and
a drop would lose it. So both hold the table's gate alone (engine.Gate): they
wait for the writes under way to end, and the writes that come meanwhile
wait for them, then check their rows against the new schema, or find the
table gone (rows.py). A store's own writes (``write_rows``)
take no gate: their tables change only as the store declares them
(``ensure_tables``), before it writes.
"""

import functools
import json
import logging
from contextlib import contextmanager
from dataclasses import replace
from typing import Any, Dict, Iterator, List, Optional, Sequence

import duckdb

from core.errors import TooLarge, Unavailable

from .engine import Conn, Gate, db_message, on_pool, quote
from .schema import (column_type, edges_of, fts_schema, row_type, row_type_name,
    BUCKET, check_name, Conflict, EMPTY, INDEXABLE, NotFound, observe, TableError, TABLES, TableSchema)

logger = logging.getLogger(__name__)

SCAN_BATCH = 2000


def _structure(kind: str) -> str:
    """A column type as json_transform's structure: '"BIGINT"', '["VARCHAR"]'."""
    if kind.endswith("[]"):
        return f'["{kind[:-2]}"]'
    return f'"{kind}"'


def converted(value: str, kind: str) -> str:
    """SQL that turns ``value``, a JSON value (a column's ``to_json`` or a ``_rest`` field), into a value of the
    column type ``kind``: JSON stays JSON (null as NULL), a vector is read as floats and fixed to its size. The rows
    were checked to fit; a value that still does not (text as a number) fails the change, rather than becoming
    NULL (json_transform_strict)."""
    if kind == "JSON":
        return f"(CASE WHEN json_type({value}) = 'NULL' THEN NULL ELSE {value} END)"
    if kind.startswith("FLOAT["):
        return f"CAST(json_transform_strict({value}, '[\"FLOAT\"]') AS {kind})"
    return f"json_transform_strict({value}, '{_structure(kind)}')"


class Ddl:
    """TableStore's tables, edge types, schemas and indexes (a mixin: tables.py builds the store)."""

    def _ensure(self, workspace: str, tables: Sequence[str], graph: Optional[bool] = None) -> None:
        """Make the tables missing among ``tables``, as graph tables (whose rows edges can join) if ``graph``.
        Unsaid, a table is a graph table unless the service keeps the workspace (its stores say what they make)."""
        if graph is None:
            graph = self.owner_of(workspace) is None
        # Tables already there need no lock, so a write to one never waits for another table's schema change.
        if all(check_name("table", table) in self._tables(workspace) for table in tables):
            return
        with self._schema_lock:
            known = self._tables(workspace)
            for table in tables:
                if check_name("table", table) in known:
                    continue
                self._check_new_name(workspace, table)
                self._check_index_name(workspace, table)
                db = self._db()
                with db.transaction():
                    db.execute(f"CREATE TABLE {row_type(workspace, table)} (id VARCHAR PRIMARY KEY, _rest JSON, "
                               "_seq BIGINT)")
                    self._save_schema(db, workspace, table, replace(EMPTY, graph=graph))
                known[table] = replace(EMPTY, graph=graph)

    def _gate(self, workspace: str, table: str) -> Gate:
        """The table's gate: its writes share it, a change of its schema has it alone (the module's docstring)."""
        with self._gates_lock:
            return self._gates.setdefault((workspace, table), Gate())

    def _check_new_name(self, workspace: str, name: str) -> None:
        """Conflict when the workspace has a table or an edge type of this name, the database's way: ignoring case
        (a query names tables and edge types alike by their bare names, so one name is never both)."""
        lower = name.lower()
        for table in self._tables(workspace):
            if table.lower() == lower:
                raise Conflict(f"{workspace!r} already has a table named {table!r}; pick another name")
        for edge_type in self._edges_of(workspace):
            if edge_type.lower() == lower:
                raise Conflict(f"{workspace!r} already has a relationship (edge type) named {edge_type!r}; pick "
                               "another name")

    def _check_index_name(self, workspace: str, table: str) -> None:
        """Conflict when the table's full-text index would be named as another table's: DuckDB's fts names it
        fts_<workspace>_<table> (fts_schema), the same for a_b/c and a/b_c, and the database ignores case."""
        index = f"fts_{workspace}_{table}".lower()
        for other, tables in list(self._load().items()):
            for name in list(tables):
                if f"fts_{other}_{name}".lower() == index:
                    raise Conflict(f"The full-text index of {other}/{name} is named fts_{other}_{name}, as one of "
                                   f"{workspace}/{table} would be: pick another name")

    @staticmethod
    def _save_schema(db: Conn, workspace: str, table: str, schema: TableSchema) -> None:
        db.execute(f"INSERT INTO {TABLES} (workspace, name, kind, strict, bucket, graph, fields) VALUES "
                   "($n, $t, 'table', $strict, $bucket, $graph, $fields) ON CONFLICT (workspace, name) DO UPDATE SET "
                   "strict = excluded.strict, bucket = excluded.bucket, graph = excluded.graph, fields = excluded.fields",
                   {"n": workspace, "t": table, "strict": schema.strict, "bucket": schema.bucket, "graph": schema.graph,
                    "fields": json.dumps([f.to_dict() for f in schema.fields.values()])})

    @contextmanager
    def _making(self, workspace: str, tables: Sequence[str], edge_types: Sequence[str],
                owner: Optional[str] = None) -> Iterator[None]:
        """Make the missing tables and edge types a write needs, before its transaction; if the write fails, drop
        the ones made here again, unless another writer has filled them since. ``owner`` is the write's, which has
        been checked."""
        # What is there already needs no lock (see _schemas).
        tables = [t for t in dict.fromkeys(tables) if check_name("table", t) not in self._tables(workspace)]
        edge_types = [t for t in dict.fromkeys(edge_types) if check_name("edge type", t) not in self._edges_of(workspace)]
        made_tables: List[str] = []
        made_types: List[str] = []
        try:
            if tables or edge_types:
                with self._schema_lock:
                    for table in tables:
                        if table not in self._tables(workspace):
                            self._ensure(workspace, [table])
                            made_tables.append(table)
                    for edge_type in edge_types:
                        if edge_type not in self._edges_of(workspace):
                            self._ensure_edge_type(workspace, edge_type)
                            made_types.append(edge_type)
            yield
        except BaseException:
            try:
                with self._schema_lock:
                    for edge_type in made_types:
                        if edge_type in self._edges_of(workspace) and not self._edge_info(workspace, edge_type)["count"]:
                            self._drop_edge_type(workspace, edge_type, owner=owner)
                    for table in made_tables:
                        if self._has(workspace, table):
                            self._drop_table(workspace, table, owner=owner, if_empty=True)
            except Exception:
                logger.exception("Could not drop what a failed write made in %s", workspace)
            raise

    def _ensure_edge_type(self, workspace: str, edge_type: str) -> None:
        with self._schema_lock:
            known = self._edges_of(workspace)
            if check_name("edge type", edge_type) in known:
                return
            self._check_new_name(workspace, edge_type)
            self._db().execute(f"INSERT INTO {TABLES} (workspace, name, kind) VALUES ($n, $t, 'edge')",
                               {"n": workspace, "t": edge_type})
            known.add(edge_type)

    def _drop_table(self, workspace: str, table: str, *, owner: Optional[str] = None, if_empty: bool = False,
                    whole_workspace: bool = False) -> bool:
        """Delete the table with its rows and their edges; True when it was a bucket, whose files ``drop_table``
        then removes. It holds the table's gate alone, as a schema change does: a write under way would otherwise
        commit into the dropped table, and its row be gone. ``if_empty``: only a table that holds no rows once those
        writes are done (what a failed write made, which another may have filled meanwhile). Conflict while a ref
        of another table names it, unless the whole workspace goes (``whole_workspace``)."""
        self._check_owner(workspace, [table], owner, rows=False)
        with self._schema_lock, self._ref_lock(workspace), self._gate(workspace, table).alone():
            if not self._has(workspace, table):
                raise NotFound(f"No table {table!r} in {workspace!r}")
            if not whole_workspace:
                self._check_unreferenced(workspace, table)
            if if_empty and self._count(workspace, table):
                return False
            bucket = self._schema(workspace, table).bucket
            db = self._db()
            with db.transaction():
                db.execute(f"DELETE FROM {edges_of(workspace)} WHERE from_table = $t OR to_table = $t", {"t": table})
                self._drop_fulltext_index(db, workspace, table)
                db.execute(f"DROP TABLE {row_type(workspace, table)}")
                db.execute(f"DELETE FROM {TABLES} WHERE workspace = $n AND name = $t", {"n": workspace, "t": table})
            del self._schemas[workspace][table]
            self._fulltext_seq.pop((workspace, table), None)
            return bucket

    async def drop_table(self, workspace: str, table: str, *, owner: Optional[str] = None) -> None:
        """Delete the table with its rows: a bucket with its files."""
        if await self._run(functools.partial(self._drop_table, workspace, table, owner=owner)):
            await self._files_dropped(workspace, [table])

    def _drop_edge_type(self, workspace: str, edge_type: str, *, owner: Optional[str] = None) -> None:
        """Delete the edge type with every edge of it."""
        self._check_owner(workspace, [edge_type], owner, rows=False)
        with self._schema_lock:
            if check_name("edge type", edge_type) not in self._edges_of(workspace):
                raise NotFound(f"No edge type {edge_type!r} in {workspace!r}")
            db = self._db()
            with db.transaction():
                db.execute(f"DELETE FROM {edges_of(workspace)} WHERE type = $t", {"t": edge_type})
                db.execute(f"DELETE FROM {TABLES} WHERE workspace = $n AND name = $t", {"n": workspace, "t": edge_type})
            self._edge_types[workspace].discard(edge_type)

    drop_edge_type = on_pool(_drop_edge_type)

    def _create_table(self, workspace: str, table: str, schema: TableSchema = EMPTY, *,
                      owner: Optional[str] = None) -> Dict[str, Any]:
        """Make an empty table with this schema; Conflict if it exists. A bucket (``schema.bucket``) needs ``files``
        to keep its bytes (Unavailable without), and a folder for them that no other bucket shares and no files
        fill yet (Conflict)."""
        self._check_owner(workspace, [table], owner, rows=False)
        with self._schema_lock:
            if self._has(workspace, table):
                raise Conflict(f"A table named {table!r} already exists in {workspace!r}")
            if schema.bucket:
                self._check_new_bucket(workspace, table)
            self._ensure(workspace, [table], schema.graph)
            try:
                self._set_schema(workspace, table, schema, owner=owner)
            except Exception:
                # All of it, or no table: unless a client's write filled it meanwhile, as a first write makes one.
                self._drop_table(workspace, table, owner=owner, if_empty=True)
                raise
            return self._table_info(workspace, table)

    def _check_new_bucket(self, workspace: str, table: str) -> None:
        """Whether a bucket can be made here: ``files`` keeps buckets' bytes, in a folder per bucket named after the
        workspace and the table, which on a disk that ignores case would be the same for names that differ only by
        it; and that folder holds no files yet (``files.check_new``)."""
        if self.files is None:
            raise Unavailable("This table store was given nothing to keep buckets' files: no bucket can be made")
        folder = (workspace.lower(), table.lower())
        for other, tables in list(self._load().items()):
            for name, schema in list(tables.items()):
                if schema.bucket and (other.lower(), name.lower()) == folder:
                    raise Conflict(f"The bucket {other}/{name} keeps its files in the folder {workspace}/{table} "
                                   "would have on a disk that ignores case: pick another name")
        self.files.check_new(workspace, table)

    async def create_table(self, workspace: str, table: str, schema: TableSchema = EMPTY, *,
                           owner: Optional[str] = None) -> Dict[str, Any]:
        schema = await self._probed(workspace, table, schema, owner)
        return await self._run(functools.partial(self._create_table, workspace, table, schema, owner=owner))

    def _scan(self, workspace: str, table: str) -> Iterator[Dict[str, Any]]:
        """Every row, whole, in pages along the id."""
        after = ""
        while True:
            rows = self._read_rows(workspace, table, " WHERE id > $last ORDER BY id LIMIT $n",
                                   {"last": after, "n": SCAN_BATCH}, vector=True)
            yield from rows
            if len(rows) < SCAN_BATCH:
                return
            after = rows[-1]["id"]

    def _check_rows(self, workspace: str, table: str, old: TableSchema, new: TableSchema,
                    dropping: Sequence[str] = ()) -> None:
        """TableError, naming a few rows, if rows there now break what the new schema adds. The fields
        ``dropping`` leave the rows with the change (a removed vector field's values), so strict does not count
        them."""
        changed = [f for name, f in new.fields.items()
                   if (o := old.fields.get(name)) is None or (o.type, o.list, o.required) != (f.type, f.list, f.required)]
        # Strict refuses undeclared fields: check when it comes on, or fields leave the schema but not the rows.
        kept = set(new.fields) | set(dropping)
        check_extra = new.strict and (not old.strict or not kept >= set(old.fields))
        if not changed and not check_extra:
            return
        bad = []
        for row in self._scan(workspace, table):
            try:
                for f in changed:
                    f.check(row)
                extra = [k for k in row if k != "id" and k not in kept] if check_extra else []
                if extra:
                    raise TableError(f"{', '.join(extra)} not declared")
            except TableError as e:
                bad.append(f"{row.get('id')} ({e})")
                if len(bad) == 3:
                    break
        if bad:
            raise TableError(f"Rows there now do not fit the new schema: {'; '.join(bad)}")

    def _drop_fulltext_index(self, db: Conn, workspace: str, table: str) -> None:
        """Drop the table's full-text index, if it has one (DuckDB's fts keeps it in a schema of its own, which
        dropping the table would leave behind)."""
        if self._fulltext_seq.get((workspace, table)) is not None or self.database.has_schema(
                f"fts_{workspace}_{table}"):
            db.execute(f"DROP SCHEMA IF EXISTS {fts_schema(workspace, table)} CASCADE")
        self._fulltext_seq[(workspace, table)] = None

    def _apply_schema(self, db: Conn, workspace: str, table: str, old: TableSchema, new: TableSchema) -> None:
        """Give the table the columns and indexes of ``new``, in the transaction on ``db``, by building it again: a
        table of the new columns, the rows copied in, the old table dropped and the new one named as it was. A field
        that leaves the schema leaves its values among the row's undeclared fields (``_rest``; a vector field's go),
        one that comes in takes the values the rows hold under its name, and one whose type changed has its values
        converted (the rows were checked to fit). Built again, since DuckDB changes no column of a table with an index
        and makes no index after updates in the same transaction; a table copies in about a second per million rows."""
        t, new_name = row_type(workspace, table), f"{table}__new"  # a name no table has: they hold no "__"
        built = f"{quote(workspace)}.{quote(new_name)}"
        columns = ["id VARCHAR PRIMARY KEY", *(f"{quote(n)} {column_type(f)}" for n, f in new.fields.items()),
                   "_rest JSON", "_seq BIGINT"]
        db.execute(f"CREATE TABLE {built} ({', '.join(columns)})")
        values, params = [], {}
        for i, (name, f) in enumerate(new.fields.items()):
            col, kind, was = quote(name), column_type(f), old.fields.get(name)
            if was is None:
                values.append(converted(f"(_rest -> '$.\"{name}\"')", kind))
            elif column_type(was) == kind:
                values.append(col)
            else:
                values.append(converted(f"to_json({col})", kind))
        # The rest loses the fields that come in, but for a null it holds for one, which stays as the null a write notes
        # for a declared field (rows.py); it loses the nulls noted for a vector field that leaves (whose values go),
        # and gains the values of the other fields that leave. Made as a map of JSON values: json_merge_patch would
        # drop the nulls inside them. The lambdas' ``_entry`` is a name no column has (a field starts with a letter),
        # which DuckDB would read instead.
        incoming = [n for n in new.fields if n not in old.fields]
        gone = [n for n, f in old.fields.items() if n not in new.fields and f.type == "vector"]
        leaving = [n for n, f in old.fields.items() if n not in new.fields and f.type != "vector"]
        rest = "_rest"
        if incoming or gone or leaving:
            params.update(_in=incoming, _gone=gone)
            kept = ("map_from_entries(list_filter(map_entries(CAST(coalesce(_rest, '{}') AS MAP(VARCHAR, JSON))), "
                    "lambda _entry: NOT list_contains($_gone, _entry.key) "
                    "AND NOT (list_contains($_in, _entry.key) AND _entry.value IS NOT NULL)))")
            if leaving:
                params.update({f"_l{i}": n for i, n in enumerate(leaving)})
                entries = ", ".join(f"{{'key': $_l{i}, 'value': to_json({quote(n)})}}" for i, n in enumerate(leaving))
                kept = (f"map_concat({kept}, map_from_entries(list_filter([{entries}], "
                        "lambda _entry: _entry.value IS NOT NULL)))")
            rest = f"NULLIF(to_json({kept}), '{{}}'::JSON)"
        values.append(rest)
        db.execute(f"INSERT INTO {built} SELECT id, {', '.join(values)}, _seq FROM {t}", params)
        db.execute(f"DROP TABLE {t}")
        db.execute(f"ALTER TABLE {built} RENAME TO {quote(table)}")
        for name, f in new.fields.items():
            if f.indexed and not f.list and f.type in INDEXABLE:
                db.execute(f"CREATE {'UNIQUE ' if f.unique else ''}INDEX {quote(f'{table}.{name}')} ON {t} ({quote(name)})")

    def _set_schema(self, workspace: str, table: str, new: TableSchema, *, owner: Optional[str] = None) -> TableSchema:
        """Replace the table's schema (making the table if needed), after checking the rows there fit it.
        ``graph`` only counts when this makes the table; an existing table keeps its kind. A vector field's
        options never change: it is removed (its vectors with it) and added again. A bucket's schema never changes
        (TableError); a table becomes one only as ``_create_table`` makes it. It holds the table's gate alone from
        the rows' check to the commit, so no write slips between them, and the workspace's ref lock, as what is
        joined may change (refs.py)."""
        self._check_owner(workspace, [table], owner, rows=False)
        with self._schema_lock, self._ref_lock(workspace), self._gate(workspace, table).alone():
            if self._has(workspace, table) and self._schema(workspace, table).bucket:
                raise TableError(f"{workspace}/{table} is a bucket: its fields are fixed "
                                 f"({', '.join(BUCKET.fields)}), and only its file routes write its rows")
            # A table made here (a schema put before any row) is of the workspace's kind: graph says nothing yet.
            self._ensure(workspace, [table])
            old = self._schema(workspace, table)
            # Whether a table is a graph table is settled when it is made.
            new = replace(new, graph=old.graph)
            if new.bucket and not old.bucket and (old != replace(EMPTY, graph=old.graph)
                                                  or self._count(workspace, table)):
                raise TableError(f"{workspace}/{table} is a table already: only a new table is made a bucket")
            vector_was, vector = old.vector, new.vector
            if vector_was is not None and vector_was.name in new.fields \
                    and new.fields[vector_was.name].vector_options != vector_was.vector_options:
                raise TableError(f"The options of the vector field {vector_was.name} cannot change: remove the field "
                                 "and add it again, which drops its vectors (rows get new ones when written again)")
            if vector is not None and vector.dimensions is None:
                raise TableError(f"{vector.name}: dimensions are needed (a model's are found when the field is made)")
            vector_dropped = vector_was if vector_was is not None and vector_was.name not in new.fields else None
            if vector is not None and (vector_was is None or vector_was.name != vector.name):
                count = self._count(workspace, table)
                if count > self.vector_max_rows:
                    raise TooLarge(f"{workspace}/{table} holds {count} rows, past VECTOR_MAX_ROWS "
                                   f"({self.vector_max_rows}), the most a table with a vector field holds")
            self._check_ref_targets(workspace, table, new)
            self._check_rows(workspace, table, old, new, [vector_dropped.name] if vector_dropped is not None else [])
            db = self._db()
            try:
                with db.transaction():
                    self._apply_schema(db, workspace, table, old, new)
                    self._check_ref_rows(db, workspace, table, old, new)
                    # The full-text index covers every full-text field: made again (by the next search) when they
                    # change.
                    if old.fulltext != new.fulltext:
                        self._drop_fulltext_index(db, workspace, table)
                        db.execute(f"UPDATE {TABLES} SET fts_seq = NULL WHERE workspace = $n AND name = $t",
                                   {"n": workspace, "t": table})
                    self._save_schema(db, workspace, table, new)
            except duckdb.Error as e:
                # What the rows' check could not see (a unique value held twice is caught as the index is made).
                raise TableError(f"Could not change the schema of {row_type_name(workspace, table)}: "
                                 f"{db_message(e)}") from None
            self._schemas[workspace][table] = new
            return new

    async def set_schema(self, workspace: str, table: str, new: TableSchema, *,
                         owner: Optional[str] = None) -> TableSchema:
        new = await self._probed(workspace, table, new, owner)
        # It checks every row against the new schema (``_check_rows``): a long call.
        return await self._run(functools.partial(self._set_schema, workspace, table, new, owner=owner), long=True)

    @on_pool
    def schema(self, workspace: str, table: str, sample: int = 500) -> Dict[str, Any]:
        """The declared schema, and the fields the newest ``sample`` rows hold."""
        rows = []
        if self._has(workspace, table):
            rows = self._read_rows(workspace, table, " ORDER BY id DESC LIMIT $n", {"n": sample}, vector=True)
        return {**self._schema(workspace, table).to_dict(), "observed": observe(rows)}
