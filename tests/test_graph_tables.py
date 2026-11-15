"""Graph tables of /db: rows joined by edges, read back by neighbors, traversal and queries."""

import pytest
from fastapi.testclient import TestClient


from api import app as app_module  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        yield c


def edge(type_, from_, to, **props):
    return {"type": type_, "from": from_, "to": to, **props}


@pytest.fixture(scope="module")
def crm(client):
    """people and companies are graph tables, as tables are unless made plain; notes is a plain one."""
    assert client.post("/db", json={"name": "crm"}).status_code == 201
    r = client.post("/db/crm", json={"name": "people", "graph": True, "fields": [{"name": "name", "type": "string"}]})
    assert r.status_code == 201, r.text
    assert r.json()["graph"] is True
    assert client.post("/db/crm", json={"name": "companies"}).status_code == 201
    assert client.post("/db/crm", json={"name": "notes", "graph": False}).status_code == 201
    assert client.post("/db/crm/_transaction", json={"puts": {
        "people": [{"id": "ann", "name": "Ann"}, {"id": "bob", "name": "Bob"}, {"id": "cy", "name": "Cy"}],
        "companies": [{"id": "acme", "name": "Acme"}],
        "notes": [{"id": "n1", "text": "call"}],
    }}).status_code == 204
    assert client.put("/db/crm/_edges", json={"edges": [
        edge("knows", "people/ann", "people/bob", since=2020),
        edge("knows", "people/bob", "people/cy"),
        edge("works_at", "people/ann", "companies/acme", role="CTO"),
        edge("works_at", "people/cy", "companies/acme"),
    ]}).status_code == 204
    return "crm"


def test_graph_is_chosen_at_creation_and_listed(client, crm):
    ns = client.get("/db/crm").json()
    tables = {t["name"]: t for t in ns["tables"]}
    assert tables["people"]["graph"] is True and tables["notes"]["graph"] is False
    assert [(e["name"], e["count"]) for e in ns["edges"]] == [("knows", 2), ("works_at", 2)]
    assert ns["size_bytes"] == sum(t["size_bytes"] for t in ns["tables"]) + sum(e["size_bytes"] for e in ns["edges"])
    # _edges adds the tables each type joins, which the workspace's description leaves out.
    assert "joins" not in ns["edges"][0]
    edges = client.get("/db/crm/_edges").json()["edges"]
    assert [{k: v for k, v in e.items() if k != "joins"} for e in edges] == ns["edges"]
    assert [e["joins"] for e in edges] == [[{"from": "people", "to": "people", "count": 2}],
                                           [{"from": "people", "to": "companies", "count": 2}]]
    # Rows of a graph table read and write like any other.
    assert client.get("/db/crm/people/ann").json() == {"id": "ann", "name": "Ann"}
    assert client.get("/db/crm/people?name=Bob").json()["rows"] == [{"id": "bob", "name": "Bob"}]
    assert client.get("/db/crm/people/_schema").json()["graph"] is True
    # A schema change keeps the kind: PUT _schema carries no graph flag.
    r = client.put("/db/crm/people/_schema", json={"fields": [{"name": "name", "type": "string", "indexed": True}]})
    assert r.status_code == 200 and r.json()["graph"] is True
    r = client.put("/db/crm/notes/_schema", json={"fields": []})
    assert r.status_code == 200 and r.json()["graph"] is False
    # A table made by its first write is a graph table, as one made by PUT _schema is.
    assert client.put("/db/crm/tasks/t1", json={"x": 1}).status_code == 201
    assert client.put("/db/crm/tags/_schema", json={"fields": []}).status_code == 200
    kinds = {t["name"]: t["graph"] for t in client.get("/db/crm").json()["tables"]}
    assert kinds == {"companies": True, "notes": False, "people": True, "tags": True, "tasks": True}
    # Gone again, for the tests after this one to draw the graph of people and companies alone.
    assert client.delete("/db/crm/tasks").status_code == 204 and client.delete("/db/crm/tags").status_code == 204


