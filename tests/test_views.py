"""Views and dashboards (views/, api/view_routes.py): kept beside a workspace, checked when saved, run with their
params bound, within their time limit and row cap, and kept for their refresh interval; the admin writes them and a
client key runs them."""

import asyncio
import math
import re
import time

import pytest
from fastapi.testclient import TestClient


from api import app as app_module  # noqa: E402
from core.config import Config  # noqa: E402
from db.schema import MAX_K  # noqa: E402
from views import Views  # noqa: E402
from tests.fakes import FakeModel, registered  # noqa: E402
from tests.test_vectors import ShortReranker  # noqa: E402

SALES = [{"id": f"s{i}", "region": ["north", "south", "east"][i % 3], "total": i, "note": "plain"} for i in range(10)]
# A table that a query reading every triple of its rows takes seconds over (15 million: 3 to 6 s without a limit,
# measured), and a small time limit stops.
SLOW_ROWS = 250
LIMIT_MS = 100
# Every five of the rows: about 10^12, minutes of work (four take 0.9 s, measured).
CROSS = "SELECT count(*) AS n FROM grid a, grid b, grid c, grid d, grid e"
NESTED = ("SELECT count(*) AS n FROM grid g WHERE (SELECT count(*) FROM grid a, grid b, grid c WHERE a.x < g.x AND "
          "b.x > a.x AND c.x <> b.x) >= 0")
# Rows each slow to make: the limit holds while the result is read too.
STREAMED = ("SELECT g.x, (SELECT count(*) FROM grid a, grid b, grid c WHERE a.x < g.x AND b.x > a.x AND c.x <> b.x) AS c "
            "FROM grid g")
# The sales' fields, declared, so the views' SQL reads them as columns (a table's undeclared fields are in _rest).
SALES_FIELDS = [{"name": "region", "type": "string"}, {"name": "total", "type": "integer"},
                {"name": "note", "type": "string"}]


def fill_sales(client, ws, rows=SALES):
    assert client.put(f"/db/{ws}/sales/_schema", json={"fields": SALES_FIELDS}).status_code == 200
    assert client.put(f"/db/{ws}/sales/_bulk", json={"rows": rows}).status_code == 204


def unit(*v):
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


class TopicEmbedder(FakeModel):
    """Embeds a text by whether it says cat or dog, so nearness is predictable."""

    async def predict(self, request):
        return {"embeddings": [unit(t.count("cat") + 0.1, t.count("dog") + 0.1) for t in request.texts], "tokens": 1}


@pytest.fixture(scope="module")
def client():
    with registered(TopicEmbedder, task="embedding", match="topic:*"), TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def ws(client, request):
    """A workspace of the test's own holding sales; dropped after it."""
    words = re.sub(r"[^A-Za-z0-9]+", "_", request.node.name.removeprefix("test_")).strip("_")
    # Its end, so the parametrized ones differ.
    name = "vw_" + words[-44:].lstrip("_")
    assert client.post("/db", json={"name": name}).status_code == 201
    fill_sales(client, name)
    yield name
    client.delete(f"/db/{name}")


@pytest.fixture
def views():
    return app_module.app.state.views


def sql_view(name="by_region", query="SELECT region, sum(total) AS total FROM sales GROUP BY region ORDER BY region",
             params=None, **extra):
    return {"name": name, "source": {"kind": "sql", "query": query},
            "params": params or {"type": "object", "properties": {}}, **extra}


def put(client, ws, spec, status=200, kind="_views"):
    r = client.put(f"/db/{ws}/{kind}/{spec['name']}", json=spec)
    assert r.status_code == status, r.text
    return r.json()


def run(client, ws, name, status=200, **params):
    r = client.post(f"/db/{ws}/_views/{name}", json={"params": params})
    assert r.status_code == status, r.text
    return r.json()


@pytest.fixture
def counted(views, monkeypatch):
    """How many times the views asked the database: each call of the table store's reads they use."""
    calls = []
    tables = views.tables
    for method in ("timed_read", "list", "search"):
        real = getattr(tables, method)

        async def call(*args, _real=real, _method=method, **kwargs):
            calls.append(_method)
            return await _real(*args, **kwargs)

        monkeypatch.setattr(tables, method, call)
    return calls


class Clock:
    """A clock a test moves on by hand."""

    def __init__(self):
        self.now = 1_000_000.0

    def __call__(self):
        return self.now


