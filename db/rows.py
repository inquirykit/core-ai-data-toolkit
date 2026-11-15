"""
Reading and writing a table's rows: a part of TableStore (tables.py, where
the model is told). Filters, sorts, full-text search and pages for the
lists; inserts, upserts, patches, deletes and transactions, each checked
against the table's schema and its owner, with unique-key clashes answered
as Conflicts (``_writing``).

A row is stored as its columns: a declared field's in its own, the others
together in ``_rest`` (a JSON object), and ``_seq``, the number of the write
that last changed it (from one sequence, so it grows with every write). Read
back, a row holds the fields it has a value for, as it was written.

The full-text index (DuckDB's fts, one per table over its full-text fields)
does not follow writes: it holds the rows as they were when it was built,
and beside it (HELD) the ``_seq`` each one had then: a number no other
row, nor the row's next write, has, so a row with one of them is as the
index holds it. A search reads the index for those rows, and scans the
others for the same words, split as the index splits them
(schema.words_of, IGNORE), so a row is found as soon as its write commits,
even a write that took its number before the build and committed after it
(a sequence is not part of a transaction); those rows have no score yet,
and come first. A search rebuilds the index when a row was written since
it was built (a ``_seq`` past the newest it saw), unless it was rebuilt a
moment ago (within REBUILD_SPACING times what the last build took), so a
table written all the time is rebuilt about a twentieth of the time at most.
"""

import functools
import json
import logging
import re
import threading
import time
from contextlib import ExitStack, contextmanager
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import duckdb

from .engine import SEQUENCE, Clash, Conn, on_pool, quote, retried
from .refs import expand_tree
from .schema import (column_type, edges_of, fts_schema, ordered, row_type, check_edge, check_edge_ref, EdgeRef, check_field, check_name, check_row, Conflict, EMPTY, encode_cursor,
    filter_value, IGNORE, MAX_ANY_WORDS, merge_patch, words_of, NotFound, Search, search_query, TableError, TABLES, TableSchema)

logger = logging.getLogger(__name__)

# A unique value held twice, as DuckDB says it: as the statement runs (naming the field), or when two rows of the same
# transaction hold it, or at the commit when another transaction stored it meanwhile (naming the value only).
DUPLICATE = re.compile(r'Duplicate key "([^":]+): (.*?)" violates')
DUPLICATE_AT_COMMIT = re.compile(r'(?:PRIMARY KEY or UNIQUE )?constraint violation: duplicate key "(.*?)"', re.I)
# A search waits for the full-text index to be rebuilt when rows changed since its last build, unless that build was
# less than this many times its own length ago: about a twentieth of the time at most goes to rebuilding.
REBUILD_SPACING = 20
# A rebuild of a table with no rows records this as the write it holds up to.
NO_ROWS = -1
# The table, in a full-text index's schema, of the rows it holds: their write numbers (_seq) as it was built. So a
# store's own UPDATE of a full-text field gives the row a new number, as a write here does (evidence's search rows).
HELD = "tk_held"


Filters = Sequence[Tuple[str, str]]  # (field, the value as the query string gives it)


Sort = Sequence[Tuple[str, bool]]  # (field, descending)


Row = Dict[str, Any]


def _ends(refs: Iterable[EdgeRef]) -> List[str]:
    """The names an edge write touches: each edge's type and the tables at its ends."""
    return [name for ref in refs for name in (ref.type, ref.from_table, ref.to_table)]


def decoded(raw: Dict[str, Any], vector: Optional[str] = None) -> Row:
    """A row as the database gives it (its columns), as it was written: the fields with a value, ``_rest``'s among
    them (a declared field written as null is noted there, ``_encoded``), without ``_seq`` and a search's ``_score``.
    A column's NULL alone is no field."""
    row = {k: v for k, v in raw.items() if v is not None and not k.startswith("_") and k != vector}
    rest = raw.get("_rest")
    if isinstance(rest, dict):
        row.update({k: v for k, v in rest.items() if k not in row and k != vector})
    return row


def _words(fields: Sequence[str]) -> str:
    """SQL for a row's full-text fields as the index splits them: their words between spaces, a space at either end,
    so ``contains(…, ' word ')`` finds a word and ``contains(…, ' start')`` a word starting so."""
    text = "concat_ws(' ', " + ", ".join(quote(f) for f in fields) + ")"
    return f"(' ' || regexp_replace(strip_accents(lower({text})), '{IGNORE}', ' ', 'g') || ' ')"


