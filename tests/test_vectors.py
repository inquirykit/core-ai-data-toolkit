"""Vector fields in /db tables: embedded with a model or given, searched by nearness within filters and full-text
words, left out of lists."""

import math

import pytest
from fastapi.testclient import TestClient


from api import app as app_module  # noqa: E402
from core.config import Config  # noqa: E402
from tests.fakes import FakeModel, registered  # noqa: E402

TOPICS = ("cat", "stock", "dog")


def keyword_vector(text):
    """A unit vector from which topics a text mentions, so nearness is predictable."""
    v = [float(sum(text.lower().count(w) for w in (topic, topic[:3] + "s"))) for topic in TOPICS] + [0.1]
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


class KeywordEmbedder(FakeModel):
    """Embeds by keyword_vector; ``batches`` keeps how many texts each request held."""

    batches = []

    async def predict(self, request):
        KeywordEmbedder.batches.append(len(request.texts))
        return {"embeddings": [keyword_vector(t) for t in request.texts], "tokens": sum(len(t.split()) for t in request.texts)}


@pytest.fixture(scope="module")
def client():
    with registered(KeywordEmbedder, task="embedding", match="kw:*"), TestClient(app_module.app) as c:
        yield c


DOCS = [
    {"id": "1", "text": "A cat sleeps on the mat", "tenant": "acme", "year": 2024, "tags": ["pets"]},
    {"id": "2", "text": "Cats and kittens play", "tenant": "acme", "year": 2025, "tags": ["pets", "young"]},
    {"id": "3", "text": "Stock markets fell", "tenant": "acme", "year": 2025, "tags": ["finance"]},
    {"id": "4", "text": "My cat chased a dog", "tenant": "other", "year": 2025, "tags": ["pets"]},
]
FIELDS = [
    {"name": "text", "type": "string", "fulltext": True},
    {"name": "tenant", "type": "string", "indexed": True},
    {"name": "year", "type": "integer", "indexed": True},
    {"name": "tags", "type": "string", "list": True},
    {"name": "embedding", "type": "vector", "model": "kw:embed", "from": "text"},
]


@pytest.fixture(scope="module")
def docs(client):
    assert client.post("/db", json={"name": "vectors"}).status_code == 201
    r = client.post("/db/vectors", json={"name": "docs", "graph": False, "fields": FIELDS})
    assert r.status_code == 201, r.text
    # Its dimensions were found by embedding a probe text.
    assert next(f for f in r.json()["fields"] if f["name"] == "embedding") == {
        "name": "embedding", "type": "vector", "list": False, "required": False, "indexed": False, "unique": False,
        "fulltext": False, "dimensions": 4, "similarity": "COSINE", "quantization": "NONE", "model": "kw:embed",
        "from": "text", "query_prompt": "", "document_prompt": ""}
    KeywordEmbedder.batches.clear()
    assert client.put("/db/vectors/docs/_bulk", json={"rows": DOCS}).status_code == 204
    # One request for the four rows.
    assert KeywordEmbedder.batches == [4]
    return "docs"


def search(client, table="docs", status=200, **body):
    r = client.post(f"/db/vectors/{table}/_search", json=body)
    assert r.status_code == status, r.text
    return r.json()


def found(client, **body):
    return [hit["row"]["id"] for hit in search(client, **body)["results"]]


def test_a_text_finds_the_nearest_rows_first(client, docs):
    answer = search(client, text="cat", k=3)
    results = answer["results"]
    assert {r["row"]["id"] for r in results[:2]} == {"1", "2"}
    assert [r["distance"] for r in results] == sorted(r["distance"] for r in results)
    assert results[0]["distance"] < 0.01 and answer["tokens"] == 1
    # The rows come whole but for their vector.
    assert set(results[0]["row"]) == {"id", "text", "tenant", "year", "tags"}
    assert found(client, text="stock prices", k=1) == ["3"]


def test_filters_and_words_narrow_the_search(client, docs):
    assert set(found(client, text="cat", k=5, filters={"tenant": "acme", "year": 2025})) == {"2", "3"}
    # As the list reads its query string: a declared field's value as its type.
    assert set(found(client, text="cat", k=5, filters={"tenant": "acme", "year": "2025"})) == {"2", "3"}
    assert found(client, text="cat", k=5, filters={"tags": "young"}) == ["2"]
    near_dog = found(client, text="dog", k=5, q="cat*")
    assert near_dog[0] == "4" and set(near_dog) == {"1", "2", "4"}
    assert found(client, text="stock", k=5, q="sleeps") == ["1"]
    assert found(client, text="cat", k=5, filters={"tenant": "other"}, q="dog") == ["4"]
    assert found(client, text="cat", k=5, filters={"tenant": "nobody"}) == []
    # k of the rows selected come back, nearest first, though nearer ones are left out.
    assert found(client, text="cat", k=1, filters={"tenant": "acme", "year": 2025}) == ["2"]


