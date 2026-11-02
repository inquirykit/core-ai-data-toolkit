import pytest
from fastapi.testclient import TestClient

from db.tables import decode_cursor, encode_cursor, merge_patch

pytest.importorskip("arcadedb_embedded")

from api import app as app_module  # noqa: E402
from core.config import Config  # noqa: E402


@pytest.fixture(scope="module")
def client():
    # The JVM must start on the main thread (see Database.start_jvm).
    app_module.app.state.database.start_jvm()
    with TestClient(app_module.app) as c:
        yield c


ROWS = [
    {"id": "1", "title": "milk", "tags": ["shop", "food"], "n": 3, "done": False},
    {"id": "2", "title": "bread", "tags": ["shop"], "n": 30, "done": True},
    {"id": "3", "title": "taxes", "tags": ["admin"], "n": 7, "done": False, "code": "30"},
]


@pytest.fixture(scope="module")
def app_ns(client):
    assert client.post("/db", json={"name": "app"}).status_code == 201
    assert client.put("/db/app/todo/_bulk", json={"rows": ROWS}).status_code == 204
    return "app"


def without_sizes(namespace):
    """The namespace's listing without the sizes on disk, which depend on ArcadeDB's page sizes."""
    tables = [{k: v for k, v in t.items() if k != "size_bytes"} for t in namespace["tables"]]
    return {**{k: v for k, v in namespace.items() if k != "size_bytes"}, "tables": tables}


def ids(r):
    assert r.status_code == 200, r.text
    return [row["id"] for row in r.json()["rows"]]


def test_namespaces_are_listed_with_their_tables(client, app_ns):
    assert client.post("/db", json={"name": "app"}).status_code == 409
    assert client.post("/db", json={"name": "bad__name"}).status_code == 400
    assert client.post("/db", json={"name": "_x"}).status_code == 400
    listed = {n["name"]: n for n in client.get("/db").json()["namespaces"]}
    # Made by its first write, it is a graph table, as new tables are.
    todo = {"name": "todo", "count": 3, "strict": False, "graph": True, "fields": []}
    assert without_sizes(listed["app"])["tables"] == [todo]
    app = client.get("/db/app").json()
    assert without_sizes(app) == {"name": "app", "tables": [todo], "edges": [], "managed": None}
    # Its files on disk: the bucket and the id index, at least a page each.
    assert app["tables"][0]["size_bytes"] >= 65536 + 262144
    assert app["size_bytes"] == app["tables"][0]["size_bytes"]
    assert client.get("/db/nope").status_code == 404
    assert client.get("/db/nope/todo").status_code == 404
    assert client.post("/db/nope/todo", json={"id": "x"}).status_code == 404


def test_list_filters_sorts_and_pages(client, app_ns):
    assert ids(client.get("/db/app/todo")) == ["1", "2", "3"]
    assert ids(client.get("/db/app/todo?tags=shop")) == ["1", "2"]
    assert ids(client.get("/db/app/todo?tags=shop&done=true")) == ["2"]
    assert ids(client.get("/db/app/todo?n=30")) == ["2"]
    assert ids(client.get('/db/app/todo?code="30"')) == ["3"]
    assert ids(client.get("/db/app/todo?code=null")) == ["1", "2"]
    assert ids(client.get("/db/app/todo?_sort=-n")) == ["2", "3", "1"]
    first = client.get("/db/app/todo?_limit=2&_sort=title").json()
    assert [r["id"] for r in first["rows"]] == ["2", "1"]
    second = client.get(f"/db/app/todo?_limit=2&_sort=title&_cursor={first['next']}").json()
    assert [r["id"] for r in second["rows"]] == ["3"] and "next" not in second
    assert client.get("/db/app/todo/_count?tags=shop").json() == {"count": 2}
    assert ids(client.get("/db/app/missing")) == []
    assert client.get("/db/app/missing/_count").json() == {"count": 0}


@pytest.mark.parametrize("query", ["_bogus=1", "_cursor=nope", "_sort=a`b", "bad`field=1", "_limit=0"])
def test_bad_list_parameters_are_refused(client, app_ns, query):
    assert client.get(f"/db/app/todo?{query}").status_code in (400, 422)
    assert client.get("/db/app/todo/_count?_limit=1").status_code == 400