def test_edges_join_rows_of_graph_tables_only(client, crm):
    bad = [
        (edge("knows", "people/ann", "notes/n1"), "not a graph table"),
        (edge("knows", "people/ann", "people/nobody"), "No row 'nobody'"),
        (edge("knows", "people/ann", "nothing/x"), "No table 'nothing'"),
        (edge("knows", "ann", "people/bob"), "Bad row reference"),
        (edge("bad__type", "people/ann", "people/bob"), "Bad edge type name"),
        ({"from": "people/ann", "to": "people/bob"}, "needs 'type'"),
        (edge("knows", "people/ann", "people/bob", **{"@x": 1}), "reserved"),
    ]
    for e, why in bad:
        r = client.put("/db/crm/_edges", json={"edges": [e]})
        assert r.status_code == 400 and why in r.json()["detail"], (e, r.text)
    # All or none: the good edge before the bad one is not stored.
    r = client.put("/db/crm/_edges", json={"edges": [edge("knows", "people/cy", "people/ann"), bad[0][0]]})
    assert r.status_code == 400
    assert client.get("/db/crm/people/cy/_edges?_type=knows&_direction=out").json() == {"edges": []}
    assert client.put("/db/nope/_edges", json={"edges": [edge("knows", "people/ann", "people/bob")]}).status_code == 404
    # An edge type's first edge cannot give it a table's name.
    assert client.put("/db/crm/_edges", json={"edges": [edge("notes", "people/ann", "people/bob")]}).status_code == 409


def test_a_rows_edges_and_neighbors(client, crm):
    r = client.get("/db/crm/people/ann/_edges")
    assert r.status_code == 200, r.text
    assert sorted(r.json()["edges"], key=lambda e: e["type"]) == [
        {"type": "knows", "from": "people/ann", "to": "people/bob", "since": 2020},
        {"type": "works_at", "from": "people/ann", "to": "companies/acme", "role": "CTO"},
    ]
    assert client.get("/db/crm/people/bob/_edges?_direction=in").json()["edges"] == [
        {"type": "knows", "from": "people/ann", "to": "people/bob", "since": 2020}]
    assert client.get("/db/crm/people/bob/_edges?_direction=out&_type=knows").json()["edges"] == [
        {"type": "knows", "from": "people/bob", "to": "people/cy"}]
    assert client.get("/db/crm/people/bob/_edges?_type=works_at").json()["edges"] == []
    # Pages, like the row list.
    first = client.get("/db/crm/companies/acme/_neighbors?_limit=1").json()
    assert len(first["neighbors"]) == 1 and first["next"]
    second = client.get(f"/db/crm/companies/acme/_neighbors?_limit=1&_cursor={first['next']}").json()
    assert len(second["neighbors"]) == 1 and "next" not in second
    assert {n["row"]["id"] for n in first["neighbors"] + second["neighbors"]} == {"ann", "cy"}
    assert all(n["table"] == "people" and n["edge"]["type"] == "works_at" for n in first["neighbors"])
    # The neighbor is the other end, whichever direction the edge goes.
    both = client.get("/db/crm/people/bob/_neighbors").json()["neighbors"]
    assert sorted(n["row"]["id"] for n in both) == ["ann", "cy"]
    assert next(n for n in both if n["row"]["id"] == "ann")["edge"]["from"] == "people/ann"
    for path, status in [("/db/crm/notes/n1/_edges", 404), ("/db/crm/people/zed/_edges", 404),
                         ("/db/crm/people/ann/_edges?_type=likes", 404), ("/db/crm/people/ann/_edges?_direction=up", 422),
                         ("/db/crm/people/ann/_edges?name=x", 400), ("/db/crm/people/ann/_neighbors?_cursor=zz", 400)]:
        assert client.get(path).status_code == status, path