def test_vectors_are_left_out_of_lists_but_not_one_row(client, docs):
    rows = client.get("/db/vectors/docs").json()["rows"]
    assert len(rows) == 4 and not any("embedding" in row for row in rows)
    bulk = client.post("/db/vectors/docs/_bulk-get", json={"ids": ["1", "2"]}).json()["rows"]
    assert [r["id"] for r in bulk] == ["1", "2"] and not any("embedding" in row for row in bulk)
    assert client.get("/db/vectors/docs/1").json()["embedding"] == pytest.approx(keyword_vector(DOCS[0]["text"]))
    # A full-text search of the list too, best match first.
    assert client.get("/db/vectors/docs?_search=cat*&_sort=-id").json()["rows"][0] == DOCS[3]
    assert client.get("/db/vectors/docs?_search=kittens").json()["rows"] == [DOCS[1]]
    # A raw query returns what it selects.
    queried = client.post("/db/vectors/_query", json={"query": "SELECT embedding FROM docs WHERE id = '3'"}).json()
    assert queried["rows"][0]["embedding"] == pytest.approx(keyword_vector(DOCS[2]["text"]))
    # Not a filter, nor a sort.
    assert client.get("/db/vectors/docs?embedding=1").status_code == 400
    assert client.get("/db/vectors/docs?_sort=embedding").status_code == 400


def test_a_row_does_not_send_a_vector_embedded_for_it(client, docs):
    vector = keyword_vector("cat")
    r = client.put("/db/vectors/docs/x", json={"text": "cat", "embedding": vector})
    assert r.status_code == 400 and "embedded from text" in r.json()["detail"]
    assert client.post("/db/vectors/docs", json={"text": "cat", "embedding": vector}).status_code == 400
    assert client.post("/db/vectors/_transaction", json={"puts": {"docs": [{"id": "x", "embedding": vector}]}}).status_code == 400
    assert client.patch("/db/vectors/docs/1", json={"embedding": vector}).status_code == 400
    assert client.get("/db/vectors/docs/x").status_code == 404


def test_a_table_embedding_its_rows_or_a_search_is_data_in_the_request_log(client, docs):
    search(client, text="cat", k=1)
    client.app.state.monitoring.flush()
    rows = client.get("/admin/requests?model=kw:embed&limit=5").json()["requests"]
    assert rows and {r["source"] for r in rows} == {"data"}


def test_writes_embed_the_text_they_set(client, docs):
    KeywordEmbedder.batches.clear()
    assert client.put("/db/vectors/docs/5", json={"text": "a dog barks", "tenant": "t5"}).status_code == 201
    assert client.get("/db/vectors/docs/5").json()["embedding"] == pytest.approx(keyword_vector("a dog barks"))
    # A patch that leaves the text alone leaves the vector; one that sets it embeds it.
    assert client.patch("/db/vectors/docs/5", json={"tenant": "t6"}).status_code == 200
    assert KeywordEmbedder.batches == [1]
    assert client.patch("/db/vectors/docs/5", json={"text": "stock and more stock"}).status_code == 200
    assert found(client, text="stock", k=1, filters={"tenant": "t6"}) == ["5"]
    # Without the text, no vector.
    assert client.patch("/db/vectors/docs/5", json={"text": None}).json().get("embedding") is None
    assert found(client, text="stock", k=5, filters={"tenant": "t6"}) == []
    # A transaction embeds its rows of every table together.
    assert client.post("/db/vectors/_transaction", json={
        "puts": {"docs": [{"id": "6", "text": "dogs"}, {"id": "7", "text": "cats"}, {"id": "8"}]}}).status_code == 204
    assert KeywordEmbedder.batches[-1] == 2
    assert client.get("/db/vectors/docs/8").json()["embedding"] is None
    assert client.post("/db/vectors/docs/_bulk-delete", json={"ids": ["5", "6", "7", "8"]}).status_code == 204


def test_each_write_is_split_into_requests_the_task_takes(client, docs, monkeypatch):
    monkeypatch.setattr(Config, "MAX_EMBEDDING_INPUTS", 2)
    KeywordEmbedder.batches.clear()
    rows = [{"id": f"b{i}", "text": f"cat {i}"} for i in range(5)]
    assert client.put("/db/vectors/docs/_bulk", json={"rows": rows}).status_code == 204
    assert KeywordEmbedder.batches == [2, 2, 1]
    assert client.post("/db/vectors/docs/_bulk-delete", json={"ids": [r["id"] for r in rows]}).status_code == 204


