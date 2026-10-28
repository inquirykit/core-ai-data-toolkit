"""Saved conversations and prompt presets of the playground, and turning a conversation into an eval example."""

import pytest
from fastapi.testclient import TestClient

from api import app as app_module
from api.playground_routes import example_from
from config import Config
from errors import Refused


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        # The test client's thread cannot start the JVM; the main thread does it here.
        app_module.app.state.database.start_jvm()
        yield c


CHAT = {
    "title": "Capitals",
    "settings": {"system": "Be brief.", "temperature": 0.2, "max_tokens": 64},
    "columns": [
        {"model": "org/a", "log": [
            {"role": "user", "content": "Capital of France?"},
            {"role": "assistant", "content": "Paris.", "model": "org/a"},
            {"role": "user", "content": "And Spain?"},
            {"role": "assistant", "content": "Madrid.", "model": "org/a"},
        ]},
        {"model": "org/b", "log": [{"role": "user", "content": "Capital of France?"}]},
    ],
}


def test_conversations_are_saved_listed_replaced_and_deleted(client):
    r = client.post("/playground/conversations", json=CHAT)
    assert r.status_code == 201, r.text
    saved = r.json()
    assert saved["title"] == "Capitals" and saved["columns"][0]["log"][1]["model"] == "org/a"
    assert saved["created"] == saved["updated"]

    listed = client.get("/playground/conversations").json()["conversations"]
    assert listed == [{"id": saved["id"], "title": "Capitals", "created": saved["created"], "updated": saved["updated"],
                       "models": ["org/a", "org/b"], "turns": 2}]
    assert client.get(f"/playground/conversations/{saved['id']}").json()["settings"]["system"] == "Be brief."

    r = client.put(f"/playground/conversations/{saved['id']}", json={**CHAT, "title": "Renamed"})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Renamed" and r.json()["created"] == saved["created"] and r.json()["updated"] >= saved["updated"]
    assert client.put("/playground/conversations/nope", json=CHAT).status_code == 404

    assert client.delete(f"/playground/conversations/{saved['id']}").status_code == 204
    assert client.get(f"/playground/conversations/{saved['id']}").status_code == 404
    assert client.get("/playground/conversations").json()["conversations"] == []

    bad = {**CHAT, "columns": [{"model": "m", "log": [{"role": "system", "content": "x"}]}]}
    assert client.post("/playground/conversations", json=bad).status_code == 422
    assert client.post("/playground/conversations", json={**CHAT, "columns": []}).status_code == 422


def test_presets_are_saved_by_name(client):
    r = client.post("/playground/presets", json={"name": "Terse", "system": "One word answers.", "temperature": 0.1, "max_tokens": 16})
    assert r.status_code == 201, r.text
    terse = r.json()
    client.post("/playground/presets", json={"name": "Chatty", "system": "Elaborate."})
    presets = client.get("/playground/presets").json()["presets"]
    assert [p["name"] for p in presets] == ["Chatty", "Terse"]
    assert presets[0]["temperature"] == 0.7 and presets[0]["max_tokens"] == 256, "defaults"

    r = client.put(f"/playground/presets/{terse['id']}", json={"name": "Terse", "system": "Two words.", "temperature": 0.1, "max_tokens": 16})
    assert r.status_code == 200 and r.json()["system"] == "Two words."
    for p in client.get("/playground/presets").json()["presets"]:
        assert client.delete(f"/playground/presets/{p['id']}").status_code == 204
    assert client.get("/playground/presets").json()["presets"] == []


def test_saving_needs_the_client_key_and_the_database(client, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "secret")
    assert client.get("/playground/presets").status_code == 401
    assert client.get("/playground/presets", headers={"X-API-Key": "secret"}).status_code == 200
    monkeypatch.setattr(Config, "API_KEY", None)
    monkeypatch.setattr(client.app.state, "tables", None)
    assert client.get("/playground/presets").status_code == 503


def test_example_from_a_conversation():
    example = example_from(CHAT, 0, None)
    assert example["reference"] == "Madrid."
    assert example["input"]["messages"] == [
        {"role": "system", "content": "Be brief."},
        {"role": "user", "content": "Capital of France?"},
        {"role": "assistant", "content": "Paris."},
        {"role": "user", "content": "And Spain?"},
    ]
    assert example["input"]["text"] == "System: Be brief.\nUser: Capital of France?\nAssistant: Paris.\nUser: And Spain?\nAssistant:"
    assert example["task"] == "text-generation" and example["tags"] == ["playground", "org/a"]
    # A column without a reply: the whole chat is the input and the reference is the caller's.
    example = example_from(CHAT, 1, "Paris")
    assert example["reference"] == "Paris" and example["input"]["messages"][-1]["content"] == "Capital of France?"
    with pytest.raises(Refused):
        example_from(CHAT, 2, None)
    with pytest.raises(Refused):
        example_from({"columns": [{"model": "m", "log": [{"role": "assistant", "content": "hi"}]}]}, 0, None)


def test_a_conversation_becomes_a_dataset_example(client, monkeypatch):
    saved = client.post("/playground/conversations", json=CHAT).json()
    dataset = client.post("/admin/datasets", json={"name": "from-playground"}).json()
    monkeypatch.setattr(Config, "API_KEY", "client")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin")
    path = f"/playground/conversations/{saved['id']}/example"
    assert client.post(path, json={"dataset_id": dataset["id"]}, headers={"X-API-Key": "client"}).status_code == 401
    r = client.post(path, json={"dataset_id": dataset["id"], "reference": "Madrid"}, headers={"X-API-Key": "admin"})
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 1 and r.json()["example"]["reference"] == "Madrid"
    examples = client.get(f"/admin/datasets/{dataset['id']}/examples", headers={"X-API-Key": "admin"}).json()["examples"]
    assert len(examples) == 1 and examples[0]["reference"] == "Madrid" and "playground" in examples[0]["tags"]
    assert client.post(path, json={"dataset_id": "nope"}, headers={"X-API-Key": "admin"}).status_code == 404
    assert client.post(path, json={"dataset_id": dataset["id"], "column": 5}, headers={"X-API-Key": "admin"}).status_code == 400
    client.delete(f"/admin/datasets/{dataset['id']}", headers={"X-API-Key": "admin"})
    client.delete(f"/playground/conversations/{saved['id']}", headers={"X-API-Key": "admin"})


def test_the_playground_namespace_is_kept_by_the_service(client):
    """Saved chats and presets change through /playground only: /db shows them, read-only, from the start."""
    ns = client.get("/db/playground").json()
    assert ns["managed"]
    assert sorted((t["name"], t["graph"]) for t in ns["tables"]) == [("conversations", False), ("presets", False)]
    # A stray row could break the lists, and a dropped namespace every save after it: both are refused.
    assert client.put("/db/playground/conversations/junk", json={"hello": 1}).status_code == 403
    assert client.delete("/db/playground").status_code == 403
    assert client.post("/db", json={"name": "playground"}).status_code == 409
    assert client.get("/playground/conversations").status_code == 200