def test_traverse_reaches_rows_nearest_first(client, crm):
    r = client.get("/db/crm/people/ann/_traverse?_type=knows&_depth=3")
    assert r.status_code == 200, r.text
    assert [(x["table"], x["row"]["id"], x["depth"]) for x in r.json()["rows"]] == [
        ("people", "bob", 1), ("people", "cy", 2)]
    rows = client.get("/db/crm/people/ann/_traverse?_depth=1").json()["rows"]
    assert sorted((x["table"], x["row"]["id"]) for x in rows) == [("companies", "acme"), ("people", "bob")]
    # Both ways, every type: everyone is within two steps of Acme.
    rows = client.get("/db/crm/companies/acme/_traverse?_direction=both&_depth=2").json()["rows"]
    assert sorted((x["row"]["id"], x["depth"]) for x in rows) == [("ann", 1), ("bob", 2), ("cy", 1)]
    assert client.get("/db/crm/companies/acme/_traverse?_direction=in&_depth=1&_limit=1").json()["rows"][0]["depth"] == 1
    assert client.get("/db/crm/people/ann/_traverse?_depth=0").status_code == 422
    assert client.get("/db/crm/people/ann/_traverse?_cursor=x").status_code == 400


def test_graph_view_loads_rows_and_the_edges_among_them(client, crm):
    r = client.get("/db/crm/_graph")
    assert r.status_code == 200, r.text
    g = r.json()
    assert g["tables"] == ["companies", "people"]
    assert sorted((n["table"], n["row"]["id"]) for n in g["nodes"]) == [
        ("companies", "acme"), ("people", "ann"), ("people", "bob"), ("people", "cy")]
    assert sorted((e["type"], e["from"], e["to"]) for e in g["edges"]) == [
        ("knows", "people/ann", "people/bob"), ("knows", "people/bob", "people/cy"),
        ("works_at", "people/ann", "companies/acme"), ("works_at", "people/cy", "companies/acme")]
    assert next(e for e in g["edges"] if e["from"] == "people/ann" and e["type"] == "knows")["since"] == 2020
    assert g["outside"] == 0 and g["more"] == {"nodes": False, "edges": False}
    # One table, one edge type: edges to rows not loaded are counted, not returned.
    g = client.get("/db/crm/_graph?_table=people&_type=works_at").json()
    assert g["tables"] == ["people"] and g["edges"] == [] and g["outside"] == 2
    # The node budget is shared among the tables; a cut is reported.
    g = client.get("/db/crm/_graph?_limit=2").json()
    assert [(n["table"], n["row"]["id"]) for n in g["nodes"]] == [("companies", "acme"), ("people", "ann")]
    assert g["more"]["nodes"] is True and g["outside"] == 1
    # A table with fewer rows than its share leaves the rest to the others.
    g = client.get("/db/crm/_graph?_limit=3").json()
    assert [(n["table"], n["row"]["id"]) for n in g["nodes"]] == [("companies", "acme"), ("people", "ann"), ("people", "bob")]
    g = client.get("/db/crm/_graph?_edge_limit=1").json()
    assert len(g["edges"]) == 1 and g["more"]["edges"] is True
    for path, status in [("/db/crm/_graph?_table=notes", 400), ("/db/crm/_graph?_table=nothing", 404),
                         ("/db/crm/_graph?_type=likes", 404), ("/db/crm/_graph?name=x", 400),
                         ("/db/crm/_graph?_limit=0", 422), ("/db/nope/_graph", 404)]:
        assert client.get(path).status_code == status, path


