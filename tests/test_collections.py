import math

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("arcadedb_embedded")

from api import app as app_module  # noqa: E402
from config import Config  # noqa: E402
from tests.fakes import FakeModel, registered  # noqa: E402

TOPICS = ("cat", "stock", "dog")


class KeywordEmbedder(FakeModel):
    """Vectors from which topics a text mentions, so nearness is predictable."""

    async def predict(self, request):
        vectors = []
        for text in request.texts:
            v = [float(sum(text.lower().count(w) for w in (topic, topic[:3] + "s"))) for topic in TOPICS] + [0.1]
            norm = math.sqrt(sum(x * x for x in v))
            vectors.append([x / norm for x in v])
        return {"embeddings": vectors, "tokens": sum(len(t.split()) for t in request.texts)}


@pytest.fixture(scope="module")
def client():
    # The JVM must start on the main thread (see Database.start_jvm);
    # the test client runs the app on another one.
    app_module.app.state.database.start_jvm()
    with registered(KeywordEmbedder, task="embedding", match="kw:*"), TestClient(app_module.app) as c:
        yield c


def create(client, name, **kw):
    return client.post("/collections", json={"name": name, "model": "kw:embed", **kw})


DOCS = [
    {"id": "1", "text": "A cat sleeps on the mat", "tenant": "acme", "year": 2024, "tags": ["pets"]},
    {"id": "2", "text": "Cats and kittens play", "tenant": "acme", "year": 2025, "tags": ["pets", "young"]},
    {"id": "3", "text": "Stock markets fell", "tenant": "acme", "year": 2025, "tags": ["finance"]},
    {"id": "4", "text": "My cat chased a dog", "tenant": "other", "year": 2025, "tags": ["pets"]},
]


@pytest.fixture(scope="module")
def docs(client):
    r = create(client, "docs", fields={"tenant": "STRING", "year": "INTEGER", "tags": "LIST"})
    assert r.status_code == 200, r.text
    assert r.json()["dimensions"] == 4
    r = client.post("/collections/docs/items", json={"items": DOCS})
    assert r.status_code == 200, r.text
    return "docs"


def search(client, query, **kw):
    r = client.post("/collections/docs/search", json={"query": query, **kw})
    assert r.status_code == 200, r.text
    return r.json()["results"]


def test_search_returns_the_nearest_items_first(client, docs):
    results = search(client, "cat", k=3)
    assert [r["id"] for r in results][:2] == ["1", "2"] or [r["id"] for r in results][:2] == ["2", "1"]
    assert results[0]["distance"] <= results[-1]["distance"]
    assert set(results[0]) == {"id", "text", "tenant", "year", "tags", "distance"}
    assert search(client, "stock prices", k=1)[0]["id"] == "3"


def test_search_within_a_sql_filter(client, docs):
    results = search(client, "cat", k=5, where="tenant = :t AND year >= :y", params={"t": "acme", "y": 2025})
    assert {r["id"] for r in results} == {"2", "3"}
    assert [r["id"] for r in search(client, "cat", k=5, where="'young' IN tags")] == ["2"]


def test_query_filters_and_pages_without_vectors(client, docs):
    r = client.post("/collections/docs/query", json={"where": "tenant = :t", "params": {"t": "acme"}, "limit": 2, "skip": 1})
    assert r.status_code == 200, r.text
    assert [i["id"] for i in r.json()["items"]] == ["2", "3"]
    assert "embedding" not in r.json()["items"][0]


@pytest.mark.parametrize("body, status", [
    ({"where": "nonsense (((", "query": "cat"}, 400),
    ({"where": "1 = 1; DROP TYPE tk_docs", "query": "cat"}, 400),
    # Parameter names must start with a letter, so the store's own _tk_ ones cannot be set.
    ({"where": "id = :_tk_k", "params": {"_tk_k": 1}, "query": "cat"}, 422),
    ({"where": "id IN (INSERT INTO tk_docs CONTENT {id: 'evil'})", "query": "cat"}, 400),
])
def test_bad_or_writing_where_is_refused(client, docs, body, status):
    assert client.post("/collections/docs/search", json=body).status_code == status
    assert client.get("/collections/docs/items/evil").status_code == 404
    assert client.get("/collections/docs").json()["count"] == 4


def test_items_are_replaced_by_id_and_deleted(client):
    assert create(client, "notes", fields={"done": "BOOLEAN"}).status_code == 200
    r = client.post("/collections/notes/items", json={"items": [{"text": "buy a dog"}, {"id": "n1", "text": "sell stock", "done": False}]})
    ids = r.json()["ids"]
    assert len(ids) == 2 and ids[1] == "n1" and r.json()["tokens"] == 5
    client.post("/collections/notes/items", json={"items": [{"id": "n1", "text": "sold the stock", "done": True}]})
    assert client.get("/collections/notes/items/n1").json() == {"id": "n1", "text": "sold the stock", "done": True}
    assert client.get("/collections/notes").json()["count"] == 2
    assert client.delete("/collections/notes/items/n1").status_code == 200
    assert client.delete("/collections/notes/items/n1").status_code == 404
    assert client.delete("/collections/notes").status_code == 200
    assert client.get("/collections/notes").status_code == 404


@pytest.mark.parametrize("item, message", [
    ({"text": "x", "colour": "red"}, "not a field"),
    ({"text": "x", "year": "2024"}, "must be INTEGER"),
    ({"text": "x", "year": True}, "must be INTEGER"),
])
def test_items_must_fit_the_fields(client, docs, item, message):
    r = client.post("/collections/docs/items", json={"items": [item]})
    assert r.status_code == 400 and message in r.json()["detail"]


def test_collection_rules(client, docs):
    assert create(client, "docs").status_code == 400  # exists
    assert create(client, "bad", fields={"text": "STRING"}).status_code == 400  # built-in name
    assert create(client, "1bad").status_code == 422
    assert create(client, "bad", fields={"x": "DATETIME"}).status_code == 422
    assert client.post("/collections", json={"name": "bad", "model": "Qwen/Qwen2.5-0.5B-Instruct"}).status_code == 400  # not an embedder
    listed = client.get("/collections").json()["collections"]
    assert {"name": "docs", "model": "kw:embed", "dimensions": 4, "count": 4} .items() <= next(
        c for c in listed if c["name"] == "docs").items()
    assert client.post("/collections/missing/search", json={"query": "x"}).status_code == 404


def test_creating_needs_the_admin_key_and_using_the_client_key(client, docs, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    client_key = {"X-API-Key": "client-key"}
    assert client.post("/collections", json={"name": "k", "model": "kw:e"}, headers=client_key).status_code == 401
    assert client.delete("/collections/docs", headers=client_key).status_code == 401
    assert client.post("/collections/docs/search", json={"query": "cat"}, headers=client_key).status_code == 200
    assert client.post("/collections/docs/search", json={"query": "cat"}).status_code == 401


def test_collections_can_be_off(client, monkeypatch):
    monkeypatch.setattr(client.app.state, "collections", None)
    assert client.get("/collections").status_code == 503