def test_a_table_without_a_model_takes_and_searches_vectors(client):
    r = client.post("/db/vectors", json={"name": "points", "fields": [
        {"name": "label", "type": "string"},
        {"name": "v", "type": "vector", "dimensions": 3, "similarity": "EUCLIDEAN"}]})
    assert r.status_code == 201, r.text
    assert r.json()["graph"] is True
    rows = [{"id": "a", "label": "x", "v": [1, 0, 0]}, {"id": "b", "label": "y", "v": [0, 1, 0]},
            {"id": "c", "label": "x", "v": [0.9, 0.2, 0]}, {"id": "d", "label": "x"}]
    assert client.put("/db/vectors/points/_bulk", json={"rows": rows}).status_code == 204
    assert found(client, table="points", vector=[1, 0, 0], k=3) == ["a", "c", "b"]
    assert found(client, table="points", vector=[1, 0, 0], k=3, filters={"label": "y"}) == ["b"]
    assert "tokens" not in search(client, table="points", vector=[0, 0, 1], k=1)
    # A text needs a model; a vector its size; a row's vector its size and numbers.
    assert "no model" in search(client, table="points", status=400, text="x")["detail"]
    assert "3 numbers" in search(client, table="points", status=400, vector=[1, 0])["detail"]
    assert client.put("/db/vectors/points/e", json={"v": [1, 0]}).status_code == 400
    assert client.put("/db/vectors/points/e", json={"v": [1, "0", 0]}).status_code == 400
    # The graph view and traversals leave the vectors out too.
    nodes = client.get("/db/vectors/_graph?_table=points").json()["nodes"]
    assert len(nodes) == 4 and not any("v" in n["row"] for n in nodes)
    assert client.get("/db/vectors/points/a").json()["v"] == pytest.approx([1, 0, 0])
    # So does the SQL the Data section pages a table's rows with (rowsQuery in data.ts).
    paged = client.post("/db/vectors/_query", json={"query": (
        "SELECT * EXCLUDE (v, _seq) FROM points\nORDER BY id\nLIMIT 25 OFFSET 0")})
    assert paged.status_code == 200, paged.text
    assert [{k: v for k, v in r.items() if k != "_rest"} for r in paged.json()["rows"]] == \
        [{"id": i, "label": label} for i, label in zip("abcd", "xyxx")]


def test_distances_are_the_similarity_s_lower_nearer(client):
    """COSINE's is 1 minus the cosine, EUCLIDEAN's the squared distance, DOT_PRODUCT's -(1 + the dot product) / 2."""
    rows = [{"id": "a", "v": [1, 0, 0]}, {"id": "b", "v": [0, 1, 0]}, {"id": "c", "v": [0.6, 0.8, 0]},
            {"id": "d", "v": [-1, 0, 0]}]
    expected = {"COSINE": [0, 0.4, 1, 2], "EUCLIDEAN": [0, 0.8, 2, 4], "DOT_PRODUCT": [-1, -0.8, -0.5, 0]}
    for similarity, distances in expected.items():
        table = f"by_{similarity.lower()}"
        fields = [{"name": "v", "type": "vector", "dimensions": 3, "similarity": similarity}]
        assert client.put(f"/db/vectors/{table}/_schema", json={"fields": fields}).status_code == 200
        assert client.put(f"/db/vectors/{table}/_bulk", json={"rows": rows}).status_code == 204
        results = search(client, table=table, vector=[1, 0, 0], k=4)["results"]
        assert [(h["row"]["id"], h["distance"]) for h in results] == [
            (i, pytest.approx(d, abs=1e-4)) for i, d in zip("acbd", distances)], similarity


def test_a_vector_field_keeps_its_options(client):
    fields = [{"name": "v", "type": "vector", "dimensions": 2}]
    assert client.put("/db/vectors/pairs/_schema", json={"fields": fields}).status_code == 200
    assert client.put("/db/vectors/pairs/p1", json={"v": [1, 0]}).status_code == 201
    for field in ({**fields[0], "dimensions": 3}, {**fields[0], "similarity": "DOT_PRODUCT"},
                  {"name": "v", "type": "any"}):
        r = client.put("/db/vectors/pairs/_schema", json={"fields": [field]})
        assert r.status_code == 400 and "remove the field and add it again" in r.json()["detail"], r.text
    # The same field again is no change; required is no option of its index.
    assert client.put("/db/vectors/pairs/_schema", json={"fields": fields}).status_code == 200
    # Removed, its vectors go with it; added again with other options, rows get vectors as they are written.
    assert client.put("/db/vectors/pairs/_schema", json={"fields": []}).status_code == 200
    assert "v" not in client.get("/db/vectors/pairs/p1").json()
    assert client.put("/db/vectors/pairs/_schema", json={"fields": [{**fields[0], "dimensions": 3}]}).status_code == 200
    assert found(client, table="pairs", vector=[1, 0, 0]) == []
    assert client.put("/db/vectors/pairs/p1", json={"v": [1, 0, 0]}).status_code == 200
    assert found(client, table="pairs", vector=[1, 0, 0]) == ["p1"]


class PromptedEmbedder(KeywordEmbedder):
    """Embeds as KeywordEmbedder, keeping every text it is given; its own prompts are ``own``."""

    texts = []
    own = {}

    @classmethod
    def prompts(cls, model_id):
        return dict(PromptedEmbedder.own)

    async def predict(self, request):
        PromptedEmbedder.texts.extend(request.texts)
        return await super().predict(request)


def prompts_of(response):
    field = next(f for f in response.json()["fields"] if f["type"] == "vector")
    return field["query_prompt"], field["document_prompt"]


PROMPTED = [{"name": "text", "type": "string"}, {"name": "v", "type": "vector", "model": "pr:embed", "from": "text"}]