def test_queries_read_the_workspace_s_tables_and_edges_in_sql(client, crm):
    def query(q, **params):
        r = client.post("/db/crm/_query", json={"query": q, "params": params})
        assert r.status_code == 200, r.text
        return r.json()["rows"]

    assert query("SELECT name FROM people WHERE id = $id", id="ann") == [{"name": "Ann"}]
    # The edges are rows of _edges: the type, the two ends, and the edge's properties as JSON.
    assert query("SELECT p.id, c._rest->>'name' AS at FROM people p JOIN _edges e ON e.from_table = 'people' AND e.from_id = p.id "
                 "AND e.type = 'works_at' JOIN companies c ON c.id = e.to_id WHERE p.name = $n", n="Ann") == [
        {"id": "ann", "at": "Acme"}]
    assert sorted(r["from_id"] for r in query("SELECT from_id FROM _edges WHERE type = 'works_at' AND to_id = 'acme'")) \
        == ["ann", "cy"]
    assert query("SELECT a.from_id AS a, b.to_id AS c FROM _edges a JOIN _edges b ON b.from_id = a.to_id "
                 "WHERE a.type = 'knows' AND b.type = 'knows' AND a.from_id = 'ann'") == [{"a": "ann", "c": "cy"}]
    assert query("SELECT props->>'role' AS role FROM _edges WHERE type = 'works_at' AND from_id = 'ann'") == [
        {"role": "CTO"}]
    # A walk, as the traversal route takes it a step at a time.
    rows = query("WITH RECURSIVE walk(t, i, d) AS (SELECT 'people', $id, 0 UNION SELECT e.to_table, e.to_id, d + 1 "
                 "FROM walk JOIN _edges e ON e.from_table = walk.t AND e.from_id = walk.i WHERE d < 2) "
                 "SELECT t, i FROM walk WHERE d > 0 ORDER BY t, i", id="bob")
    assert [(r["t"], r["i"]) for r in rows] == [("companies", "acme"), ("people", "cy")]
    # A parameter called like a table is a value.
    assert query("SELECT name FROM people WHERE id = $people", people="ann") == [{"name": "Ann"}]
    # Read-only, limited, and a bad query is the client's error.
    r = client.post("/db/crm/_query", json={"query": "DELETE FROM people"})
    assert r.status_code == 400 and "only a SELECT" in r.json()["detail"]
    # A WITH whose part writes is a SELECT to DuckDB's parser: it is refused as it runs, here or in the service's
    # own tables, and nothing is written.
    for writes in ["WITH w AS (INSERT INTO people (id, _seq) VALUES ('eve', 0) RETURNING id) SELECT * FROM w",
                   "WITH d AS (DELETE FROM people RETURNING id) SELECT * FROM d",
                   "WITH u AS (UPDATE main.tk_table SET strict = true RETURNING name) SELECT * FROM u",
                   "SELECT nextval('main.tk_seq')"]:
        r = client.post("/db/crm/_query", json={"query": writes})
        assert r.status_code == 400 and "only reads run here" in r.json()["detail"], r.text
    assert client.get("/db/crm/people/_count").json() == {"count": 3}
    assert client.get("/db/crm/people/_schema").json()["strict"] is False
    assert client.post("/db/crm/_query", json={"query": "SELECT FROM"}).status_code == 400
    assert len(client.post("/db/crm/_query", json={"query": "SELECT * FROM people", "limit": 2}).json()["rows"]) == 2
    assert client.post("/db/nope/_query", json={"query": "SELECT 1"}).status_code == 404