# ----------------------------------------------------------------- storage
def test_views_and_dashboards_are_put_got_listed_and_deleted(client, ws):
    saved = put(client, ws, sql_view(label="Sales by region", display={"type": "bar", "x": "region", "y": ["total"]}))
    assert saved["label"] == "Sales by region" and saved["display"]["type"] == "bar"
    assert saved["created"] == saved["updated"]
    got = client.get(f"/db/{ws}/_views/by_region").json()
    assert got["source"]["query"].startswith("SELECT region") and got["created"] == saved["created"]
    put(client, ws, sql_view("again"))
    again = put(client, ws, sql_view("again", label="Changed"))
    assert again["label"] == "Changed"
    # By name, paged.
    first = client.get(f"/db/{ws}/_views?_limit=1").json()
    assert [v["name"] for v in first["views"]] == ["again"] and first["next"]
    rest = client.get(f"/db/{ws}/_views?_limit=1&_cursor={first['next']}").json()
    assert [v["name"] for v in rest["views"]] == ["by_region"] and "next" not in rest
    # The body names what the path does.
    assert client.put(f"/db/{ws}/_views/other", json=sql_view("again")).status_code == 400
    # A dashboard of them.
    board = {"name": "main", "title": "Sales", "refresh_s": 10, "items": [{"view": "by_region", "width": 8},
                                                                           {"view": "again"}]}
    saved = put(client, ws, board, kind="_dashboards")
    assert saved["items"] == [{"view": "by_region", "width": 8}, {"view": "again", "width": 6}]
    assert client.get(f"/db/{ws}/_dashboards/main").json()["title"] == "Sales"
    assert [d["name"] for d in client.get(f"/db/{ws}/_dashboards").json()["dashboards"]] == ["main"]
    # A view a dashboard shows is not deleted.
    r = client.delete(f"/db/{ws}/_views/again")
    assert r.status_code == 409 and "main" in r.json()["detail"]
    assert client.delete(f"/db/{ws}/_dashboards/main").status_code == 204
    assert client.delete(f"/db/{ws}/_views/again").status_code == 204
    assert client.get(f"/db/{ws}/_views/again").status_code == 404
    assert client.get(f"/db/{ws}/_dashboards/main").status_code == 404
    # Deleting what is not there is no error; a workspace that is not there is a 404.
    assert client.delete(f"/db/{ws}/_views/again").status_code == 204
    assert client.put("/db/nowhere_ws/_views/x", json=sql_view("x")).status_code == 404
    assert client.get("/db/nowhere_ws/_views").status_code == 404


def test_a_dashboard_names_views_of_its_workspace_and_refreshes_every_5_s_at_least(client, ws):
    put(client, ws, sql_view())
    board = {"name": "b", "title": "B", "items": [{"view": "by_region"}, {"view": "missing_one"}]}
    r = client.put(f"/db/{ws}/_dashboards/b", json=board)
    assert r.status_code == 400 and "missing_one" in r.json()["detail"]
    for refresh in (1, 4, -1):
        assert client.put(f"/db/{ws}/_dashboards/b", json={**board, "items": [], "refresh_s": refresh}).status_code == 422
    for refresh in (0, 5, 60):
        put(client, ws, {**board, "items": [{"view": "by_region"}], "refresh_s": refresh}, kind="_dashboards")
    assert client.put(f"/db/{ws}/_dashboards/b", json={**board, "items": [{"view": "by_region", "width": 13}]}
                      ).status_code == 422


def test_a_view_may_read_the_service_s_own_data(client):
    """A dashboard of the evals' tables is a fair use: views are made in the service's own workspaces too."""
    spec = sql_view("datasets", "SELECT count(*) AS n FROM datasets")
    put(client, "evals", spec)
    try:
        assert run(client, "evals", "datasets")["columns"] == ["n"]
    finally:
        assert client.delete("/db/evals/_views/datasets").status_code == 204


def test_views_survive_a_reopen_of_the_store(client, ws, views):
    put(client, ws, sql_view(label="Kept"))
    reopened = Views(views.database, views.tables, views.max_rows, views.time_limit_ms)
    assert client.portal.call(reopened.get_view, ws, "by_region")["label"] == "Kept"
    answer = client.portal.call(reopened.run, ws, "by_region", {})
    assert answer["rows"][0] == {"region": "east", "total": 2 + 5 + 8}