def test_a_field_takes_its_models_prompts_and_puts_them_before_the_texts(client, docs, monkeypatch):
    monkeypatch.setattr(PromptedEmbedder, "own", {"query": "find: ", "document": "doc: ", "STS": "same: "})
    with registered(PromptedEmbedder, task="embedding", match="pr:*"):
        r = client.put("/db/vectors/prompted/_schema", json={"fields": PROMPTED})
        assert r.status_code == 200 and prompts_of(r) == ("find: ", "doc: "), r.text
        PromptedEmbedder.texts.clear()
        assert client.put("/db/vectors/prompted/r1", json={"text": "a cat"}).status_code == 201
        assert found(client, table="prompted", text="cat") == ["r1"]
        assert PromptedEmbedder.texts == ["doc: a cat", "find: cat"]

        # Sent again without them, the field keeps its prompts; the query prompt may change, as only searches use it.
        assert prompts_of(client.put("/db/vectors/prompted/_schema", json={"fields": PROMPTED})) == ("find: ", "doc: ")
        again = [PROMPTED[0], {**PROMPTED[1], "query_prompt": "search: "}]
        assert prompts_of(client.put("/db/vectors/prompted/_schema", json={"fields": again})) == ("search: ", "doc: ")
        PromptedEmbedder.texts.clear()
        found(client, table="prompted", text="cat")
        assert PromptedEmbedder.texts == ["search: cat"]
        # The document prompt is in every stored vector: changing it is changing the field.
        changed = [PROMPTED[0], {**PROMPTED[1], "document_prompt": ""}]
        r = client.put("/db/vectors/prompted/_schema", json={"fields": changed})
        assert r.status_code == 400 and "remove the field and add it again" in r.json()["detail"], r.text


def test_a_model_whose_prompts_have_other_names_is_refused_until_the_query_prompt_is_given(client, docs, monkeypatch):
    # As microsoft/harrier-oss-v1-270m names them: no query, no document.
    monkeypatch.setattr(PromptedEmbedder, "own", {"web_search_query": "Instruct: search\nQuery: ", "sts_query": "x"})
    with registered(PromptedEmbedder, task="embedding", match="pr:*"):
        r = client.put("/db/vectors/harrier/_schema", json={"fields": PROMPTED})
        assert r.status_code == 400, r.text
        assert "web_search_query: 'Instruct: search\\nQuery: '" in r.json()["detail"]
        given = [PROMPTED[0], {**PROMPTED[1], "query_prompt": "Instruct: search\nQuery: "}]
        r = client.put("/db/vectors/harrier/_schema", json={"fields": given})
        assert r.status_code == 200 and prompts_of(r) == ("Instruct: search\nQuery: ", ""), r.text


def test_a_prompt_counts_toward_the_longest_text(client, docs, monkeypatch):
    monkeypatch.setattr(PromptedEmbedder, "own", {"query": "q" * 10, "document": "d" * 10})
    monkeypatch.setattr(client.app.state.tables, "embed_max_chars", 15)
    with registered(PromptedEmbedder, task="embedding", match="pr:*"):
        assert client.put("/db/vectors/long/_schema", json={"fields": PROMPTED}).status_code == 200
        r = client.put("/db/vectors/long/r1", json={"text": "a cat sat"})
        assert r.status_code == 400 and "text with its prompt is 19 characters" in r.json()["detail"], r.text
        assert "query prompt is 16 characters" in search(client, table="long", status=400, text="a cats")["detail"]


def test_a_vector_field_leaves_a_strict_table_with_its_vectors(client):
    """Strict does not refuse the values of the vector field being removed, as they go with it: so its options can
    change on a strict table too (remove it, add it again)."""
    fields = [{"name": "text", "type": "string"}, {"name": "v", "type": "vector", "dimensions": 2}]
    assert client.put("/db/vectors/strict_raw/_schema", json={"strict": True, "fields": fields}).status_code == 200
    assert client.put("/db/vectors/strict_raw/a", json={"text": "x", "v": [1, 0]}).status_code == 201
    # Renamed in one put: v goes with its values, w comes empty.
    renamed = [fields[0], {**fields[1], "name": "w", "dimensions": 3}]
    r = client.put("/db/vectors/strict_raw/_schema", json={"strict": True, "fields": renamed})
    assert r.status_code == 200, r.text
    assert client.get("/db/vectors/strict_raw/a").json() == {"id": "a", "text": "x"}
    # Strict still refuses a field the rows keep.
    assert client.put("/db/vectors/strict_raw/b", json={"text": "y", "w": [0, 1, 0]}).status_code == 201
    r = client.put("/db/vectors/strict_raw/_schema", json={"strict": True, "fields": renamed[1:]})
    assert r.status_code == 400 and "text not declared" in r.json()["detail"], r.text

    embedded = [{"name": "text", "type": "string"}, {"name": "e", "type": "vector", "model": "kw:embed", "from": "text"}]
    assert client.put("/db/vectors/strict_kw/_schema", json={"strict": True, "fields": embedded}).status_code == 200
    rows = [{"id": "a", "text": "a cat"}, {"id": "b"}]
    assert client.put("/db/vectors/strict_kw/_bulk", json={"rows": rows}).status_code == 204
    r = client.put("/db/vectors/strict_kw/_schema", json={"strict": True, "fields": embedded[:1]})
    assert r.status_code == 200, r.text
    assert client.get("/db/vectors/strict_kw/b").json() == {"id": "b"}
    again = [embedded[0], {**embedded[1], "similarity": "EUCLIDEAN"}]
    assert client.put("/db/vectors/strict_kw/_schema", json={"strict": True, "fields": again}).status_code == 200
    assert client.put("/db/vectors/strict_kw/_bulk", json={"rows": rows}).status_code == 204
    assert found(client, table="strict_kw", text="cat", k=2) == ["a"]