def test_edges_are_replaced_deleted_and_go_with_their_rows(client, crm):
    # One edge of a type per pair: putting it again replaces its properties.
    assert client.put("/db/crm/_edges", json={"edges": [edge("knows", "people/ann", "people/bob", since=2021)]}).status_code == 204
    assert client.get("/db/crm/people/ann/_edges?_type=knows").json()["edges"] == [
        {"type": "knows", "from": "people/ann", "to": "people/bob", "since": 2021}]
    # Replacing a row keeps its edges.
    assert client.put("/db/crm/people/ann", json={"name": "Anne"}).status_code == 200
    assert len(client.get("/db/crm/people/ann/_edges").json()["edges"]) == 2
    # A transaction stores rows and the edges between them, and deletes edges before rows.
    assert client.post("/db/crm/_transaction", json={
        "puts": {"people": [{"id": "dee", "name": "Dee"}]},
        "edges": [edge("knows", "people/dee", "people/ann")],
        "delete_edges": [edge("knows", "people/bob", "people/cy"), edge("knows", "people/zz", "people/ann")],
    }).status_code == 204
    assert client.get("/db/crm/people/bob/_edges?_direction=out").json()["edges"] == []
    assert client.get("/db/crm/people/ann/_edges?_direction=in").json()["edges"] == [
        {"type": "knows", "from": "people/dee", "to": "people/ann"}]
    assert client.post("/db/crm/_edges/_delete", json={"edges": [edge("knows", "people/dee", "people/ann")]}).status_code == 204
    assert client.get("/db/crm/people/ann/_edges?_direction=in").json()["edges"] == []
    # Deleting a row deletes its edges.
    assert client.delete("/db/crm/people/ann").status_code == 204
    assert client.get("/db/crm/companies/acme/_neighbors").json()["neighbors"][0]["row"]["id"] == "cy"
    assert {e["name"]: (e["count"], e["joins"]) for e in client.get("/db/crm/_edges").json()["edges"]} == {
        "knows": (0, []), "works_at": (1, [{"from": "people", "to": "companies", "count": 1}])}
    # Dropping an edge type (admin) and a graph table take their edges along.
    assert client.delete("/db/crm/_edges/knows").status_code == 204
    assert client.delete("/db/crm/_edges/knows").status_code == 404
    assert client.delete("/db/crm/companies").status_code == 204
    assert {e["name"]: e["count"] for e in client.get("/db/crm/_edges").json()["edges"]} == {"works_at": 0}
    assert client.get("/db/crm/people/cy/_edges").json()["edges"] == []
    assert client.delete("/db/crm").status_code == 204
    assert client.get("/db/crm/_edges").status_code == 404
    assert "crm" not in {n["name"] for n in client.get("/db").json()["workspaces"]}


def test_graph_tables_survive_a_reload(client):
    assert client.post("/db", json={"name": "g"}).status_code == 201
    assert client.post("/db/g", json={"name": "nodes", "graph": True}).status_code == 201
    assert client.put("/db/g/nodes/_bulk", json={"rows": [{"id": "a"}, {"id": "b"}]}).status_code == 204
    assert client.put("/db/g/_edges", json={"edges": [edge("to", "nodes/a", "nodes/b")]}).status_code == 204
    store = client.app.state.tables
    store._schemas = None
    assert client.get("/db/g").json()["tables"][0]["graph"] is True
    assert client.get("/db/g").json()["edges"] == [{"name": "to", "count": 1, "size_bytes": pytest.approx(0, abs=10 ** 9)}]
    assert client.get("/db/g/nodes/a/_neighbors").json()["neighbors"][0]["row"] == {"id": "b"}
    assert client.delete("/db/g").status_code == 204


def test_graph_writes_need_the_client_key_and_drops_the_admin_key(client, monkeypatch):
    from core.config import Config

    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    as_client = {"X-API-Key": "client-key"}
    assert client.put("/db/x/_edges", json={"edges": []}).status_code == 401
    assert client.delete("/db/x/_edges/t", headers=as_client).status_code == 401
    assert client.put("/db/x/_edges", json={"edges": []}, headers=as_client).status_code == 404
    # Making an edge type is the admin's, as making a table is.
    assert client.post("/db/x/_edges", json={"name": "t"}, headers=as_client).status_code == 401


def test_a_new_table_is_a_graph_table_unless_made_plain_or_kept_by_the_service(client):
    assert client.post("/db", json={"name": "kinds"}).status_code == 201
    made = client.post("/db/kinds", json={"name": "made"}).json()
    plain = client.post("/db/kinds", json={"name": "plain", "graph": False}).json()
    assert made["graph"] is True and plain["graph"] is False
    # Both are stored the same way; only a graph table's rows take edges.
    for table, row in [("made", "r1"), ("made", "r2"), ("plain", "r1"), ("plain", "r2")]:
        assert client.put(f"/db/kinds/{table}/{row}", json={}).status_code == 201
    assert client.put("/db/kinds/_edges", json={"edges": [edge("next", "made/r1", "made/r2")]}).status_code == 204
    assert client.put("/db/kinds/_edges", json={"edges": [edge("next", "plain/r1", "plain/r2")]}).status_code == 400
    # A client's first write makes one too, and so does a schema put before any row.
    assert client.put("/db/kinds/written/w1", json={}).status_code == 201
    assert client.put("/db/kinds/declared/_schema", json={"fields": []}).json()["graph"] is True
    assert client.post("/db/kinds/_transaction", json={"puts": {"batch": [{"id": "b1"}]}}).status_code == 204
    graph = {t["name"]: t["graph"] for t in client.get("/db/kinds").json()["tables"]}
    assert graph == {"batch": True, "declared": True, "made": True, "plain": False, "written": True}
    # The service's own stores keep the plain tables they declare.
    assert not any(t["graph"] for t in client.get("/db/tasks").json()["tables"])
    assert client.delete("/db/kinds").status_code == 204


