"""
The embedded DuckDB database every store shares, and what stores build on.

DuckDB runs inside this process: no server. Its database is one file in
DATA_DIR, opened on first use. One process can open it, so there is one
``Database``, made by api/app.py and handed to every store: the /db tables
(vector fields among them), traces and evals, the evidence ledger, jobs,
request metrics and the enrichers' result cache.

Its calls block, and never run on the event loop. A store's async methods run
them on the database's own thread pool (``Database.run``; ``on_pool`` makes a
blocking method such a coroutine); a store's background writer (BatchWriter)
writes on a thread of its own. Each thread works through a cursor of its own
(``Database.open`` gives it its ``Conn``), so a transaction is the thread's, as
it begins, commits or rolls back on that cursor.

At open, the database loads its full-text extension (``fts``; downloaded the
first time) and then turns off DuckDB's access to files outside the database,
so SQL an admin sends cannot read or write files, attach databases or install
extensions.

A store declares its tables (``DocumentStore.SCHEMA``), and on first use each
is made, or given what a database from an earlier version lacks: the declared
columns and indexes are added; nothing is dropped or changed. There are no
migrations of data: the data is test data, made again by a fresh database. A
store whose rows are /db tables declares those (``DocumentStore.TABLES``), in
a workspace it keeps; one that records in the background is a QueuedStore.
"""

import asyncio
import functools
import itertools
import json
import logging
import os
import queue
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Callable, Dict, Iterator, List, NamedTuple, Optional, Sequence, Tuple

import duckdb

from core.errors import Conflict, Unavailable

if TYPE_CHECKING:
    from .tables import TableStore

logger = logging.getLogger(__name__)

# The sequence every /db table's rows take their write number (_seq) from: the full-text index knows what it holds by
# the number it was built at (rows.py).
SEQUENCE = "main.tk_seq"
# A named parameter in a statement's text ($name). DuckDB refuses a value for a parameter the statement does not name,
# so a call passes only those it names.
PARAM = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")


class Index(NamedTuple):
    """One of a table's indexes."""
    name: str
    columns: Tuple[str, ...]
    unique: bool


def quote(name: str) -> str:
    """A name as a DuckDB identifier, quoted: names are checked before they reach here, but quoting keeps a word
    DuckDB reserves (default, order...) a name."""
    return '"' + name.replace('"', '""') + '"'


# A whole DOUBLE up to this is read back as an int (beyond, an int could not stand for it exactly).
EXACT = 2 ** 53


def _plain(value: Any, kind: str) -> Any:
    """A value DuckDB hands back as JSON-ready Python: a JSON column's text read, a fixed-size array (a vector) as a
    list, a whole DOUBLE as an int (3 written to a number column reads back 3, not 3.0, as JSON has one number)."""
    if value is None:
        return None
    if kind == "JSON":
        return json.loads(value)
    if isinstance(value, tuple):
        return list(value)
    if kind == "DOUBLE" and isinstance(value, float) and value.is_integer() and abs(value) < EXACT:
        return int(value)
    return value


