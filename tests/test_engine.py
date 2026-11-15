"""The shared database's schema handling: declared tables brought up to date, and a close that waits for the work
on its pool."""

import asyncio
import shutil
import subprocess
import sys
import threading

import pytest


from db import DocumentStore  # noqa: E402
from db.tables import TableStore, schema_of  # noqa: E402


def test_the_database_closes_once_the_call_running_on_its_pool_is_done(own_db):
    """A call whose caller was cancelled (a loop stopped at shutdown) runs on, on its pool thread: the database
    closes after it, so its schema change is kept, never cut by the close."""
    started, gate = threading.Event(), threading.Event()

    def make_type():
        db = own_db.open()
        started.set()
        gate.wait(10)
        db.execute("CREATE TABLE main.tk_late (x INTEGER)")

    async def cancelled_on_the_pool():
        call = asyncio.ensure_future(own_db.run(make_type))
        assert await asyncio.to_thread(started.wait, 10)
        call.cancel()

    asyncio.run(cancelled_on_the_pool())
    closing = threading.Thread(target=own_db.close)
    closing.start()
    closing.join(0.3)
    assert closing.is_alive(), "closed beside the call still running"
    gate.set()
    closing.join(10)
    assert not closing.is_alive()
    # Opened again: the table the call made was saved.
    assert own_db.has_table("main", "tk_late")


def test_long_calls_never_take_the_threads_of_the_short_ones(own_db):
    """However many long calls (a view's SQL, a schema change) run, a row's read finds a thread at once."""
    import time

    def slow():
        time.sleep(0.5)

    def quick():
        return own_db.open().rows("SELECT 1 AS one")

    async def scenario():
        slow_calls = [asyncio.create_task(own_db.run(slow, long=True)) for _ in range(own_db.threads * 2)]
        await asyncio.sleep(0.05)
        started = time.monotonic()
        assert await own_db.run(quick) == [{"one": 1}]
        waited = time.monotonic() - started
        await asyncio.gather(*slow_calls)
        return waited

    assert asyncio.run(scenario()) < 0.3


def test_a_type_from_an_earlier_version_gets_the_declared_props_and_indexes(own_db):
    class Before(DocumentStore):
        SCHEMA = {"tk_thing": {"props": {"id": "VARCHAR"}, "indexes": [("id", True)]}}

    class After(DocumentStore):
        SCHEMA = {"tk_thing": {"props": {"id": "VARCHAR", "kind": "VARCHAR", "n": "BIGINT", "text": "VARCHAR"},
                               "indexes": [("id", True), ("kind", False), ("kind, n", False)]}}

    Before(own_db)._db()
    assert set(own_db.columns("main", "tk_thing")) == {"id"}
    After(own_db)._db()
    assert own_db.columns("main", "tk_thing") == {"id": "VARCHAR", "kind": "VARCHAR", "n": "BIGINT", "text": "VARCHAR"}
    assert {(i.columns, i.unique) for i in own_db.indexes("main", "tk_thing")} == {
        (("id",), True), (("kind",), False), (("kind", "n"), False)}
    # Up to date already: nothing is made twice.
    After(own_db)._db()
    assert len(own_db.indexes("main", "tk_thing")) == 3


def test_a_managed_table_gets_the_schema_declared_now(own_db):
    tables = TableStore(own_db)
    tables.manage("svc", "a test", {"things": schema_of("name:string")})
    tables.ensure_managed("svc")
    # A later version of the store declares more.
    later = TableStore(own_db)
    later.manage("svc", "a test", {"things": schema_of("name:string", "kind:string+")})
    later.ensure_managed("svc")
    assert later._schema("svc", "things").fields["kind"].indexed
    # Saved, so a store opened later reads it back.
    assert TableStore(own_db)._schema("svc", "things").fields["kind"].indexed
    assert (("kind",), False) in {(i.columns, i.unique) for i in own_db.indexes("svc", "things")}


def test_a_managed_workspace_is_made_when_the_workspaces_are_first_loaded(own_db):
    """/db lists the service's workspaces before their stores are first used."""
    tables = TableStore(own_db)
    tables.manage("svc", "a test", {"things": schema_of("name:string")})
    listed = {n["name"]: n for n in tables._listing(managed=True)[0]}
    assert "svc" not in {n["name"] for n in tables._listing()[0]}
    assert listed["svc"]["managed"] == "a test"
    assert [t["name"] for t in listed["svc"]["tables"]] == ["things"]


def test_a_managed_workspace_stays_managed_when_no_store_declares_it(own_db):
    """The database marks the workspace as managed, so a run that does not declare it (a store of an earlier version
    kept it, say) still keeps it from clients."""
    import asyncio

    from db.tables import UNDECLARED, Conflict

    tables = TableStore(own_db)
    tables.manage("svc", "a test", {"things": schema_of("name:string")})
    tables.ensure_managed("svc")
    later = TableStore(own_db)
    assert asyncio.run(later.owner("svc")) == UNDECLARED
    assert later._describe("svc")["managed"] == UNDECLARED
    assert asyncio.run(later.owner("other")) is None
    with pytest.raises(Conflict, match="kept by the service"):
        later._create_workspace("svc")