def test_dropping_a_workspace_removes_its_views_and_one_made_again_has_none(client, ws, counted):
    put(client, ws, sql_view())
    put(client, ws, {"name": "d", "title": "D", "items": [{"view": "by_region"}]}, kind="_dashboards")
    assert run(client, ws, "by_region")["cached"] is False
    assert run(client, ws, "by_region")["cached"] is True
    assert client.delete(f"/db/{ws}").status_code == 204
    assert client.post("/db", json={"name": ws}).status_code == 201
    assert client.get(f"/db/{ws}/_views").json() == {"views": []}
    assert client.get(f"/db/{ws}/_dashboards").json() == {"dashboards": []}
    run(client, ws, "by_region", status=404)
    # The same view made again asks the database: nothing kept of the old one answers.
    fill_sales(client, ws, SALES[:3])
    put(client, ws, sql_view())
    answer = run(client, ws, "by_region")
    assert answer["cached"] is False and len(answer["rows"]) == 3
    assert counted.count("timed_read") == 2


# ------------------------------------------------------------------ params
def test_a_view_runs_with_its_params_bound_never_written_in(client, ws):
    params = {"type": "object", "properties": {"note": {"type": "string"}, "min": {"type": "integer", "default": 5}},
              "required": ["note"]}
    put(client, ws, sql_view("notes", "SELECT id, total FROM sales WHERE note = $note AND total >= $min ORDER BY id",
                             params=params))
    assert [r["id"] for r in run(client, ws, "notes", note="plain")["rows"]] == ["s5", "s6", "s7", "s8", "s9"]
    assert [r["id"] for r in run(client, ws, "notes", note="plain", min=8)["rows"]] == ["s8", "s9"]
    # Matched as it is: written into the SQL it would have made every row match.
    sly = "x' OR 1=1 --"
    assert run(client, ws, "notes", note=sly)["rows"] == []
    assert client.put(f"/db/{ws}/sales/odd", json={"note": sly, "total": 7}).status_code == 201
    # (Other values than the run before: its answer is kept for the view's refresh_s.)
    assert [r["id"] for r in run(client, ws, "notes", note=sly, min=7)["rows"]] == ["odd"]


def test_a_run_refuses_unknown_missing_and_wrongly_typed_params(client, ws):
    params = {"type": "object", "additionalProperties": True, "required": ["region"], "properties": {
        "region": {"type": "string", "enum": ["north", "south", "east"]},
        "min": {"type": "integer", "minimum": 0, "default": 0}}}
    put(client, ws, sql_view("by", "SELECT count(*) AS n FROM sales WHERE region = $region AND total >= $min",
                             params=params))
    assert run(client, ws, "by", region="north")["rows"] == [{"n": 4}]
    assert run(client, ws, "by", region="north", min=4)["rows"] == [{"n": 2}]
    # Whatever additionalProperties says, a param it does not declare is refused.
    assert "Unknown parameter 'max'" in run(client, ws, "by", status=400, region="north", max=1)["detail"]
    assert "'region' is required" in run(client, ws, "by", status=400)["detail"]
    assert "Parameter 'min'" in run(client, ws, "by", status=400, region="north", min="four")["detail"]
    assert "Parameter 'min'" in run(client, ws, "by", status=400, region="north", min=-1)["detail"]
    assert "Parameter 'region'" in run(client, ws, "by", status=400, region="west")["detail"]


def test_a_subquery_s_value_comes_in_its_row(client, ws):
    put(client, ws, sql_view("sub", "SELECT id, (SELECT count(*) FROM sales) AS n FROM sales WHERE total < 2 ORDER BY id"))
    assert run(client, ws, "sub")["rows"] == [{"id": "s0", "n": 10}, {"id": "s1", "n": 10}]


def test_a_params_schema_is_checked_when_saved(client, ws):
    bad = [{"type": "array"}, {"type": "object", "properties": {"_x": {}}}, {"type": "object", "properties": []},
           {"type": "object", "properties": {"a": {"type": "nonsense"}}},
           {"type": "object", "properties": {"a": {"$ref": "https://example.com/s.json"}}},
           {"type": "object", "properties": {"a": {"$ref": "#/$defs/nowhere"}}}]
    for params in bad:
        r = client.put(f"/db/{ws}/_views/v", json=sql_view("v", params=params))
        assert r.status_code == 422, (params, r.text)
    # One that refers to its own parts is fine.
    params = {"type": "object", "$defs": {"year": {"type": "integer", "minimum": 2000}},
              "properties": {"year": {"$ref": "#/$defs/year"}}}
    put(client, ws, sql_view("v", "SELECT $year AS year", params=params))
    assert run(client, ws, "v", year=2025)["rows"] == [{"year": 2025}]
    assert "Parameter 'year'" in run(client, ws, "v", status=400, year=1999)["detail"]