def test_rows_keep_their_json(client, app_ns):
    row = {"id": "j", "nested": {"a": [1, {"b": None}], "s": "x"}, "f": 1.5, "empty": []}
    assert client.put("/db/app/json/j", json=row).json() == row
    assert client.get("/db/app/json/j").json() == row


def test_insert_put_patch_delete(client, app_ns):
    r = client.post("/db/app/notes", json={"text": "hi"})
    assert r.status_code == 201
    made = r.json()
    assert made["text"] == "hi" and r.headers["location"].endswith(f"/db/app/notes/{made['id']}")
    assert client.post("/db/app/notes", json={"id": made["id"], "text": "again"}).status_code == 409

    r = client.put("/db/app/notes/n1", json={"text": "one", "meta": {"a": 1, "b": 2}})
    assert r.status_code == 201 and r.json() == {"id": "n1", "text": "one", "meta": {"a": 1, "b": 2}}
    r = client.put("/db/app/notes/n1", json={"id": "n1", "text": "uno", "meta": {"a": 1, "b": 2}})
    assert r.status_code == 200
    assert client.put("/db/app/notes/n1", json={"id": "other"}).status_code == 400

    r = client.patch("/db/app/notes/n1", json={"meta": {"b": None, "c": 3}, "pinned": True},
                     headers={"Content-Type": "application/merge-patch+json"})
    assert r.status_code == 200, r.text
    assert r.json() == {"id": "n1", "text": "uno", "meta": {"a": 1, "c": 3}, "pinned": True}
    assert client.get("/db/app/notes/n1").json() == r.json()
    assert client.patch("/db/app/notes/missing", json={"x": 1}).status_code == 404
    assert client.patch("/db/app/notes/n1", json={"id": "n2"}).status_code == 400

    assert client.delete("/db/app/notes/n1").status_code == 204
    assert client.delete("/db/app/notes/n1").status_code == 204
    assert client.get("/db/app/notes/n1").status_code == 404
    assert client.get("/db/app/notes/_x").status_code == 400


def test_made_ids_are_uuid7_in_creation_order(client, app_ns):
    import uuid

    made = [client.post("/db/app/log", json={"i": i}).json()["id"] for i in range(20)]
    assert all(len(i) == 32 and uuid.UUID(i).version == 7 for i in made)
    assert ids(client.get("/db/app/log")) == made


def test_made_ids_are_uuid7_before_python_3_14_too(client, app_ns, monkeypatch):
    """uuid.uuid7 is new in Python 3.14; the service runs on 3.11 and makes them itself there."""
    import time
    import uuid

    monkeypatch.delattr(uuid, "uuid7", raising=False)
    made = []
    for i in range(3):
        made.append(client.post("/db/app/log_old", json={"i": i}).json()["id"])
        time.sleep(0.002)
    assert all(len(i) == 32 and uuid.UUID(i).version == 7 and uuid.UUID(i).variant == uuid.RFC_4122 for i in made)
    assert ids(client.get("/db/app/log_old")) == made


def test_bulk_routes(client, app_ns):
    rows = [{"id": f"b{i}", "i": i} for i in range(5)]
    assert client.put("/db/app/bulk/_bulk", json={"rows": rows}).status_code == 204
    r = client.post("/db/app/bulk/_bulk-get", json={"ids": ["b3", "zz", "b0", "b3"]})
    assert ids(r) == ["b3", "b0"]
    assert client.post("/db/app/bulk/_bulk-delete", json={"ids": ["b0", "b1", "zz"]}).status_code == 204
    assert client.get("/db/app/bulk/_count").json() == {"count": 3}
    assert client.put("/db/app/bulk/_bulk", json={"rows": [{"i": 1}]}).status_code == 400


