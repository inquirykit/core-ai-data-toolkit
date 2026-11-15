"""A client key's grants (access/grants.py, checked by the guards in api/deps.py): the tables it may read, write and
delete in, the models it may use, and whether it manages models. A key without grants reaches what any client key
does."""

import pytest
from fastapi.testclient import TestClient

from access import ClientKeys, Grants
from access.grants import MANAGE_MODELS, NEEDS, allows, target
from api import app as app_module
from core.config import Config
from models.families.generic.transformers_model import TransformersModel
from .fakes import FakeModel, swapped
from .test_access import CLIENT_ROUTES, _routes
from .test_allowed_models import OTHER, REQUESTS

ADMIN = {"X-API-Key": "admin-key"}


@pytest.fixture
def keys(own_db, monkeypatch):
    """A store of its own in place of the app's, so the keys a test makes lock no other test's client routes."""
    store = ClientKeys(own_db)
    monkeypatch.setattr(app_module.app.state, "client_keys", store)
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    return store


@pytest.fixture
def client(keys):
    with swapped(TransformersModel, FakeModel), TestClient(app_module.app) as c:
        yield c


def key(keys, **grants):
    """The headers of a new key with these grants."""
    _, token = keys._make("worker", grants=Grants(**grants))
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ the rule
def test_entries_add_up():
    tables = {"docs.*": ["read"], "docs.notes": ["write", "delete"], "*.*": ["read"]}
    assert allows(tables, "docs", "notes", "read") and allows(tables, "docs", "notes", "delete")
    assert allows(tables, "docs", "people", "read") and not allows(tables, "docs", "people", "write")
    assert allows(tables, "crm", "deals", "read") and not allows(tables, "crm", "deals", "delete")
    assert not allows({"docs.notes": ["read"]}, "docs", "people", "read")  # no entry: shut


def test_the_whole_workspace_takes_its_own_entry():
    assert allows({"docs.*": ["read"], "docs._evidence": []}, "docs", "*", "read")
    assert not allows({"docs.notes": ["read"], "docs.people": ["read"]}, "docs", "*", "read")
    assert not allows({"docs.*": ["read"]}, "*", "*", "read")  # every workspace: *.*
    assert allows({"*.*": ["read"]}, "*", "*", "read")


def test_what_a_route_reaches():
    assert target("/db/{workspace}/{table}", {"workspace": "docs", "table": "notes"}, {}) == ("docs", "notes")
    assert target("/db/{workspace}/{bucket}/_files", {"workspace": "docs", "bucket": "pdfs"}, {}) == ("docs", "pdfs")
    assert target("/db/{workspace}/{table}", {"workspace": "docs", "table": "notes"}, {"_expand": "x"}) == ("docs", "*")
    assert target("/db/{workspace}/{table}/{row_id}/_neighbors", {"workspace": "docs", "table": "notes"}, {}) == ("docs", "*")
    assert target("/db/{workspace}/_transaction", {"workspace": "docs"}, {}) == ("docs", "*")
    assert target("/db", {}, {}) == ("*", "*")
    assert target("/evidence/cases", {}, {}) == ("default", "_evidence")
    assert target("/evidence/cases", {}, {"workspace": "crm"}) == ("crm", "_evidence")
    assert target("/evidence/public-key", {}, {}) is None
    assert target("/inference", {}, {}) is None


def test_the_listed_routes_are_the_apps():
    """A route renamed would leave its line here doing nothing: each one names a route as the app serves it."""
    served = {(level, name) for level, name, _ in _routes()}
    assert all(("client", name) in served for name in NEEDS)
    assert MANAGE_MODELS <= {name.split(" ", 1)[1] for level, name in served if level == "admin"}
    # A client route that deletes by POST must say so, or a key that may only write could delete through it.
    deleting = {name for level, name, _ in _routes() if level == "client" and "delete" in name and name.startswith("POST")}
    assert deleting and all("delete" in NEEDS.get(name, []) for name in deleting)


# ------------------------------------------------------------------ through the app
def test_a_key_reaches_only_the_tables_and_actions_granted(client, keys):
    headers = key(keys, tables={"docs.notes": ["read"], "docs.drafts": ["write"]})
    assert client.get("/db/docs/notes", headers=headers).status_code != 403
    assert client.post("/db/docs/notes/_bulk-get", json={"ids": ["a"]}, headers=headers).status_code != 403
    refused = [
        ("POST", "/db/docs/notes", {"id": "a"}),
        ("DELETE", "/db/docs/notes/a", None),
        ("POST", "/db/docs/notes/_bulk-delete", {"ids": ["a"]}),
        ("GET", "/db/docs/people", None),  # no entry
        ("GET", "/db/crm/notes", None),
        ("PATCH", "/db/docs/drafts/a", {"x": 1}),  # it answers the row: write without read is not enough
    ]
    for method, path, body in refused:
        r = client.request(method, path, json=body, headers=headers)
        assert r.status_code == 403, f"{method} {path}: {r.status_code} {r.text[:200]}"
    assert client.put("/db/docs/drafts/a", json={"x": 1}, headers=headers).status_code != 403


def test_what_reaches_past_one_table_needs_the_whole_workspace(client, keys):
    one = key(keys, tables={"docs.notes": ["read", "write", "delete"]})
    whole = key(keys, tables={"docs.*": ["read", "write", "delete"]})
    for path in ("/db/docs", "/db/docs/notes?_expand=author", "/db/docs/notes/a/_neighbors", "/db/docs/_graph"):
        assert client.get(path, headers=one).status_code == 403, path
        assert client.get(path, headers=whole).status_code != 403, path
    body = {"puts": {"notes": [{"id": "a"}]}}
    assert client.post("/db/docs/_transaction", json=body, headers=one).status_code == 403
    assert client.get("/db", headers=whole).status_code == 403  # every workspace: *.*
    assert client.get("/db", headers=key(keys, tables={"*.*": ["read"]})).status_code == 200


