"""
Graph tables: the rows of a /db table made with ``graph`` can be joined by
edges. A workspace's edges, of every type, are rows of its ``_edges`` table
(type, from_table, from_id, to_table, to_id, props); at most one edge of a type
joins a given pair of rows, so putting an edge again replaces its
properties. Edges go with the rows they join: deleting a row deletes its
edges, and so does dropping a table. Neighbors, traversals and the view a
graph is drawn from read the graph, giving rows without their vector, as
lists do.

A read-only SQL query (``sql_query``, and ``timed_read``, what a view's SQL
runs as) is one SELECT in DuckDB's SQL, over the workspace's tables by their
names (the workspace is the schema it reads first); ``check_sql`` says,
before a view is saved, whether a statement is one. A time-limited read is
stopped when its time runs out (the cursor is interrupted), and gives a
number of rows at most. An edge write is checked as a row write is
(tables.py): one in a managed workspace, of a kept name, or with an end in a
kept table is its keeper's alone, and none has an end in a bucket (its rows
are files, written by its file routes alone).

GraphStore is the TableStore the service runs (api/app.py makes one): every
table method, plus the edge, graph and query ones here.
"""

import json
import threading
from typing import Any, Dict, List, Optional, Sequence, Tuple

import duckdb

from core.errors import Unavailable

from .engine import SEQUENCE, Conn, db_message, on_long_pool, on_pool, quote, retried
from .schema import (edges_of, ordered, check_direction, check_edge, check_edge_ref, check_name, Conflict, EdgeRef,
    encode_cursor, GRAPH_MAX_EDGES, GRAPH_MAX_NODES, MAX_DEPTH, NotFound, TableError)
from .ddl import SCAN_BATCH
from .rows import _ends
from .tables import TableStore