def test_a_query_reads_its_workspace_s_tables_by_their_names(client):
    """A bare name is a table of the query's workspace; a field or a value called like a table is what it is. A
    table's undeclared fields are in _rest, as JSON."""
    assert client.post("/db", json={"name": "names"}).status_code == 201
    assert client.post("/db/names/_transaction", json={"puts": {
        "person": [{"id": "ann", "company": "acme", "tags": ["x"], "knows": 1}, {"id": "bob", "company": "company"}],
        "company": [{"id": "acme", "name": "Acme"}], "limit": [{"id": "l1"}]}}).status_code == 204

    def query(q):
        r = client.post("/db/names/_query", json={"query": q})
        assert r.status_code == 200, (q, r.text)
        return r.json()["rows"]

    assert query("SELECT id FROM person WHERE _rest->>'company' = 'acme'") == [{"id": "ann"}]
    assert query("SELECT id FROM person WHERE _rest->>'company' = 'company'") == [{"id": "bob"}]
    assert query("SELECT _rest->'tags' AS tags, (_rest->>'knows')::INTEGER AS knows FROM person WHERE id = 'ann'") == [
        {"tags": ["x"], "knows": 1}]
    # A word of SQL as a table's name, quoted.
    assert query('SELECT id FROM "limit"') == [{"id": "l1"}]
    assert client.delete("/db/names").status_code == 204


def test_a_failed_write_leaves_no_table_or_relationship_it_made(client):
    """Tables and edge types are made before a write's transaction, and dropped again when it fails."""
    assert client.post("/db", json={"name": "undo"}).status_code == 201
    assert client.post("/db/undo/_transaction", json={"puts": {"people": [{"id": "a"}, {"id": "b"}]}}).status_code == 204
    # The second edge's end is missing: the batch fails, and the edge type it made goes too.
    r = client.put("/db/undo/_edges", json={"edges": [edge("likes", "people/a", "people/b"), edge("likes", "people/a", "people/zzz")]})
    assert r.status_code == 400, r.text
    # So do a table and an edge type a failed transaction made.
    r = client.post("/db/undo/_transaction", json={"puts": {"pets": [{"id": "rex"}]},
                                                    "edges": [edge("owns", "people/a", "pets/rex"), edge("owns", "people/b", "pets/nope")]})
    assert r.status_code == 400, r.text
    ns = client.get("/db/undo").json()
    assert [t["name"] for t in ns["tables"]] == ["people"] and ns["edges"] == []
    # One that was there before stays, even empty.
    assert client.post("/db/undo/_edges", json={"name": "likes"}).status_code == 201
    assert client.put("/db/undo/_edges", json={"edges": [edge("likes", "people/a", "people/zzz")]}).status_code == 400
    assert [e["name"] for e in client.get("/db/undo").json()["edges"]] == ["likes"]
    assert client.delete("/db/undo").status_code == 204