def test_a_default_is_found_through_a_ref(client, ws):
    """A default where the parameter's $ref points is its default, as the page's form shows it; one beside the $ref
    wins. Left out, the parameter would be bound as null."""
    params = {"type": "object", "$defs": {"year": {"type": "integer", "default": 2025}, "same": {"$ref": "#/$defs/year"}},
              "properties": {"year": {"$ref": "#/$defs/year"}, "other": {"$ref": "#/$defs/same"},
                             "mine": {"$ref": "#/$defs/year", "default": 2030}}}
    put(client, ws, sql_view("v", "SELECT $year AS year, $other AS other, $mine AS mine", params=params))
    assert run(client, ws, "v")["rows"] == [{"year": 2025, "other": 2025, "mine": 2030}]
    assert run(client, ws, "v", year=2026)["rows"] == [{"year": 2026, "other": 2025, "mine": 2030}]


# ----------------------------------------------------------------- sources
def test_rows_take_param_filters_and_leave_out_one_not_given(client, ws):
    params = {"type": "object", "properties": {"region": {"type": "string"}, "n": {"type": "integer", "default": 2}}}
    spec = {"name": "top", "params": params, "source": {
        "kind": "rows", "table": "sales", "filters": {"region": {"$param": "region"}}, "sort": "-total",
        "limit": {"$param": "n"}}}
    put(client, ws, spec)
    answer = run(client, ws, "top", region="north")
    assert [r["id"] for r in answer["rows"]] == ["s9", "s6"]
    assert answer["columns"][0] == "id" and set(answer["columns"]) == {"id", "note", "region", "total"}
    assert answer["truncated"] is False
    # No region: the filter is left out, so every region's rows come.
    assert [r["id"] for r in run(client, ws, "top", n=3)["rows"]] == ["s9", "s8", "s7"]
    # A value that does not fit where it goes is refused, saying where.
    assert "limit" in run(client, ws, "top", status=400, n=0)["detail"]
    # Every $param is one of the params.
    r = client.put(f"/db/{ws}/_views/top", json={**spec, "params": {"type": "object", "properties": {}}})
    assert r.status_code == 422 and "region" in r.text


def test_a_filter_s_value_is_matched_as_the_value_it_is(client, ws):
    """A filter's value is matched as the JSON value it is, never read again as the list's query string reads text:
    the text "null" is that text, and quotes are part of the text."""
    rows = [{"id": "text_null", "code": "null"}, {"id": "null", "code": None}, {"id": "none"},
            {"id": "quoted", "code": '"q"'}, {"id": "bare", "code": "q"}]
    assert client.put(f"/db/{ws}/codes/_bulk", json={"rows": rows}).status_code == 204
    params = {"type": "object", "properties": {"code": {"type": "string"}}}
    put(client, ws, {"name": "by_code", "params": params,
                     "source": {"kind": "rows", "table": "codes", "filters": {"code": {"$param": "code"}}}})

    def ids(**given):
        return sorted(r["id"] for r in run(client, ws, "by_code", **given)["rows"])

    assert ids(code="null") == ["text_null"]
    assert ids(code='"q"') == ["quoted"]
    assert ids(code="q") == ["bare"]
    # Written in the spec: null is a field with no value, the text "null" that text.
    put(client, ws, {"name": "no_code", "source": {"kind": "rows", "table": "codes", "filters": {"code": None}}})
    assert sorted(r["id"] for r in run(client, ws, "no_code")["rows"]) == ["none", "null"]
    put(client, ws, {"name": "null_text", "source": {"kind": "rows", "table": "codes", "filters": {"code": "null"}}})
    assert [r["id"] for r in run(client, ws, "null_text")["rows"]] == ["text_null"]