def test_a_write_refused_anyway_spends_no_model_time(client, docs, monkeypatch):
    """The owner of everything a write touches, the rows and the table's room are checked before the model runs."""
    KeywordEmbedder.batches.clear()
    puts = {"docs": [{"id": f"t{i}", "text": "cat"} for i in range(5)]}
    # A kept table among a transaction's deletes, or at an end of its edges, or as their type.
    for touched in ({"deletes": {"evidence_captures": ["x"]}},
                    {"edges": [{"type": "cites", "from": "docs/t0", "to": "evidence_captures/x"}]},
                    {"delete_edges": [{"type": "evidence_links", "from": "docs/t0", "to": "docs/t1"}]}):
        r = client.post("/db/vectors/_transaction", json={"puts": puts, **touched})
        assert r.status_code == 403 and "evidence_" in r.json()["detail"], r.text
    # A row the write refuses: a bad id, a field of the wrong type, a strict table's undeclared field.
    assert client.put("/db/vectors/docs/_bulk", json={"rows": [*puts["docs"], {"id": "_bad", "text": "cat"}]}).status_code == 400
    assert client.put("/db/vectors/docs/_bulk", json={"rows": [*puts["docs"], {"id": "t9", "year": "old"}]}).status_code == 400
    assert client.post("/db/vectors/_transaction", json={"puts": {**puts, "other": [{"id": 1}]}}).status_code == 400
    assert client.post("/db/vectors/_transaction", json={"puts": puts, "edges": [{"type": "x"}]}).status_code == 400
    # A table with no room left.
    monkeypatch.setattr(client.app.state.tables, "vector_max_rows", 4)
    r = client.put("/db/vectors/docs/_bulk", json={"rows": puts["docs"]})
    assert r.status_code == 413, r.text
    assert KeywordEmbedder.batches == []
    assert all(client.get(f"/db/vectors/docs/t{i}").status_code == 404 for i in range(5))


def test_a_text_too_long_to_embed_is_refused_by_name(client, docs, monkeypatch):
    assert client.app.state.tables.embed_max_chars == Config.MAX_INPUT_CHARS
    monkeypatch.setattr(client.app.state.tables, "embed_max_chars", 10)
    KeywordEmbedder.batches.clear()
    r = client.put("/db/vectors/docs/_bulk", json={"rows": [{"id": "s1", "text": "a cat"},
                                                            {"id": "s2", "text": "a cat on a mat"}]})
    assert r.status_code == 400, r.text
    assert r.json()["detail"].startswith("Row 's2' of docs: text is 14 characters, past MAX_INPUT_CHARS (10)")
    r = client.patch("/db/vectors/docs/1", json={"text": "a cat on a mat"})
    assert r.status_code == 400 and "MAX_INPUT_CHARS (10)" in r.json()["detail"], r.text
    assert KeywordEmbedder.batches == []
    assert client.get("/db/vectors/docs/1").json()["text"] == DOCS[0]["text"]


@pytest.mark.parametrize("fields, message", [
    ([{"name": "a", "type": "vector", "dimensions": 2}, {"name": "b", "type": "vector", "dimensions": 2}], "one vector field"),
    ([{"name": "v", "type": "vector"}], "needs its dimensions"),
    ([{"name": "v", "type": "vector", "dimensions": 2, "list": True}], "never a list"),
    ([{"name": "v", "type": "vector", "dimensions": 2, "indexed": True}], "never a list"),
    ([{"name": "v", "type": "vector", "model": "kw:embed"}], "go together"),
    ([{"name": "v", "type": "vector", "dimensions": 2, "from": "text"}, {"name": "text", "type": "string"}], "go together"),
    ([{"name": "v", "type": "vector", "model": "kw:embed", "from": "n"}, {"name": "n", "type": "integer"}], "is not one"),
    ([{"name": "v", "type": "vector", "model": "kw:embed", "from": "missing"}], "is not one"),
    ([{"name": "v", "type": "vector", "model": "kw:embed", "from": "t", "required": True},
      {"name": "t", "type": "string"}], "cannot be required"),
    ([{"name": "v", "type": "vector", "model": "kw:embed", "from": "t", "dimensions": 8},
      {"name": "t", "type": "string"}], "4 numbers, not 8"),
    ([{"name": "n", "type": "integer", "dimensions": 2}], "a vector field's"),
    ([{"name": "v", "type": "vector", "dimensions": 2, "query_prompt": "q: "}], "go with a model"),
])
def test_vector_field_rules(client, fields, message):
    r = client.put("/db/vectors/rules/_schema", json={"fields": fields})
    assert r.status_code == 400 and message in r.json()["detail"], r.text