def test_evidence_is_the_workspaces_evidence_entry(client, keys):
    headers = key(keys, tables={"default._evidence": ["read"]})
    assert client.get("/evidence/cases", headers=headers).status_code == 200
    assert client.get("/evidence/public-key", headers=key(keys, tables={"crm._evidence": ["read"]})).status_code != 403
    assert client.post("/evidence/cases", json={}, headers=headers).status_code == 403
    assert client.get("/evidence/cases", params={"workspace": "crm"}, headers=headers).status_code == 403
    assert client.get("/db/default/notes", headers=headers).status_code == 403


def test_a_key_without_grants_reaches_every_table(client, keys):
    _, token = keys._make("worker")
    assert client.get("/db", headers={"X-API-Key": token}).status_code == 200


@pytest.mark.parametrize("route", sorted(r for r in REQUESTS if r in CLIENT_ROUTES or r == "POST /models/load"))
def test_a_model_outside_the_keys_models_is_forbidden(route, client, keys):
    """Every client route that runs a model, as tests/test_allowed_models.py finds them, holds a key to its models."""
    headers = key(keys, models=["allowed/model"], manage_models=True)
    method, path = route.split(" ", 1)
    r = client.request(method, path, headers=headers, **REQUESTS[route])
    assert r.status_code == 403, f"{route}: {r.status_code} {r.text[:200]}"
    assert "this key's models" in r.json()["detail"]


def test_a_key_runs_its_own_models(client, keys):
    headers = key(keys, models=["allowed/model"])
    body = {"model_id": "allowed/model", "task": "text-generation", "input": {"text": "hi"}}
    assert client.post("/inference", json=body, headers=headers).status_code == 200
    assert client.post("/inference", json={**body, "model_id": OTHER}, headers=headers).status_code == 403


def test_manage_models_opens_load_unload_and_pin_only(client, keys):
    load = {"model_id": "some/model", "task": "text-generation"}
    plain = key(keys)
    assert client.post("/models/load", json=load, headers=plain).status_code == 401
    manager = key(keys, manage_models=True)
    assert client.post("/models/load", json=load, headers=manager).status_code == 200
    assert client.put("/models/some/model/text-generation/pin", json={"pinned": True}, headers=manager).status_code == 200
    assert client.delete("/models/some/model/text-generation", headers=manager).status_code == 200
    assert client.delete("/models/clear", headers=manager).status_code == 401
    assert client.get("/models/status", headers=manager).status_code == 401


def test_a_manager_of_some_models_unloads_and_pins_only_those(client, keys):
    assert client.post("/models/load", json={"model_id": OTHER, "task": "text-generation"}, headers=ADMIN).status_code == 200
    manager = key(keys, models=["allowed/model"], manage_models=True)
    assert client.put(f"/models/{OTHER}/text-generation/pin", json={"pinned": True}, headers=manager).status_code == 403
    assert client.delete(f"/models/{OTHER}/text-generation", headers=manager).status_code == 403
    assert client.delete(f"/models/{OTHER}/text-generation", headers=ADMIN).status_code == 200


# ------------------------------------------------------------------ the routes
def test_grants_are_made_changed_and_kept(client, keys, own_db):
    none = {"tables": None, "models": None, "manage_models": False}
    for body in ({"name": "a"}, {"name": "b", "grants": {}}):
        assert client.post("/admin/keys", json=body, headers=ADMIN).json()["grants"] == none
    grants = {"tables": {"docs.*": ["read"]}, "models": None, "manage_models": False}
    r = client.post("/admin/keys", json={"name": "worker", "grants": grants}, headers=ADMIN)
    assert r.status_code == 201 and r.json()["grants"] == grants
    made, headers = r.json(), {"X-API-Key": r.json()["token"]}
    assert client.get("/db/docs/notes", headers=headers).status_code != 403
    assert client.get("/db/crm/notes", headers=headers).status_code == 403

    changed = {"tables": {"crm.*": ["read"]}, "models": ["a/b"], "manage_models": True}
    r = client.patch(f"/admin/keys/{made['id']}", json={"grants": changed}, headers=ADMIN)
    assert r.status_code == 200 and r.json()["grants"] == changed
    assert client.get("/db/crm/notes", headers=headers).status_code != 403  # from the next request on
    assert client.get("/db/docs/notes", headers=headers).status_code == 403
    assert ClientKeys(own_db)._listed()[0].grants == Grants(**changed)

    assert client.patch(f"/admin/keys/{made['id']}", json={"grants": {}}, headers=ADMIN).json()["grants"] == none
    assert client.get("/db/docs/notes", headers=headers).status_code != 403
    assert client.patch("/admin/keys/nope", json={"grants": {}}, headers=ADMIN).status_code == 404


@pytest.mark.parametrize("grants", [
    {"tables": {"*.notes": ["read"]}},  # a table of every workspace is not an entry
    {"tables": {"docs": ["read"]}},
    {"tables": {"docs.notes": ["drop"]}},
    {"tables": {"docs._views": ["read"]}},
    {"manage": True},
])
def test_bad_grants_are_refused(client, keys, grants):
    r = client.post("/admin/keys", json={"name": "w", "grants": grants}, headers=ADMIN)
    assert r.status_code == 422
    if "tables" in grants and "drop" not in repr(grants):
        assert "is not workspace.table, workspace.* or *.*" in r.text