def test_transaction_is_all_or_nothing(client, app_ns):
    body = {"puts": {"a": [{"id": "1"}], "b": [{"id": "1"}, {"id": "2"}]}, "deletes": {"todo": ["3"], "never": ["x"]}}
    assert client.post("/db/app/_transaction", json=body).status_code == 204
    assert client.get("/db/app/b/_count").json() == {"count": 2}
    assert client.get("/db/app/todo/3").status_code == 404
    client.put("/db/app/todo/3", json=ROWS[2])

    # A bad row anywhere refuses the whole transaction before anything is written.
    bad = {"puts": {"a": [{"id": "2"}], "b": [{"nope": 1}]}, "deletes": {"todo": ["1"]}}
    assert client.post("/db/app/_transaction", json=bad).status_code == 400
    assert client.get("/db/app/a/2").status_code == 404
    assert client.get("/db/app/todo/1").status_code == 200


def test_dropping_tables_and_namespaces(client):
    assert client.post("/db", json={"name": "tmp"}).status_code == 201
    client.put("/db/tmp/t/1", json={})
    assert client.delete("/db/tmp/t").status_code == 204
    assert client.get("/db/tmp").json()["tables"] == []
    assert client.delete("/db/tmp/t").status_code == 404
    client.put("/db/tmp/t/1", json={})
    assert client.delete("/db/tmp").status_code == 204
    assert client.get("/db/tmp").status_code == 404
    # A namespace made again starts empty.
    assert client.post("/db", json={"name": "tmp"}).status_code == 201
    assert client.get("/db/tmp/t").json() == {"rows": []}