def words_held(column: str, text: str) -> Tuple[str, Dict[str, Any]]:
    """The condition that the text column ``column`` (SQL) holds every word of ``text``, as the full-text index splits
    words (schema.words_of), and its parameters: a scan, for text with no index (traces). One that holds nothing
    when the text has no word."""
    words = list(dict.fromkeys(words_of(text)))
    if not words:
        return "false", {}
    split = f"(' ' || regexp_replace(strip_accents(lower(coalesce({column}, ''))), '{IGNORE}', ' ', 'g') || ' ')"
    params = {f"_word{i}": w for i, w in enumerate(words)}
    return "(" + " AND ".join(f"contains({split}, ' ' || $_word{i} || ' ')" for i in range(len(words))) + ")", params


class Rows:
    """TableStore's reads and writes of rows (a mixin: tables.py builds the store)."""

    @contextmanager
    def _writing(self, workspace: str = "", tables: Iterable[str] = ()) -> Iterator[Conn]:
        """A transaction whose unique-key clashes (within it, or with a concurrent writer) are Conflicts. It holds
        the gates of the tables whose rows it writes (``tables``), so their schemas do not change meanwhile (ddl.py);
        it waits for a change under way, after which the rows are checked again (``_fits``). One that writes a joined
        table also holds the workspace's ref lock and the gates of all its joined tables, which its refs' checks and
        cascades read and write (refs.py)."""
        db = self._db()
        tables = set(tables)
        joined = bool(tables & self._joined(workspace))
        with ExitStack() as gates:
            if joined:
                gates.enter_context(self._ref_lock(workspace))
                # Stable now: what is joined changes only under the lock.
                tables |= self._joined(workspace)
            for table in sorted(tables):
                gates.enter_context(self._gate(workspace, table).shared())
            if not joined and tables & self._joined(workspace):
                raise Clash("A ref to a table this write touches was declared meanwhile; send it again")
            try:
                with db.transaction():
                    yield db
            except (duckdb.ConstraintException, duckdb.TransactionException) as e:
                text = str(e)
                # A key (a row's id, an edge's ends and type) stored by a write that committed meanwhile, which this
                # one could not see: it runs again (``retried``) and finds it. A write never stores a key twice.
                if "violates primary key" in text or (isinstance(e, duckdb.TransactionException)
                                                      and DUPLICATE_AT_COMMIT.search(text)):
                    raise Clash("Another write stored the same row or edge at the same time; send it again") from None
                found = DUPLICATE.search(text)
                if found:
                    name, value = found.groups()
                    raise Conflict(f"Another row already has {name} = {value!r}, and {name} is unique") from None
                at_commit = DUPLICATE_AT_COMMIT.search(text)
                if at_commit:
                    raise Conflict(f"Two rows would hold the unique value {at_commit.group(1)!r}") from None
                if isinstance(e, duckdb.ConstraintException):
                    raise TableError(f"The write breaks a rule of the table: {e}") from None
                raise

    def _fits(self, workspace: str, puts: Dict[str, List[Row]], checked: Dict[str, TableSchema],
              merge: bool = False) -> None:
        """Check the rows again where their table's schema is no longer the one they were checked against
        (``checked``), as it changed while the write waited at its gate: within ``_writing``, where it cannot
        change again. NotFound when the table was dropped meanwhile. Rows to ``merge`` are checked once merged."""
        for table, rows in puts.items():
            if rows and not self._has(workspace, table):
                raise NotFound(f"{workspace}/{table} was dropped while the write waited for it")
            schema = self._schema(workspace, table)
            if not merge and schema is not checked.get(table):
                for row in rows:
                    check_row(row, schema)

    # ------------------------------------------------------------ the rows' columns
    def _read_rows(self, workspace: str, table: str, rest: str, params: Dict[str, Any], *, vector: bool = False,
                   source: Optional[str] = None) -> List[Row]:
        """The table's rows ``rest`` selects (its WHERE, ORDER BY, LIMIT), decoded; without the vector field, whose
        hundreds of numbers a row are read only with the row alone (``_get``), unless ``vector``. ``source`` is what
        they are read from, when not the table itself (a search's, which adds its score)."""
        field = self._schema(workspace, table).vector
        leave_out = field.name if field is not None and not vector else None
        columns = f"* EXCLUDE ({quote(leave_out)})" if leave_out else "*"
        rows = self._query(f"SELECT {columns} FROM {source or row_type(workspace, table)}{rest}", params)
        return [decoded(r, leave_out) for r in rows]

    def _encoded(self, workspace: str, table: str, rows: Sequence[Row]) -> Tuple[str, List[str], Dict[str, Any]]:
        """The rows as one INSERT takes them: ``INSERT INTO … SELECT`` each column's values as a list, unnested
        together, so a thousand rows are one statement, not a thousand; and the columns' names. The fields a row does
        not declare go into ``_rest``. Each row gets its ``_seq`` in the order given."""
        schema = self._schema(workspace, table)
        names, columns, params = ["id"], ["unnest($id::VARCHAR[])"], {"id": [row["id"] for row in rows]}
        for i, (name, f) in enumerate(schema.fields.items()):
            kind = column_type(f)
            names.append(quote(name))
            values = [row.get(name) for row in rows]
            if kind == "JSON":
                columns.append(f"unnest($c{i}::VARCHAR[])::JSON")
                values = [None if value is None else json.dumps(value) for value in values]
            else:
                columns.append(f"unnest($c{i}::{kind}[])")
            params[f"c{i}"] = values
        rests = []
        for row in rows:
            extra = {k: v for k, v in row.items() if k != "id" and k not in schema.fields}
            # A declared field written as null is kept as written: its column is NULL either way, so the null is noted
            # among the rest, and the row reads back with it (``decoded``) where a field never written stays out.
            extra.update({k: None for k in schema.fields if k in row and row[k] is None})
            rests.append(json.dumps(extra) if extra else None)
        names += ["_rest", "_seq"]
        columns += ["unnest($rest::VARCHAR[])::JSON", f"nextval('{SEQUENCE}')"]
        params["rest"] = rests
        return f"INSERT INTO {row_type(workspace, table)} ({', '.join(names)}) SELECT {', '.join(columns)}", names, params

    def _put(self, db: Conn, workspace: str, table: str, rows: Sequence[Row]) -> None:
        """Store the rows whole, each replacing any with its id: of rows sharing an id the last, at its place (a
        statement may not change a row twice), so it ends as one write after another would leave it. A unique value
        is checked once the whole statement is in, not row by row: rows handing values on among themselves (a takes
        b's, b a new one) are stored, where one write after another would have refused the first."""
        last: Dict[str, Row] = {}
        for row in reversed(rows):
            last.setdefault(row["id"], row)
        if not last:
            return
        insert, names, params = self._encoded(workspace, table, list(reversed(last.values())))
        updates = ", ".join(f"{n} = excluded.{n}" for n in names if n != "id")
        db.execute(f"{insert} ON CONFLICT (id) DO UPDATE SET {updates}", params)

    def _add(self, db: Conn, workspace: str, table: str, row: Row) -> None:
        """Store a new row: a Conflict (``_writing``) when its id is taken."""
        insert, _, params = self._encoded(workspace, table, [row])
        db.execute(insert, params)

    def write_rows(self, db: Conn, workspace: str, table: str, rows: Sequence[Row]) -> None:
        """For a store that keeps its rows in this table: store ``rows`` whole on ``db``, in the store's transaction,
        each replacing any with its id, as a write here does (unchecked: the store's rows are its own). It takes no gate
        (ddl.py): the store's tables change only as it declares them, before it writes. Blocking."""
        self._put(db, workspace, table, rows)

    def write_row(self, db: Conn, workspace: str, table: str, row: Row) -> None:
        """``write_rows`` of one row."""
        self._put(db, workspace, table, [row])

    def _delete_ids(self, db: Conn, workspace: str, table: str, ids: List[str]) -> None:
        """Delete these rows, and the edges at them."""
        if self._schema(workspace, table).graph:
            db.execute(f"DELETE FROM {edges_of(workspace)} WHERE (from_table = $t AND from_id = ANY($ids)) OR "
                       "(to_table = $t AND to_id = ANY($ids))", {"t": table, "ids": ids})
        db.execute(f"DELETE FROM {row_type(workspace, table)} WHERE id = ANY($ids)", {"ids": ids})

    # ---------------------------------------------------------------- filters
    @staticmethod
    def _where(filters: Filters, schema: TableSchema) -> Tuple[List[str], Dict[str, Any]]:
        """The filters as conditions and their parameters: on a declared field, equality as its type (a list
        holding the value); on another, its JSON value equal to the value's, or a list of them holding it."""
        terms, params = [], {}
        for i, (name, raw) in enumerate(filters):
            check_field(name)
            declared = schema.fields.get(name)
            value = declared.parse(raw) if declared else filter_value(raw)
            if declared and column_type(declared) != "JSON":
                col = quote(name)
                if value is None:
                    terms.append(f"{col} IS NULL")
                elif declared.list:
                    params[f"v{i}"] = value
                    terms.append(f"list_contains({col}, $v{i})")
                else:
                    params[f"v{i}"] = value
                    terms.append(f"{col} = $v{i}")
                continue
            # Read back through ->, which writes the JSON as to_json does: a letter beyond ASCII as itself, where
            # json.dumps wrote an escape.
            json_value = f"({quote(name)} -> '$')" if declared else f"(_rest -> '$.\"{name}\"')"
            if value is None:
                terms.append(f"({json_value} IS NULL OR json_type({json_value}) = 'NULL')")
                continue
            params[f"v{i}"] = value
            terms.append(f"(CASE WHEN json_type({json_value}) = 'ARRAY' THEN list_contains(CAST({json_value} AS JSON[]), "
                         f"to_json($v{i})) ELSE {json_value} = to_json($v{i}) END)")
        return terms, params

    @staticmethod
    def _sort_keys(schema: TableSchema, name: str, desc: bool) -> List[str]:
        """A field to sort by, as ORDER BY keys: its column; an undeclared one's numbers by their value, then its other
        values by their text."""
        check_field(name)
        way = " DESC" if desc else ""
        if name in schema.fields:
            return [f"{quote(name)}{way}"]
        value = f"(_rest -> '$.\"{name}\"')"
        number = f"(CASE WHEN json_type({value}) IN ('UBIGINT', 'BIGINT', 'DOUBLE') THEN ({value} ->> '$')::DOUBLE END)"
        return [f"{number}{way}", f"({value} ->> '$'){way}"]

    # -------------------------------------------------------------- full-text
    def _fulltext_lock(self, workspace: str, table: str) -> threading.Lock:
        with self._schema_lock:
            return self._rebuild_locks.setdefault((workspace, table), threading.Lock())

    def _fresh_index(self, workspace: str, table: str) -> Optional[int]:
        """The write the table's full-text index holds up to, rebuilt first when rows changed since and the last build
        was not just now (REBUILD_SPACING); None while it has no index."""
        key = (workspace, table)
        built = self._fulltext_seq.get(key)
        latest = self._query(f"SELECT coalesce(max(_seq), {NO_ROWS}) AS s FROM {row_type(workspace, table)}")[0]["s"]
        if built is not None and latest <= built:
            return built
        at, took = self._fulltext_built.get(key, (0.0, 0.0))
        if built is not None and time.time() - at < REBUILD_SPACING * took:
            return built
        with self._fulltext_lock(workspace, table):
            if self._fulltext_seq.get(key) != built:
                return self._fulltext_seq.get(key)  # another search rebuilt it meanwhile
            return self._rebuild_index(workspace, table)

    def _rebuild_index(self, workspace: str, table: str) -> Optional[int]:
        """Build the table's full-text index again over every row, and note the write it holds up to."""
        fields = self._schema(workspace, table).fulltext
        if not fields:
            return None
        start = time.monotonic()
        db = self._db()
        t = row_type(workspace, table)
        with db.transaction():
            # The index, the rows it holds and the number are read in one transaction, so they agree.
            seq = db.rows(f"SELECT coalesce(max(_seq), {NO_ROWS}) AS s FROM {t}")[0]["s"]
            name = t.replace("'", "''")
            columns = ", ".join(f"'{f}'" for f in fields)
            db.execute(f"PRAGMA create_fts_index('{name}', 'id', {columns}, stemmer = 'none', stopwords = 'none', "
                       f"ignore = '{IGNORE}', overwrite = 1)")
            db.execute(f"CREATE OR REPLACE TABLE {fts_schema(workspace, table)}.{HELD} AS SELECT _seq FROM {t}")
            db.execute(f"UPDATE {TABLES} SET fts_seq = $s WHERE workspace = $n AND name = $t",
                       {"s": seq, "n": workspace, "t": table})
        self._fulltext_seq[(workspace, table)] = seq
        self._fulltext_built[(workspace, table)] = (time.time(), time.monotonic() - start)
        return seq

    def searching(self, workspace: str, table: str, found: Optional[Search] = None,
                  rank: Optional[Search] = None) -> Tuple[str, List[str], Dict[str, Any]]:
        """What a search of the table's full-text fields reads from and the conditions a row must meet, with their
        parameters: the table with its rows' ``_score`` (BM25, higher better; NULL for a row the index does not hold
        as it is now, which comes first: ORDER BY _score DESC NULLS FIRST), for ``found`` (schema.search_query) and the
        words ``rank`` ranks by (schema.any_words: a row holds one of them). TableError for a table
        with no full-text field. For TableStore's lists, and for stores that search their own tables in SQL
        (evidence/store.py). Blocking: it may rebuild the index first."""
        fields = self._schema(workspace, table).fulltext
        if not fields:
            raise TableError(f"{table} has no full-text field to search: make a string field full-text in its schema")
        words = _words(fields)
        terms: List[str] = []
        params: Dict[str, Any] = {}

        def holds(word: str, name: str, start_only: bool = False) -> str:
            params[name] = word
            after = "" if start_only else " || ' '"
            return f"contains({words}, ' ' || ${name}{after})"

        if found is not None:
            terms += [f"NOT {holds(w, f'_fx{i}')}" for i, w in enumerate(found.excluded)]
            terms += [holds(p, f"_fp{i}", start_only=True) for i, p in enumerate(found.prefixes)]
            terms += [f"NOT {holds(p, f'_fq{i}', start_only=True)}" for i, p in enumerate(found.excluded_prefixes)]
            for i, (phrase, wanted) in enumerate([(p, True) for p in found.phrases] + [(p, False) for p in found.unwanted]):
                params[f"_fh{i}"] = phrase
                any_field = " OR ".join(f"contains(lower(coalesce({quote(f)}, '')), $_fh{i})" for f in fields)
                terms.append(f"({any_field})" if wanted else f"NOT ({any_field})")
        must = found.words if found is not None else ()
        any_of = rank.rank if rank is not None else ()
        # The words a row must hold, every one of the search's and one of those it is ranked by: each as the index
        # matches them (all of them, or any) and as a row it does not hold is scanned for them.
        wanted = []
        if must:
            wanted.append((" ".join(must), 1, " AND ".join(holds(w, f"_fw{i}") for i, w in enumerate(must))))
        if any_of:
            wanted.append((" ".join(any_of), 0,
                           "(" + " OR ".join(holds(w, f"_fr{i}") for i, w in enumerate(any_of)) + ")"))
        ranking = (" ".join(any_of), 0) if any_of else ((" ".join(must), 1) if must else None)
        built = self._fresh_index(workspace, table)
        t = row_type(workspace, table)
        if built is None:
            # No index yet: every row is scanned, none scored.
            terms += [scanned for _, _, scanned in wanted]
            return f"(SELECT *, NULL::DOUBLE AS _score FROM {t})", terms, params
        index = fts_schema(workspace, table)
        # A row the index holds as it is now: its write number is one the index saw (``_held``).
        held = "(h._seq IS NOT NULL)"
        if rank is None and found is not None and found.prefixes:
            # BM25 scores words, not their starts: a word* scores as the index's words that start so, the commonest
            # first, any of them (with the search's words too).
            started = [r["term"] for p in found.prefixes for r in self._query(
                f"SELECT term FROM {index}.dict WHERE starts_with(term, $p) ORDER BY df DESC, term LIMIT {MAX_ANY_WORDS}",
                {"p": p})]
            if started:
                ranking = (" ".join([*must, *started]), 0)
        for i, (query, conjunctive, scanned) in enumerate(wanted):
            params[f"_fm{i}"] = query
            terms.append(f"(CASE WHEN _held THEN {index}.match_bm25(id, $_fm{i}, conjunctive := {conjunctive}) "
                         f"IS NOT NULL ELSE {scanned} END)")
        score = "NULL::DOUBLE"
        if ranking is not None:
            params["_fs"] = ranking[0]
            # A row the index holds scores 0 when it has none of the words it is ranked by (one a word* finds by a
            # word past those that rank it), so it comes after the others, not first with the unscored.
            score = (f"CASE WHEN {held} THEN coalesce({index}.match_bm25(r.id, $_fs, conjunctive := {ranking[1]}), 0) "
                     "END")
        return (f"(SELECT r.*, {held} AS _held, {score} AS _score FROM {t} AS r "
                f"LEFT JOIN {index}.{HELD} AS h ON h._seq = r._seq)"), terms, params

    # ------------------------------------------------------------------ reads
    def _filtered(self, workspace: str, table: str, filters: Filters, search: Optional[str],
                  rank: Optional[Search] = None) -> Tuple[Optional[str], str, Dict[str, Any]]:
        """What a list of rows reads from (None: the table) and its WHERE, with the parameters."""
        schema = self._schema(workspace, table)
        terms, params = self._where(filters, schema)
        source = None
        if search is not None or rank is not None:
            source, words, more = self.searching(workspace, table, search_query(search) if search is not None else None,
                                                 rank)
            terms, params = terms + words, {**params, **more}
        return source, (" WHERE " + " AND ".join(terms) if terms else ""), params

    def _count(self, workspace: str, table: str, filters: Filters = (), search: Optional[str] = None) -> int:
        if not self._has(workspace, table):
            return 0
        source, where, params = self._filtered(workspace, table, filters, search)
        rows = self._query(f"SELECT count(*) AS n FROM {source or row_type(workspace, table)}{where}", params)
        return rows[0]["n"] if rows else 0

    def _list(self, workspace: str, table: str, filters: Filters = (), sort: Sort = (), limit: int = 100,
              offset: int = 0, search: Optional[str] = None,
              expand: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """One page of rows the filters (and the search, the best match first unless sorted) select, each with what
        its ``expand`` paths reach (refs.py), and the cursor of the next page (None on the last)."""
        if not self._has(workspace, table):
            return [], None
        schema = self._schema(workspace, table)
        source, where, params = self._filtered(workspace, table, filters, search)
        if schema.vector is not None and any(f == schema.vector.name for f, _ in sort):
            raise TableError(f"{schema.vector.name} is a vector field: rows are not sorted by it")
        keys = [key for f, desc in sort if f != "id" for key in self._sort_keys(schema, f, desc)]
        if search is not None and not sort:
            keys.append("_score DESC NULLS FIRST")
        # id last breaks ties, so pages never overlap or skip a row.
        id_desc = next((desc for f, desc in sort if f == "id"), False)
        keys.append("id DESC" if id_desc else "id")
        params.update(_skip=offset, _limit=limit + 1)
        plan = self._plan(workspace, table, expand_tree(expand)) if expand else []
        rows = self._read_rows(workspace, table, f"{where} ORDER BY {', '.join(keys)} LIMIT $_limit OFFSET $_skip",
                               params, source=source)
        more = len(rows) > limit
        page = [ordered(r) for r in rows[:limit]]
        self._expand_rows(workspace, page, plan, [0])
        return page, (encode_cursor(offset + limit) if more else None)

    async def list(self, workspace: str, table: str, filters: Filters = (), sort: Sort = (), limit: int = 100,
                   offset: int = 0, search: Optional[str] = None,
                   expand: Optional[str] = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        """``_list``; one with a ``search`` is a long call (Database), since it may rebuild the table's index first."""
        return await self._run(functools.partial(self._list, workspace, table, filters, sort, limit, offset, search,
                                                 expand), long=search is not None)

    async def count(self, workspace: str, table: str, filters: Filters = (), search: Optional[str] = None) -> int:
        """``_count``; one with a ``search`` is a long call, as ``list``'s is."""
        return await self._run(functools.partial(self._count, workspace, table, filters, search), long=search is not None)

    def _get(self, workspace: str, table: str, row_id: str, db: Optional[Conn] = None) -> Optional[Dict[str, Any]]:
        """The row, whole: its vector too."""
        if not self._has(workspace, table):
            return None
        rows = self._query(f"SELECT * FROM {row_type(workspace, table)} WHERE id = $id", {"id": row_id}, db)
        return ordered(decoded(rows[0])) if rows else None

    def _get_expanded(self, workspace: str, table: str, row_id: str, expand: Optional[str]) -> Dict[str, Any]:
        plan = self._plan(workspace, table, expand_tree(expand)) if expand else []
        row = self._get(workspace, table, row_id)
        if row is None:
            raise NotFound(f"No row {row_id!r} in {workspace}/{table}")
        self._expand_rows(workspace, [row], plan, [0])
        return row

    async def get(self, workspace: str, table: str, row_id: str, expand: Optional[str] = None) -> Dict[str, Any]:
        """The row, whole, with what its ``expand`` paths reach (refs.py)."""
        return await self._run(self._get_expanded, workspace, table, row_id, expand)

    def _written_after(self, workspace: str, table: str, after: int, upto: Optional[int], limit: int
                       ) -> Tuple[List[Dict[str, Any]], int]:
        """Up to ``limit`` rows (without their vector) last written after write number ``after`` and, with ``upto``,
        no later than it, oldest write first, and the number of the last one (``after`` when none). A reader that
        keeps the number reads every row once, and again only once it is written again (a workflow's run)."""
        if not self._has(workspace, table):
            return [], after
        field = self._schema(workspace, table).vector
        leave_out = field.name if field is not None else None
        columns = f"* EXCLUDE ({quote(leave_out)})" if leave_out else "*"
        params: Dict[str, Any] = {"after": after, "limit": limit}
        until = ""
        if upto is not None:
            until, params["upto"] = " AND _seq <= $upto", upto
        rows = self._query(f"SELECT {columns} FROM {row_type(workspace, table)} WHERE _seq > $after{until} "
                           "ORDER BY _seq LIMIT $limit", params)
        return [ordered(decoded(r, leave_out)) for r in rows], (rows[-1]["_seq"] if rows else after)

    written_after = on_pool(_written_after)

    def _last_write(self, workspace: str, table: str) -> int:
        """The number of the table's newest write (0 for none, or no table), once no write to the table is under way.
        A write takes its rows' numbers as it starts and may commit after a later one: a reader that goes up to the
        later one's number and keeps it (a workflow's run) would pass the earlier rows for good. A store's own writes
        (``write_rows``) take no gate, so a kept table's rows can still come late."""
        if not self._has(workspace, table):
            return 0
        with self._gate(workspace, table).alone():
            rows = self._query(f"SELECT max(_seq) AS last FROM {row_type(workspace, table)}", {})
        return rows[0]["last"] or 0

    last_write = on_pool(_last_write)

    def _bulk_get(self, workspace: str, table: str, ids: List[str]) -> List[Dict[str, Any]]:
        """The rows with these ids, in the order asked, without their vector; missing ones are left out."""
        if not self._has(workspace, table) or not ids:
            return []
        rows = self._read_rows(workspace, table, " WHERE id = ANY($ids)", {"ids": list(ids)})
        by_id = {r["id"]: ordered(r) for r in rows}
        return [by_id[i] for i in dict.fromkeys(ids) if i in by_id]

    bulk_get = on_pool(_bulk_get)

    # ----------------------------------------------------------------- writes
    @retried
    def _insert(self, workspace: str, table: str, row: Dict[str, Any], *, owner: Optional[str] = None) -> Dict[str, Any]:
        """Add a new row; Conflict if its id (or a unique field's value) is taken."""
        self._check_owner(workspace, [table], owner)
        schema = self._schema(workspace, table) if self._has(workspace, table) else EMPTY
        check_row(row, schema)
        self._ensure(workspace, [table])
        with self._writing(workspace, [table]) as db:
            self._fits(workspace, {table: [row]}, {table: schema})
            if self._get(workspace, table, row["id"], db) is not None:
                raise Conflict(f"Row {row['id']!r} already exists in {workspace}/{table}; PUT replaces it")
            self._check_room(workspace, table, self._schema(workspace, table), [row["id"]], db)
            self._add(db, workspace, table, row)
            self._settle_refs(db, workspace, {table: [row["id"]]}, {})
        return ordered(row)

    async def insert(self, workspace: str, table: str, row: Dict[str, Any], *,
                     owner: Optional[str] = None) -> Dict[str, Any]:
        row = (await self._embedded(workspace, {table: [row]}, owner))[table][0]
        return await self._run(functools.partial(self._insert, workspace, table, row, owner=owner))

    @retried
    def _upsert(self, workspace: str, table: str, row: Dict[str, Any], *, owner: Optional[str] = None) -> bool:
        """Store ``row``, replacing any with its id. True when it was new."""
        self._check_owner(workspace, [table], owner)
        schema = self._schema(workspace, table) if self._has(workspace, table) else EMPTY
        check_row(row, schema)
        self._ensure(workspace, [table])
        with self._writing(workspace, [table]) as db:
            self._fits(workspace, {table: [row]}, {table: schema})
            created = self._get(workspace, table, row["id"], db) is None
            if created:
                self._check_room(workspace, table, self._schema(workspace, table), [row["id"]], db)
            self._put(db, workspace, table, [row])
            self._settle_refs(db, workspace, {table: [row["id"]]}, {})
        return created

    async def upsert(self, workspace: str, table: str, row: Dict[str, Any], *, owner: Optional[str] = None) -> bool:
        row = (await self._embedded(workspace, {table: [row]}, owner))[table][0]
        return await self._run(functools.partial(self._upsert, workspace, table, row, owner=owner))

    @retried
    def _patch(self, workspace: str, table: str, row_id: str, patch: Dict[str, Any], *,
               owner: Optional[str] = None) -> Dict[str, Any]:
        """Merge ``patch`` into the row (RFC 7396) and return the result; NotFound if there is none."""
        self._check_owner(workspace, [table], owner)
        if "id" in patch and patch["id"] != row_id:
            raise TableError("A patch cannot change a row's id")
        if not self._has(workspace, table):
            raise NotFound(f"No row {row_id!r} in {workspace}/{table}")
        with self._writing(workspace, [table]) as db:
            current = self._get(workspace, table, row_id, db)
            if current is None:
                raise NotFound(f"No row {row_id!r} in {workspace}/{table}")
            row = check_row({**merge_patch(current, patch), "id": row_id}, self._schema(workspace, table))
            self._put(db, workspace, table, [row])
            self._settle_refs(db, workspace, {table: [row_id]}, {})
        return ordered(row)

    async def patch(self, workspace: str, table: str, row_id: str, patch: Dict[str, Any], *,
                    owner: Optional[str] = None) -> Dict[str, Any]:
        """``_patch``; a patch that sets the text a vector field is embedded from gets that text's vector."""
        patch = (await self._embedded(workspace, {table: [patch]}, owner, patch=True))[table][0]
        return await self._run(functools.partial(self._patch, workspace, table, row_id, patch, owner=owner))

    @retried
    def _transaction(self, workspace: str, puts: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                     deletes: Optional[Dict[str, List[str]]] = None, edges: Sequence[Dict[str, Any]] = (),
                     delete_edges: Sequence[Dict[str, Any]] = (), *, owner: Optional[str] = None,
                     merge: bool = False) -> None:
        """Upsert ``puts``, store ``edges``, delete ``delete_edges`` and then ``deletes`` (table -> rows or ids),
        all at once or none of it. With ``merge``, each row put is merged over the one of its id (``_merged``)."""
        puts, deletes = puts or {}, deletes or {}
        tables = self._tables(workspace)
        checked = {table: tables.get(check_name("table", table), EMPTY) for table in puts}
        for table, rows in puts.items():
            for row in rows:
                # A row to merge is checked whole, once merged.
                check_row(row, EMPTY if merge else checked[table])
        for table in deletes:
            check_name("table", table)
        checked_edges = [check_edge(e) for e in edges]
        edge_refs = [check_edge_ref(e) for e in delete_edges]
        self._check_owner(workspace, [*puts, *deletes, *_ends(ref for ref, _ in checked_edges), *_ends(edge_refs)], owner)
        # Deleting from a table that does not exist yet deletes nothing: only puts need tables. The gates are those of
        # every table it writes, edges' ends among them, so a drop of one waits for it (ddl.py).
        ends = [t for ref, _ in checked_edges for t in (ref.from_table, ref.to_table)]
        with self._making(workspace, [t for t, rows in puts.items() if rows], [ref.type for ref, _ in checked_edges],
                          owner), self._writing(workspace, [*puts, *deletes, *ends]) as db:
            self._fits(workspace, puts, checked, merge)
            for table, rows in puts.items():
                if merge:
                    rows = self._merged(workspace, table, rows)
                self._check_room(workspace, table, tables.get(table, EMPTY), [row["id"] for row in rows], db)
                self._put(db, workspace, table, rows)
            # Edges after the rows they may join, and before the rows they may hang on go.
            if checked_edges or edge_refs:
                self._apply_edges(db, workspace, checked_edges, edge_refs)
            for table, ids in deletes.items():
                if ids and table in tables:
                    self._delete_ids(db, workspace, table, ids)
            self._settle_refs(db, workspace, {table: [row["id"] for row in rows] for table, rows in puts.items()},
                              {table: ids for table, ids in deletes.items() if table in tables})

    def _apply_edges(self, db: Conn, workspace: str, puts: List[Tuple[EdgeRef, Dict[str, Any]]],
                     deletes: List[EdgeRef]) -> None:
        """Store and delete edges within a transaction: GraphStore's (graph.py); a plain table store has none."""
        raise TableError("Edges need the graph store")

    async def transaction(self, workspace: str, puts: Optional[Dict[str, List[Dict[str, Any]]]] = None,
                          deletes: Optional[Dict[str, List[str]]] = None, edges: Sequence[Dict[str, Any]] = (),
                          delete_edges: Sequence[Dict[str, Any]] = (), *, owner: Optional[str] = None,
                          merge: bool = False) -> None:
        """``_transaction``, the rows' vectors embedded first, all of them in one batch per model, once the owner of
        every table and edge type it touches is known to be ``owner``. A row to ``merge`` is embedded as a patch is:
        only when it sets the text, else its vector stays as stored."""
        if puts:
            touched = [*(check_name("table", t) for t in deletes or {}),
                       *_ends(check_edge(e)[0] for e in edges), *_ends(check_edge_ref(e) for e in delete_edges)]
            puts = await self._embedded(workspace, puts, owner, patch=merge, touched=touched)
        await self._run(functools.partial(self._transaction, workspace, puts, deletes, edges, delete_edges,
                                          owner=owner, merge=merge))

    async def delete(self, workspace: str, table: str, ids: List[str], *, owner: Optional[str] = None) -> None:
        """Delete the rows with these ids; ids with no row are ignored."""
        await self.transaction(workspace, deletes={table: ids}, owner=owner)

    async def bulk_put(self, workspace: str, table: str, rows: List[Dict[str, Any]], *,
                       owner: Optional[str] = None) -> None:
        await self.transaction(workspace, puts={table: rows}, owner=owner)

    async def bulk_merge(self, workspace: str, table: str, rows: List[Dict[str, Any]], *,
                         owner: Optional[str] = None) -> None:
        """Each row merged over the one of its id, or added where there is none: its fields set, the row's others
        kept, its vector among them (a workflow's Table write, which has the rows without their vectors)."""
        await self.transaction(workspace, puts={table: rows}, owner=owner, merge=True)

    def _merged(self, workspace: str, table: str, rows: List[Row]) -> List[Row]:
        """``rows`` each over the stored row of its id (a later one of the same id over the earlier), checked whole
        against the table's schema. Within the write's transaction, on its thread's cursor."""
        if not self._has(workspace, table):
            raise NotFound(f"{workspace}/{table} was dropped while the write waited for it")
        stored = {r["id"]: r for r in self._read_rows(workspace, table, " WHERE id = ANY($ids)",
                                                      {"ids": [row["id"] for row in rows]}, vector=True)}
        schema, out = self._schema(workspace, table), []
        for row in rows:
            stored[row["id"]] = merged = {**stored.get(row["id"], {}), **row}
            out.append(check_row(merged, schema))
        return out