def test_search_rules(client, docs):
    assert client.post("/db/vectors/plain/_search", json={"vector": [1.0]}).status_code == 404
    assert client.put("/db/vectors/plain/p", json={"x": 1}).status_code == 201
    assert "no vector field" in search(client, table="plain", status=400, vector=[1.0])["detail"]
    assert client.post("/db/vectors/docs/_search", json={"text": "cat", "vector": [1.0] * 4}).status_code == 422
    assert client.post("/db/vectors/docs/_search", json={}).status_code == 422
    assert client.post("/db/vectors/docs/_search", json={"text": "cat", "k": 0}).status_code == 422
    assert client.post("/db/vectors/docs/_search", json={"text": "cat", "k": 101}).status_code == 422
    assert search(client, status=400, text="cat", filters={"embedding": 1})["detail"].startswith("Filter embedding")
    # No row id starts with _, so the path is the search's alone.
    assert client.get("/db/vectors/docs/_search").status_code == 400
    assert client.put("/db/vectors/docs/_search", json={"text": "x"}).status_code == 400


def test_a_table_with_vectors_holds_vector_max_rows(client, monkeypatch):
    monkeypatch.setattr(client.app.state.tables, "vector_max_rows", 3)
    fields = [{"name": "v", "type": "vector", "dimensions": 2}]
    assert client.put("/db/vectors/capped/_schema", json={"fields": fields}).status_code == 200
    rows = [{"id": f"r{i}", "v": [1, i]} for i in range(3)]
    assert client.put("/db/vectors/capped/_bulk", json={"rows": rows}).status_code == 204
    r = client.put("/db/vectors/capped/r3", json={"v": [1, 3]})
    assert r.status_code == 413 and "VECTOR_MAX_ROWS" in r.json()["detail"]
    assert client.post("/db/vectors/capped", json={"id": "r3"}).status_code == 413
    assert client.put("/db/vectors/capped/_bulk", json={"rows": [*rows, {"id": "r3"}]}).status_code == 413
    # Rows already there are written again freely.
    assert client.put("/db/vectors/capped/_bulk", json={"rows": rows}).status_code == 204
    assert client.put("/db/vectors/capped/r0", json={"v": [0, 1]}).status_code == 200
    # A table past it gets no vector field.
    assert client.put("/db/vectors/many/_bulk", json={"rows": [{"id": f"m{i}"} for i in range(4)]}).status_code == 204
    assert client.put("/db/vectors/many/_schema", json={"fields": fields}).status_code == 413
    assert client.get("/db/vectors/many/_schema").json()["fields"] == []


def test_a_model_outside_allowed_models_is_refused(client, docs, monkeypatch):
    monkeypatch.setattr(Config, "ALLOWED_MODELS", ["allowed/model"])
    r = client.put("/db/vectors/refused/_schema", json={"fields": [
        {"name": "t", "type": "string"}, {"name": "v", "type": "vector", "model": "kw:embed", "from": "t"}]})
    assert r.status_code == 403 and "ALLOWED_MODELS" in r.json()["detail"]
    assert client.get("/db/vectors/refused/_schema").json()["fields"] == []
    assert client.put("/db/vectors/docs/z", json={"text": "cat"}).status_code == 403
    assert client.get("/db/vectors/docs/z").status_code == 404
    assert search(client, status=403, text="cat")["detail"]
    # A row without the text needs no model.
    assert client.put("/db/vectors/docs/z", json={"tenant": "t"}).status_code == 201
    assert client.delete("/db/vectors/docs/z").status_code == 204


def test_without_an_embedder_a_write_that_needs_one_is_refused(client, docs, monkeypatch):
    monkeypatch.setattr(client.app.state.tables, "embed", None)
    assert client.put("/db/vectors/docs/z", json={"text": "cat"}).status_code == 503
    assert search(client, status=503, text="cat")["detail"]
    assert client.put("/db/vectors/docs/z", json={"tenant": "t"}).status_code == 201
    assert client.delete("/db/vectors/docs/z").status_code == 204


