"""
The embedded ArcadeDB database every store shares, and what stores build on.

ArcadeDB (arcadedb-embedded, which bundles its own Java runtime) runs inside
this process: no separate server. Its database lives in ARCADEDB_PATH and is
opened on first use, which starts the JVM (see start_jvm) and the database
(about two seconds together). One process can open it, so there is one
``Database``, made by api/app.py and handed to every store: the collections,
the /db tables, traces and evals, the evidence ledger, jobs, request metrics
and the enrichers' result cache.

Its calls block, and never run on the event loop. A store's async methods run
them on the database's own thread pool (``Database.run``; ``on_pool`` makes a
blocking method such a coroutine); a store's background writer (BatchWriter)
writes on a thread of its own.

A store declares its types (``DocumentStore.SCHEMA``), and on first use each
is made, or given what a database from an earlier version lacks: the declared
properties and indexes are added; nothing is dropped or changed. Changes to
data are migrations (``DocumentStore.MIGRATIONS``): each runs once per
database, in order, and the ids of those applied are kept in ``tk__migration``.
"""

import asyncio
import functools
import logging
import os
import queue
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import errors
from errors import ServiceError

logger = logging.getLogger(__name__)

# The migrations applied to this database, one row each (see DocumentStore.MIGRATIONS).
MIGRATIONS = "tk__migration"


class StoreError(ServiceError):
    """A request a store refuses; api/app.py answers it with ``status`` and the message (errors.py)."""

    status = 400


class Unavailable(StoreError, errors.Unavailable):
    """The database cannot be used here."""

    status = 503  # before StoreError's 400 in the bases' order


def _plain(value: Any) -> Any:
    """Java values ArcadeDB hands back, as JSON-ready Python ones."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _plain(v) for k, v in value.items()}
    try:
        return [_plain(v) for v in value]
    except TypeError:
        return str(value)


class Database:
    """The embedded ArcadeDB database in ``path``, opened on first use, and the thread pool its calls run on."""

    def __init__(self, path: str, heap: str = "1g", threads: int = 4):
        self.path = path
        self.heap = heap
        self.threads = threads
        self._db = None
        self._open_lock = threading.Lock()
        # ArcadeDB calls block; they run here, never on the event loop.
        self._executor: Optional[ThreadPoolExecutor] = None

    async def run(self, fn: Callable, *args: Any) -> Any:
        """Run the blocking ``fn(*args)`` on the database's thread pool."""
        try:
            self.start_jvm()
            if self._executor is None:
                self._executor = ThreadPoolExecutor(max_workers=self.threads, thread_name_prefix="arcadedb")
            return await asyncio.get_running_loop().run_in_executor(self._executor, fn, *args)
        except ImportError as e:
            raise Unavailable("The database needs arcadedb-embedded: pip install -r requirements.txt") from e

    def start_jvm(self) -> None:
        """Start ArcadeDB's JVM (once, about 0.2 s), on the main thread.

        A JVM started on another thread keeps the process from exiting, so
        this refuses to. Under uvicorn the event loop runs on the main
        thread, so the first request that needs the database starts it there;
        elsewhere (a test client's thread) call this from the main thread first.
        """
        import jpype

        if jpype.isJVMStarted():
            return
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError("ArcadeDB's JVM must be started on the main thread: call Database.start_jvm() there")
        from arcadedb_embedded.jvm import start_jvm

        start_jvm(heap_size=self.heap)

    def open(self):
        """The ArcadeDB database, opened (or made) on first use. Blocking."""
        with self._open_lock:
            if self._db is None:
                import arcadedb_embedded as arcade

                if arcade.database_exists(self.path):
                    try:
                        db = arcade.open_database(self.path)
                    except Exception as e:
                        # The lock is the OS's, so a process that died holds none: this one is alive.
                        if "locked by another process" not in str(e):
                            raise
                        raise Unavailable(
                            f"The database in {self.path} is open in another process (is the service already "
                            f"running? lsof {self.path}/database.lck shows which). Only one process can open it."
                        ) from None
                else:
                    db = arcade.create_database(self.path)
                    logger.info("Created ArcadeDB database at %s", self.path)
                if not any(True for _ in db.query("sql", "SELECT name FROM schema:types WHERE name = :n", {"n": MIGRATIONS})):
                    db.command("sql", f"CREATE DOCUMENT TYPE {MIGRATIONS}")
                    db.command("sql", f"CREATE PROPERTY {MIGRATIONS}.id STRING")
                    db.command("sql", f"CREATE INDEX ON {MIGRATIONS} (id) UNIQUE")
                self._db = db
            return self._db

    def has_type(self, name: str) -> bool:
        rows = self.open().query("sql", "SELECT name FROM schema:types WHERE name = :name", {"name": name})
        return any(True for _ in rows)

    def properties(self, type_name: str) -> Set[str]:
        """The names of the type's declared properties."""
        sql = "SELECT expand(properties) FROM schema:types WHERE name = :n"
        return {r["name"] for r in self.rows(self.open().query("sql", sql, {"n": type_name}))}

    def indexes(self, type_name: str) -> Set[Tuple[Tuple[str, ...], str]]:
        """(properties, index type) of the type's own indexes, not their per-bucket parts: ``(("a", "b"), "LSM_TREE")``."""
        found = set()
        for r in self.rows(self.open().query("sql", "SELECT FROM schema:indexes WHERE typeName = :t", {"t": type_name})):
            if r.get("associatedBucketId") is None:
                props = tuple(p.split(" by item")[0] for group in r.get("properties") or [] for p in group)
                found.add((props, r.get("indexType") or ""))
        return found

    @staticmethod
    def rows(result: Any) -> List[Dict[str, Any]]:
        """A query's or command's result as plain dicts, without ArcadeDB's ``@`` fields."""
        return [{k: _plain(v) for k, v in row.to_dict().items() if not k.startswith("@")} for row in result]

    def close(self) -> None:
        """Close the database (it reopens on next use)."""
        def close_db():
            with self._open_lock:
                if self._db is not None:
                    self._db.close()
                    self._db = None

        if self._executor is None:
            close_db()
            return
        self._executor.submit(close_db).result()
        self._executor.shutdown(wait=True)
        self._executor = None