class Conn:
    """A thread's cursor on the shared database: its statements, and its transaction."""

    def __init__(self, cursor: Any, generation: int):
        self.cursor = cursor
        self.generation = generation
        self._depth = 0

    def _run(self, sql: str, params: Optional[Dict[str, Any]]) -> Any:
        if params:
            named = set(PARAM.findall(sql))
            params = {k: v for k, v in params.items() if k in named}
        return self.cursor.execute(sql, params or None)

    def rows(self, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """The rows a query returns, as dicts: JSON columns read, vectors as lists."""
        result = self._run(sql, params)
        if result.description is None:
            return []
        columns = [(d[0], str(d[1])) for d in result.description]
        return [{name: _plain(value, kind) for (name, kind), value in zip(columns, row)} for row in result.fetchall()]

    def first(self, sql: str, params: Optional[Dict[str, Any]], n: int) -> List[Dict[str, Any]]:
        """The first ``n`` rows a query returns, as ``rows`` gives them; the rest are never read."""
        result = self._run(sql, params)
        if result.description is None:
            return []
        columns = [(d[0], str(d[1])) for d in result.description]
        return [{name: _plain(value, kind) for (name, kind), value in zip(columns, row)} for row in result.fetchmany(n)]

    def execute(self, sql: str, params: Optional[Dict[str, Any]] = None) -> int:
        """Run a statement; how many rows it changed (0 for one that changes no rows)."""
        result = self._run(sql, params)
        description = result.description
        if description and len(description) == 1 and description[0][0] == "Count":
            found = result.fetchall()
            return int(found[0][0]) if found else 0
        return 0

    @contextmanager
    def transaction(self, read_only: bool = False) -> Iterator["Conn"]:
        """A transaction on this thread's cursor: committed when the block ends, rolled back when it raises. One
        begun inside another is part of it. In a ``read_only`` one, DuckDB refuses any statement that writes,
        however the SQL hides it (a WITH that inserts is a SELECT to its parser); it is never part of another."""
        if self._depth:
            if read_only:
                raise RuntimeError("A read-only transaction cannot be part of another")
            self._depth += 1
            try:
                yield self
            finally:
                self._depth -= 1
            return
        self.cursor.execute("BEGIN TRANSACTION READ ONLY" if read_only else "BEGIN TRANSACTION")
        self._depth = 1
        try:
            yield self
        except BaseException:
            self._depth = 0
            self._rollback()
            raise
        self._depth = 0
        try:
            self.cursor.execute("COMMIT")
        except BaseException:
            self._rollback()
            raise

    def _rollback(self) -> None:
        # A transaction a failed statement aborted, or one DuckDB ended itself: either way it is over afterwards.
        try:
            self.cursor.execute("ROLLBACK")
        except duckdb.Error:
            pass


class Database:
    """The embedded DuckDB database in the file ``path``, opened on first use, and the thread pools its calls run on.

    DuckDB calls block; they run on ``threads`` threads, never on the event loop. Those that may take long (a view's or
    an admin's SQL, a schema change checking every row: ``long=True``) run on a pool of their own, of ``threads - 1``,
    so however many of them run, a row's read or write still finds a thread (four slow reads held every thread before,
    and a get by id waited seconds behind them)."""

    def __init__(self, path: str, memory: str = "1GB", threads: int = 4):
        self.path = path
        self.memory = memory
        self.threads = threads
        self._con: Optional[Any] = None
        self._generation = 0
        self._local = threading.local()
        self._open_lock = threading.Lock()
        self._pools: Dict[bool, ThreadPoolExecutor] = {}

    async def run(self, fn: Callable, *args: Any, long: bool = False) -> Any:
        """Run the blocking ``fn(*args)`` on the database's thread pool, the long calls' with ``long``."""
        pool = self._pools.get(long)
        if pool is None:
            size = max(1, self.threads - 1) if long else self.threads
            pool = self._pools[long] = ThreadPoolExecutor(max_workers=size, thread_name_prefix="duckdb-long" if long else "duckdb")
        return await asyncio.get_running_loop().run_in_executor(pool, fn, *args)

    def open(self) -> Conn:
        """This thread's cursor on the database, which is opened (or made) on first use. Blocking."""
        conn = getattr(self._local, "conn", None)
        if conn is not None and conn.generation == self._generation and self._con is not None:
            return conn
        with self._open_lock:
            if self._con is None:
                self._con = self._connect()
                self._generation += 1
            conn = self._local.conn = Conn(self._con.cursor(), self._generation)
        return conn

    def _connect(self) -> Any:
        folder = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(folder, exist_ok=True)
        made = not os.path.exists(self.path)
        try:
            con = duckdb.connect(self.path, config={"memory_limit": self.memory})
        except duckdb.IOException as e:
            if "lock" not in str(e).lower():
                raise
            raise Unavailable(f"The database {self.path} is open in another process (is the service already "
                              f"running? lsof {self.path} shows which). Only one process can open it.") from None
        try:
            con.execute("INSTALL fts")
            con.execute("LOAD fts")
        except duckdb.Error as e:
            con.close()
            raise Unavailable(f"The database needs DuckDB's full-text extension (fts), which could not be loaded: "
                              f"{e}. It is downloaded the first time; offline, install it once with network "
                              "(python -c \"import duckdb; duckdb.connect().execute('INSTALL fts')\")") from None
        # Last: once off, DuckDB's access to other files cannot be turned on again while the database is open (a folder
        # it should still reach, for backups say, is named before: SET allowed_directories).
        con.execute("SET enable_external_access = false")
        con.execute(f"CREATE SEQUENCE IF NOT EXISTS {SEQUENCE}")
        if made:
            logger.info("Created the DuckDB database at %s", self.path)
        return con

    def has_table(self, schema: str, name: str) -> bool:
        return bool(self.open().rows("SELECT 1 FROM duckdb_tables() WHERE schema_name = $s AND table_name = $t",
                                     {"s": schema, "t": name}))

    def has_schema(self, name: str) -> bool:
        return bool(self.open().rows("SELECT 1 FROM duckdb_schemas() WHERE schema_name = $s", {"s": name}))

    def columns(self, schema: str, table: str) -> Dict[str, str]:
        """The table's columns and their types, in order."""
        rows = self.open().rows("SELECT column_name AS c, data_type AS t FROM duckdb_columns() WHERE schema_name = $s "
                                "AND table_name = $t ORDER BY column_index", {"s": schema, "t": table})
        return {r["c"]: r["t"] for r in rows}

    def indexes(self, schema: str, table: str) -> List[Index]:
        """The table's indexes (made by CREATE INDEX; its primary key is not one of them)."""
        rows = self.open().rows("SELECT index_name AS n, expressions AS e, is_unique AS u FROM duckdb_indexes() "
                                "WHERE schema_name = $s AND table_name = $t", {"s": schema, "t": table})
        return [Index(r["n"], tuple(_index_columns(r["e"])), bool(r["u"])) for r in rows]

    def close(self) -> None:
        """Close the database (it reopens on next use), once the calls running on its pool have finished; those
        queued but not begun are dropped. Blocking."""
        pools, self._pools = self._pools, {}
        for pool in pools.values():
            pool.shutdown(wait=True, cancel_futures=True)
        with self._open_lock:
            if self._con is not None:
                self._con.close()
                self._con = None
                self._generation += 1


def _index_columns(expressions: Any) -> List[str]:
    """The columns of an index, from duckdb_indexes()' expressions: text such as ``[n, '"Name"']``."""
    if isinstance(expressions, str):
        expressions = [e.strip() for e in expressions.strip("[]").split(",") if e.strip()]
    return [str(e).strip("'").strip('"') for e in expressions or []]


def open_elsewhere(path: str) -> bool:
    """Whether another process has the database in ``path`` open: DuckDB then refuses this process a connection, even
    to read. Only for a process that has not opened the database itself."""
    if not os.path.exists(path):
        return False
    try:
        duckdb.connect(path, read_only=True).close()
    except duckdb.IOException as e:
        return "lock" in str(e).lower()
    except duckdb.Error:
        return False
    return False


def db_message(e: Exception) -> str:
    """DuckDB's own words in one of its errors, without its error class's name, cut to 400 characters: what a refusal
    tells the caller."""
    text = str(e)
    return re.sub(r"^[A-Za-z ]+ Error: ", "", text.split("\n\nLINE")[0]).strip()[:400]


class Clash(Conflict):
    """A write that stored a row (or an edge) whose key another write stored meanwhile, which it could not see: run
    again, it finds that one and replaces it (or says it exists). A 409 only when it clashes every time."""


def conflicted(e: BaseException) -> bool:
    """Whether a write failed because another transaction changed what it changed (DuckDB is optimistic), or stored
    a row of the same key first, which this one could not see, so running it again can succeed."""
    if isinstance(e, Clash):
        return True
    text = str(e)
    if isinstance(e, duckdb.TransactionException):
        return "onflict" in text or "constraint violation" in text
    return isinstance(e, duckdb.ConstraintException) and "violates primary key" in text


def retry_concurrent(work: Callable[[], Any], attempts: int = 8) -> Any:
    """Run ``work``, again after a concurrent change to the same rows undid it."""
    for attempt in range(attempts):
        try:
            return work()
        except Exception as e:
            if not conflicted(e) or attempt == attempts - 1:
                raise
            # Spread the retries, so writers that clashed do not clash again in step.
            time.sleep(random.uniform(0.01, 0.03) * (attempt + 1))


def retried(method: Callable) -> Callable:
    """A store's write run again, whole, after a concurrent change to the same rows: it re-reads what it needs."""
    @functools.wraps(method)
    def wrapper(self, *args: Any, **kwargs: Any) -> Any:
        return retry_concurrent(lambda: method(self, *args, **kwargs))
    return wrapper


class Gate:
    """A lock that many hold together (``shared``) or one holds alone (``alone``), which waits for those inside to
    leave and keeps new ones out meanwhile. A table's writes share its gate and a change of its schema has it alone
    (ddl.py), since that change copies the rows into a new table, and DuckDB would not tell it of a row committed
    meanwhile, which the copy misses. Not reentrant: a thread takes a gate once."""

    def __init__(self) -> None:
        self._turn = threading.Condition()
        self._inside = 0
        self._alone = False

    @contextmanager
    def shared(self) -> Iterator[None]:
        with self._turn:
            self._turn.wait_for(lambda: not self._alone)
            self._inside += 1
        try:
            yield
        finally:
            with self._turn:
                self._inside -= 1
                self._turn.notify_all()

    @contextmanager
    def alone(self) -> Iterator[None]:
        with self._turn:
            self._turn.wait_for(lambda: not self._alone)
            self._alone = True
            self._turn.wait_for(lambda: not self._inside)
        try:
            yield
        finally:
            with self._turn:
                self._alone = False
                self._turn.notify_all()


class Where:
    """A query's conditions, added one at a time, each value a parameter of its own (``$_w0``, ``$_w1``...). A value
    that is None or "" adds nothing, so each of a list's filters is one call: ``f"SELECT * FROM t{where} ..."``
    with ``where.params``."""

    def __init__(self, *conditions: str):
        self.terms: List[str] = list(conditions)
        self.params: Dict[str, Any] = {}

    def add(self, column: str, op: str, value: Any) -> "Where":
        """``column op value`` (``ts >= 1.5``), unless the value is None or ""."""
        if value is not None and value != "":
            self.terms.append(f"{column} {op} {self._param(value)}")
        return self

    def among(self, column: str, values: Optional[Sequence[Any]]) -> "Where":
        """``column`` is one of ``values``, unless there are none."""
        if values:
            self.terms.append(f"{column} = ANY({self._param(list(values))})")
        return self

    def contains(self, column: str, value: Any) -> "Where":
        """The list ``column`` holds ``value``, unless it is None or ""."""
        if value is not None and value != "":
            self.terms.append(f"list_contains({column}, {self._param(value)})")
        return self

    def like(self, columns: Sequence[str], text: Optional[str]) -> "Where":
        """One of ``columns`` holds ``text``, ignoring case: as written, since LIKE's wildcards in it (% for any
        characters, _ for one) are escaped."""
        if text:
            escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            param = self._param(f"%{escaped}%")
            self.terms.append("(" + " OR ".join(f"{c} ILIKE {param} ESCAPE '\\'" for c in columns) + ")")
        return self

    def _param(self, value: Any) -> str:
        name = f"_w{len(self.params)}"
        self.params[name] = value
        return f"${name}"

    def __str__(self) -> str:
        return f" WHERE {' AND '.join(self.terms)}" if self.terms else ""


def on_pool(method: Callable) -> Callable:
    """A store's blocking method, made a coroutine that runs it on the database's thread pool.

    ``get = on_pool(_get)`` keeps the blocking ``_get`` for the store's own blocking code to call.
    """
    @functools.wraps(method)
    async def run(self: "Store", *args: Any, **kwargs: Any) -> Any:
        return await self._run(functools.partial(method, self, *args, **kwargs))
    return run


def on_long_pool(method: Callable) -> Callable:
    """``on_pool`` for a call that may take long (a view's or an admin's SQL): on the long calls' pool (``Database``)."""
    @functools.wraps(method)
    async def run(self: "Store", *args: Any, **kwargs: Any) -> Any:
        return await self._run(functools.partial(method, self, *args, **kwargs), long=True)
    return run


class Store:
    """What every store builds on: the shared database, and its thread pool for the blocking calls."""

    def __init__(self, database: Database):
        self.database = database

    async def _run(self, fn: Callable, *args: Any, long: bool = False) -> Any:
        """``fn(*args)`` on the database's pool (the long calls' with ``long``)."""
        return await self.database.run(fn, *args, long=long)

    def _db(self) -> Conn:
        return self.database.open()

    @staticmethod
    def _rows(db: Conn, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        return db.rows(sql, params)

    def _select(self, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """The rows a query returns. Blocking."""
        return self._db().rows(sql, params)

    def _one(self, sql: str, params: Optional[Dict[str, Any]] = None) -> Optional[Dict[str, Any]]:
        """The first row a query returns, or None. Blocking."""
        rows = self._select(sql, params)
        return rows[0] if rows else None

    def _commit(self, db: Conn, work: Callable[[], Any]) -> Any:
        """Run ``work`` in a transaction, again after a concurrent change to the same rows; what it returns. What it
        changes from what it reads, it reads in ``work``, so a run again reads what the other change wrote."""
        def once():
            with db.transaction():
                return work()

        return retry_concurrent(once)

    def _command(self, sql: str, params: Optional[Dict[str, Any]] = None) -> int:
        """Run one write in its own transaction (again after a concurrent change); how many rows it changed."""
        db = self._db()

        def once() -> int:
            with db.transaction():
                return db.execute(sql, params)

        return retry_concurrent(once)


class DocumentStore(Store):
    """A store of its own tables in the shared database (evals, jobs, the evidence ledger, monitoring...).

    ``SCHEMA`` maps each table (in DuckDB's ``main`` schema, a ``tk_`` name) to its ``props`` (column -> DuckDB type:
    VARCHAR, BIGINT, DOUBLE, BOOLEAN, JSON, VARCHAR[]...) and ``indexes`` ((columns, unique) pairs; one column, or
    several comma-separated for a composite index). On first use each table is made, or given the columns and indexes
    it lacks. Subclasses extend ``_ensure_schema`` for more. A row written (``_insert``) holds those columns only; a
    JSON column takes any JSON value.

    ``TABLES`` are /db tables (db/tables.py) the store keeps its rows in, in the workspace ``WORKSPACE``
    (or the one it is made for), which /db shows the admin read-only as kept by ``OWNER``: declared to the
    TableStore when the store is made, and made, or brought up to date, on first use, like ``SCHEMA``'s tables.
    A workspace of that name an admin made through /db is refused, not taken over.
    """

    SCHEMA: Dict[str, Dict[str, Any]] = {}
    WORKSPACE = ""
    OWNER = ""
    TABLES: Dict[str, Any] = {}

    def __init__(self, database: Database, tables: Optional["TableStore"] = None,
                 workspace: Optional[str] = None):
        super().__init__(database)
        self._ready = False
        self._schema_lock = threading.Lock()
        self._t = tables
        self.workspace = workspace or self.WORKSPACE
        if tables is not None and self.TABLES:
            tables.manage(self.workspace, self.OWNER, self.TABLES)

    def _db(self) -> Conn:
        db = self.database.open()
        if not self._ready:
            with self._schema_lock:
                if not self._ready:
                    self._ensure_schema(db)
                    self._ready = True
        return db

    def _delete_older(self, table: str, column: str, cutoff: float) -> int:
        """Delete the table's rows whose ``column`` is below ``cutoff`` (a time); how many went. Blocking."""
        return self._command(f"DELETE FROM {table} WHERE {column} < $cutoff", {"cutoff": cutoff})

    def _ensure_schema(self, db: Conn) -> None:
        for table, spec in self.SCHEMA.items():
            columns = self.database.columns("main", table)
            made = not columns
            if made:
                cols = ", ".join(f"{quote(c)} {kind}" for c, kind in spec["props"].items())
                db.execute(f"CREATE TABLE IF NOT EXISTS main.{table} ({cols})")
            else:
                for column, kind in spec["props"].items():
                    if column not in columns:
                        self._change(table, f"ALTER TABLE main.{table} ADD COLUMN {quote(column)} {kind}")
            indexed = {i.columns for i in self.database.indexes("main", table)}
            for cols, unique in spec.get("indexes", []):
                names = tuple(c.strip() for c in cols.split(","))
                if names not in indexed:
                    name = f"{table}_{'_'.join(names)}"
                    statement = (f"CREATE {'UNIQUE ' if unique else ''}INDEX IF NOT EXISTS {quote(name)} "
                                 f"ON main.{table} ({', '.join(quote(c) for c in names)})")
                    if made:
                        db.execute(statement)
                    else:
                        self._change(table, statement)
        if self._t is not None and self.TABLES:
            self._t.ensure_managed(self.workspace)

    def _change(self, table: str, statement: str) -> None:
        """A schema statement a table made earlier lacks (logged, and a failure too: the store still works without
        the new index, say)."""
        try:
            self.database.open().execute(statement)
            logger.info("Schema of %s brought up to date: %s", table, statement)
        except duckdb.Error:
            logger.exception("Could not bring the schema of %s up to date: %s", table, statement)

    def _values(self, table: str, doc: Dict[str, Any]) -> Dict[str, Any]:
        """``doc`` as the table's columns take it: JSON columns' values as their JSON text. ValueError for a key that
        is not a column (a store's own mistake)."""
        props = self.SCHEMA[table]["props"]
        extra = [k for k in doc if k not in props]
        if extra:
            raise ValueError(f"{table} has no column {', '.join(extra)}: declare it in {type(self).__name__}.SCHEMA")
        return {k: (json.dumps(v) if props[k] == "JSON" and v is not None else v) for k, v in doc.items()}

    def _insert(self, db: Conn, table: str, doc: Dict[str, Any]) -> None:
        values = self._values(table, doc)
        names = list(values)
        db.execute(f"INSERT INTO main.{table} ({', '.join(quote(n) for n in names)}) "
                   f"VALUES ({', '.join(f'$p{i}' for i in range(len(names)))})",
                   {f"p{i}": values[n] for i, n in enumerate(names)})

    def _insert_many(self, db: Conn, table: str, docs: Sequence[Dict[str, Any]]) -> None:
        """Insert rows in one statement: each column's values as a list, unnested together."""
        if not docs:
            return
        props = self.SCHEMA[table]["props"]
        rows = [self._values(table, d) for d in docs]
        names = [c for c in props if any(c in r for r in rows)]
        lists = {f"p{i}": [r.get(n) for r in rows] for i, n in enumerate(names)}
        # A JSON column's values go in as text and are read as JSON; the others as their own type.
        cast = lambda n: "VARCHAR" if props[n] == "JSON" else props[n]  # noqa: E731
        select = ", ".join(f"unnest($p{i}::{cast(n)}[])" for i, n in enumerate(names))
        db.execute(f"INSERT INTO main.{table} ({', '.join(quote(n) for n in names)}) SELECT {select}", lists)


_STOP = object()


class BatchWriter:
    """Writes queued items in batches on one background thread, so whoever records never waits.

    A full queue drops the item (counted in ``dropped``). A batch the database
    refuses is written again an item at a time, so a row it cannot hold costs
    only itself. The thread starts on first use, and again after ``close``.
    """

    def __init__(self, write: Callable[[List[Any]], None], name: str, max_queue: int = 5000, batch_size: int = 200):
        self.write = write
        self.name = name
        self.batch_size = batch_size
        self.dropped = 0
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=max_queue)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

    def put(self, item: Any) -> None:
        self._ensure()
        try:
            self._queue.put_nowait(item)
        except queue.Full:
            self.dropped += 1

    def _ensure(self) -> None:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._loop, name=self.name, daemon=True)
                self._thread.start()

    def _loop(self) -> None:
        stopping = False
        while not stopping:
            batch = [self._queue.get()]
            while len(batch) < self.batch_size:
                try:
                    batch.append(self._queue.get_nowait())
                except queue.Empty:
                    break
            stopping = any(item is _STOP for item in batch)
            items = [item for item in batch if item is not _STOP]
            try:
                if items:
                    self._write(items)
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _write(self, items: List[Any]) -> None:
        try:
            self.write(items)
            return
        except Exception:
            if len(items) == 1:
                logger.exception("%s could not write a row", self.name)
                return
        # One at a time: what the database refuses is lost alone, said once for the batch.
        lost: List[BaseException] = []
        for item in items:
            try:
                self.write([item])
            except Exception as e:
                lost.append(e)
        if lost:
            logger.error("%s could not write %d of %d rows: %s", self.name, len(lost), len(items), lost[0],
                         exc_info=lost[0])

    def flush(self) -> None:
        """Block until everything put so far is written."""
        self._ensure()
        self._queue.join()

    def close(self, timeout: float = 10) -> None:
        """Write what is queued and stop the thread."""
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._queue.put(_STOP)
            thread.join(timeout=timeout)


class QueuedStore(DocumentStore):
    """A store that records in the background: rows go onto a queue its BatchWriter writes, so whoever records
    never waits. ``_write`` gets the queued items, (table, row) pairs unless a store says otherwise; ``prune``
    deletes the rows of each ``RETAINED`` (table, time column) older than ``retention_days``."""

    RETAINED: Sequence[Tuple[str, str]] = ()

    def __init__(self, database: Database, writer: str, retention_days: float, max_queue: int,
                 batch_size: int = 200, tables: Optional["TableStore"] = None):
        super().__init__(database, tables)
        self.retention_days = retention_days
        self.writer = BatchWriter(self._write, writer, max_queue, batch_size)
        # Ids that increase in the order rows are recorded, for the stores whose lists page by them.
        self._ids = itertools.count(time.time_ns() // 1000)
        self._id_lock = threading.Lock()

    @property
    def dropped(self) -> int:
        """Rows not kept because the queue was full."""
        return self.writer.dropped

    def _next_id(self) -> int:
        with self._id_lock:
            return next(self._ids)

    def _write(self, items: List[Any]) -> None:
        db = self._db()
        by_table: Dict[str, List[Dict[str, Any]]] = {}
        for table, doc in items:
            by_table.setdefault(table, []).append(doc)
        self._commit(db, lambda: [self._insert_many(db, table, docs) for table, docs in by_table.items()])

    def _prune(self) -> Any:
        cutoff = time.time() - self.retention_days * 86400
        return sum(self._delete_older(table, column, cutoff) for table, column in self.RETAINED)

    async def prune(self) -> Any:
        """Delete what is older than the retention (api/app.py runs this hourly); how many went."""
        return await self._run(self._prune)

    def flush(self) -> None:
        """Block until everything recorded so far is written."""
        self.writer.flush()

    def close(self) -> None:
        """Write what is queued and stop the writer. Recording again restarts it."""
        self.writer.close()
