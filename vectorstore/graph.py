"""
Graph tables: the rows of a /db table made with ``graph`` can be joined by
edges. An edge type is an ArcadeDB edge type ``ne_<namespace>__<name>``, made
on the first edge of that name; at most one edge of a type joins a given pair
of rows, so putting an edge again replaces its properties. Edges go with the
rows they join: deleting a row deletes its edges, and so does dropping a
graph table. Neighbors, traversals, the view a graph is drawn from and
read-only SQL or Cypher queries read the graph.

GraphStore is the TableStore the service runs (api/app.py makes one): every
table method, plus the edge and graph ones here.
"""

import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .schema import (edge_type_name_of, ordered, row_type, row_type_name, check_direction, check_edge, check_edge_ref,
    check_name, Conflict, EdgeRef, encode_cursor, GRAPH_MAX_EDGES, GRAPH_MAX_NODES, MAX_DEPTH, NotFound,
    QUERY_LANGUAGES, TableError)
from .tables import SCAN_BATCH, TableStore, _arcade_message, _retried

# A read-only graph query is rewritten token by token (see GraphStore._rewrite): a bare word or a quoted string
# that names a table or an edge type becomes its ArcadeDB type where the query names a type there, and everything
# else, backticked names included, passes through.
TOKEN = re.compile(r"""`[^`]*`|"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*'|[A-Za-z_][A-Za-z0-9_]*|[^A-Za-z_`"']+""")
_STRINGS = r"""(?:\s*(?:'[^']*'|"[^"]*")\s*,)*"""  # the strings before this one in a list: 'a', 'b',
# Where a type is named, as what the query reads up to the name ends with. SQL: SELECT FROM people, x INSTANCEOF
# people, MATCH {type: people}; and quoted: out('knows') and the other graph functions, @type = 'people',
# @type IN ['people', ...].
SQL_TYPE_WORD = re.compile(r"(?i)(?:\bFROM|\bINSTANCEOF|[{,]\s*(?:type|class)\s*:)\s*$")
SQL_TYPE_STRING = re.compile(r"(?i)(?:\bINSTANCEOF|[{,]\s*(?:type|class)\s*:|@(?:type|class)\s*(?:==?|<>|!=)|"
                             r"@(?:type|class)\s+IN\s*\[" + _STRINGS + r"|(?:\b(?:out|both|oute|ine|bothe)\s*|\bin)\(" +
                             _STRINGS + r")\s*$")
# Cypher: a label or relationship type follows a colon outside a map ((n:people), [e:knows], n:people) or a bar
# after another one ([:knows|likes]); quoted, it is compared with type(e) or labels(n), or looked for in labels(n).
CYPHER_TYPE_STRING = re.compile(r"(?i)\b(?:type|labels)\s*\(\s*\w+\s*\)\s*(?:=|<>|IN\s*\[" + _STRINGS + r")\s*$")
CYPHER_IN_LABELS = re.compile(r"(?i)\s*IN\s+labels\s*\(")