def open_elsewhere(path: str) -> bool:
    """Whether another process has the database in ``path`` open: it holds the OS lock on database.lck. No JVM.

    Only for a process that has not opened the database itself: closing the probe would drop that process's
    own lock (POSIX locks belong to the process, and any close of the file releases them).
    """
    import fcntl

    try:
        lock_file = open(os.path.join(path, "database.lck"), "rb")
    except FileNotFoundError:
        return False
    with lock_file:
        try:
            fcntl.lockf(lock_file, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            return True
    return False


def retry_concurrent(work: Callable[[], Any], attempts: int = 8) -> Any:
    """Run ``work``, again after a concurrent change to the same record (ArcadeDB is optimistic) undid it."""
    for attempt in range(attempts):
        try:
            return work()
        except Exception as e:
            if "ConcurrentModification" not in str(e) or attempt == attempts - 1:
                raise
            # Spread the retries, so writers that clashed do not clash again in step.
            time.sleep(random.uniform(0.01, 0.03) * (attempt + 1))


def content(fields: Iterable[str]) -> str:
    """``{a: :a, b: :b}``: an INSERT's CONTENT naming each field as a parameter."""
    return "{" + ", ".join(f"{f}: :{f}" for f in fields) + "}"


def on_pool(method: Callable) -> Callable:
    """A store's blocking method, made a coroutine that runs it on the database's thread pool.

    ``get = on_pool(_get)`` keeps the blocking ``_get`` for the store's own blocking code to call.
    """
    @functools.wraps(method)
    async def run(self: "DocumentStore", *args: Any, **kwargs: Any) -> Any:
        return await self._run(functools.partial(method, self, *args, **kwargs))
    return run


class DocumentStore:
    """A store of its own document types in the shared database (collections, evals, jobs, the evidence ledger...).

    ``SCHEMA`` maps each type to its ``props`` (name -> ArcadeDB type), ``indexes`` ((props, unique) pairs; props
    is one name, or several comma-separated for a composite index) and optional ``fulltext`` props. On first use
    each type is made, or given the props and indexes it lacks. Subclasses extend ``_ensure_schema`` for more.

    ``MIGRATIONS`` are (id, method name) pairs: after the schema, each method not applied to this database yet
    runs with the database, in order, and its id is recorded. A migration that fails stops the ones after it
    and runs again on next use; it should be safe to run again.
    """

    SCHEMA: Dict[str, Dict[str, Any]] = {}
    MIGRATIONS: Sequence[Tuple[str, str]] = ()

    def __init__(self, database: Optional[Database]):
        self.database = database
        self._ready = False
        self._schema_lock = threading.Lock()

    async def _run(self, fn: Callable, *args: Any) -> Any:
        """``fn(*args)`` on the database's pool; inline for a store kept in memory (no database)."""
        if self.database is None:
            return fn(*args)
        return await self.database.run(fn, *args)

    def _db(self):
        db = self.database.open()
        if not self._ready:
            with self._schema_lock:
                if not self._ready:
                    self._ensure_schema(db)
                    self._migrate(db)
                    self._ready = True
        return db

    def _rows(self, db, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        return self.database.rows(db.query("sql", sql, params or {}))

    def _select(self, sql: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """The rows a query returns (the schema made sure of first). Blocking."""
        return self._rows(self._db(), sql, params)

    def _delete_older(self, type_name: str, column: str, cutoff: float) -> int:
        """Delete the type's rows whose ``column`` is below ``cutoff`` (a time); how many went. Blocking."""
        return self._command(f"DELETE FROM {type_name} WHERE {column} < :cutoff", {"cutoff": cutoff})

    def _ensure_schema(self, db) -> None:
        for type_name, spec in self.SCHEMA.items():
            made = not self.database.has_type(type_name)
            if made:
                db.command("sql", f"CREATE DOCUMENT TYPE {type_name}")
            props = set() if made else self.database.properties(type_name)
            for prop, kind in spec["props"].items():
                if prop not in props:
                    self._change(db, type_name, made, f"CREATE PROPERTY {type_name}.{prop} {kind}")
            # ArcadeDB keeps one index per set of props (it is named after them): one there is left as it is.
            indexed = set() if made else {p for p, _ in self.database.indexes(type_name)}
            wanted = [(p, "UNIQUE" if unique else "NOTUNIQUE") for p, unique in spec["indexes"]]
            wanted += [(p, "FULL_TEXT") for p in spec.get("fulltext", [])]
            for prop, kind in wanted:
                if tuple(p.strip() for p in prop.split(",")) not in indexed:
                    self._change(db, type_name, made, f"CREATE INDEX ON {type_name} ({prop}) {kind}")

    @staticmethod
    def _change(db, type_name: str, made: bool, statement: str) -> None:
        """A schema statement for a type made just now, or one an existing type lacks (logged, and a failure
        too: the store still works without the new index, say)."""
        if made:
            db.command("sql", statement)
            return
        try:
            db.command("sql", statement)
            logger.info("Schema of %s brought up to date: %s", type_name, statement)
        except Exception:
            logger.exception("Could not bring the schema of %s up to date: %s", type_name, statement)

    def _migrate(self, db) -> None:
        if not self.MIGRATIONS:
            return
        applied = {r["id"] for r in self._rows(db, f"SELECT id FROM {MIGRATIONS}")}
        for migration_id, method in self.MIGRATIONS:
            if migration_id in applied:
                continue
            started = time.perf_counter()
            getattr(self, method)(db)
            self._commit(db, lambda: self._insert(db, MIGRATIONS, {"id": migration_id, "applied": time.time()}))
            logger.info("Applied migration %s in %.2fs", migration_id, time.perf_counter() - started)

    def _commit(self, db, work) -> None:
        """Run ``work`` in a transaction, again after a concurrent change to the same record."""
        def once():
            with db.transaction():
                work()

        retry_concurrent(once)

    def _insert(self, db, type_name: str, doc: Dict[str, Any], bucket: Optional[str] = None) -> None:
        target = f"BUCKET:{bucket}" if bucket else type_name
        db.command("sql", f"INSERT INTO {target} CONTENT {content(doc)}", doc)

    def _command(self, sql: str, params: Optional[Dict[str, Any]] = None) -> int:
        """Run one write in its own transaction (again after a concurrent change); how many records it changed."""
        db = self._db()

        def once() -> int:
            with db.transaction():
                rows = self.database.rows(db.command("sql", sql, params or {}))
            return int(rows[0].get("count") or 0) if rows else 0

        return retry_concurrent(once)


_STOP = object()


class BatchWriter:
    """Writes queued items in batches on one background thread, so whoever records never waits.

    A full queue drops the item (counted in ``dropped``). The thread starts on
    first use, and again after ``close``. ArcadeDB's JVM starts on the main
    thread (see Database.start_jvm); until it is up, the thread waits.
    """

    def __init__(self, write: Callable[[List[Any]], None], name: str, max_queue: int = 5000, batch_size: int = 200):
        self.write = write
        self.name = name
        self.batch_size = batch_size
        self.dropped = 0
        self._queue: "queue.Queue[Any]" = queue.Queue(maxsize=max_queue)
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._closing = False

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
                if items and self._jvm_up():
                    self.write(items)
                elif items:
                    self.dropped += len(items)
            except Exception:
                logger.exception("%s could not write %d rows", self.name, len(items))
            finally:
                for _ in batch:
                    self._queue.task_done()

    def _jvm_up(self) -> bool:
        """Wait for the JVM; False if closing first (the batch is then dropped)."""
        import jpype

        while not jpype.isJVMStarted():
            if self._closing:
                return False
            time.sleep(0.5)
        return True

    def flush(self) -> None:
        """Block until everything put so far is written."""
        self._ensure()
        self._queue.join()

    def close(self, timeout: float = 10) -> None:
        """Write what is queued and stop the thread."""
        self._closing = True
        thread = self._thread
        if thread is not None and thread.is_alive():
            self._queue.put(_STOP)
            thread.join(timeout=timeout)
        self._closing = False
