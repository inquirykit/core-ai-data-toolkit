"""The shared database's schema handling: declared types brought up to date, and migrations run once."""

import pytest

pytest.importorskip("arcadedb_embedded")

from vectorstore import DocumentStore  # noqa: E402
from vectorstore.engine import MIGRATIONS  # noqa: E402
from vectorstore.tables import TableStore, schema_of  # noqa: E402


def _type_indexes(database, type_name):
    rows = database.rows(database.open().query("sql", "SELECT FROM schema:indexes WHERE typeName = :t", {"t": type_name}))
    return [r for r in rows if r.get("associatedBucketId") is None]


def test_a_type_from_an_earlier_version_gets_the_declared_props_and_indexes(arcade):
    class Before(DocumentStore):
        SCHEMA = {"tk_thing": {"props": {"id": "STRING"}, "indexes": [("id", True)]}}

    class After(DocumentStore):
        SCHEMA = {"tk_thing": {"props": {"id": "STRING", "kind": "STRING", "n": "LONG", "text": "STRING"},
                               "indexes": [("id", True), ("kind", False), ("kind, n", False)], "fulltext": ["text"]}}

    Before(arcade)._db()
    assert arcade.properties("tk_thing") == {"id"}
    After(arcade)._db()
    assert arcade.properties("tk_thing") == {"id", "kind", "n", "text"}
    assert arcade.indexes("tk_thing") == {(("id",), "LSM_TREE"), (("kind",), "LSM_TREE"), (("kind", "n"), "LSM_TREE"),
                                          (("text",), "FULL_TEXT")}
    # Up to date already: nothing is made twice.
    After(arcade)._db()
    assert len(_type_indexes(arcade, "tk_thing")) == 4


def test_migrations_run_once_each_in_order(arcade):
    calls = []

    class Store(DocumentStore):
        MIGRATIONS = (("test/1", "_one"), ("test/2", "_two"))

        def _one(self, db):
            calls.append(1)

        def _two(self, db):
            calls.append(2)

    class Later(Store):
        MIGRATIONS = Store.MIGRATIONS + (("test/3", "_three"),)

        def _three(self, db):
            calls.append(3)

    Store(arcade)._db()
    Store(arcade)._db()
    assert calls == [1, 2]
    Later(arcade)._db()
    assert calls == [1, 2, 3]
    applied = [r["id"] for r in arcade.rows(arcade.open().query("sql", f"SELECT id FROM {MIGRATIONS} ORDER BY applied"))]
    assert applied == ["test/1", "test/2", "test/3"]


def test_a_failed_migration_runs_again(arcade):
    attempts = []

    class Store(DocumentStore):
        MIGRATIONS = (("test/flaky", "_flaky"),)

        def _flaky(self, db):
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("not yet")

    store = Store(arcade)
    with pytest.raises(RuntimeError):
        store._db()
    store._db()
    store._db()
    assert len(attempts) == 2


def test_a_managed_table_gets_the_schema_declared_now(arcade):
    tables = TableStore(arcade)
    tables.manage("svc", "a test", {"things": schema_of("name:string")})
    tables.ensure_managed("svc")
    # A later version of the store declares more.
    later = TableStore(arcade)
    later.manage("svc", "a test", {"things": schema_of("name:string", "kind:string+")})
    later.ensure_managed("svc")
    assert later._schema("svc", "things").fields["kind"].indexed
    # Saved, so a store opened later reads it back.
    assert TableStore(arcade)._schema("svc", "things").fields["kind"].indexed
    assert (("kind",), "LSM_TREE") in arcade.indexes("ns_svc__things")


def test_a_kept_namespace_is_made_when_the_namespaces_are_first_loaded(arcade):
    """/db lists the service's namespaces before their stores are first used."""
    tables = TableStore(arcade)
    tables.manage("svc", "a test", {"things": schema_of("name:string")})
    listed = {n["name"]: n for n in tables._namespaces()}
    assert listed["svc"]["managed"] == "a test"
    assert [t["name"] for t in listed["svc"]["tables"]] == ["things"]


def test_a_kept_namespace_stays_kept_when_its_store_is_switched_off(arcade):
    """The database marks the namespace as kept, so a run that does not declare it (EVIDENCE_DIR left empty, say)
    still keeps it from clients."""
    import asyncio

    from vectorstore.tables import SWITCHED_OFF, Conflict

    tables = TableStore(arcade)
    tables.manage("svc", "a test", {"things": schema_of("name:string")})
    tables.ensure_managed("svc")
    later = TableStore(arcade)
    assert asyncio.run(later.owner("svc")) == SWITCHED_OFF
    assert later._describe("svc")["managed"] == SWITCHED_OFF
    assert asyncio.run(later.owner("other")) is None
    with pytest.raises(Conflict, match="kept by the service"):
        later._create_namespace("svc")


def test_a_store_takes_over_a_namespace_made_through_db_only_when_it_says_so(arcade):
    """An admin's namespace is not taken over; the playground's, which it made through /db until 2026-10, is."""
    import asyncio

    from vectorstore.tables import SWITCHED_OFF, TableSchema

    tables = TableStore(arcade)
    tables._create_namespace("mine")
    asyncio.run(tables.upsert("mine", "notes", {"id": "n1", "text": "kept"}))
    tables.manage("mine", "a test", {"notes": TableSchema()})
    with pytest.raises(RuntimeError, match="made through /db"):
        tables.ensure_managed("mine")
    assert asyncio.run(tables.owner("mine")) == "a test"
    # The store's own check refused it, but a run that adopts it marks it as kept, rows and all.
    adopting = TableStore(arcade)
    adopting.manage("mine", "a test", {"notes": TableSchema()}, adopt=True)
    adopting.ensure_managed("mine")
    assert asyncio.run(TableStore(arcade).owner("mine")) == SWITCHED_OFF
    assert asyncio.run(adopting.get("mine", "notes", "n1"))["text"] == "kept"


def test_a_plain_table_store_refuses_edges(arcade):
    """Edges are the graph store's (vectorstore/graph.py); a TableStore says so instead of failing inside."""
    import asyncio

    from vectorstore.tables import TableError

    tables = TableStore(arcade)
    tables.manage("plain", "a test", {})
    tables.ensure_managed("plain")
    with pytest.raises(TableError, match="graph store"):
        asyncio.run(tables.transaction("plain", edges=[{"type": "knows", "from": "a/1", "to": "a/2"}]))