class GraphStore(TableStore):
    """A TableStore whose graph tables' rows can be linked by edges."""

    def _vertex(self, namespace: str, table: str, row_id: str, db, what: str) -> None:
        """TableError unless ``table/row_id`` is a row of a graph table."""
        if not self._has(namespace, table):
            raise TableError(f"No table {table!r} in {namespace!r} ({what})")
        if not self._schema(namespace, table).graph:
            raise TableError(f"{table!r} is not a graph table, so its rows cannot be linked ({what})")
        if self._get(namespace, table, row_id, db) is None:
            raise TableError(f"No row {row_id!r} in {namespace}/{table} ({what})")

    def _put_edge(self, db, namespace: str, ref: EdgeRef, props: Dict[str, Any]) -> bool:
        """Store the edge, replacing the properties of the one already joining the pair. True when new."""
        self._vertex(namespace, ref.from_table, ref.from_id, db, "the edge's from")
        self._vertex(namespace, ref.to_table, ref.to_id, db, "the edge's to")
        # Its type was made before the transaction (_making), since schema changes are not part of one.
        edge = edge_type_name_of(namespace, ref.type)
        params = {"f": ref.from_id, "t": ref.to_id, "tt": row_type_name(namespace, ref.to_table), "props": props}
        # Among the row's own out-edges (found by its id index), not by a scan of the edge type.
        rows = self.database.rows(db.command(
            "sql", f"UPDATE (SELECT expand(outE('{edge}')) FROM {row_type(namespace, ref.from_table)} WHERE id = :f) "
                   f"CONTENT :props WHERE inV().id = :t AND inV().@type = :tt", params))
        if rows and rows[0].get("count"):
            return False
        db.command("sql", f"CREATE EDGE `{edge}` FROM (SELECT FROM {row_type(namespace, ref.from_table)} WHERE id = :f) "
                          f"TO (SELECT FROM {row_type(namespace, ref.to_table)} WHERE id = :t) CONTENT :props", params)
        return True

    def _apply_edges(self, db, namespace: str, puts: List[Tuple[EdgeRef, Dict[str, Any]]], deletes: List[EdgeRef]) -> None:
        for ref, props in puts:
            self._put_edge(db, namespace, ref, props)
        for ref in deletes:
            self._delete_edge(db, namespace, ref)

    def _delete_edge(self, db, namespace: str, ref: EdgeRef) -> None:
        if ref.type not in self._edges_of(namespace) or not self._has(namespace, ref.from_table) \
                or not self._has(namespace, ref.to_table):
            return
        edge = edge_type_name_of(namespace, ref.type)
        db.command("sql", f"DELETE FROM (SELECT expand(outE('{edge}')) FROM {row_type(namespace, ref.from_table)} "
                          f"WHERE id = :f) WHERE inV().id = :t AND inV().@type = :tt",
                   {"f": ref.from_id, "t": ref.to_id, "tt": row_type_name(namespace, ref.to_table)})

    @_retried
    def _put_edges(self, namespace: str, edges: List[Dict[str, Any]]) -> List[bool]:
        checked = [check_edge(e) for e in edges]
        self._tables(namespace)
        with self._making(namespace, [], [ref.type for ref, _ in checked]), self._writing() as db:
            return [self._put_edge(db, namespace, ref, props) for ref, props in checked]

    async def put_edges(self, namespace: str, edges: List[Dict[str, Any]]) -> List[bool]:
        """Store the edges (``{type, from: 'table/id', to: 'table/id', ...properties}``), all or none; for each,
        whether it was new. 400 unless both ends are rows of graph tables."""
        return await self._run(self._put_edges, namespace, edges)

    @_retried
    def _delete_edges(self, namespace: str, edges: List[Dict[str, Any]]) -> None:
        refs = [check_edge_ref(e) for e in edges]
        self._tables(namespace)
        with self._writing() as db:
            for ref in refs:
                self._delete_edge(db, namespace, ref)

    async def delete_edges(self, namespace: str, edges: List[Dict[str, Any]]) -> None:
        """Delete these edges (by type, from and to); ones that are not there are ignored."""
        await self._run(self._delete_edges, namespace, edges)

    def _create_edge_type(self, namespace: str, edge_type: str) -> Dict[str, Any]:
        with self._schema_lock:
            if check_name("edge type", edge_type) in self._edges_of(namespace):
                raise Conflict(f"A relationship (edge type) named {edge_type!r} already exists in {namespace!r}")
            self._ensure_edge_type(namespace, edge_type)
            return self._edge_info(namespace, edge_type)

    async def create_edge_type(self, namespace: str, edge_type: str) -> Dict[str, Any]:
        """Make an empty edge type, as its first edge would; Conflict if it exists or a table has its name."""
        return await self._run(self._create_edge_type, namespace, edge_type)

    def _edge_type_details(self, namespace: str) -> List[Dict[str, Any]]:
        prefix = len(row_type_name(namespace, ""))
        types = []
        for name in sorted(self._edges_of(namespace)):
            # A scan of the edges, so it is here and not in the namespace's description, which every listing reads.
            rows = self._query(f"SELECT @out.@type AS f, @in.@type AS t, count(*) AS n "
                               f"FROM `{edge_type_name_of(namespace, name)}` GROUP BY f, t")
            joins = sorted(({"from": r["f"][prefix:], "to": r["t"][prefix:], "count": r["n"]} for r in rows),
                           key=lambda j: (-j["count"], j["from"], j["to"]))
            types.append({**self._edge_info(namespace, name), "joins": joins})
        return types

    async def edge_types(self, namespace: str) -> List[Dict[str, Any]]:
        """The namespace's edge types as its description lists them, each with the tables its edges join: every
        pair of the table they leave and the table they arrive at, with how many edges join it, most first."""
        return await self._run(self._edge_type_details, namespace)

    def _edge_name(self, namespace: str, type_name: str) -> str:
        return type_name[len(edge_type_name_of(namespace, "")):]

    def _edge_types_clause(self, namespace: str, types: Sequence[str]) -> str:
        """The edge types as the arguments of ``outE()`` and friends; every edge type when none are named."""
        known = self._edges_of(namespace)
        missing = [t for t in types if check_name("edge type", t) not in known]
        if missing:
            raise NotFound(f"No edge type named {', '.join(missing)} in {namespace!r}")
        return ", ".join(f"'{edge_type_name_of(namespace, t)}'" for t in types)

    def _as_edge(self, namespace: str, r: Dict[str, Any]) -> Dict[str, Any]:
        """A result row holding an edge's properties and its ends as ``__t``, ``__ft``... as the API shows an edge."""
        table_name = lambda t: t[len(row_type_name(namespace, "")):]  # noqa: E731
        return {"type": self._edge_name(namespace, r["__t"]),
                "from": f"{table_name(r['__ft'])}/{r['__fid']}", "to": f"{table_name(r['__tt'])}/{r['__tid']}",
                **{k: v for k, v in r.items() if not k.startswith("__")}}

    def _edges(self, namespace: str, table: str, row_id: str, types: Sequence[str], direction: str,
               limit: int, offset: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        if not self._has(namespace, table) or not self._schema(namespace, table).graph:
            raise NotFound(f"No graph table {table!r} in {namespace!r}")
        if self._get(namespace, table, row_id) is None:
            raise NotFound(f"No row {row_id!r} in {namespace}/{table}")
        kinds = self._edge_types_clause(namespace, types)
        rows = self._query(
            f"SELECT *, @type AS __t, outV().@type AS __ft, outV().id AS __fid, inV().@type AS __tt, inV().id AS __tid "
            f"FROM (SELECT expand({check_direction(direction)}E({kinds})) FROM {row_type(namespace, table)} WHERE id = :id) "
            f"SKIP :_skip LIMIT :_limit", {"id": row_id, "_skip": offset, "_limit": limit + 1})
        more = len(rows) > limit
        return [self._as_edge(namespace, r) for r in rows[:limit]], (encode_cursor(offset + limit) if more else None)

    async def edges(self, namespace: str, table: str, row_id: str, types: Sequence[str] = (), direction: str = "both",
                    limit: int = 100, offset: int = 0) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """A page of the row's edges, of these types (all when none) and in this direction, and the next cursor."""
        return await self._run(self._edges, namespace, table, row_id, types, direction, limit, offset)

    def _rows_by_ref(self, namespace: str, refs: Sequence[str]) -> Dict[str, Dict[str, Any]]:
        """The rows ``table/id`` references name, by reference."""
        wanted: Dict[str, List[str]] = {}
        for ref in refs:
            table, row_id = ref.split("/", 1)
            wanted.setdefault(table, []).append(row_id)
        found = {}
        for table, ids in wanted.items():
            for row in self._bulk_get(namespace, table, ids):
                found[f"{table}/{row['id']}"] = row
        return found

    def _neighbors(self, namespace: str, table: str, row_id: str, types: Sequence[str], direction: str,
                   limit: int, offset: int) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        edges, after = self._edges(namespace, table, row_id, types, direction, limit, offset)
        me = f"{table}/{row_id}"
        others = [e["to"] if e["from"] == me else e["from"] for e in edges]
        rows = self._rows_by_ref(namespace, others)
        return [{"table": ref.split("/", 1)[0], "row": rows.get(ref), "edge": e}
                for ref, e in zip(others, edges)], after

    async def neighbors(self, namespace: str, table: str, row_id: str, types: Sequence[str] = (),
                        direction: str = "both", limit: int = 100, offset: int = 0):
        """A page of the rows one edge away (with the edge each is reached by), and the next cursor."""
        return await self._run(self._neighbors, namespace, table, row_id, types, direction, limit, offset)

    def _traverse(self, namespace: str, table: str, row_id: str, types: Sequence[str], direction: str,
                  depth: int, limit: int) -> List[Dict[str, Any]]:
        if not self._has(namespace, table) or not self._schema(namespace, table).graph:
            raise NotFound(f"No graph table {table!r} in {namespace!r}")
        if self._get(namespace, table, row_id) is None:
            raise NotFound(f"No row {row_id!r} in {namespace}/{table}")
        if not 1 <= depth <= MAX_DEPTH:
            raise TableError(f"_depth is 1 to {MAX_DEPTH}")
        kinds = self._edge_types_clause(namespace, types)
        rows = self._query(
            f"SELECT *, @type AS __t, $depth AS __d FROM (TRAVERSE {check_direction(direction)}({kinds}) "
            f"FROM (SELECT FROM {row_type(namespace, table)} WHERE id = :id) MAXDEPTH {int(depth)} "
            f"STRATEGY BREADTH_FIRST) WHERE $depth > 0 LIMIT :_limit", {"id": row_id, "_limit": limit})
        prefix = len(row_type_name(namespace, ""))
        return [{"table": r["__t"][prefix:], "depth": r["__d"],
                 "row": ordered({k: v for k, v in r.items() if not k.startswith("__")})} for r in rows]

    async def traverse(self, namespace: str, table: str, row_id: str, types: Sequence[str] = (),
                       direction: str = "out", depth: int = 2, limit: int = 100) -> List[Dict[str, Any]]:
        """The rows reachable from this one within ``depth`` edges, nearest first, each once, with its table
        and the depth it was first reached at."""
        return await self._run(self._traverse, namespace, table, row_id, types, direction, depth, limit)

    def _graph(self, namespace: str, tables: Sequence[str], types: Sequence[str], node_limit: int,
               edge_limit: int) -> Dict[str, Any]:
        schemas = self._tables(namespace)
        if tables:
            for t in tables:
                if check_name("table", t) not in schemas:
                    raise NotFound(f"No table {t!r} in {namespace!r}")
                if not schemas[t].graph:
                    raise TableError(f"{t!r} is not a graph table, so it has no edges to draw")
            chosen = list(dict.fromkeys(tables))
        else:
            # A copy first: another thread may add a table meanwhile (the schemas are read without a lock).
            chosen = sorted(t for t, schema in list(schemas.items()) if schema.graph)
        if not 1 <= node_limit <= GRAPH_MAX_NODES:
            raise TableError(f"_limit is 1 to {GRAPH_MAX_NODES}")
        if not 1 <= edge_limit <= GRAPH_MAX_EDGES:
            raise TableError(f"_edge_limit is 1 to {GRAPH_MAX_EDGES}")
        kinds = self._edge_types_clause(namespace, types)

        # The first rows of each table by id, the node budget shared evenly: a table with fewer rows than its
        # share leaves the rest to the others.
        fetched = {t: self._query(f"SELECT * FROM {row_type(namespace, t)} ORDER BY id LIMIT :n", {"n": node_limit + 1})
                   for t in chosen}
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
        prefix = len(row_type_name(namespace, ""))
        for t in chosen:
            ids = [r["id"] for r in fetched[t][:take[t]]]
            for start in range(0, len(ids), SCAN_BATCH):
                if more_edges:
                    break
                rows = self._query(
                    f"SELECT *, @type AS __t, outV().@type AS __ft, outV().id AS __fid, inV().@type AS __tt, "
                    f"inV().id AS __tid FROM (SELECT expand(outE({kinds})) FROM {row_type(namespace, t)} "
                    f"WHERE id IN :ids) LIMIT :n", {"ids": ids[start:start + SCAN_BATCH], "n": edge_limit + 1})
                if len(rows) > edge_limit:
                    more_edges = True
                for r in rows[:edge_limit]:
                    if (r["__tt"][prefix:], r["__tid"]) not in loaded:
                        outside += 1
                        continue
                    if len(edges) >= edge_limit:
                        more_edges = True
                        break
                    edges.append(self._as_edge(namespace, r))
        return {"nodes": nodes, "edges": edges, "tables": chosen, "outside": outside,
                "more": {"nodes": more_nodes, "edges": more_edges}}

    async def graph(self, namespace: str, tables: Sequence[str] = (), types: Sequence[str] = (),
                    node_limit: int = 500, edge_limit: int = 5000) -> Dict[str, Any]:
        """Rows of the graph tables named (all of them when none), up to ``node_limit`` shared evenly among the
        tables, and the edges of these types (all when none) that join two of them, up to ``edge_limit``: what
        a view of the graph draws. ``outside`` counts the edges leaving a loaded row for one not loaded; ``more``
        says whether either limit cut the result."""
        return await self._run(self._graph, namespace, tables, types, node_limit, edge_limit)

    def _rewrite(self, namespace: str, language: str, query: str) -> str:
        """The query with the namespace's table and edge type names turned into their ArcadeDB types where it names
        a type (SQL_TYPE_WORD and the patterns after it): a field, an alias, a parameter, a keyword or a value
        called like a table keeps its name."""
        tables, edges = self._tables(namespace), self._edges_of(namespace)

        def name(token: str) -> Optional[str]:
            if token in tables:
                return row_type_name(namespace, token)
            if token in edges:
                return edge_type_name_of(namespace, token)
            return None

        def names_type(m: "re.Match", quoted: bool) -> bool:
            before = query[:m.start()]
            if language == "sql":
                return bool((SQL_TYPE_STRING if quoted else SQL_TYPE_WORD).search(before))
            if quoted:
                return bool(CYPHER_TYPE_STRING.search(before) or CYPHER_IN_LABELS.match(query, m.end()))
            # Within a map ({since: 2020}) a colon is followed by a value.
            if depth:
                return False
            return before.rstrip().endswith(":") or bool(
                type_end is not None and re.fullmatch(r"\s*\|\s*:?\s*", query[type_end:m.start()]))

        out: List[str] = []
        depth, type_end = 0, None  # Cypher: how deep in braces; where the last label or type named ended
        for m in TOKEN.finditer(query):
            token = m.group()
            if token[0] in "'\"":
                if len(token) > 2 and (known := name(token[1:-1])) and names_type(m, quoted=True):
                    token = f"{token[0]}{known}{token[0]}"
            elif token[0].isalpha() or token[0] == "_":
                if (known := name(token)) and names_type(m, quoted=False):
                    token, type_end = f"`{known}`", m.end()
            elif token[0] != "`":
                depth = max(0, depth + token.count("{") - token.count("}"))
            out.append(token)
        return "".join(out)

    def _unname(self, namespace: str, value: Any) -> Any:
        """Result values with the namespace's ArcadeDB type names turned back into table and edge names, and
        vertices and edges as the API shows rows and edges."""
        table_prefix, edge_prefix = row_type_name(namespace, ""), edge_type_name_of(namespace, "")
        if isinstance(value, str):
            if value.startswith(table_prefix):
                return value[len(table_prefix):]
            if value.startswith(edge_prefix):
                return value[len(edge_prefix):]
            return value
        if isinstance(value, dict):
            return {k: self._unname(namespace, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._unname(namespace, v) for v in value]
        return value

    def _element(self, namespace: str, value: Any) -> Any:
        """A Java vertex or edge in a result as the API shows them, a row as ``{table, row}`` (apart, so a field
        called ``table`` stays the row's) and an edge as ``{type, from, to, ...}``; a Cypher path as the list of its
        rows and edges in order; other values as they are."""
        if hasattr(value, "toAlternatingList"):
            return [self._element(namespace, v) for v in value.toAlternatingList()]
        if hasattr(value, "getTypeName") and hasattr(value, "toJSON"):
            # Its JSON, rather than its Java map, so the values come back as Python ones.
            props = {k: v for k, v in json.loads(str(value.toJSON())).items() if not k.startswith("@")}
            kind = str(value.getTypeName())
            if hasattr(value, "getOut") and hasattr(value, "getIn"):
                ends = []
                for end in (value.getOut(), value.getIn()):
                    v = end.asVertex(True) if hasattr(end, "asVertex") else end
                    ends.append(f"{self._unname(namespace, str(v.getTypeName()))}/{str(v.get('id'))}")
                return {"type": self._unname(namespace, kind), "from": ends[0], "to": ends[1], **props}
            return {"table": self._unname(namespace, kind), "row": ordered(props)}
        if isinstance(value, dict):
            return {k: self._element(namespace, v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._element(namespace, v) for v in value]
        return value

    def _graph_query(self, namespace: str, language: str, query: str, params: Dict[str, Any],
                     limit: int) -> List[Dict[str, Any]]:
        if language not in QUERY_LANGUAGES:
            raise TableError(f"language is one of {', '.join(QUERY_LANGUAGES)}")
        sql = self._rewrite(namespace, language, query)
        try:
            result = self._db().query(language, sql, params)
            rows = []
            for r in result:
                raw = {str(k): v for k, v in r.to_dict().items() if not str(k).startswith("@")}
                rows.append(self._unname(namespace, {k: self._element(namespace, v) for k, v in raw.items()}))
                if len(rows) >= limit:
                    break
            return rows
        except Exception as e:
            if type(e).__name__ != "ArcadeDBError":
                raise
            raise TableError(f"The query failed: {_arcade_message(e)}") from None

    async def graph_query(self, namespace: str, language: str, query: str, params: Optional[Dict[str, Any]] = None,
                          limit: int = 100) -> List[Dict[str, Any]]:
        """Up to ``limit`` rows of a read-only SQL or Cypher query over the namespace's tables and edges, named
        as the API names them (see ``_rewrite``)."""
        return await self._run(self._graph_query, namespace, language, query, params or {}, limit)


