import pytest
from fastapi.testclient import TestClient

from db.tables import decode_cursor, encode_cursor, merge_patch


from api import app as app_module  # noqa: E402
from core.config import Config  # noqa: E402


@pytest.fixture(scope="module")
def client():
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


def without_sizes(workspace):
    """The workspace's listing without the sizes in the database, which are the database's estimates, and when it was
    made."""
    tables = [{k: v for k, v in t.items() if k != "size_bytes"} for t in workspace["tables"]]
    return {**{k: v for k, v in workspace.items() if k not in ("size_bytes", "created")}, "tables": tables}


def ids(r):
    assert r.status_code == 200, r.text
    return [row["id"] for row in r.json()["rows"]]


def test_workspaces_are_listed_with_their_tables(client, app_ns):
    assert client.post("/db", json={"name": "app"}).status_code == 409
    assert client.post("/db", json={"name": "bad__name"}).status_code == 400
    assert client.post("/db", json={"name": "_x"}).status_code == 400
    listed = {n["name"]: n for n in client.get("/db").json()["workspaces"]}
    # Made by its first write, it is a graph table, as new tables are. The list names it and its kind; the
    # workspace's own page counts its rows, sizes it and gives its schema.
    entry = {"name": "todo", "graph": True, "bucket": False, "vector": None, "kept": None}
    assert listed["app"]["tables"] == [entry] and listed["app"]["edges"] == []
    todo = {**entry, "count": 3, "strict": False, "fields": []}
    app = client.get("/db/app").json()
    assert app["created"] == listed["app"]["created"] > 0
    assert without_sizes(app) == {"name": "app", "label": None, "tables": [todo], "edges": [], "managed": None}
    # About its bytes in the database, and the workspace's the sum of its tables'.
    assert app["tables"][0]["size_bytes"] > 0
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


def test_dropping_tables_and_workspaces(client):
    assert client.post("/db", json={"name": "tmp"}).status_code == 201
    client.put("/db/tmp/t/1", json={})
    assert client.delete("/db/tmp/t").status_code == 204
    assert client.get("/db/tmp").json()["tables"] == []
    assert client.delete("/db/tmp/t").status_code == 404
    client.put("/db/tmp/t/1", json={})
    assert client.delete("/db/tmp").status_code == 204
    assert client.get("/db/tmp").status_code == 404
    # A workspace made again starts empty.
    assert client.post("/db", json={"name": "tmp"}).status_code == 201
    assert client.get("/db/tmp/t").json() == {"rows": []}