def test_an_edge_type_can_be_made_before_its_first_edge(client):
    assert client.post("/db", json={"name": "made"}).status_code == 201
    assert client.post("/db/made", json={"name": "people", "graph": True}).status_code == 201
    r = client.post("/db/made/_edges", json={"name": "knows"})
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "knows" and r.json()["count"] == 0
    assert [(e["name"], e["count"], e["joins"]) for e in client.get("/db/made/_edges").json()["edges"]] == [("knows", 0, [])]
    assert client.post("/db/made/_edges", json={"name": "knows"}).status_code == 409
    # A query reads a bare name as a table or an edge type, so one name is not both, however it is made.
    assert client.post("/db/made/_edges", json={"name": "people"}).status_code == 409
    assert client.post("/db/made", json={"name": "knows"}).status_code == 409
    assert client.put("/db/made/knows/k1", json={}).status_code == 409
    assert client.post("/db/made/_edges", json={"name": "two__under"}).status_code == 400
    assert client.post("/db/made/_edges", json={"name": "knows", "extra": 1}).status_code == 422
    assert client.post("/db/nope/_edges", json={"name": "knows"}).status_code == 404
    # Its first edge goes in as into any edge type.
    assert client.put("/db/made/people/a", json={}).status_code == 201
    assert client.put("/db/made/people/b", json={}).status_code == 201
    assert client.put("/db/made/_edges", json={"edges": [edge("knows", "people/a", "people/b")]}).status_code == 204
    assert client.get("/db/made").json()["edges"][0]["count"] == 1
    assert client.delete("/db/made").status_code == 204


def test_queries_need_the_admin_key_since_they_can_reach_other_workspaces(client, monkeypatch):
    """A query reads its workspace's tables by their bare names, but SQL can name any schema."""
    from core.config import Config

    assert client.post("/db", json={"name": "other"}).status_code == 201
    assert client.put("/db/other/secret/s1", json={"token": "hunter2"}).status_code == 201
    assert client.post("/db", json={"name": "mine"}).status_code == 201
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    body = {"query": "SELECT id, _rest FROM other.secret"}
    assert client.post("/db/mine/_query", json=body).status_code == 401
    assert client.post("/db/mine/_query", json=body, headers={"X-API-Key": "client-key"}).status_code == 401
    as_admin = {"X-API-Key": "admin-key"}
    r = client.post("/db/mine/_query", json=body, headers=as_admin)
    assert r.status_code == 200 and r.json()["rows"] == [{"id": "s1", "_rest": {"token": "hunter2"}}]
    assert client.post("/db/nope/_query", json={"query": "SELECT 1"}, headers=as_admin).status_code == 404
    monkeypatch.undo()
    assert client.delete("/db/mine").status_code == 204 and client.delete("/db/other").status_code == 204


def test_dropping_a_table_deletes_its_edges_a_batch_at_a_time(client, monkeypatch):
    """A graph table's rows go before its type, a batch per transaction, so no edge is left dangling."""
    from db import ddl

    monkeypatch.setattr(ddl, "SCAN_BATCH", 2)
    assert client.post("/db", json={"name": "dropping"}).status_code == 201
    people = [{"id": f"p{i}"} for i in range(5)]
    assert client.post("/db/dropping/_transaction", json={
        "puts": {"people": people, "companies": [{"id": "acme"}]},
        "edges": [edge("works_at", f"people/{p['id']}", "companies/acme") for p in people]
        + [edge("knows", "people/p0", "people/p1")]}).status_code == 204
    assert client.delete("/db/dropping/people").status_code == 204
    assert client.get("/db/dropping/companies/acme/_edges").json() == {"edges": []}
    assert [(e["name"], e["count"]) for e in client.get("/db/dropping").json()["edges"]] == [("knows", 0), ("works_at", 0)]
    # Without edge types, no row has an edge: the type is dropped at once.
    assert client.post("/db/dropping", json={"name": "loose"}).status_code == 201
    assert client.put("/db/dropping/loose/l1", json={}).status_code == 201
    assert client.delete("/db/dropping/_edges/knows").status_code == 204
    assert client.delete("/db/dropping/_edges/works_at").status_code == 204
    assert client.delete("/db/dropping/loose").status_code == 204
    assert [t["name"] for t in client.get("/db/dropping").json()["tables"]] == ["companies"]
    assert client.delete("/db/dropping").status_code == 204
