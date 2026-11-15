"""Ref fields: tables of a workspace joined by their rows' ids (db/refs.py)."""

import pytest
from fastapi.testclient import TestClient

from api import app as app_module
from db import refs

WS = "shop_refs"


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        yield c


def ref(name, to, **more):
    return {"name": name, "type": "ref", "to": to, **more}


def make(client, table, fields, rows=()):
    r = client.post(f"/db/{WS}", json={"name": table, "fields": fields})
    assert r.status_code == 201, r.text
    if rows:
        assert client.put(f"/db/{WS}/{table}/_bulk", json={"rows": list(rows)}).status_code == 204
    return r.json()


@pytest.fixture(scope="module")
def shop(client):
    """customers <- orders <- items -> products; orders refuse a deleted customer, items go with their order."""
    assert client.post("/db", json={"name": WS}).status_code == 201
    make(client, "customers", [{"name": "name", "type": "string"}],
         [{"id": "c1", "name": "Ada"}, {"id": "c2", "name": "Bo"}])
    make(client, "products", [{"name": "name", "type": "string"}, {"name": "price", "type": "number"}],
         [{"id": "p1", "name": "Lamp", "price": 30}, {"id": "p2", "name": "Bulb", "price": 4.5}])
    make(client, "orders", [ref("customer", "customers", required=True), {"name": "status", "type": "string"}],
         [{"id": "o1", "customer": "c1", "status": "new"}, {"id": "o2", "customer": "c1", "status": "paid"}])
    make(client, "items", [ref("order", "orders", required=True, on_delete="cascade"), ref("product", "products"),
                           {"name": "qty", "type": "integer"}],
         [{"id": "i1", "order": "o1", "product": "p1", "qty": 2}, {"id": "i2", "order": "o1", "product": "p2", "qty": 1},
          {"id": "i3", "order": "o2", "product": "p2", "qty": 5}])
    return WS


def rows(r):
    assert r.status_code == 200, r.text
    return r.json()["rows"]


def test_a_ref_field_names_a_table_of_the_workspace(client, shop):
    fields = {f["name"]: f for f in client.get(f"/db/{WS}/items/_schema").json()["fields"]}
    # A ref is indexed (a list of them cannot be), and refuses a delete unless it says otherwise.
    assert fields["order"] == {"name": "order", "type": "ref", "to": "orders", "on_delete": "cascade", "list": False,
                               "required": True, "indexed": True, "unique": False, "fulltext": False}
    assert fields["product"]["on_delete"] == "restrict"
    # Other fields show neither.
    assert "to" not in fields["qty"] and "on_delete" not in fields["qty"]
    assert client.post(f"/db/{WS}", json={"name": "files", "bucket": True}).status_code == 201
    for bad, says in [
        ({"name": "x", "type": "ref"}, "to names"),
        (ref("x", "nowhere"), "no table 'nowhere'"),
        (ref("x", "bad"), "not those of bad"),
        (ref("x", "files"), "bucket"),
        (ref("x", "customers", list=True, on_delete="cascade"), "list of refs"),
        (ref("x", "customers", required=True, on_delete="unset"), "required ref"),
        ({"name": "x", "type": "string", "to": "customers"}, "a ref field's"),
        (ref("x", "customers", fulltext=True), "full-text"),
    ]:
        r = client.post(f"/db/{WS}", json={"name": "bad", "fields": [bad]})
        assert r.status_code == 400 and says in r.json()["detail"], (bad, r.text)
    assert client.post(f"/db/{WS}", json={"name": "bad", "fields": [ref("x", "customers", on_delete="nope")]}
                       ).status_code == 422
    # Nor can a table's ref name its own rows once it is there.
    r = client.put(f"/db/{WS}/customers/_schema", json={"fields": [{"name": "name", "type": "string"}, ref("referrer", "customers")]})
    assert r.status_code == 400 and "not those of customers" in r.json()["detail"], r.text


def test_a_write_names_rows_that_are_there(client, shop):
    r = client.post(f"/db/{WS}/orders", json={"id": "o9", "customer": "nobody"})
    assert r.status_code == 400 and "customers has no row 'nobody'" in r.json()["detail"]
    r = client.post(f"/db/{WS}/orders", json={"id": "o9", "customer": 5})
    assert r.status_code == 400 and "an id of customers" in r.json()["detail"]
    assert client.put(f"/db/{WS}/orders/o2", json={"customer": "nobody", "status": "paid"}).status_code == 400
    assert client.patch(f"/db/{WS}/orders/o2", json={"customer": "nobody"}).status_code == 400
    # A bulk write is refused whole.
    r = client.put(f"/db/{WS}/orders/_bulk", json={"rows": [{"id": "o8", "customer": "c2"}, {"id": "o9", "customer": "x"}]})
    assert r.status_code == 400
    assert client.get(f"/db/{WS}/orders/o8").status_code == 404
    # Rows that refer to each other go together, in any order.
    r = client.post(f"/db/{WS}/_transaction", json={"puts": {
        "items": [{"id": "i9", "order": "o9", "product": "p1"}], "orders": [{"id": "o9", "customer": "c9"}],
        "customers": [{"id": "c9", "name": "Cy"}]}})
    assert r.status_code == 204, r.text
    assert client.patch(f"/db/{WS}/orders/o9", json={"customer": "c2"}).status_code == 200
    # A filter on a ref reads it as the id it is.
    assert [o["id"] for o in rows(client.get(f"/db/{WS}/orders?customer=c2"))] == ["o9"]