class GraphStore(TableStore):
    """A TableStore whose graph tables' rows can be linked by edges."""

    def _vertex(self, workspace: str, table: str, row_id: str, db: Conn, what: str) -> None:
        """TableError unless ``table/row_id`` is a row of a graph table."""
        if not self._has(workspace, table):
            raise TableError(f"No table {table!r} in {workspace!r} ({what})")
        if not self._schema(workspace, table).graph:
            raise TableError(f"{table!r} is not a graph table, so its rows cannot be linked ({what})")
        if self._get(workspace, table, row_id, db) is None:
            raise TableError(f"No row {row_id!r} in {workspace}/{table} ({what})")

    @staticmethod
    def _ref_params(ref: EdgeRef) -> Dict[str, Any]:
        return {"type": ref.type, "ft": ref.from_table, "fi": ref.from_id, "tt": ref.to_table, "ti": ref.to_id}

    _IS = "type = $type AND from_table = $ft AND from_id = $fi AND to_table = $tt AND to_id = $ti"

    def _put_edge(self, db: Conn, workspace: str, ref: EdgeRef, props: Dict[str, Any]) -> bool:
        """Store the edge, replacing the properties of the one already joining the pair. True when new."""
        self._vertex(workspace, ref.from_table, ref.from_id, db, "the edge's from")
        self._vertex(workspace, ref.to_table, ref.to_id, db, "the edge's to")
        params = {**self._ref_params(ref), "props": json.dumps(props)}
        if db.execute(f"UPDATE {edges_of(workspace)} SET props = $props::JSON WHERE {self._IS}", params):
            return False
        db.execute(f"INSERT INTO {edges_of(workspace)} (type, from_table, from_id, to_table, to_id, props, _seq) "
                   f"VALUES ($type, $ft, $fi, $tt, $ti, $props::JSON, nextval('{SEQUENCE}'))", params)
        return True

    def _apply_edges(self, db: Conn, workspace: str, puts: List[Tuple[EdgeRef, Dict[str, Any]]],
                     deletes: List[EdgeRef]) -> None:
        for ref, props in puts:
            self._put_edge(db, workspace, ref, props)
        for ref in deletes:
            self._delete_edge(db, workspace, ref)

    def _delete_edge(self, db: Conn, workspace: str, ref: EdgeRef) -> None:
        db.execute(f"DELETE FROM {edges_of(workspace)} WHERE {self._IS}", self._ref_params(ref))

    @on_pool
    @retried
    def put_edges(self, workspace: str, edges: List[Dict[str, Any]], *, owner: Optional[str] = None) -> List[bool]:
        """Store the edges (``{type, from: 'table/id', to: 'table/id', ...properties}``), all or none; for each,
        whether it was new. 400 unless both ends are rows of graph tables."""
        checked = [check_edge(e) for e in edges]
        self._tables(workspace)
        self._check_owner(workspace, _ends(ref for ref, _ in checked), owner)
        # The gates of the tables its ends are in, so a drop of one waits for it, and it for the drop.
        with self._making(workspace, [], [ref.type for ref, _ in checked], owner), \
                self._writing(workspace, [t for ref, _ in checked for t in (ref.from_table, ref.to_table)]) as db:
            return [self._put_edge(db, workspace, ref, props) for ref, props in checked]

    @on_pool
    @retried
    def delete_edges(self, workspace: str, edges: List[Dict[str, Any]], *, owner: Optional[str] = None) -> None:
        """Delete these edges (by type, from and to); ones that are not there are ignored."""
        refs = [check_edge_ref(e) for e in edges]
        self._tables(workspace)
        self._check_owner(workspace, _ends(refs), owner)
        with self._writing() as db:
            for ref in refs:
                self._delete_edge(db, workspace, ref)

    @on_pool
    def create_edge_type(self, workspace: str, edge_type: str, *, owner: Optional[str] = None) -> Dict[str, Any]:
        """Make an empty edge type, as its first edge would; Conflict if it exists or a table has its name."""
        self._check_owner(workspace, [edge_type], owner, rows=False)
        with self._schema_lock:
            if check_name("edge type", edge_type) in self._edges_of(workspace):
                raise Conflict(f"A relationship (edge type) named {edge_type!r} already exists in {workspace!r}")
            self._ensure_edge_type(workspace, edge_type)
            return self._edge_info(workspace, edge_type)

    @on_pool
    def edge_types(self, workspace: str) -> List[Dict[str, Any]]:
        """The workspace's edge types as its description lists them, each with the tables its edges join: every
        pair of the table they leave and the table they arrive at, with how many edges join it, most first."""
        types = []
        for name in sorted(self._edges_of(workspace)):
            # A scan of the edges, so it is here and not in the workspace's description, which every listing reads.
            rows = self._query(f"SELECT from_table AS f, to_table AS t, count(*) AS n FROM {edges_of(workspace)} "
                               "WHERE type = $type GROUP BY f, t", {"type": name})
            joins = sorted(({"from": r["f"], "to": r["t"], "count": r["n"]} for r in rows),
                           key=lambda j: (-j["count"], j["from"], j["to"]))
            types.append({**self._edge_info(workspace, name), "joins": joins})
        return types

    def _types(self, workspace: str, types: Sequence[str]) -> Tuple[str, Dict[str, Any]]:
        """The condition that keeps edges of these types (every type when none are named), and its parameters;
        NotFound for a type the workspace lacks."""
        known = self._edges_of(workspace)
        missing = [t for t in types if check_name("edge type", t) not in known]
        if missing:
            raise NotFound(f"No edge type named {', '.join(missing)} in {workspace!r}")
        return ("type = ANY($types)", {"types": list(types)}) if types else ("true", {})

    @staticmethod
    def _as_edge(r: Dict[str, Any]) -> Dict[str, Any]:
        """An edge row as the API shows an edge: ``{type, from: 'table/id', to: 'table/id', ...its properties}``."""
        return {"type": r["type"], "from": f"{r['from_table']}/{r['from_id']}", "to": f"{r['to_table']}/{r['to_id']}",
                **(r.get("props") or {})}

    @staticmethod
    def _at(direction: str) -> str:
        """The condition that keeps the edges leaving ($t, $id), arriving at it, or both."""
        out, into = "(from_table = $t AND from_id = $id)", "(to_table = $t AND to_id = $id)"
        return {"out": out, "in": into, "both": f"({out} OR {into})"}[check_direction(direction)]

    def _edges(self, workspace: str, table: str, row_id: str, types: Sequence[str] = (), direction: str = "both",
               limit: int = 100, offset: int = 0) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """A page of the row's edges, of these types (all when none) and in this direction, the first made first,
        and the next cursor."""
        if not self._has(workspace, table) or not self._schema(workspace, table).graph:
            raise NotFound(f"No graph table {table!r} in {workspace!r}")
        if self._get(workspace, table, row_id) is None:
            raise NotFound(f"No row {row_id!r} in {workspace}/{table}")
        kinds, params = self._types(workspace, types)
        rows = self._query(f"SELECT * FROM {edges_of(workspace)} WHERE {kinds} AND {self._at(direction)} "
                           "ORDER BY _seq LIMIT $_limit OFFSET $_skip",
                           {**params, "t": table, "id": row_id, "_skip": offset, "_limit": limit + 1})
        more = len(rows) > limit
        return [self._as_edge(r) for r in rows[:limit]], (encode_cursor(offset + limit) if more else None)

    edges = on_pool(_edges)

    def _rows_by_ref(self, workspace: str, refs: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """The rows ``table/id`` references name, by reference."""
        wanted: Dict[str, List[str]] = {}
        for ref in refs:
            table, row_id = ref.split("/", 1)
            wanted.setdefault(table, []).append(row_id)
        found = {}
        for table, ids in wanted.items():
            for row in self._bulk_get(workspace, table, ids):
                found[f"{table}/{row['id']}"] = row
        return found

    @on_pool
    def neighbors(self, workspace: str, table: str, row_id: str, types: Sequence[str] = (), direction: str = "both",
                  limit: int = 100, offset: int = 0) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """A page of the rows one edge away (with the edge each is reached by), and the next cursor."""
        edges, after = self._edges(workspace, table, row_id, types, direction, limit, offset)
        me = f"{table}/{row_id}"
        others = [e["to"] if e["from"] == me else e["from"] for e in edges]
        rows = self._rows_by_ref(workspace, others)
        return [{"table": ref.split("/", 1)[0], "row": rows.get(ref), "edge": e}
                for ref, e in zip(others, edges)], after

    @on_pool
    def traverse(self, workspace: str, table: str, row_id: str, types: Sequence[str] = (), direction: str = "out",
                 depth: int = 2, limit: int = 100) -> List[Dict[str, Any]]:
        """The rows reachable from this one within ``depth`` edges, nearest first, each once, with its table
        and the depth it was first reached at: a breadth-first walk, a query per step."""
        if not self._has(workspace, table) or not self._schema(workspace, table).graph:
            raise NotFound(f"No graph table {table!r} in {workspace!r}")
        if self._get(workspace, table, row_id) is None:
            raise NotFound(f"No row {row_id!r} in {workspace}/{table}")
        if not 1 <= depth <= MAX_DEPTH:
            raise TableError(f"_depth is 1 to {MAX_DEPTH}")
        kinds, params = self._types(workspace, types)
        check_direction(direction)
        seen = {(table, row_id)}
        frontier = [(table, row_id)]
        reached: List[Tuple[str, str, int]] = []
        for step in range(1, depth + 1):
            if not frontier or len(reached) >= limit:
                break
            ends = {"t": [t for t, _ in frontier], "i": [i for _, i in frontier]}
            ways = {"out": [("from", "to")], "in": [("to", "from")], "both": [("from", "to"), ("to", "from")]}[direction]
            found = []
            for near, far in ways:
                found += self._query(
                    f"SELECT e.{far}_table AS t, e.{far}_id AS i, e._seq AS s FROM {edges_of(workspace)} e "
                    f"JOIN (SELECT unnest($t) AS t, unnest($i) AS i) f ON e.{near}_table = f.t AND e.{near}_id = f.i "
                    f"WHERE {kinds}", {**params, **ends})
            frontier = []
            for r in sorted(found, key=lambda r: r["s"]):
                key = (r["t"], r["i"])
                if key not in seen:
                    seen.add(key)
                    frontier.append(key)
                    reached.append((r["t"], r["i"], step))
        reached = reached[:limit]
        rows = self._rows_by_ref(workspace, [f"{t}/{i}" for t, i, _ in reached])
        return [{"table": t, "depth": d, "row": rows[f"{t}/{i}"]} for t, i, d in reached if f"{t}/{i}" in rows]

    @on_pool
    def graph(self, workspace: str, tables: Sequence[str] = (), types: Sequence[str] = (), node_limit: int = 500,
              edge_limit: int = 5000) -> Dict[str, Any]:
        """Rows of the graph tables named (all of them when none, but kept tables, which need the admin key to be
        read and so are drawn only when named), up to ``node_limit`` shared evenly among the tables, and the edges
        of these types (all when none) that join two of them, up to ``edge_limit``: what a view of the graph draws.
        ``outside`` counts the edges leaving a loaded row for one not loaded; ``more`` says whether either limit
        cut the result."""
        schemas = self._tables(workspace)
        if tables:
            for t in tables:
                if check_name("table", t) not in schemas:
                    raise NotFound(f"No table {t!r} in {workspace!r}")
                if not schemas[t].graph:
                    raise TableError(f"{t!r} is not a graph table, so it has no edges to draw")
            chosen = list(dict.fromkeys(tables))
        else:
            # A copy first: another thread may add a table meanwhile (the schemas are read without a lock).
            chosen = sorted(t for t, schema in list(schemas.items()) if schema.graph and self.keeper(t) is None)
        if not 1 <= node_limit <= GRAPH_MAX_NODES:
            raise TableError(f"_limit is 1 to {GRAPH_MAX_NODES}")
        if not 1 <= edge_limit <= GRAPH_MAX_EDGES:
            raise TableError(f"_edge_limit is 1 to {GRAPH_MAX_EDGES}")
        kinds, params = self._types(workspace, types)

        # The first rows of each table by id, the node budget shared evenly: a table with fewer rows than its
        # share leaves the rest to the others.
        fetched = {t: self._read_rows(workspace, t, " ORDER BY id LIMIT $n", {"n": node_limit + 1}) for t in chosen}
        take = {t: 0 for t in chosen}
        left = node_limit
        while left > 0:
            open_tables = [t for t in chosen if take[t] < len(fetched[t])]
            if not open_tables:
                break
            share = max(1, left // len(open_tables))
            for t in open_tables:
                extra = min(share, len(fetched[t]) - take[t], left)
                take[t] += extra
                left -= extra
                if left == 0:
                    break
        nodes = [{"table": t, "row": ordered(r)} for t in chosen for r in fetched[t][:take[t]]]
        more_nodes = any(len(fetched[t]) > take[t] for t in chosen)
        loaded = {(n["table"], n["row"]["id"]) for n in nodes}

        # The edges leaving the loaded rows, kept when they arrive at a loaded row too.
        edges, outside, more_edges = [], 0, False
        for t in chosen:
            ids = [r["id"] for r in fetched[t][:take[t]]]
            for start in range(0, len(ids), SCAN_BATCH):
                if more_edges:
                    break
                rows = self._query(f"SELECT * FROM {edges_of(workspace)} WHERE {kinds} AND from_table = $t AND "
                                   "from_id = ANY($ids) ORDER BY _seq LIMIT $n",
                                   {**params, "t": t, "ids": ids[start:start + SCAN_BATCH], "n": edge_limit + 1})
                if len(rows) > edge_limit:
                    more_edges = True
                for r in rows[:edge_limit]:
                    if (r["to_table"], r["to_id"]) not in loaded:
                        outside += 1
                        continue
                    if len(edges) >= edge_limit:
                        more_edges = True
                        break
                    edges.append(self._as_edge(r))
        return {"nodes": nodes, "edges": edges, "tables": chosen, "outside": outside,
                "more": {"nodes": more_nodes, "edges": more_edges}}

    # ------------------------------------------------------------ SQL queries
    def check_sql(self, query: str) -> None:
        """TableError unless ``query`` is one SELECT (a WITH ... SELECT among them), as DuckDB reads it: what
        ``sql_query`` and ``timed_read`` run, and what a view's SQL must be when it is saved. The parser counts a
        WITH whose part inserts, updates or deletes as a SELECT too: such SQL is refused as it runs (``_read``)."""
        try:
            statements = duckdb.extract_statements(query)
        except duckdb.Error as e:
            raise TableError(f"The SQL does not parse: {db_message(e)}") from None
        if len(statements) != 1:
            raise TableError(f"The SQL is {len(statements)} statements: send one SELECT")
        kind = str(statements[0].type).rsplit(".", 1)[-1]
        if kind != "SELECT":
            raise TableError(f"The SQL is a {kind}: only a SELECT is read here, so nothing changes")

    def _read(self, workspace: str, query: str, params: Optional[Dict[str, Any]], limit: int,
              time_limit_ms: Optional[int] = None) -> List[Dict[str, Any]]:
        """Up to ``limit`` rows of a SELECT already checked (``check_sql``), its unqualified names read in the
        workspace first, in a read-only transaction, so it changes nothing; stopped (QueryTimedOut) past
        ``time_limit_ms``. It is given only the ``params`` the SQL names, as DuckDB's parser reads it: one written
        in a string or a comment ('$n%') is none, and DuckDB refuses a value for it."""
        if workspace not in self._load():
            raise NotFound(f"No workspace named {workspace!r}; an admin creates workspaces")
        named = set(sql_parameters(query))
        params = {k: v for k, v in (params or {}).items() if k in named}
        db = self._db()
        db.execute(f"SET search_path = '{quote(workspace).replace(chr(39), chr(39) * 2)}'")
        timer = threading.Timer(time_limit_ms / 1000, db.cursor.interrupt) if time_limit_ms else None
        try:
            if timer is not None:
                timer.start()
            with db.transaction(read_only=True):
                return db.first(query, params, limit)
        except duckdb.InterruptException:
            raise QueryTimedOut(time_limit_ms) from None
        except duckdb.Error as e:
            if isinstance(e, duckdb.TransactionException) and "read-only" in str(e):
                raise TableError("The SQL writes (a WITH whose part inserts, updates or deletes, or a nextval): only "
                                 "reads run here") from None
            raise TableError(f"The query failed: {db_message(e)}") from None
        finally:
            if timer is not None:
                timer.cancel()
            db.execute("RESET search_path")

    @on_long_pool
    def sql_query(self, workspace: str, query: str, params: Optional[Dict[str, Any]] = None,
                  limit: int = 100) -> List[Dict[str, Any]]:
        """Up to ``limit`` rows of a read-only SQL query over the workspace's tables, named as the API names them."""
        self.check_sql(query)
        return self._read(workspace, query, params, limit)

    @on_long_pool
    def timed_read(self, workspace: str, query: str, params: Optional[Dict[str, Any]], limit: int,
                   time_limit_ms: int) -> Tuple[List[Dict[str, Any]], bool]:
        """Up to ``limit`` rows of a read-only SQL query, as ``sql_query`` gives them, and whether there were more
        (one more is read to know, never the rest), within ``time_limit_ms``: past it the query is stopped, and
        QueryTimedOut raised, never a part of the rows."""
        self.check_sql(query)
        rows = self._read(workspace, query, params, limit + 1, time_limit_ms)
        return rows[:limit], len(rows) > limit


    @on_long_pool
    def timed_read_over(self, workspace: str, rows: List[Dict[str, Any]], query: str, limit: int,
                        time_limit_ms: int) -> Tuple[List[Dict[str, Any]], bool]:
        """``timed_read`` of a SELECT that reads ``rows`` too, as a table named ``rows`` beside the workspace's: each
        field a column, of its values' type (text, a number, true/false) or JSON when they are of several or nested.
        Each row it gives comes back as JSON would hold it (a date as its text). The rows go in as one parameter, so
        nothing of them is written into the SQL."""
        self.check_sql(query)
        shape = {name: _column_of([row[name] for row in rows if row.get(name) is not None])
                 for name in dict.fromkeys(name for row in rows for name in row)}
        wrapped = ("WITH rows AS (SELECT unnest(r) FROM (SELECT unnest(from_json($rows::JSON, $shape)) AS r)) "
                   f"SELECT to_json(_out) AS _row FROM ({query.strip().rstrip(';')}\n) AS _out")
        params = {"rows": json.dumps(rows), "shape": json.dumps([shape])}
        read = self._read(workspace, wrapped, params, limit + 1, time_limit_ms)
        return [r["_row"] for r in read[:limit]], len(read) > limit


def _column_of(values: List[Any]) -> str:
    """The column type that holds every one of ``values``: JSON for nested ones, mixed ones or none at all."""
    kinds = {"BOOLEAN" if isinstance(v, bool) else "BIGINT" if isinstance(v, int) else "DOUBLE"
             if isinstance(v, float) else "VARCHAR" if isinstance(v, str) else "JSON" for v in values}
    if kinds == {"BIGINT", "DOUBLE"}:
        return "DOUBLE"
    return kinds.pop() if len(kinds) == 1 else "JSON"


class QueryTimedOut(Unavailable):
    """A read ran out of its time limit and was stopped (``GraphStore.timed_read``)."""

    def __init__(self, time_limit_ms: int):
        super().__init__(f"The query took longer than its limit of {time_limit_ms} ms and was stopped")
        self.time_limit_ms = time_limit_ms


def sql_parameters(query: str) -> List[str]:
    """The named parameters (``$name``) a SQL statement uses, by name, as DuckDB's parser reads it, so none in a
    comment or a string counts. TableError for SQL it cannot read."""
    try:
        statements = duckdb.extract_statements(query)
    except duckdb.Error as e:
        raise TableError(f"The SQL does not parse: {db_message(e)}") from None
    return sorted({name for st in statements for name in st.named_parameters})