def test_a_value_written_in_a_source_is_bounded_as_a_run_takes_it(client, ws):
    """A rows source's limit, a search's k and the like, written in the spec, are refused when saved if no run could
    take them; given by a $param, they are checked when it runs."""
    for source in ({"kind": "rows", "table": "sales", "limit": 5000}, {"kind": "rows", "table": "sales", "limit": 0},
                   {"kind": "rows", "table": "sales", "search": ""},
                   {"kind": "search", "table": "sales", "text": "cat", "k": 0},
                   {"kind": "search", "table": "sales", "text": "cat", "k": MAX_K + 1},
                   {"kind": "search", "table": "sales", "text": ""},
                   {"kind": "search", "table": "sales", "vector": []}):
        r = client.put(f"/db/{ws}/_views/v", json={"name": "v", "source": source})
        assert r.status_code == 422, (source, r.text)
    put(client, ws, {"name": "v", "source": {"kind": "rows", "table": "sales", "limit": 1000, "sort": ""}})
    assert len(run(client, ws, "v")["rows"]) == len(SALES)


def test_a_search_takes_a_vector_or_a_text_its_model_embeds(client, ws):
    fields = [{"name": "text", "type": "string"},
              {"name": "v", "type": "vector", "model": "topic:embed", "from": "text"}]
    assert client.post(f"/db/{ws}", json={"name": "pets", "fields": fields}).status_code == 201
    rows = [{"id": "c", "text": "a cat"}, {"id": "d", "text": "a dog"}, {"id": "cc", "text": "cat cat cat"}]
    assert client.put(f"/db/{ws}/pets/_bulk", json={"rows": rows}).status_code == 204
    params = {"type": "object", "properties": {"near": {"type": "string"}, "k": {"type": "integer", "default": 2}}}
    put(client, ws, {"name": "near", "params": params, "source": {
        "kind": "search", "table": "pets", "text": {"$param": "near"}, "k": {"$param": "k"}}})
    answer = run(client, ws, "near", near="dog")
    assert [r["id"] for r in answer["rows"]] == ["d", "c"] and "_distance" in answer["columns"]
    assert "v" not in answer["rows"][0]
    # A vector given directly.
    put(client, ws, {"name": "by_vector", "source": {"kind": "search", "table": "pets", "vector": unit(1, 0), "k": 1}})
    assert [r["id"] for r in run(client, ws, "by_vector")["rows"]] == ["cc"]
    # A vector as a param.
    params = {"type": "object", "properties": {"v": {"type": "array", "items": {"type": "number"}}}}
    put(client, ws, {"name": "by_param", "params": params,
                     "source": {"kind": "search", "table": "pets", "vector": {"$param": "v"}, "k": 3}})
    assert run(client, ws, "by_param", v=unit(0, 1))["rows"][0]["id"] == "d"
    assert "vector or a text" in run(client, ws, "by_param", status=400)["detail"]


def test_a_hybrid_search_gives_each_row_its_score(client, ws):
    fields = [{"name": "text", "type": "string", "fulltext": True},
              {"name": "v", "type": "vector", "model": "topic:embed", "from": "text"}]
    assert client.post(f"/db/{ws}", json={"name": "pets_words", "fields": fields}).status_code == 201
    rows = [{"id": "c", "text": "a cat"}, {"id": "d", "text": "a dog"}]
    assert client.put(f"/db/{ws}/pets_words/_bulk", json={"rows": rows}).status_code == 204
    put(client, ws, {"name": "mixed", "source": {"kind": "search", "table": "pets_words", "text": "dog", "hybrid": True}})
    answer = run(client, ws, "mixed")
    assert answer["rows"][0]["id"] == "d" and {"_distance", "_score"} <= set(answer["columns"])
    # A reranker too: it likes the shortest text, and its scores are the rows' _score.
    assert client.put(f"/db/{ws}/pets_words/cc", json={"text": "cat"}).status_code == 201
    with registered(ShortReranker, task="rerank", match="rr:*"):
        put(client, ws, {"name": "reread", "source": {"kind": "search", "table": "pets_words", "text": "dog", "k": 1,
                                                      "rerank": "rr:short"}})
        assert [(r["id"], r["_score"]) for r in run(client, ws, "reread")["rows"]] == [("cc", pytest.approx(1 / 4))]


# ------------------------------------------------------------ what is refused
def test_a_source_is_sql_a_search_or_rows(client, ws):
    r = client.put(f"/db/{ws}/_views/c", json={"name": "c", "source": {"kind": "cypher", "query": "MATCH (n) RETURN n"}})
    assert r.status_code == 422 and "union_tag_invalid" in r.text