def test_namespaces_need_the_admin_key_and_rows_the_client_key(client, app_ns, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    as_client = {"X-API-Key": "client-key"}
    assert client.post("/db", json={"name": "k"}, headers=as_client).status_code == 401
    assert client.delete("/db/app", headers=as_client).status_code == 401
    assert client.delete("/db/app/todo", headers=as_client).status_code == 401
    assert client.get("/db/app/todo", headers=as_client).status_code == 200
    assert client.get("/db/app/todo").status_code == 401
    assert client.post("/db", json={"name": "k"}, headers={"X-API-Key": "admin-key"}).status_code == 201


PRODUCTS = {"name": "products", "fields": [
    {"name": "sku", "type": "string", "unique": True},
    {"name": "title", "type": "string", "required": True},
    {"name": "price", "type": "number", "indexed": True},
    {"name": "stock", "type": "integer"},
    {"name": "tags", "type": "string", "list": True, "indexed": True},
    {"name": "meta", "type": "object"},
]}


@pytest.fixture(scope="module")
def shop(client):
    assert client.post("/db", json={"name": "shop"}).status_code == 201
    r = client.post("/db/shop", json=PRODUCTS)
    assert r.status_code == 201, r.text
    assert r.json()["strict"] is False
    assert [f["name"] for f in r.json()["fields"]] == ["meta", "price", "sku", "stock", "tags", "title"]
    # Each indexed field adds an index file, so this table is bigger on disk than one with only the id index.
    assert r.json()["size_bytes"] > 65536 + 262144
    assert next(f for f in r.json()["fields"] if f["name"] == "sku") == {
        "name": "sku", "type": "string", "list": False, "required": False, "indexed": True, "unique": True,
        "fulltext": False}
    rows = [
        {"id": "1", "title": "Milk", "sku": "30", "price": 2.5, "stock": 4, "tags": ["dairy", "sale"]},
        {"id": "2", "title": "Bread", "sku": "A2", "price": 3, "stock": 0, "tags": ["bakery"], "meta": {"k": 1}},
        {"id": "3", "title": "Jam", "sku": "A3", "price": 10.0, "tags": [], "extra": True},
    ]
    assert client.put("/db/shop/products/_bulk", json={"rows": rows}).status_code == 204
    return "shop"


def schema_of(client, table):
    r = client.get(f"/db/shop/{table}/_schema")
    assert r.status_code == 200, r.text
    return r.json()


def put_schema(client, table, fields, strict=False):
    return client.put(f"/db/shop/{table}/_schema", json={"fields": fields, "strict": strict})


def test_tables_are_created_with_their_schema(client, shop):
    assert client.post("/db/shop", json={"name": "products"}).status_code == 409
    assert client.post("/db/shop", json={"name": "bad__name"}).status_code == 400
    assert client.post("/db/nope", json={"name": "t"}).status_code == 404
    for bad in [{"name": "a", "type": "object", "indexed": True}, {"name": "a", "list": True, "unique": True,
                "type": "string"}, {"name": "id"}]:
        assert client.post("/db/shop", json={"name": "t", "fields": [bad]}).status_code == 400, bad
    assert client.post("/db/shop", json={"name": "t", "fields": [{"name": "a"}, {"name": "a"}]}).status_code == 400
    assert "t" not in [t["name"] for t in client.get("/db/shop").json()["tables"]]
    empty = client.post("/db/shop", json={"name": "empty"}).json()
    assert empty.pop("size_bytes") > 0
    assert empty == {"name": "empty", "count": 0, "strict": False, "graph": True, "fields": []}


def test_the_schema_shows_declared_and_observed_fields(client, shop):
    schema = schema_of(client, "products")
    assert len(schema["fields"]) == 6
    observed = {f["name"]: f for f in schema["observed"]["fields"]}
    assert schema["observed"]["sampled"] == 3
    assert observed["tags"] == {"name": "tags", "present": 3, "types": {"list<string>": 2, "list": 1}}
    assert observed["price"]["types"] == {"number": 2, "integer": 1} or observed["price"]["types"] == {"number": 3}
    assert observed["extra"] == {"name": "extra", "present": 1, "types": {"boolean": 1}}
    assert schema_of(client, "missing") == {"strict": False, "graph": False, "fields": [],
                                            "observed": {"sampled": 0, "fields": []}}


def test_filters_on_declared_fields_read_values_as_the_field_type(client, shop):
    assert ids(client.get("/db/shop/products?sku=30")) == ["1"]  # a string field: no quotes needed
    assert ids(client.get("/db/shop/products?tags=sale")) == ["1"]
    assert ids(client.get("/db/shop/products?price=3")) == ["2"]
    assert ids(client.get("/db/shop/products?stock=null")) == ["3"]
    assert ids(client.get("/db/shop/products?_sort=-price")) == ["3", "2", "1"]
    assert client.get("/db/shop/products?stock=many").status_code == 400
    assert client.get("/db/shop/products?stock=1.5").status_code == 400


def test_writes_must_fit_the_schema(client, shop):
    wrong = [{"sku": 5}, {"price": "cheap"}, {"stock": 2.7}, {"stock": True}, {"tags": "dairy"}, {"tags": [1]},
             {"tags": [None]}, {"meta": [1]}]
    for row in [{"title": "x", **w} for w in wrong] + [{}, {"title": None}]:
        assert client.put("/db/shop/products/x", json=row).status_code == 400, row
    assert client.patch("/db/shop/products/1", json={"stock": "3"}).status_code == 400
    assert client.patch("/db/shop/products/1", json={"title": None}).status_code == 400  # removes a required field
    bad = {"puts": {"products": [{"id": "ok", "title": "ok"}, {"id": "y", "title": "y", "price": "x"}]}}
    assert client.post("/db/shop/_transaction", json=bad).status_code == 400
    assert client.get("/db/shop/products/ok").status_code == 404
    assert client.put("/db/shop/products/x", json={"title": "x", "price": None, "other": "anything"}).status_code == 201
    client.delete("/db/shop/products/x")


def test_unique_fields_refuse_a_second_row_with_the_value(client, shop):
    r = client.post("/db/shop/products", json={"title": "t", "sku": "A2"})
    assert r.status_code == 409 and "sku" in r.json()["detail"]
    assert client.put("/db/shop/products/2", json={"title": "Bread", "sku": "A2", "price": 4}).status_code == 200
    rows = [{"id": "u1", "title": "a", "sku": "U"}, {"id": "u2", "title": "b", "sku": "U"}]
    assert client.put("/db/shop/products/_bulk", json={"rows": rows}).status_code == 409
    assert client.get("/db/shop/products/u1").status_code == 404


def test_schema_changes_check_the_rows_first(client, shop):
    client.put("/db/shop/items/_bulk", json={"rows": [{"id": "1", "n": 1, "c": "a"}, {"id": "2", "n": "two", "c": "a"}]})
    r = put_schema(client, "items", [{"name": "n", "type": "integer", "indexed": True}])
    assert r.status_code == 400 and "2 (" in r.json()["detail"]
    assert put_schema(client, "items", [{"name": "c", "type": "string", "unique": True}]).status_code == 400
    assert put_schema(client, "items", [{"name": "q", "required": True}]).status_code == 400
    assert put_schema(client, "items", [{"name": "n"}], strict=True).status_code == 400  # c is not declared
    assert schema_of(client, "items")["fields"] == []
    # A failed change leaves the indexes as they were.
    assert put_schema(client, "items", [{"name": "c", "type": "string", "indexed": True}]).status_code == 200
    assert put_schema(client, "items", [{"name": "c", "type": "string", "unique": True}]).status_code == 400
    assert schema_of(client, "items")["fields"][0]["indexed"] is True
    assert ids(client.get("/db/shop/items?c=a")) == ["1", "2"]

    client.patch("/db/shop/items/2", json={"n": 2})
    fields = [{"name": "n", "type": "integer", "indexed": True}, {"name": "c", "type": "string"}]
    assert put_schema(client, "items", fields, strict=True).status_code == 200
    assert client.put("/db/shop/items/3", json={"n": "three"}).status_code == 400
    assert client.put("/db/shop/items/3", json={"n": 3, "z": 1}).status_code == 400  # strict
    # Changing a field's type remakes its index; leaving it out frees it.
    assert put_schema(client, "items", [{"name": "n", "type": "number", "indexed": True}, {"name": "c"}]).status_code == 200
    assert ids(client.get("/db/shop/items?n=2")) == ["2"]
    assert put_schema(client, "items", []).status_code == 200
    assert client.put("/db/shop/items/3", json={"n": "three", "z": 1}).status_code == 201
    assert put_schema(client, "made_by_schema", [{"name": "a", "type": "string"}]).status_code == 200


def test_schemas_survive_a_reload(client, shop):
    store = app_module.app.state.tables
    before = schema_of(client, "products")
    store._schemas = None
    assert schema_of(client, "products") == before


def test_schema_changes_need_the_admin_key(client, shop, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    as_client = {"X-API-Key": "client-key"}
    assert client.post("/db/shop", json={"name": "k"}, headers=as_client).status_code == 401
    assert client.put("/db/shop/products/_schema", json={"fields": []}, headers=as_client).status_code == 401
    assert client.get("/db/shop/products/_schema", headers=as_client).status_code == 200


def test_a_fresh_database_lists_no_namespaces(client, tmp_path):
    # The dashboard's first call, before any namespace (or the type holding them) exists.
    import asyncio

    from db import Database
    from db.tables import TableStore

    db = Database(str(tmp_path / "fresh"))
    try:
        tables = TableStore(db)
        assert asyncio.run(tables.namespaces()) == []
        assert asyncio.run(TableStore(db).namespaces()) == []
    finally:
        db.close()


def test_service_tables_declare_full_text_fields_with_a_tilde():
    from db.tables import schema_of

    fields = schema_of("title:string~", "tags:string[]").fields
    assert fields["title"].fulltext and not fields["title"].indexed and not fields["tags"].fulltext


def test_merge_patch_and_cursor_helpers():
    assert merge_patch({"a": 1, "b": {"c": 2, "d": 3}}, {"a": None, "b": {"c": None, "e": 4}}) == {"b": {"d": 3, "e": 4}}
    assert merge_patch({"a": [1]}, {"a": [2]}) == {"a": [2]}
    assert decode_cursor(encode_cursor(200)) == 200


def test_concurrent_writes_to_one_row_are_retried(client, app_ns):
    """ArcadeDB is optimistic: writers that change the same row at once clash, and the store runs them again."""
    import asyncio

    store = app_module.app.state.tables

    async def many():
        return await asyncio.gather(
            *(store.patch(app_ns, "busy", "r1", {f"k{i}": i}) for i in range(30)),
            *(store.upsert(app_ns, "busy", {"id": "r1", "n": i}) for i in range(30)),
            return_exceptions=True,
        )

    client.portal.call(store.upsert, app_ns, "busy", {"id": "r1", "n": 0})
    failed = [r for r in client.portal.call(many) if isinstance(r, Exception)]
    assert failed == []
    client.portal.call(store.drop_table, app_ns, "busy")


def test_the_services_namespaces_are_read_only_here(client):
    """evals and evidence are /db tables, kept by their stores: listed and readable, never written through /db."""
    client.post("/admin/datasets", json={"name": "visible-in-db"})
    listing = {n["name"]: n for n in client.get("/db").json()["namespaces"]}
    assert listing["evals"]["managed"] and {"datasets", "runs", "scores"} <= {t["name"] for t in listing["evals"]["tables"]}
    names = [r["name"] for r in client.get("/db/evals/datasets").json()["rows"]]
    assert "visible-in-db" in names
    assert client.post("/db/evals/_query", json={"language": "sql", "query": "SELECT name FROM datasets"}).status_code == 200
    for method, path, body in [("post", "/db/evals/datasets", {"id": "x", "name": "sneaky"}),
                               ("put", "/db/evals/datasets/x", {"name": "sneaky"}),
                               ("delete", "/db/evals/datasets", None),
                               ("delete", "/db/evals", None),
                               ("put", "/db/evidence/captures/_bulk", {"rows": []})]:
        r = client.request(method.upper(), path, json=body)
        assert r.status_code == 403, (method, path, r.text)
    assert client.post("/db", json={"name": "evidence"}).status_code == 409


def test_the_services_namespaces_need_the_admin_key(client, monkeypatch):
    """What evals and evidence keep is admin data (/admin's Evals, /evidence), so /db asks for the admin key first."""
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    as_client, as_admin = {"X-API-Key": "client-key"}, {"X-API-Key": "admin-key"}
    for path in ["/db/evals", "/db/evals/datasets", "/db/evals/datasets/_count"]:
        assert client.get(path, headers=as_client).status_code == 401, path
        assert client.get(path, headers=as_admin).status_code == 200, path
    # The key is checked before the table is looked up.
    assert client.get("/db/evidence/captures", headers=as_client).status_code == 401
    # Without the admin key a write is refused for the key, before the namespace's read-only rule.
    assert client.put("/db/evals/datasets/x", json={"name": "sneaky"}, headers=as_client).status_code == 401
    assert client.put("/db/evals/datasets/x", json={"name": "sneaky"}, headers=as_admin).status_code == 403


def test_a_namespace_whose_store_is_switched_off_stays_kept(client, monkeypatch):
    """A run with ENRICHERS_DIR (or EVIDENCE_DIR) left empty declares no store for the namespace; the database marks
    it as kept, so it stays admin-only and read-only."""
    from db.tables import SWITCHED_OFF

    assert "enrichers" in [n["name"] for n in client.get("/db").json()["namespaces"]]
    monkeypatch.delitem(app_module.app.state.tables.managed, "enrichers")
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    as_client, as_admin = {"X-API-Key": "client-key"}, {"X-API-Key": "admin-key"}
    assert client.get("/db/enrichers/results", headers=as_client).status_code == 401
    assert client.put("/db/enrichers/results/x", json={"planted": True}, headers=as_client).status_code == 401
    assert client.get("/db/enrichers", headers=as_admin).json()["managed"] == SWITCHED_OFF
    assert client.put("/db/enrichers/results/x", json={"planted": True}, headers=as_admin).status_code == 403
    assert client.delete("/db/enrichers", headers=as_admin).status_code == 403


def test_rows_do_not_wait_for_a_schema_change(client, app_ns):
    """Only schema changes take the store's lock: a slow one (a scan of every row, an index build) holds up the
    other schema changes, not the reads and writes of tables that are there."""
    import threading
    import time

    store = app_module.app.state.tables
    assert client.put(f"/db/{app_ns}/quick/q1", json={"n": 1}).status_code == 201
    held, done = threading.Event(), threading.Event()

    def slow_schema_change():
        with store._schema_lock:
            held.set()
            done.wait(5)

    thread = threading.Thread(target=slow_schema_change)
    thread.start()
    held.wait(5)
    try:
        started = time.monotonic()
        assert client.get(f"/db/{app_ns}/quick/q1").json() == {"id": "q1", "n": 1}
        assert client.put(f"/db/{app_ns}/quick/q2", json={"n": 2}).status_code == 201
        assert client.post(f"/db/{app_ns}/_transaction", json={"puts": {"quick": [{"id": "q3"}]}}).status_code == 204
        assert client.get(f"/db/{app_ns}").status_code == 200
        assert time.monotonic() - started < 2
    finally:
        done.set()
        thread.join()


# ---------------------------------------------------------------- full text
def test_search_text_becomes_an_index_query():
    from db.schema import Search, TableError, search_query

    assert search_query("quick dog") == Search('+"quick" +"dog"')
    # The index finds a phrase's words; the rows it finds are checked for the phrase, which only leaves rows out.
    assert search_query('"quick  Brown" -lazy -"red fox"') == Search('+"quick Brown" -"lazy"', ("quick brown",), ("red fox",))
    # A prefix keeps the parser's syntax out of its stem; a phrase needs only its quotes taken out.
    assert search_query("phish* site.exa* a-b* site.example.com").query == r'+phish* +site.exa* +a\-b* +"site.example.com"'
    # One the index's words cannot start so: as written.
    assert search_query('a:b* *x* b2")*').query == '+"a:b" +"*x" +"b2 )"'
    assert search_query('C++ wo"rd "unclosed phrase') == Search('+"C++" +"wo rd" +"unclosed phrase"', ("unclosed phrase",))
    assert search_query("!!! dog - *").query == '+"dog"'
    assert search_query('Phish* "Login  page" -spam dog').terms == ("phish", "login page", "dog")
    for nothing in ("", "  ", "-only", "* !", '-"two words"'):
        with pytest.raises(TableError):
            search_query(nothing)
    with pytest.raises(TableError):
        search_query("x" * 501)


NOTES = [
    {"id": "n1", "title": "Phishing report", "body": "A login page at phishing-site.example.com", "status": "open"},
    {"id": "n2", "title": "Weekly notes", "body": "Nothing about phishing here, only the weather", "status": "done"},
    {"id": "n3", "title": "Quick brown fox", "body": "jumps over the lazy dog", "status": "open"},
    {"id": "n4", "title": "Phishing phishing phishing", "body": "Phishing again", "status": "open"},
    {"id": "n5", "title": "Version notes", "body": "Upgraded to v1.2 on Monday", "status": "done"},
]


@pytest.fixture(scope="module")
def notes(client):
    assert client.post("/db", json={"name": "words"}).status_code == 201
    r = client.post("/db/words", json={"name": "notes", "fields": [
        {"name": "title", "type": "string", "fulltext": True},
        {"name": "body", "type": "string", "fulltext": True},
        {"name": "status", "type": "string", "indexed": True},
    ]})
    assert r.status_code == 201, r.text
    assert [f["name"] for f in r.json()["fields"] if f["fulltext"]] == ["body", "title"]
    assert client.put("/db/words/notes/_bulk", json={"rows": NOTES}).status_code == 204
    return "words"


def search(client, query, **params):
    r = client.get("/db/words/notes", params={"_search": query, **params})
    assert r.status_code == 200, r.text
    return [row["id"] for row in r.json()["rows"]]


def test_a_search_finds_every_word_across_the_full_text_fields_best_first(client, notes):
    # The most phishing first; n2 holds it once, in a longer text.
    assert search(client, "phishing") == ["n4", "n1", "n2"]
    assert search(client, "Phishing LOGIN") == ["n1"], "every word, any case, in any full-text field"
    assert search(client, "quick dog") == ["n3"], "one word in the title, the other in the body"
    assert search(client, '"brown fox"') == ["n3"] and search(client, '"fox brown"') == []
    assert search(client, 'fox -"lazy dog"') == [] and search(client, 'fox -"dog lazy"') == ["n3"]
    assert search(client, "phishing -weather") == ["n4", "n1"]
    assert search(client, "phish*") == ["n4", "n1", "n2"] and search(client, "jump*") == ["n3"]
    assert search(client, "phishing-site.example.com") == ["n1"] and search(client, "v1.2") == ["n5"]
    assert search(client, "nowhere") == []


def test_a_search_goes_with_filters_sorts_pages_and_counts(client, notes):
    assert search(client, "phishing", status="open") == ["n4", "n1"]
    assert search(client, "phishing", _sort="-id") == ["n4", "n2", "n1"]
    first = client.get("/db/words/notes", params={"_search": "phishing", "_limit": 2}).json()
    rest = client.get("/db/words/notes", params={"_search": "phishing", "_limit": 2, "_cursor": first["next"]}).json()
    assert [r["id"] for r in first["rows"] + rest["rows"]] == ["n4", "n1", "n2"] and "next" not in rest
    assert "$score" not in first["rows"][0] and first["rows"][0]["status"] == "open"
    r = client.get("/db/words/notes/_count", params={"_search": "phishing", "status": "open"})
    assert r.status_code == 200 and r.json() == {"count": 2}


def test_rows_are_searched_as_they_are_now(client, notes):
    assert client.patch("/db/words/notes/n3", json={"body": "sleeps all day"}).status_code == 200
    assert search(client, "quick dog") == [] and search(client, "quick sleeps") == ["n3"]
    assert client.delete("/db/words/notes/n5").status_code == 204
    assert search(client, "v1.2") == []
    assert client.put("/db/words/notes/n5", json=NOTES[4]).status_code == 201


@pytest.mark.parametrize("query, detail", [
    ({"_search": "-only"}, "a word to find"),
    ({"_search": "x" * 501}, "500"),
])
def test_bad_searches_are_refused(client, notes, query, detail):
    r = client.get("/db/words/notes", params=query)
    assert r.status_code in (400, 422) and detail in r.text


def test_a_table_without_full_text_fields_cannot_be_searched(client, notes, app_ns):
    r = client.get(f"/db/{app_ns}/todo", params={"_search": "milk"})
    assert r.status_code == 400 and "full-text" in r.json()["detail"]
    assert client.get(f"/db/{app_ns}/todo/_count", params={"_search": "milk"}).status_code == 400


@pytest.mark.parametrize("field, detail", [
    ({"name": "n", "type": "integer", "fulltext": True}, "only a string field"),
    ({"name": "t", "type": "string", "list": True, "fulltext": True}, "only a string field"),
    ({"name": "t", "type": "string", "indexed": True, "fulltext": True}, "not both"),
    ({"name": "t", "type": "string", "unique": True, "fulltext": True}, "not both"),
])
def test_only_plain_string_fields_are_full_text(client, notes, field, detail):
    r = client.put("/db/words/other/_schema", json={"fields": [field]})
    assert r.status_code == 400 and detail in r.json()["detail"]


def test_full_text_fields_come_and_go_with_the_schema(client, notes):
    rows = [{"id": "1", "title": "red apple", "note": "crisp"}, {"id": "2", "title": "green pear", "note": "soft apple"}]
    assert client.put("/db/words/fruit/_bulk", json={"rows": rows}).status_code == 204

    def found(query):
        r = client.get("/db/words/fruit", params={"_search": query})
        return [row["id"] for row in r.json()["rows"]] if r.status_code == 200 else r.status_code

    assert found("apple") == 400
    # Made full-text over rows already there; then a second field joins the same index.
    assert client.put("/db/words/fruit/_schema", json={"fields": [{"name": "title", "type": "string", "fulltext": True}]}).status_code == 200
    assert found("apple") == ["1"]
    fields = [{"name": "title", "type": "string", "fulltext": True}, {"name": "note", "type": "string", "fulltext": True}]
    assert client.put("/db/words/fruit/_schema", json={"fields": fields}).status_code == 200
    assert sorted(found("apple")) == ["1", "2"]
    # From full-text to indexed, and back to nothing: the field keeps its values, the search goes.
    fields = [{"name": "title", "type": "string", "indexed": True}]
    assert client.put("/db/words/fruit/_schema", json={"fields": fields}).status_code == 200
    assert found("apple") == 400
    assert [r["id"] for r in client.get("/db/words/fruit", params={"title": "green pear"}).json()["rows"]] == ["2"]
    assert client.put("/db/words/fruit/_schema", json={"fields": []}).status_code == 200
    assert client.get("/db/words/fruit/2").json()["note"] == "soft apple"
    # A reload reads the saved schema, full-text included.
    store = app_module.app.state.tables
    assert client.put("/db/words/fruit/_schema", json={"fields": [{"name": "note", "type": "string", "fulltext": True}]}).status_code == 200
    store._schemas, store._loaded = None, False
    assert found("soft") == ["2"]