def test_a_delete_follows_the_refs_on_delete(client, shop):
    # restrict: refused while orders name the customer, and nothing is deleted.
    r = client.delete(f"/db/{WS}/customers/c9")
    assert r.status_code == 204
    r = client.delete(f"/db/{WS}/customers/c2")
    assert r.status_code == 409 and "Rows of orders (o9)" in r.json()["detail"]
    assert client.get(f"/db/{WS}/customers/c2").status_code == 200
    # cascade: an order's items go with it, and the customer can go with its orders.
    r = client.post(f"/db/{WS}/_transaction", json={"deletes": {"customers": ["c2"], "orders": ["o9"]}})
    assert r.status_code == 204, r.text
    assert client.get(f"/db/{WS}/items/i9").status_code == 404
    # restrict again, one step on: a product its items name stays.
    assert client.delete(f"/db/{WS}/products/p2").status_code == 409
    # unset: a deleted team leaves its people without one.
    make(client, "teams", [], [{"id": "red"}])
    make(client, "people", [ref("team", "teams", on_delete="unset")], [{"id": "s1", "team": "red"}])
    assert client.delete(f"/db/{WS}/teams/red").status_code == 204
    assert client.get(f"/db/{WS}/people/s1").json() == {"id": "s1"}


def test_a_list_of_refs_loses_a_deleted_id(client, shop):
    make(client, "tags", [], [{"id": "t1"}, {"id": "t2"}])
    make(client, "posts", [ref("tags", "tags", list=True, on_delete="unset")],
         [{"id": "a", "tags": ["t1", "t2"]}, {"id": "b", "tags": ["t2"]}])
    assert next(f for f in client.get(f"/db/{WS}/posts/_schema").json()["fields"])["indexed"] is False
    assert client.put(f"/db/{WS}/posts/c", json={"tags": ["t1", "t3"]}).status_code == 400
    assert client.delete(f"/db/{WS}/tags/t2").status_code == 204
    assert [p["tags"] for p in rows(client.get(f"/db/{WS}/posts"))] == [["t1"], []]
    r = client.get(f"/db/{WS}/posts/a?_expand=tags")
    assert r.json()["_expand"] == {"tags": [{"id": "t1"}]}
    r = client.get(f"/db/{WS}/tags/t1?_expand=posts:tags")
    assert [p["id"] for p in r.json()["_expand"]["posts:tags"]] == ["a"]


def test_a_table_a_ref_names_is_dropped_last(client, shop):
    make(client, "a", [], [{"id": "1"}])
    make(client, "b", [ref("a", "a")], [{"id": "1", "a": "1"}])
    r = client.delete(f"/db/{WS}/a")
    assert r.status_code == 409 and "b.a refer to a" in r.json()["detail"]
    assert client.delete(f"/db/{WS}/b").status_code == 204
    assert client.delete(f"/db/{WS}/a").status_code == 204


def test_a_schema_change_checks_the_ids_there_now(client, shop):
    make(client, "notes", [], [{"id": "n1", "customer": "c1"}, {"id": "n2", "customer": "gone"}])
    r = client.put(f"/db/{WS}/notes/_schema", json={"fields": [ref("customer", "customers")]})
    assert r.status_code == 400 and "n2 (customer: customers has no row 'gone')" in r.json()["detail"]
    assert client.get(f"/db/{WS}/notes/_schema").json()["fields"] == []
    assert client.delete(f"/db/{WS}/notes/n2").status_code == 204
    assert client.put(f"/db/{WS}/notes/_schema", json={"fields": [ref("customer", "customers")]}).status_code == 200