@pytest.mark.parametrize("query, says", [
    ("INSERT INTO sales (id) VALUES ('x')", "only a SELECT"),
    ("UPDATE sales SET total = 0", "only a SELECT"),
    ("DELETE FROM sales", "only a SELECT"),
    ("CREATE TABLE other (x INTEGER)", "only a SELECT"),
    ("COPY sales TO 'sales.csv'", "only a SELECT"),
    ("PRAGMA create_fts_index('sales', 'id', 'note')", "only a SELECT"),
    ("EXPLAIN SELECT * FROM sales", "only a SELECT"),
    ("SELECT * FROM sales; DELETE FROM sales", "2 statements"),
    ("SELECT * FROM sales /* never closed", "does not parse"),
    ("SELECT * FROM sales WHERE region = $region", "$region"),
    # A quote in a comment opens no string that would hide what follows from the checks.
    ("SELECT total FROM sales -- don't count refunds\nWHERE total > $least -- each region's sum", "$least"),
    ("SELECT total FROM sales /* it's */ WHERE total > $least /* ' */", "$least"),
])
def test_sql_that_is_not_one_select_or_names_an_undeclared_param_is_refused_when_saved(client, ws, query, says):
    r = client.put(f"/db/{ws}/_views/bad", json=sql_view("bad", query))
    assert r.status_code == 400 and says in r.json()["detail"], r.text
    assert client.get(f"/db/{ws}/_views/bad").status_code == 404


def test_a_view_whose_with_writes_is_refused_as_it_runs_and_writes_nothing(client, ws):
    """DuckDB's parser counts a WITH that inserts as a SELECT, so the save takes it; a client's run is refused."""
    put(client, ws, sql_view("w", "WITH w AS (INSERT INTO sales (id, _seq) VALUES ('x', 0) RETURNING id) SELECT * FROM w"))
    assert "only reads run here" in run(client, ws, "w", status=400)["detail"]
    assert client.get(f"/db/{ws}/sales/_count").json() == {"count": len(SALES)}


def test_comments_hide_nothing_and_pass_through(client, ws):
    """What a comment says is no part of the statement: a $param in one is none of the view's (one it declares is not
    sent), and a table named after one with a quote in it is still the workspace's."""
    params = {"type": "object", "properties": {"least": {"type": "integer", "default": 8},
                                               "most": {"type": "integer", "default": 20}}}
    query = ("SELECT id -- not $most, only the least\n"
             "FROM /* the shop's */ sales WHERE total >= $least -- each region's\nORDER BY id")
    put(client, ws, sql_view("commented", query, params=params))
    assert [r["id"] for r in run(client, ws, "commented")["rows"]] == ["s8", "s9"]
    r = client.post(f"/db/{ws}/_query", json={"query": "SELECT count(*) AS n -- it's\nFROM sales -- the shop's"})
    assert r.status_code == 200 and r.json()["rows"] == [{"n": len(SALES)}], r.text
    # Nor is one in a string.
    r = client.post(f"/db/{ws}/_query", json={"query": "SELECT '$n%' AS text, $m AS m", "params": {"n": 1, "m": 2}})
    assert r.status_code == 200 and r.json()["rows"] == [{"text": "$n%", "m": 2}], r.text


def test_caps_a_view_may_lower_but_never_raise(client, ws):
    r = client.put(f"/db/{ws}/_views/v", json=sql_view("v", max_rows=Config.VIEW_MAX_ROWS + 1))
    assert r.status_code == 400 and "VIEW_MAX_ROWS" in r.text
    spec = sql_view("v")
    spec["source"]["time_limit_ms"] = Config.VIEW_TIME_LIMIT_MS + 1
    r = client.put(f"/db/{ws}/_views/v", json=spec)
    assert r.status_code == 400 and "VIEW_TIME_LIMIT_MS" in r.text


# ---------------------------------------------------------------- guards
@pytest.fixture
def grid(client, ws):
    rows = [{"id": str(i), "x": i} for i in range(SLOW_ROWS)]
    assert client.put(f"/db/{ws}/grid/_schema", json={"fields": [{"name": "x", "type": "integer"}]}).status_code == 200
    assert client.put(f"/db/{ws}/grid/_bulk", json={"rows": rows}).status_code == 204
    return ws