def test_workspaces_need_the_admin_key_and_rows_the_client_key(client, app_ns, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    as_client = {"X-API-Key": "client-key"}
    assert client.post("/db", json={"name": "k"}, headers=as_client).status_code == 401
    assert client.patch("/db/app", json={"label": "x"}, headers=as_client).status_code == 401
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
    # A table with no rows yet takes no room.
    assert r.json()["size_bytes"] == 0
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
    assert empty.pop("size_bytes") == 0
    assert empty == {"name": "empty", "count": 0, "strict": False, "graph": True, "bucket": False, "fields": [],
                     "vector": None, "kept": None}


def test_the_schema_shows_declared_and_observed_fields(client, shop):
    schema = schema_of(client, "products")
    assert len(schema["fields"]) == 6
    observed = {f["name"]: f for f in schema["observed"]["fields"]}
    assert schema["observed"]["sampled"] == 3
    assert observed["tags"] == {"name": "tags", "present": 3, "types": {"list<string>": 2, "list": 1}}
    # A whole number in a number field reads back as one (2.0 as 2): JSON has one kind of number.
    assert sum(observed["price"]["types"].values()) == 3 and set(observed["price"]["types"]) <= {"number", "integer"}
    assert observed["extra"] == {"name": "extra", "present": 1, "types": {"boolean": 1}}
    assert schema_of(client, "missing") == {"strict": False, "graph": False, "bucket": False, "fields": [],
                                            "observed": {"sampled": 0, "fields": []}}


def test_filters_on_declared_fields_read_values_as_the_field_type(client, shop):
    assert ids(client.get("/db/shop/products?sku=30")) == ["1"]  # a string field: no quotes needed
    assert ids(client.get("/db/shop/products?tags=sale")) == ["1"]
    assert ids(client.get("/db/shop/products?price=3")) == ["2"]
    assert ids(client.get("/db/shop/products?stock=null")) == ["3"]
    assert ids(client.get("/db/shop/products?_sort=-price")) == ["3", "2", "1"]
    assert client.get("/db/shop/products?stock=many").status_code == 400
    assert client.get("/db/shop/products?stock=1.5").status_code == 400


def test_a_filter_on_a_declared_json_field_matches_text_beyond_ascii(client, shop):
    """A field of any type holds JSON, written by Python with é as \\u00e9; a filter matches it as the text it is."""
    assert put_schema(client, "loose", [{"name": "x", "type": "any"}]).status_code == 200
    rows = [{"id": "1", "x": "é"}, {"id": "2", "x": ["a", "é"]}, {"id": "3", "x": "e"}, {"id": "4", "y": "é"}]
    assert client.put("/db/shop/loose/_bulk", json={"rows": rows}).status_code == 204
    assert ids(client.get("/db/shop/loose", params={"x": "é"})) == ["1", "2"]
    assert ids(client.get("/db/shop/loose", params={"y": "é"})) == ["4"]


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


def test_a_write_of_one_id_twice_keeps_the_last_at_its_place(own_db):
    """A write's rows go in one statement, which may not change a row twice: the last of an id is stored, as one write
    after another would leave it, written after the rows before it."""
    import asyncio

    from db.graph import GraphStore

    store = GraphStore(own_db)
    store._create_workspace("w")
    asyncio.run(store.bulk_put("w", "t", [{"id": "a", "n": 1}, {"id": "b", "n": 2}, {"id": "a", "n": 3, "x": "y"}]))
    rows, _ = store._written_after("w", "t", 0, None, 10)
    assert rows == [{"id": "b", "n": 2}, {"id": "a", "n": 3, "x": "y"}]


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


def test_a_fresh_database_has_the_default_workspace(client, tmp_path):
    # The dashboard's first call, before any workspace (or the type holding them) exists.
    import asyncio

    from core.errors import Forbidden
    from db import Database
    from db.tables import TableStore

    db = Database(str(tmp_path / "fresh"))
    try:
        tables = TableStore(db)
        listed, _ = asyncio.run(tables.listing())
        assert [(w["name"], w["label"], w["managed"], w["tables"]) for w in listed] == [("default", None, None, [])]
        assert listed[0]["created"] > 0
        with pytest.raises(Forbidden, match="always exists"):
            tables._drop_workspace("default")
        asyncio.run(tables.upsert("default", "notes", {"id": "n1"}))
        tables._set_label("default", "Everything else")
        # A store opened later finds it, and makes no other.
        again, _ = asyncio.run(TableStore(db).listing())
        assert [(w["name"], w["label"], [t["name"] for t in w["tables"]]) for w in again] == [
            ("default", "Everything else", ["notes"])]
    finally:
        db.close()


def test_the_default_workspace_always_exists(client):
    """It is listed, written to like any other, and its tables can be dropped, but it cannot."""
    assert "default" in [w["name"] for w in client.get("/db").json()["workspaces"]]
    assert client.post("/db", json={"name": "default"}).status_code == 409
    assert client.put("/db/default/scratch/s1", json={"n": 1}).status_code == 201
    r = client.delete("/db/default")
    assert r.status_code == 403 and "always exists" in r.json()["detail"]
    assert client.get("/db/default/scratch/s1").json() == {"id": "s1", "n": 1}
    assert client.delete("/db/default/scratch").status_code == 204
    assert client.get("/db/default").status_code == 200


def test_workspaces_have_labels(client):
    r = client.post("/db", json={"name": "labelled", "label": "  Customer records "})
    assert r.status_code == 201 and r.json()["label"] == "Customer records"
    for bad in ("   ", "x" * 201, 5):
        assert client.post("/db", json={"name": "unlabelled", "label": bad}).status_code == 422, bad
    assert client.get("/db/unlabelled").status_code == 404
    listed = {w["name"]: w for w in client.get("/db").json()["workspaces"]}
    assert listed["labelled"]["label"] == "Customer records"
    r = client.patch("/db/labelled", json={"label": "Clients, 2026"})
    assert r.status_code == 200 and r.json()["label"] == "Clients, 2026" and r.json()["name"] == "labelled"
    assert client.get("/db/labelled").json()["label"] == "Clients, 2026"
    assert client.patch("/db/labelled", json={"label": None}).json()["label"] is None
    assert {w["name"]: w["label"] for w in client.get("/db").json()["workspaces"]}["labelled"] is None
    assert client.patch("/db/labelled", json={}).status_code == 422
    assert client.patch("/db/labelled", json={"label": "x", "name": "other"}).status_code == 422
    assert client.patch("/db/nope", json={"label": "x"}).status_code == 404
    assert client.patch("/db/evals", json={"label": "x"}).status_code == 403


def test_the_list_holds_the_services_own_only_when_asked(client, monkeypatch):
    workspaces = client.get("/db").json()["workspaces"]
    assert "default" in [w["name"] for w in workspaces] and all(w["managed"] is None for w in workspaces)
    assert not {"evals", "tasks", "enrichers", "playground", "evidence"} & {w["name"] for w in workspaces}
    every = {w["name"]: w for w in client.get("/db", params={"all": "true"}).json()["workspaces"]}
    assert {w["name"] for w in workspaces} < set(every)
    assert every["evals"]["managed"] and every["tasks"]["managed"] and every["default"]["managed"] is None
    assert [w["name"] for w in client.get("/db?all=false").json()["workspaces"]] == [w["name"] for w in workspaces]
    assert client.get("/db?bogus=1").status_code == 400
    assert client.get("/db?all=maybe").status_code == 422
    # The service's own are the admin's: a client key lists the workspaces only.
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    as_client, as_admin = {"X-API-Key": "client-key"}, {"X-API-Key": "admin-key"}
    assert client.get("/db", headers=as_client).status_code == 200
    assert client.get("/db?all=true", headers=as_client).status_code == 401
    assert client.get("/db?all=true").status_code == 401
    assert set(every) <= {w["name"] for w in client.get("/db?all=true", headers=as_admin).json()["workspaces"]}


def test_the_workspaces_are_listed_a_page_at_a_time(client):
    for name in ("paged_a", "paged_b", "paged_c"):
        assert client.post("/db", json={"name": name}).status_code == 201
    every = [w["name"] for w in client.get("/db").json()["workspaces"]]
    seen, cursor = [], None
    while True:
        page = client.get("/db", params={"_limit": 2, **({"_cursor": cursor} if cursor else {})}).json()
        assert len(page["workspaces"]) <= 2
        seen += [w["name"] for w in page["workspaces"]]
        cursor = page.get("next")
        if cursor is None:
            break
    assert seen == every == sorted(every) and {"paged_a", "paged_b", "paged_c"} <= set(seen)
    assert client.get("/db", params={"_limit": 0}).status_code == 422
    assert client.get("/db", params={"_cursor": "nope"}).status_code == 400


def test_the_list_reads_no_table_and_a_workspace_sizes_each_as_alone(client, monkeypatch):
    """The list of workspaces names their tables from memory: it counts and sizes none, so it costs the same
    however many tables there are. A workspace's own page gives each table and edge type the size its own read
    gives, and its own size is their sum."""
    assert client.post("/db", json={"name": "sized"}).status_code == 201
    assert client.put("/db/sized/people/_bulk", json={"rows": [{"id": "a"}, {"id": "b"}]}).status_code == 204
    assert client.put("/db/sized/notes/n1", json={"text": "x"}).status_code == 201
    edge = {"type": "knows", "from": "people/a", "to": "people/b"}
    assert client.put("/db/sized/_edges", json={"edges": [edge]}).status_code == 204
    store = app_module.app.state.tables
    counts = []
    count = store._count
    monkeypatch.setattr(store, "_count", lambda *args, **kwargs: counts.append(1) or count(*args, **kwargs))
    listed = {w["name"]: w for w in client.get("/db", params={"all": "true"}).json()["workspaces"]}
    assert not counts and len(listed) > 3
    assert [t["name"] for t in listed["sized"]["tables"]] == ["notes", "people"]
    assert listed["sized"]["edges"] == [{"name": "knows"}]
    sized = client.get("/db/sized").json()
    assert len(client.get("/db/sized/_edges").json()["edges"]) == 1
    # Each table and edge type sized alone.
    alone = {t["name"]: store._size("sized", t["name"]) for t in sized["tables"]}
    assert {t["name"]: t["size_bytes"] for t in sized["tables"]} == alone and all(alone.values())
    assert sized["edges"][0]["size_bytes"] == store._edge_info("sized", "knows")["size_bytes"] > 0
    assert sized["size_bytes"] == sum(alone.values()) + sized["edges"][0]["size_bytes"]


def test_service_tables_declare_full_text_fields_with_a_tilde():
    from db.tables import schema_of

    fields = schema_of("title:string~", "tags:string[]").fields
    assert fields["title"].fulltext and not fields["title"].indexed and not fields["tags"].fulltext


def test_merge_patch_and_cursor_helpers():
    assert merge_patch({"a": 1, "b": {"c": 2, "d": 3}}, {"a": None, "b": {"c": None, "e": 4}}) == {"b": {"d": 3, "e": 4}}
    assert merge_patch({"a": [1]}, {"a": [2]}) == {"a": [2]}
    assert decode_cursor(encode_cursor(200)) == 200


def test_concurrent_writes_to_one_row_are_retried(client, app_ns):
    """The database is optimistic: writers that change the same row at once clash, and the store runs them again."""
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


def test_concurrent_first_puts_of_one_row_or_edge_replace_each_other(tmp_path):
    """Writers that store a new row (or edge) of the same key at once cannot see each other's: the one that commits
    second clashes over the key, runs again and replaces it, as a put does, rather than answering 409."""
    import asyncio

    from db import Database
    from db.graph import GraphStore

    database = Database(str(tmp_path / "toolkit.duckdb"), threads=8)
    store = GraphStore(database)
    store._create_workspace("w")

    async def many():
        await store.bulk_put("w", "pair", [{"id": "a"}, {"id": "b"}])
        return await asyncio.gather(
            *(store.upsert("w", "pair", {"id": f"new{i % 5}", "n": i}) for i in range(60)),
            *(store.put_edges("w", [{"type": f"likes{i % 5}", "from": "pair/a", "to": "pair/b", "n": i}])
              for i in range(60)),
            return_exceptions=True,
        )

    try:
        failed = [r for r in asyncio.run(many()) if isinstance(r, Exception)]
        assert failed == []
        assert store._count("w", "pair") == 7
    finally:
        database.close()


def test_the_services_workspaces_are_read_only_here(client):
    """evals and evidence are /db tables, kept by their stores (evidence's in the workspaces): listed and readable,
    never written through /db."""
    client.post("/admin/datasets", json={"name": "visible-in-db"})
    listing = {n["name"]: n for n in client.get("/db?all=true").json()["workspaces"]}
    assert listing["evals"]["managed"] and {"datasets", "runs", "scores"} <= {t["name"] for t in listing["evals"]["tables"]}
    names = [r["name"] for r in client.get("/db/evals/datasets").json()["rows"]]
    assert "visible-in-db" in names
    assert client.post("/db/evals/_query", json={"query": "SELECT name FROM datasets"}).status_code == 200
    for method, path, body in [("post", "/db/evals/datasets", {"id": "x", "name": "sneaky"}),
                               ("put", "/db/evals/datasets/x", {"name": "sneaky"}),
                               ("delete", "/db/evals/datasets", None),
                               ("delete", "/db/evals", None),
                               ("put", "/db/default/evidence_captures/_bulk", {"rows": []})]:
        r = client.request(method.upper(), path, json=body)
        assert r.status_code == 403, (method, path, r.text)
    assert client.post("/db", json={"name": "evals"}).status_code == 409


def test_the_services_workspaces_need_the_admin_key(client, monkeypatch):
    """What evals and evidence keep is admin data (/admin's Evals, /evidence), so /db asks for the admin key first."""
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    as_client, as_admin = {"X-API-Key": "client-key"}, {"X-API-Key": "admin-key"}
    for path in ["/db/evals", "/db/evals/datasets", "/db/evals/datasets/_count"]:
        assert client.get(path, headers=as_client).status_code == 401, path
        assert client.get(path, headers=as_admin).status_code == 200, path
    # The key is checked before the table is looked up.
    assert client.get("/db/default/evidence_captures", headers=as_client).status_code == 401
    # Without the admin key a write is refused for the key, before the workspace's read-only rule.
    assert client.put("/db/evals/datasets/x", json={"name": "sneaky"}, headers=as_client).status_code == 401
    assert client.put("/db/evals/datasets/x", json={"name": "sneaky"}, headers=as_admin).status_code == 403


def test_a_workspace_no_store_declares_stays_kept(client, monkeypatch):
    """A run that declares no store for the workspace (one an earlier version kept): the database marks it as kept, so
    it stays admin-only and read-only."""
    from db.tables import UNDECLARED

    assert "enrichers" in [n["name"] for n in client.get("/db?all=true").json()["workspaces"]]
    monkeypatch.delitem(app_module.app.state.tables.managed, "enrichers")
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    as_client, as_admin = {"X-API-Key": "client-key"}, {"X-API-Key": "admin-key"}
    assert client.get("/db/enrichers/results", headers=as_client).status_code == 401
    assert client.put("/db/enrichers/results/x", json={"planted": True}, headers=as_client).status_code == 401
    assert client.get("/db/enrichers", headers=as_admin).json()["managed"] == UNDECLARED
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


def test_a_search_finds_a_row_whose_write_commits_after_the_index_is_built(own_db):
    """A write takes its number (``_seq``) as it runs, not as it commits: a row written before the index was built
    but committed after it has a number below the newest the index saw, and is still found as it is now."""
    import threading

    from db.tables import Field, TableStore, make_schema

    store = TableStore(own_db)
    store._create_workspace("w")
    store._set_schema("w", "t", make_schema([Field("text", "string", fulltext=True)]))
    store._upsert("w", "t", {"id": "a", "text": "apple"})
    assert [r["id"] for r in store._list("w", "t", search="apple")[0]] == ["a"]
    written, go = threading.Event(), threading.Event()

    def slow_write():
        with store._writing("w", ["t"]) as db:
            store._put(db, "w", "t", [{"id": "a", "text": "pear"}])
            written.set()
            go.wait(5)

    writer = threading.Thread(target=slow_write)
    writer.start()
    assert written.wait(5)
    store._upsert("w", "t", {"id": "b", "text": "plum"})
    store._rebuild_index("w", "t")
    go.set()
    writer.join(5)
    assert [r["id"] for r in store._list("w", "t", search="pear")[0]] == ["a"]
    assert store._list("w", "t", search="apple")[0] == []
    assert [r["id"] for r in store._list("w", "t", search="plum")[0]] == ["b"]


def test_a_row_the_index_holds_without_the_ranking_words_comes_last(own_db, monkeypatch):
    """Only a row the index does not hold yet has no score and comes first: one it holds that lacks the words it is
    ranked by scores 0. And a ranking by words (hybrid search's) keeps the rows that hold one of them."""
    import db.rows
    from db.tables import Field, TableStore, make_schema

    store = TableStore(own_db)
    store._create_workspace("w")
    store._set_schema("w", "t", make_schema([Field("text", "string", fulltext=True)]))
    for row_id, text in [("a", "plum tart"), ("b", "plum jam"), ("c", "plank"), ("d", "pear tart")]:
        store._upsert("w", "t", {"id": row_id, "text": text})
    store._rebuild_index("w", "t")
    # pl* is ranked by the commonest word starting so (plum), which c lacks.
    monkeypatch.setattr(db.rows, "MAX_ANY_WORDS", 1)
    assert [r["id"] for r in store._list("w", "t", search="pl*")[0]] == ["a", "b", "c"]
    # Rows holding "tart" ranked by "plum": b holds no tart, d no plum.
    assert store._best_by_words("w", "t", [], "tart", "plum", 10) == ["a"]


def test_a_table_whose_full_text_index_would_be_named_as_anothers_is_refused(own_db):
    """DuckDB names a table's full-text index fts_<workspace>_<table>, so a_b/c and a/b_c would share one."""
    from db.tables import Conflict, TableStore

    store = TableStore(own_db)
    for workspace in ("a_b", "a"):
        store._create_workspace(workspace)
    store._ensure("a_b", ["c"])
    with pytest.raises(Conflict) as caught:
        store._ensure("a", ["B_c"])
    assert "a_b/c" in str(caught.value)
    store._ensure("a", ["c_b"])


def test_a_schema_change_keeps_the_rows_nulls(own_db):
    """A field that leaves the schema takes its values among the row's undeclared fields whole, nulls inside them too,
    and one that comes in keeps the null a row holds for it: the rows read back as they were written, either way."""
    from db.tables import Field, TableStore, make_schema

    store = TableStore(own_db)
    store._create_workspace("w")
    declared = make_schema([Field("meta", "object"), Field("n", "integer"), Field("tags", "string", list=True),
                             Field("e", "string")])
    store._set_schema("w", "t", declared)
    rows = [{"id": "a", "meta": {"x": None, "y": [1, None]}, "n": None, "note": None, "tags": ["p"], "e": "f"},
            {"id": "b", "n": 2, "note": "kept"}]
    for row in rows:
        store._upsert("w", "t", row)
    store._set_schema("w", "t", make_schema([Field("note", "string")]))
    assert [store._get("w", "t", r["id"]) for r in rows] == rows
    store._set_schema("w", "t", declared)
    assert [store._get("w", "t", r["id"]) for r in rows] == rows


def test_a_schema_change_and_the_writes_to_its_table_wait_for_each_other(own_db, monkeypatch):
    """A schema change copies its table's rows into a new one: it waits for a write under way, whose row is copied,
    and a write that comes meanwhile waits for it, then is checked against the new schema."""
    import threading

    from db.tables import Field, TableError, TableStore, make_schema

    store = TableStore(own_db)
    store._create_workspace("w")
    store._upsert("w", "t", {"id": "a", "age": 1})
    inside, go = threading.Event(), threading.Event()

    def paused(method):
        def run(*args, **kwargs):
            inside.set()
            go.wait(5)
            return method(*args, **kwargs)
        return run

    # A write under way (paused in its transaction): the change waits for it.
    monkeypatch.setattr(store, "_check_room", paused(store._check_room))
    writer = threading.Thread(target=store._upsert, args=("w", "t", {"id": "b", "age": 2}))
    writer.start()
    assert inside.wait(5)
    change = threading.Thread(target=store._set_schema, args=("w", "t", make_schema([Field("age", "integer")])))
    change.start()
    change.join(0.3)
    assert change.is_alive()
    go.set()
    writer.join(5)
    change.join(5)
    assert [r["id"] for r in store._list("w", "t")[0]] == ["a", "b"]

    # A change under way (paused before it copies the rows): a write waits for it, and is refused by the new schema,
    # which the old let it through.
    monkeypatch.undo()
    inside.clear()
    go.clear()
    monkeypatch.setattr(store, "_apply_schema", paused(store._apply_schema))
    failed = []

    def write():
        try:
            store._upsert("w", "t", {"id": "c", "age": 3, "note": "not declared"})
        except TableError as e:
            failed.append(str(e))

    change = threading.Thread(target=store._set_schema,
                              args=("w", "t", make_schema([Field("age", "integer")], strict=True)))
    change.start()
    assert inside.wait(5)
    writer = threading.Thread(target=write)
    writer.start()
    writer.join(0.3)
    assert writer.is_alive()
    go.set()
    change.join(5)
    writer.join(5)
    assert failed and "note" in failed[0]
    assert [r["id"] for r in store._list("w", "t")[0]] == ["a", "b"]


def test_a_drop_and_the_writes_to_its_table_wait_for_each_other(own_db, monkeypatch):
    """A drop waits for a write under way (a row, or an edge at the table), which would otherwise commit into the
    dropped table and answer that it was stored; a write that comes meanwhile waits for it, and finds the table gone."""
    import threading

    from db.graph import GraphStore
    from db.schema import NotFound

    store = GraphStore(own_db)
    store._create_workspace("w")
    inside, go = threading.Event(), threading.Event()

    def paused(method):
        def run(*args, **kwargs):
            inside.set()
            go.wait(5)
            return method(*args, **kwargs)
        return run

    def waits_for(target, drop):
        """Run ``target`` until it pauses in its transaction, then ``drop``: the drop waits until it goes on."""
        inside.clear()
        go.clear()
        writer = threading.Thread(target=target)
        writer.start()
        assert inside.wait(5)
        dropping = threading.Thread(target=drop)
        dropping.start()
        dropping.join(0.3)
        assert dropping.is_alive()
        go.set()
        writer.join(5)
        dropping.join(5)

    store._upsert("w", "t", {"id": "a"})
    with monkeypatch.context() as m:
        m.setattr(store, "_check_room", paused(store._check_room))
        waits_for(lambda: store._upsert("w", "t", {"id": "b"}), lambda: store._drop_table("w", "t"))
    assert not store._has("w", "t")

    store._upsert("w", "t", {"id": "a"})
    with monkeypatch.context() as m:
        m.setattr(store, "_put_edge", paused(store._put_edge))
        waits_for(lambda: store.put_edges.__wrapped__(store, "w", [{"type": "knows", "from": "t/a", "to": "t/a"}]),
                  lambda: store._drop_table("w", "t"))
        # A transaction's edge to a table it neither puts to nor deletes from.
        store._upsert("w", "t", {"id": "a"})
        waits_for(lambda: store._transaction("w", puts={"u": [{"id": "1"}]},
                                             edges=[{"type": "knows", "from": "u/1", "to": "t/a"}]),
                  lambda: store._drop_table("w", "t"))
    assert own_db.open().rows("SELECT count(*) AS n FROM w._edges") == [{"n": 0}]

    # A drop under way: a write that comes meanwhile waits, then is told the table is gone, and makes no other.
    store._upsert("w", "t", {"id": "a"})
    failed = []

    def write():
        try:
            store._upsert("w", "t", {"id": "c"})
        except NotFound as e:
            failed.append(str(e))

    inside.clear()
    go.clear()
    with monkeypatch.context() as m:
        m.setattr(store, "_drop_fulltext_index", paused(store._drop_fulltext_index))
        dropping = threading.Thread(target=store._drop_table, args=("w", "t"))
        dropping.start()
        assert inside.wait(5)
        writer = threading.Thread(target=write)
        writer.start()
        writer.join(0.3)
        assert writer.is_alive()
        go.set()
        dropping.join(5)
        writer.join(5)
    assert failed and "dropped" in failed[0]
    assert not store._has("w", "t")


def test_a_failed_write_keeps_the_table_it_made_when_another_filled_it_meanwhile(own_db, monkeypatch):
    """A write that fails drops the table it made for its rows, unless another writer's row went in meanwhile: that
    one was told its row is stored, so it stays."""
    import threading

    from db.graph import GraphStore
    from db.schema import TableError

    store = GraphStore(own_db)
    store._create_workspace("w")
    made, inside, go = threading.Event(), threading.Event(), threading.Event()
    real_put, real_ensure = store._put, store._ensure

    def put(db, workspace, table, rows):
        real_put(db, workspace, table, rows)
        if any(row["id"] == "theirs" for row in rows):
            inside.set()
            go.wait(5)

    def ensure(workspace, tables, graph=None):
        real_ensure(workspace, tables, graph)
        if threading.current_thread().name == "failing":
            made.set()
            inside.wait(5)  # the other writer is in its transaction before this one goes on, and fails

    monkeypatch.setattr(store, "_put", put)
    monkeypatch.setattr(store, "_ensure", ensure)
    failed, stored = [], []

    def failing():
        try:
            store._transaction("w", puts={"fresh": [{"id": "mine"}]},
                               edges=[{"type": "k", "from": "fresh/mine", "to": "fresh/nope"}])
        except TableError as e:
            failed.append(str(e))

    def other():
        made.wait(5)
        stored.append(store._upsert("w", "fresh", {"id": "theirs"}))

    threads = [threading.Thread(target=failing, name="failing"), threading.Thread(target=other)]
    for thread in threads:
        thread.start()
    assert inside.wait(5)
    threads[0].join(0.5)  # its clean-up waits for the other's write
    go.set()
    for thread in threads:
        thread.join(5)
    assert failed and "nope" in failed[0] and stored == [True]
    assert [r["id"] for r in store._list("w", "fresh")[0]] == ["theirs"]


# ---------------------------------------------------------------- full text
def test_search_text_becomes_words_phrases_and_starts():
    from db.schema import TableError, search_query

    assert search_query("quick dog").words == ("quick", "dog")
    # A phrase's words are needed, and the rows are checked for the phrase as written; an unwanted phrase only leaves
    # rows out, its words apart no reason to.
    found = search_query('"quick  Brown" -lazy -"red fox"')
    assert (found.words, found.phrases, found.excluded, found.unwanted) == (
        ("quick", "brown"), ("quick brown",), ("lazy",), ("red fox",))
    # Words as the index splits them: a domain, version or address is one word; a hyphen or a colon splits them, so
    # a start of two words is searched as its words.
    found = search_query("phish* site.exa* a-b* site.example.com")
    assert (found.prefixes, found.words) == (("phish", "site.exa"), ("a", "b", "site.example.com"))
    assert search_query('a:b* C++ "unclosed phrase').words == ("a", "b", "c", "unclosed", "phrase")
    assert search_query("Café* !!! dog - *").prefixes == ("cafe",) and search_query("!!! dog - *").words == ("dog",)
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
    # The rows as they are, without what the search sorts by.
    assert first["rows"][0] == {"id": "n4", **{k: v for k, v in NOTES[3].items() if k != "id"}}
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


# ---------------------------------------------------------------- kept tables
KEEPER = "a test's ledger: change it through its own routes"


def refused(write, keeper=KEEPER):
    """The detail of the Forbidden (403) ``write`` raises, which names ``keeper``."""
    import asyncio

    from core.errors import Forbidden

    with pytest.raises(Forbidden) as caught:
        asyncio.run(write())
    assert caught.value.status == 403 and keeper in str(caught.value)
    return str(caught.value)


def test_a_kept_table_is_written_by_its_keeper_alone(own_db):
    """TableStore.keep: a table named with the prefix, in any workspace, is made and changed by its keeper only.
    Every write path refuses any other writer (/db passes none) with 403 naming the keeper; the keeper's pass."""
    import asyncio

    from db.graph import GraphStore
    from db.tables import DEFAULT, TableSchema

    store = GraphStore(own_db)
    store.keep("ledger_", KEEPER)
    store._create_workspace("w")
    mine = {"owner": KEEPER}
    # Its keeper makes it and links its rows; the table beside it is anyone's.
    store._create_table("w", "ledger_rows", TableSchema(graph=True), **mine)
    asyncio.run(store.bulk_put("w", "ledger_rows", [{"id": "a"}, {"id": "b"}], **mine))
    asyncio.run(store.upsert("w", "people", {"id": "p"}))
    cites = {"type": "cites", "from": "people/p", "to": "ledger_rows/a"}
    follows = {"type": "follows", "from": "ledger_rows/a", "to": "ledger_rows/b"}
    asyncio.run(store.put_edges("w", [cites, follows], **mine))

    # In the order the keeper can run them all.
    writes = {
        "insert": lambda **o: store.insert("w", "ledger_rows", {"id": "c"}, **o),
        "upsert": lambda **o: store.upsert("w", "ledger_rows", {"id": "c", "n": 1}, **o),
        "patch": lambda **o: store.patch("w", "ledger_rows", "a", {"n": 2}, **o),
        "bulk_put": lambda **o: store.bulk_put("w", "ledger_rows", [{"id": "d"}], **o),
        "delete": lambda **o: store.delete("w", "ledger_rows", ["c"], **o),
        "transaction puts": lambda **o: store.transaction("w", puts={"ledger_rows": [{"id": "e"}]}, **o),
        "transaction deletes": lambda **o: store.transaction("w", deletes={"ledger_rows": ["e"]}, **o),
        "transaction edges": lambda **o: store.transaction("w", edges=[{**cites, "to": "ledger_rows/b"}], **o),
        "transaction delete_edges": lambda **o: store.transaction("w", delete_edges=[cites], **o),
        "put_edges": lambda **o: store.put_edges("w", [cites], **o),
        "delete_edges": lambda **o: store.delete_edges("w", [follows], **o),
        "set_schema": lambda **o: store.set_schema("w", "ledger_rows", TableSchema(strict=False), **o),
        "create_table": lambda **o: store.create_table("w", "ledger_more", TableSchema(), **o),
        "a first write": lambda **o: store.upsert(DEFAULT, "ledger_new", {"id": "x"}, **o),
        "create_edge_type": lambda **o: store.create_edge_type("w", "ledger_links", **o),
        "drop_edge_type": lambda **o: store.drop_edge_type("w", "ledger_links", **o),
        "drop_table": lambda **o: store.drop_table("w", "ledger_more", **o),
    }
    for write in writes.values():
        refused(write)
    # Nothing changed, nothing was made.
    assert [r["id"] for r in asyncio.run(store.list("w", "ledger_rows"))[0]] == ["a", "b"]
    assert len(asyncio.run(store.edges("w", "ledger_rows", "a"))[0]) == 2
    assert not store._has("w", "ledger_more") and not store._has(DEFAULT, "ledger_new")
    # A workspace that holds kept tables is not dropped, but by their keeper.
    detail = refused(lambda: store.drop_workspace("w"))
    assert "ledger_rows" in detail and "people" not in detail

    for write in writes.values():
        asyncio.run(write(**mine))
    assert [r["id"] for r in asyncio.run(store.list("w", "ledger_rows"))[0]] == ["a", "b", "d"]
    asyncio.run(store.drop_table(DEFAULT, "ledger_new", **mine))
    asyncio.run(store.drop_workspace("w", **mine))
    assert not store.has_workspace("w")

    # An edge type named like a kept table is its keeper's too, between anyone's tables: a workspace holding one
    # is refused whole, before any of its tables is dropped.
    store._create_workspace("v")
    asyncio.run(store.bulk_put("v", "people", [{"id": "a"}, {"id": "b"}]))
    asyncio.run(store.put_edges("v", [{"type": "ledger_links", "from": "people/a", "to": "people/b"}], **mine))
    assert "ledger_links" in refused(lambda: store.drop_workspace("v"))
    assert asyncio.run(store.count("v", "people")) == 2 and store._edges_of("v") == {"ledger_links"}
    asyncio.run(store.drop_workspace("v", **mine))
    assert not store.has_workspace("v")


def test_a_managed_workspace_is_written_by_its_store_alone(own_db):
    """The service's own workspaces (TableStore.manage) refuse every write in the store, not only through /db,
    but their store's; one no store of this run declares refuses them all."""
    import asyncio

    from db.graph import GraphStore
    from db.tables import UNDECLARED, TableSchema

    store = GraphStore(own_db)
    store.manage("svc", KEEPER, {"things": TableSchema(graph=True)})
    store.ensure_managed("svc")
    edge = {"type": "next", "from": "things/a", "to": "things/b"}
    writes = [
        lambda **o: store.insert("svc", "things", {"id": "x"}, **o),
        lambda **o: store.upsert("svc", "things", {"id": "x"}, **o),
        lambda **o: store.patch("svc", "things", "x", {"n": 1}, **o),
        lambda **o: store.bulk_put("svc", "things", [{"id": "x"}], **o),
        lambda **o: store.delete("svc", "things", ["x"], **o),
        lambda **o: store.transaction("svc", **o),
        lambda **o: store.put_edges("svc", [edge], **o),
        lambda **o: store.delete_edges("svc", [edge], **o),
        lambda **o: store.create_edge_type("svc", "links", **o),
        lambda **o: store.drop_edge_type("svc", "links", **o),
        lambda **o: store.create_table("svc", "more", TableSchema(), **o),
        lambda **o: store.set_schema("svc", "things", TableSchema(strict=True), **o),
        lambda **o: store.set_label("svc", "x", **o),
        lambda **o: store.drop_table("svc", "things", **o),
        lambda **o: store.drop_workspace("svc", **o),
    ]
    for write in writes:
        assert "'svc' is read-only" in refused(write)
    assert store._has("svc", "things") and not store._has("svc", "more")
    # Its store writes there.
    asyncio.run(store.bulk_put("svc", "things", [{"id": "a"}, {"id": "b"}], owner=KEEPER))
    asyncio.run(store.put_edges("svc", [edge], owner=KEEPER))
    assert asyncio.run(store.count("svc", "things")) == 2
    # A run that does not declare it has no store that may.
    later = GraphStore(own_db)
    refused(lambda: later.upsert("svc", "things", {"id": "x"}), UNDECLARED)
    refused(lambda: later.upsert("svc", "things", {"id": "x"}, owner=KEEPER), UNDECLARED)


def test_a_kept_table_is_read_with_the_admin_key_and_never_written_through_db(client, monkeypatch):
    """/db lists a kept table with its keeper (``kept``), reads it with the admin key only, and the store refuses
    every write to it there, the admin's too; the workspace's other tables stay the client's."""
    from db.tables import TableSchema

    store = app_module.app.state.tables
    monkeypatch.setitem(store.kept, "ledger_", KEEPER)
    assert client.post("/db", json={"name": "books"}).status_code == 201
    store._create_table("books", "ledger_rows", TableSchema(graph=True), owner=KEEPER)
    client.portal.call(lambda: store.bulk_put("books", "ledger_rows", [{"id": "a"}, {"id": "b"}], owner=KEEPER))
    client.portal.call(lambda: store.put_edges("books", [{"type": "next", "from": "ledger_rows/a", "to": "ledger_rows/b"}],
                                               owner=KEEPER))
    assert client.put("/db/books/notes/n1", json={"text": "x"}).status_code == 201

    tables = {t["name"]: t["kept"] for t in client.get("/db/books").json()["tables"]}
    assert tables == {"ledger_rows": KEEPER, "notes": None}
    listed = {w["name"]: w for w in client.get("/db").json()["workspaces"]}["books"]
    assert {t["name"]: t["kept"] for t in listed["tables"]} == tables

    # Read-only here, with the admin key too (no key is set yet, so every call is the admin's).
    for method, path, body in [
        ("POST", "/db/books/ledger_rows", {"id": "x"}),
        ("PUT", "/db/books/ledger_rows/a", {"n": 1}),
        ("PATCH", "/db/books/ledger_rows/a", {"n": 1}),
        ("DELETE", "/db/books/ledger_rows/a", None),
        ("PUT", "/db/books/ledger_rows/_bulk", {"rows": [{"id": "x"}]}),
        ("POST", "/db/books/ledger_rows/_bulk-delete", {"ids": ["a"]}),
        ("PUT", "/db/books/ledger_rows/_schema", {"fields": []}),
        ("DELETE", "/db/books/ledger_rows", None),
        ("POST", "/db/books", {"name": "ledger_new"}),
        ("PUT", "/db/books/ledger_new/x", {}),
        ("POST", "/db/books/_transaction", {"puts": {"notes": [{"id": "n2"}], "ledger_rows": [{"id": "x"}]}}),
        ("PUT", "/db/books/_edges", {"edges": [{"type": "cites", "from": "notes/n1", "to": "ledger_rows/a"}]}),
        ("POST", "/db/books/_edges/_delete", {"edges": [{"type": "next", "from": "ledger_rows/a", "to": "ledger_rows/b"}]}),
        ("DELETE", "/db/books", None),
    ]:
        r = client.request(method, path, json=body)
        assert r.status_code == 403 and KEEPER in r.json()["detail"], (method, path, r.text)
    assert client.get("/db/books/ledger_rows/_count").json() == {"count": 2}
    assert client.get("/db/books/notes/n2").status_code == 404
    assert "ledger_new" not in [t["name"] for t in client.get("/db/books").json()["tables"]]

    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    as_client, as_admin = {"X-API-Key": "client-key"}, {"X-API-Key": "admin-key"}
    for path in ["/db/books/ledger_rows", "/db/books/ledger_rows/_count", "/db/books/ledger_rows/_schema",
                 "/db/books/ledger_rows/a", "/db/books/ledger_rows/a/_edges", "/db/books/ledger_rows/a/_neighbors",
                 "/db/books/ledger_rows/a/_traverse", "/db/books/_graph?_table=ledger_rows"]:
        assert client.get(path, headers=as_client).status_code == 401, path
        assert client.get(path).status_code == 401, path
        assert client.get(path, headers=as_admin).status_code == 200, path
    assert client.post("/db/books/ledger_rows/_bulk-get", json={"ids": ["a"]}, headers=as_client).status_code == 401
    assert client.post("/db/books/ledger_rows/_bulk-get", json={"ids": ["a"]}, headers=as_admin).json()["rows"] == [{"id": "a"}]
    # Without the admin key a write is refused for the key first; a write that names no table, by the store.
    assert client.put("/db/books/ledger_rows/a", json={}, headers=as_client).status_code == 401
    r = client.post("/db/books/_transaction", json={"deletes": {"ledger_rows": ["a"]}}, headers=as_client)
    assert r.status_code == 403 and KEEPER in r.json()["detail"]
    # The workspace's own tables need the client key only, and the graph view leaves the kept table out unless named.
    assert client.get("/db/books/notes/n1", headers=as_client).status_code == 200
    assert client.put("/db/books/notes/n2", json={}, headers=as_client).status_code == 201
    assert client.get("/db/books/_graph", headers=as_client).json()["tables"] == ["notes"]
    assert client.get("/db/books", headers=as_client).status_code == 200
    client.portal.call(lambda: store.drop_workspace("books", owner=KEEPER))