def test_expand_follows_refs_both_ways(client, shop):
    r = client.get(f"/db/{WS}/items?_expand=order.customer,product")
    got = {i["id"]: i for i in rows(r)}
    assert got["i1"]["order"] == "o1"  # the field keeps its id
    assert got["i1"]["_expand"]["product"] == {"id": "p1", "name": "Lamp", "price": 30}
    assert got["i1"]["_expand"]["order"]["_expand"]["customer"]["name"] == "Ada"
    r = client.get(f"/db/{WS}/orders/o1?_expand=customer,items:order.product")
    order = r.json()
    assert order["_expand"]["customer"] == {"id": "c1", "name": "Ada"}
    assert [(i["id"], i["_expand"]["product"]["name"]) for i in order["_expand"]["items:order"]] == [
        ("i1", "Lamp"), ("i2", "Bulb")]
    assert "_expand_more" not in order
    back = client.get(f"/db/{WS}/customers/c1?_expand=orders:customer").json()["_expand"]["orders:customer"]
    assert [o["id"] for o in back] == ["o1", "o2"]
    # A row nothing names has an empty list back, and no key for a ref it does not hold.
    assert client.post(f"/db/{WS}/customers", json={"id": "c5"}).status_code == 201
    assert client.post(f"/db/{WS}/orders", json={"id": "o5", "customer": "c5"}).status_code == 201
    assert client.post(f"/db/{WS}/items", json={"id": "i5", "order": "o5"}).status_code == 201
    assert client.get(f"/db/{WS}/customers/c5?_expand=orders:customer.items:order").json()["_expand"] == {
        "orders:customer": [{"id": "o5", "customer": "c5", "_expand": {"items:order": [
            {"id": "i5", "order": "o5"}]}}]}
    assert client.get(f"/db/{WS}/items/i5?_expand=product").json()["_expand"] == {}
    assert client.get(f"/db/{WS}/products/p1?_expand=items:product").json()["_expand"]["items:product"][0]["id"] == "i1"
    # Without _expand, rows are as they were.
    assert "_expand" not in client.get(f"/db/{WS}/items/i5").json()
    # Deleting an order directly takes its items too.
    assert client.delete(f"/db/{WS}/orders/o5").status_code == 204
    assert client.get(f"/db/{WS}/items/i5").status_code == 404


def test_expand_tells_a_list_it_cut(client, shop, monkeypatch):
    monkeypatch.setattr(refs, "BACK_LIMIT", 1)
    order = client.get(f"/db/{WS}/orders/o1?_expand=items:order").json()
    assert [i["id"] for i in order["_expand"]["items:order"]] == ["i1"]
    assert order["_expand_more"] == ["items:order"]
    monkeypatch.setattr(refs, "EXPAND_MAX", 1)
    r = client.get(f"/db/{WS}/items?_expand=order,product")
    assert r.status_code == 413


@pytest.mark.parametrize("expand, says", [
    ("qty", "items expands order, product"),
    ("nope:order", "items expands order, product"),
    ("orders:customer", "items expands"),
    ("order.customer.name.x", "3 steps at most"),
    ("bad__name:x", "each step"),
])
def test_expand_refuses_what_is_not_a_ref(client, shop, expand, says):
    r = client.get(f"/db/{WS}/items", params={"_expand": expand})
    assert r.status_code == 400 and says in r.json()["detail"], r.text


def test_a_delete_and_a_write_naming_its_row_never_both_commit(own_db):
    """Without the ref lock each would check before the other commits, and an order would name a customer gone."""
    from concurrent.futures import ThreadPoolExecutor

    from core.errors import Conflict
    from db.graph import GraphStore
    from db.tables import Field, TableError, make_schema

    store = GraphStore(own_db)
    store._create_workspace("w")
    store._create_table("w", "customers")
    store._create_table("w", "orders", make_schema([Field("customer", "ref", to="customers")]))
    for i in range(20):
        store._insert("w", "customers", {"id": f"c{i}"})

    def order(i):
        try:
            store._insert("w", "orders", {"id": f"o{i}", "customer": f"c{i}"})
        except TableError:
            pass

    def delete(i):
        try:
            store._transaction("w", deletes={"customers": [f"c{i}"]})
        except Conflict:
            pass

    with ThreadPoolExecutor(8) as pool:
        for i in range(20):
            pool.submit(order, i)
            pool.submit(delete, i)
    customers = {r["id"] for r in store._list("w", "customers", limit=100)[0]}
    orders = store._list("w", "orders", limit=100)[0]
    assert all(o["customer"] in customers for o in orders)
    assert len(orders) + (20 - len(customers)) == 20


def test_a_workspace_with_refs_is_dropped_whole(client):
    assert client.post("/db", json={"name": "refs_drop"}).status_code == 201
    assert client.post("/db/refs_drop", json={"name": "a"}).status_code == 201
    assert client.post("/db/refs_drop", json={"name": "b", "fields": [ref("a", "a")]}).status_code == 201
    assert client.delete("/db/refs_drop").status_code == 204