@pytest.mark.parametrize("query", [
    CROSS,
    NESTED,
    # A comment after the statement, where an appended clause would have gone.
    NESTED + " -- and nothing after this",
    # The slow work in a subquery.
    f"SELECT n FROM ({NESTED})",
    STREAMED,
], ids=["match", "nested", "comment", "subquery", "streamed"])
def test_a_slow_view_stops_at_its_time_limit(client, grid, query):
    spec = sql_view("slow", query)
    spec["source"]["time_limit_ms"] = LIMIT_MS
    put(client, grid, spec)
    started = time.monotonic()
    answer = run(client, grid, "slow", status=503)
    took = time.monotonic() - started
    assert f"took longer than its limit of {LIMIT_MS} ms" in answer["detail"]
    # Near the limit, not after the whole query, which takes seconds.
    assert took < 1.5, took


def test_the_time_limit_holds_whatever_the_statement_says(client, grid, views):
    """What the save refuses, run all the same: a second statement does not run, and a slow one stops."""
    tables = views.tables
    started = time.monotonic()
    with pytest.raises(Exception) as caught:
        client.portal.call(tables.timed_read, grid, NESTED, {}, 10, LIMIT_MS)
    assert type(caught.value).__name__ == "QueryTimedOut" and time.monotonic() - started < 1.5
    with pytest.raises(Exception) as caught:
        client.portal.call(tables.timed_read, grid, "SELECT count(*) AS n FROM grid; DELETE FROM grid", {}, 10, LIMIT_MS)
    assert "2 statements" in str(caught.value)
    assert client.get(f"/db/{grid}/grid/_count").json() == {"count": SLOW_ROWS}
    # The limit is the thread's statement's alone: the next read on the pool has none.
    rows, more = client.portal.call(tables.timed_read, grid, "SELECT count(*) AS n FROM grid", {}, 10, LIMIT_MS)
    assert rows == [{"n": SLOW_ROWS}] and more is False
    r = client.post(f"/db/{grid}/_query", json={"query": "SELECT count(*) AS n FROM grid g WHERE (SELECT count(*) FROM grid "
                                                          "WHERE x < g.x) >= 0"})
    assert r.status_code == 200, r.text


def test_the_service_s_limit_holds_when_lower_than_the_view_s(client, grid, views, monkeypatch):
    spec = sql_view("slow", CROSS)
    spec["source"]["time_limit_ms"] = 4000
    put(client, grid, spec)
    monkeypatch.setattr(views, "time_limit_ms", LIMIT_MS)
    assert f"limit of {LIMIT_MS} ms" in run(client, grid, "slow", status=503)["detail"]


def test_the_row_cap_cuts_rows_and_says_so(client, ws, views, monkeypatch):
    put(client, ws, sql_view("all", "SELECT id FROM sales ORDER BY id", max_rows=3))
    answer = run(client, ws, "all")
    assert [r["id"] for r in answer["rows"]] == ["s0", "s1", "s2"] and answer["truncated"] is True
    put(client, ws, sql_view("few", "SELECT id FROM sales ORDER BY id LIMIT 2", max_rows=3))
    assert run(client, ws, "few")["truncated"] is False
    put(client, ws, sql_view("exact", "SELECT id FROM sales ORDER BY id LIMIT 3", max_rows=3))
    assert run(client, ws, "exact")["truncated"] is False
    # A rows source: its own limit is its choice, the cap is the service's.
    put(client, ws, {"name": "rows_few", "source": {"kind": "rows", "table": "sales", "limit": 2}, "max_rows": 5})
    assert run(client, ws, "rows_few")["truncated"] is False
    put(client, ws, {"name": "rows_all", "source": {"kind": "rows", "table": "sales", "limit": 50}, "max_rows": 5})
    answer = run(client, ws, "rows_all")
    assert len(answer["rows"]) == 5 and answer["truncated"] is True
    # The service's cap, when lower than the view's.
    put(client, ws, sql_view("wide", "SELECT id FROM sales ORDER BY id"))
    monkeypatch.setattr(views, "max_rows", 4)
    answer = run(client, ws, "wide")
    assert len(answer["rows"]) == 4 and answer["truncated"] is True