def test_a_store_does_not_take_over_a_workspace_made_through_db(own_db):
    """An admin's workspace of the name a store keeps is refused, not taken over: its rows stay the admin's."""
    import asyncio

    from db.tables import TableSchema

    tables = TableStore(own_db)
    tables._create_workspace("mine")
    asyncio.run(tables.upsert("mine", "notes", {"id": "n1", "text": "kept"}))
    tables.manage("mine", "a test", {"notes": TableSchema()})
    with pytest.raises(RuntimeError, match="made through /db"):
        tables.ensure_managed("mine")
    assert asyncio.run(tables.owner("mine")) == "a test"
    # The database does not mark it as kept: a run that does not declare it leaves it to the admin.
    assert asyncio.run(TableStore(own_db).owner("mine")) is None


def test_a_plain_table_store_refuses_edges(own_db):
    """Edges are the graph store's (db/graph.py); a TableStore says so instead of failing inside."""
    import asyncio

    from db.tables import TableError

    tables = TableStore(own_db)
    tables.manage("plain", "a test", {})
    tables.ensure_managed("plain")
    with pytest.raises(TableError, match="graph store"):
        asyncio.run(tables.transaction("plain", edges=[{"type": "knows", "from": "a/1", "to": "a/2"}], owner="a test"))


@pytest.mark.skipif(shutil.which("lsof") is None, reason="lsof tells which process has a file open")
def test_another_process_that_has_the_database_open_is_found_with_its_pid(tmp_path):
    """What main.py's banner says when the service is already running: that the file is open elsewhere, and by whom."""
    import main
    from db.engine import open_elsewhere

    path = str(tmp_path / "toolkit.duckdb")
    # The holder keeps its connection (one let go is closed, and its lock with it).
    holder = subprocess.Popen([sys.executable, "-c", f"import duckdb, time; c = duckdb.connect({path!r}); "
                               "c.execute('CREATE TABLE x (i INT)'); print(flush=True); time.sleep(30)"],
                              stdout=subprocess.PIPE, text=True)
    try:
        holder.stdout.readline()
        assert open_elsewhere(path)
        assert main._lock_holder(path) == str(holder.pid)
    finally:
        holder.kill()
        holder.wait()


def test_no_workspace_is_named_as_the_database(own_db):
    """DuckDB names the database after its file (toolkit.duckdb in DATA_DIR), and SQL cannot tell a schema of that
    name from it: a workspace of the name is refused."""
    from db.graph import GraphStore
    from db.schema import TableError

    store = GraphStore(own_db)
    for name in ("toolkit", "Toolkit"):
        with pytest.raises(TableError, match="keeps for itself"):
            store._create_workspace(name)


def test_a_spec_store_keeps_specs_by_scope_and_name(own_db):
    """Replacing a spec keeps when it was made and its extras; a list goes by the name each gives itself, a page at a
    time; a scope is forgotten whole."""
    import time as clock

    from db.schema import decode_cursor
    from db.specs import SpecStore

    class Things(SpecStore):
        TABLE = "tk_test_thing"
        EXTRA = {"seen": "BIGINT"}

    things = Things(own_db)
    first = things._save("w", "b", {"name": "beta"})
    assert things._save("w", "a", {"name": "alpha", "n": 1})["created"] > 0
    assert things._set("w", "b", seen=3) and not things._set("w", "nope", seen=1)
    clock.sleep(0.01)
    again = things._save("w", "b", {"name": "beta", "n": 2})
    assert again == {"name": "beta", "n": 2, "created": first["created"], "updated": again["updated"], "seen": 3}
    assert again["updated"] > first["created"]
    assert things._save("w", "c", {"name": "gamma"}, create=False) is None and things._find("w", "c") is None
    page, after = things._page("w", 1, 0)
    assert [s["name"] for s in page] == ["alpha"] and decode_cursor(after) == 1
    assert [s["name"] for s in things._page("w", 1, 1)[0]] == ["beta"] and things._page("w", 1, 1)[1] is None
    things._save("other", "a", {"name": "elsewhere"})
    assert things._remove("w", "a") and not things._remove("w", "a")
    assert things._forget("w") == 1 and things._all("w") == [] and len(things._all("other")) == 1


def test_saves_of_one_new_spec_at_once_leave_one(own_db):
    """Two saves of a name neither sees yet: the one that clashes runs again and replaces the other's."""
    from concurrent.futures import ThreadPoolExecutor

    from db.specs import SpecStore

    class Things(SpecStore):
        TABLE = "tk_test_race"

    things = Things(own_db)
    for round in range(20):
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(lambda n: things._save("w", f"n{round}", {"name": f"n{round}", "by": n}), range(4)))
    assert len(things._all("w")) == 20