def test_the_client_key_searches_and_writes_rows_the_admin_key_makes_fields(client, docs, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    client_key, admin_key = {"X-API-Key": "client-key"}, {"X-API-Key": "admin-key"}
    assert client.post("/db/vectors/docs/_search", json={"text": "cat"}, headers=client_key).status_code == 200
    assert client.post("/db/vectors/docs/_search", json={"text": "cat"}).status_code == 401
    assert client.put("/db/vectors/docs/k1", json={"text": "cat"}, headers=client_key).status_code == 201
    fields = {"fields": [{"name": "v", "type": "vector", "dimensions": 2}]}
    assert client.put("/db/vectors/keyed/_schema", json=fields, headers=client_key).status_code == 401
    assert client.post("/db/vectors", json={"name": "keyed", **fields}, headers=client_key).status_code == 401
    assert client.put("/db/vectors/keyed/_schema", json=fields, headers=admin_key).status_code == 200
    assert client.delete("/db/vectors/docs/k1", headers=client_key).status_code == 204


# ------------------------------------------------------------------ hybrid
def test_rankings_fuse_by_reciprocal_rank():
    from db.vectors import RRF_K, fused

    best = fused(["a", "b", "c"], ["c", "d"])
    # b and d tie, and keep the order first met.
    assert [i for i, _ in best] == ["c", "a", "b", "d"]
    assert dict(best) == pytest.approx({"a": 1 / (RRF_K + 1), "b": 1 / (RRF_K + 2), "d": 1 / (RRF_K + 2),
                                        "c": 1 / (RRF_K + 3) + 1 / (RRF_K + 1)})
    assert fused([], []) == []


def test_a_texts_words_are_its_words_as_the_index_splits_them_none_read_as_syntax():
    from db.schema import MAX_ANY_WORDS, any_words

    assert any_words('What is "the" OR fox? -x site.example.com v1.2 e-mail *').rank == \
        ("what", "is", "the", "or", "fox", "x", "site.example.com", "v1.2", "e", "mail")
    assert any_words("Fox fox FOX").rank == ("fox",)
    assert any_words("?! -- *") is None
    assert len(any_words(" ".join(f"w{i}" for i in range(MAX_ANY_WORDS + 10))).rank) == MAX_ANY_WORDS


def test_hybrid_puts_the_rows_holding_the_texts_words_first(client, docs):
    # "kittens" means nothing to the embedder (no topic), so by meaning alone row 2 is no nearer than the others.
    hits = search(client, text="kittens", k=4, hybrid=True)["results"]
    assert hits[0]["row"]["id"] == "2" and {"distance", "score"} <= set(hits[0])
    assert [h["score"] for h in hits] == sorted((h["score"] for h in hits), reverse=True)
    # q still narrows both rankings; filters too.
    assert found(client, text="cat", q="kittens", hybrid=True) == ["2"]
    assert set(found(client, text="cat", filters={"tenant": "other"}, hybrid=True)) == {"4"}
    # Without hybrid, nothing changes: no score.
    assert "score" not in search(client, text="kittens", k=1)["results"][0]


def test_a_row_only_the_words_find_has_a_score_and_no_distance(client, docs):
    rows = [{"id": f"c{i}", "text": f"cat number {i}"} for i in range(12)] + [{"id": "z", "text": "kittens stock stock"}]
    fields = [{"name": "text", "type": "string", "fulltext": True},
              {"name": "v", "type": "vector", "model": "kw:embed", "from": "text"}]
    assert client.put("/db/vectors/mixed/_schema", json={"fields": fields}).status_code == 200
    assert client.put("/db/vectors/mixed/_bulk", json={"rows": rows}).status_code == 204
    # k=2 takes 8 nearest rows, all cats ("z" is the farthest); only "z" holds the word, so it ties the nearest cat.
    first, second = search(client, table="mixed", text="kittens", k=2, hybrid=True)["results"]
    assert first["row"]["id"].startswith("c") and "distance" in first
    assert second["row"]["id"] == "z" and "distance" not in second
    assert first["score"] == second["score"] == pytest.approx(1 / 61)


def test_hybrid_rules(client, docs):
    assert "search by a text" in search(client, status=400, vector=[1.0] * 4, hybrid=True)["detail"]
    fields = [{"name": "text", "type": "string"}, {"name": "v", "type": "vector", "model": "kw:embed", "from": "text"}]
    assert client.put("/db/vectors/wordless/_schema", json={"fields": fields}).status_code == 200
    assert "no full-text field" in search(client, table="wordless", status=400, text="cat", hybrid=True)["detail"]
    # A text with no word ranks by meaning alone.
    assert search(client, text="?!", k=2, hybrid=True)["results"]


# ------------------------------------------------------------------ rerank
class ShortReranker(FakeModel):
    """A reranker that likes short documents best: 1 / (1 + its length); ``calls`` keeps each (query, documents)."""

    calls = []

    async def predict(self, request):
        ShortReranker.calls.append((request.query, list(request.documents)))
        scores = [1 / (1 + len(d)) for d in request.documents]
        order = sorted(range(len(scores)), key=lambda i: -scores[i])
        return {"results": [{"index": i, "score": scores[i]} for i in order],
                "tokens": sum(len(d.split()) for d in request.documents)}


@pytest.fixture
def reranker():
    ShortReranker.calls.clear()
    with registered(ShortReranker, task="rerank", match="rr:*"):
        yield "rr:short"


def test_a_reranker_reads_the_rows_found_and_keeps_the_k_it_scores_best(client, docs, reranker):
    answer = search(client, text="cat", k=2, rerank=reranker)
    # Four times k rows (all four) were read, the text against each row's embedded text.
    [(query, documents)] = ShortReranker.calls
    assert query == "cat" and sorted(documents) == sorted(d["text"] for d in DOCS)
    # The shortest texts: "Stock markets fell", "My cat chased a dog".
    assert [(h["row"]["id"], h["score"]) for h in answer["results"]] == [
        ("3", pytest.approx(1 / 19)), ("4", pytest.approx(1 / 20))]
    assert all("distance" in h for h in answer["results"])
    # Embedding "cat" took 1 token, reading the rows the rest.
    assert answer["tokens"] == 1 + sum(len(d["text"].split()) for d in DOCS)
    # With hybrid too, the reranker's scores replace the fused ones.
    assert [h["row"]["id"] for h in search(client, text="cat", k=2, hybrid=True, rerank=reranker)["results"]] == ["3", "4"]


def test_a_reranker_reads_the_rows_in_requests_the_task_takes(client, docs, reranker, monkeypatch):
    monkeypatch.setattr(Config, "MAX_EMBEDDING_INPUTS", 3)
    assert found(client, text="cat", k=1, rerank=reranker) == ["3"]
    assert [len(documents) for _, documents in ShortReranker.calls] == [3, 1]


def test_rows_without_a_text_to_read_come_after_the_reranked_ones():
    import asyncio

    from db.schema import Field
    from db.vectors import Vectors

    async def rerank(model, query, documents):
        return [len(d) / 10 for d in documents], 7

    store = Vectors()
    store.rerank, store.embed_max_chars = rerank, None
    field = Field("v", "vector", dimensions=2, model="m", from_="text")
    hits = [{"row": {"id": "a", "text": "ab"}, "score": 0.5}, {"row": {"id": "b", "text": " "}, "score": 0.9},
            {"row": {"id": "c", "text": "abcd"}, "distance": 0.2, "score": 0.1}]
    out, tokens = asyncio.run(store._reranked("rr", "q", field, hits, 3))
    assert out == [{"row": {"id": "c", "text": "abcd"}, "distance": 0.2, "score": 0.4},
                   {"row": {"id": "a", "text": "ab"}, "score": 0.2}, {"row": {"id": "b", "text": " "}}]
    assert tokens == 7
    assert asyncio.run(store._reranked("rr", "q", field, hits, 1))[0] == out[:1]


def test_rerank_rules(client, docs, reranker, monkeypatch):
    assert "search by a text" in search(client, status=400, vector=[1.0] * 4, rerank=reranker)["detail"]
    assert client.post("/db/vectors/docs/_search", json={"text": "cat", "rerank": ""}).status_code == 422
    # The reranker is a model like any other: ALLOWED_MODELS holds for it.
    monkeypatch.setattr(Config, "ALLOWED_MODELS", ["kw:embed"])
    assert "ALLOWED_MODELS" in search(client, status=403, text="cat", rerank=reranker)["detail"]
    assert ShortReranker.calls == []


class TopOneReranker(ShortReranker):
    """Scores only the best of the documents it is given, as a server that caps its results might."""

    async def predict(self, request):
        answer = await super().predict(request)
        return {**answer, "results": answer["results"][:1]}


def test_a_reranker_that_scores_fewer_rows_than_it_was_given_breaks_its_contract(client, docs):
    with registered(TopOneReranker, task="rerank", match="top:*"):
        r = client.post("/db/vectors/docs/_search", json={"text": "cat", "k": 2, "rerank": "top:one"})
    assert r.status_code == 500 and "ContractViolation" in r.text, r.text


def test_a_row_written_before_its_vector_field_is_reranked_cut_to_the_longest_text(client, docs, reranker):
    # Nothing checked its length: it was written when no model embedded it.
    fields = [{"name": "text", "type": "string", "fulltext": True}]
    assert client.put("/db/vectors/stale/_schema", json={"fields": fields}).status_code == 200
    assert client.put("/db/vectors/stale/big", json={"text": "zebra " * (Config.MAX_INPUT_CHARS // 6 + 100)}).status_code == 201
    fields.append({"name": "v", "type": "vector", "model": "kw:embed", "from": "text"})
    assert client.put("/db/vectors/stale/_schema", json={"fields": fields}).status_code == 200
    assert client.put("/db/vectors/stale/small", json={"text": "a zebra"}).status_code == 201
    # Only its words find "big", which has no vector; the reranker reads both.
    hits = search(client, table="stale", text="zebra", k=2, hybrid=True, rerank=reranker)["results"]
    assert {h["row"]["id"] for h in hits} == {"big", "small"} and all("score" in h for h in hits)
    [(_, documents)] = ShortReranker.calls
    assert max(len(d) for d in documents) == Config.MAX_INPUT_CHARS


def test_a_field_made_before_its_prompts_shows_none():
    from db.schema import Field

    before = Field.from_dict({"name": "v", "type": "vector", "dimensions": 4, "similarity": "COSINE",
                              "quantization": "NONE", "model": "m", "from": "text"})
    assert before.to_dict() == {"name": "v", "type": "vector", "list": False, "required": False, "indexed": False,
                                "unique": False, "fulltext": False, "dimensions": 4, "similarity": "COSINE",
                                "quantization": "NONE", "model": "m", "from": "text"}