# ------------------------------------------------------------------ cache
def test_runs_within_the_interval_ask_the_database_once(client, ws, views, counted, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(views, "clock", clock)
    params = {"type": "object", "properties": {"min": {"type": "integer", "default": 0}}}
    put(client, ws, sql_view("total", "SELECT sum(total) AS t FROM sales WHERE total >= $min", params=params,
                             refresh_s=60))
    first = run(client, ws, "total")
    assert first["cached"] is False and first["ran_at"] == clock.now and first["rows"] == [{"t": 45}]
    assert run(client, ws, "total") == {**first, "cached": True}
    # The default given is the same values.
    assert run(client, ws, "total", min=0)["cached"] is True
    assert counted == ["timed_read"]
    # Other params ask again.
    assert run(client, ws, "total", min=5)["rows"] == [{"t": 35}]
    assert len(counted) == 2
    # Past the interval it asks again.
    clock.now += 59
    assert run(client, ws, "total")["cached"] is True
    clock.now += 2
    assert run(client, ws, "total")["cached"] is False
    assert len(counted) == 3
    # Saved again, even as it was, it is never answered from what was kept.
    put(client, ws, sql_view("total", "SELECT sum(total) AS t FROM sales WHERE total >= $min", params=params,
                             refresh_s=60))
    assert run(client, ws, "total")["cached"] is False
    assert len(counted) == 4
    # Nor once deleted and made again.
    assert client.delete(f"/db/{ws}/_views/total").status_code == 204
    put(client, ws, sql_view("total", "SELECT count(*) AS t FROM sales WHERE total >= $min", params=params))
    assert run(client, ws, "total")["rows"] == [{"t": 10}]
    assert len(counted) == 5
    # A preview is never kept.
    body = {"spec": sql_view("total", "SELECT 1 AS one"), "params": {}}
    for _ in range(2):
        r = client.post(f"/db/{ws}/_views/_try", json=body)
        assert r.status_code == 200 and r.json()["rows"] == [{"one": 1}] and r.json()["cached"] is False
    assert len(counted) == 7


def test_two_runs_at_once_ask_the_database_once(client, ws, views, counted, monkeypatch):
    put(client, ws, sql_view())
    real = views.tables.timed_read

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.2)
        return await real(*args, **kwargs)

    monkeypatch.setattr(views.tables, "timed_read", slow)

    async def both():
        return await asyncio.gather(views.run(ws, "by_region", {}), views.run(ws, "by_region", {}))

    one, two = client.portal.call(both)
    assert one["rows"] == two["rows"] and {one["cached"], two["cached"]} == {False, True}
    assert counted == ["timed_read"]


def test_the_cache_is_bounded(client, ws, views):
    from views import store

    params = {"type": "object", "properties": {"n": {"type": "integer"}}}
    put(client, ws, sql_view("n", "SELECT $n AS n", params=params))
    for n in range(store.MAX_ENTRIES + 10):
        run(client, ws, "n", n=n)
    assert len(views._kept) <= store.MAX_ENTRIES


# ------------------------------------------------------------------ keys
def test_a_client_key_runs_a_view_but_neither_writes_nor_reads_one(client, ws, monkeypatch):
    put(client, ws, sql_view())
    put(client, ws, {"name": "d", "title": "D", "items": [{"view": "by_region"}]}, kind="_dashboards")
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    as_client, as_admin = {"X-API-Key": "client-key"}, {"X-API-Key": "admin-key"}
    r = client.post(f"/db/{ws}/_views/by_region", json={"params": {}}, headers=as_client)
    assert r.status_code == 200 and len(r.json()["rows"]) == 3
    assert client.post(f"/db/{ws}/_views/by_region", json={"params": {}}).status_code == 401
    refused = [
        ("get", f"/db/{ws}/_views", None), ("get", f"/db/{ws}/_views/by_region", None),
        ("put", f"/db/{ws}/_views/by_region", sql_view()), ("delete", f"/db/{ws}/_views/by_region", None),
        ("post", f"/db/{ws}/_views/_try", {"spec": sql_view(), "params": {}}),
        ("get", f"/db/{ws}/_dashboards", None), ("get", f"/db/{ws}/_dashboards/d", None),
        ("put", f"/db/{ws}/_dashboards/d", {"name": "d", "title": "D"}), ("delete", f"/db/{ws}/_dashboards/d", None),
    ]
    for method, path, body in refused:
        kwargs = {"json": body} if body is not None else {}
        # The admin's routes answer a client key as they answer no key: it is not the one they take.
        assert client.request(method.upper(), path, headers=as_client, **kwargs).status_code == 401, (method, path)
    assert client.get(f"/db/{ws}/_views/by_region", headers=as_admin).status_code == 200
    assert client.get(f"/db/{ws}/_views/by_region", headers=as_admin).json()["name"] == "by_region"
