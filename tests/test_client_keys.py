"""Client keys (access/keys.py, api/key_routes.py, the rule in api/deps.py): one per machine, each expiring or revoked
on its own, opening the client routes only; the admin's stay with ADMIN_API_KEY."""

import time

import pytest
from fastapi.testclient import TestClient

from access import ClientKeys
from access import keys as keys_module
from api import app as app_module
from core.config import Config

ADMIN = {"X-API-Key": "admin-key"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def store(own_db, monkeypatch):
    """A store of its own in place of the app's, so the keys a test makes lock no other test's client routes."""
    keys = ClientKeys(own_db)
    monkeypatch.setattr(app_module.app.state, "client_keys", keys)
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    return keys


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


def make(client, name="worker-1", **body):
    r = client.post("/admin/keys", json={"name": name, **body}, headers=ADMIN)
    assert r.status_code == 201, r.text
    return r.json()


# ------------------------------------------------------------------ the store
def test_a_key_is_shown_once_and_kept_only_as_its_hash(own_db):
    keys = ClientKeys(own_db)
    key, token = keys._make("worker-1")
    assert token.startswith(f"aitk_{key.id}_") and len(key.id) == 8
    assert keys.find(token) is key and key.state() == "live"
    assert keys.find("aitk_nope") is None and keys.find("not-even-a-key") is None
    # Nothing in the database holds the token or its secret.
    rows = own_db.open().rows(f"SELECT * FROM {keys_module.TABLE}")
    assert len(rows) == 1 and token not in repr(rows) and token.split("_", 2)[2] not in repr(rows)
    # A new store reads the same keys back.
    again = ClientKeys(own_db)
    assert again.find(token).id == key.id and again.made_any()


def test_a_key_expires_and_a_revoke_holds_and_cannot_be_undone(own_db):
    keys = ClientKeys(own_db)
    key, token = keys._make("short-lived", expires_in_days=1)
    assert key.expires_at == pytest.approx(key.created_at + 86400)
    assert key.state(now=key.expires_at - 1) == "live" and key.state(now=key.expires_at) == "expired"

    other, _ = keys._make("revoked")
    revoked_at = keys._revoke(other.id).revoked_at
    assert keys._revoke(other.id).revoked_at == revoked_at
    assert ClientKeys(own_db)._listed("revoked")[0].id == other.id
    assert [k.id for k in keys._listed()] == [other.id, key.id]


def test_last_use_is_written_by_the_flush(own_db):
    keys = ClientKeys(own_db)
    key, token = keys._make("worker-1")
    keys.find(token)
    assert ClientKeys(own_db)._listed()[0].last_used_at is None
    keys._flush()
    assert ClientKeys(own_db)._listed()[0].last_used_at == pytest.approx(key.last_used_at)


# ------------------------------------------------------------------ the rule
def test_the_first_key_locks_the_client_routes_and_a_revoke_never_opens_them(client, store):
    assert client.get("/tasks").status_code == 200  # No API_KEY, no client key: open.
    made = make(client)
    token = made["token"]
    assert client.get("/tasks").status_code == 401
    assert client.get("/tasks", headers=bearer(token)).status_code == 200
    assert client.get("/tasks", headers={"X-API-Key": token}).status_code == 200
    assert client.get("/tasks", headers=ADMIN).status_code == 200

    r = client.post(f"/admin/keys/{made['id']}/revoke", headers=ADMIN)
    assert r.status_code == 200 and r.json()["state"] == "revoked"
    r = client.get("/tasks", headers=bearer(token))
    assert r.status_code == 401 and r.json()["detail"] == "API key revoked"
    assert client.get("/tasks").status_code == 401


def test_an_expired_key_is_refused_saying_so(client, store):
    made = make(client, expires_in_days=1)
    assert made["state"] == "live"
    store._listed()[0].expires_at = time.time() - 1
    r = client.get("/tasks", headers=bearer(made["token"]))
    assert r.status_code == 401 and r.json()["detail"] == "API key expired"


def test_a_client_key_never_opens_the_admin_routes(client, store):
    token = make(client)["token"]
    assert client.get("/admin/keys", headers=bearer(token)).status_code == 401
    assert client.post("/admin/keys", json={"name": "mine"}, headers=bearer(token)).status_code == 401
    assert client.get("/admin/config", headers=bearer(token)).status_code == 401


def test_api_key_and_client_keys_work_side_by_side(client, store, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "plain-client-key")
    token = make(client)["token"]
    for headers in ({"X-API-Key": "plain-client-key"}, bearer(token), ADMIN):
        assert client.get("/tasks", headers=headers).status_code == 200
    assert client.get("/admin/keys", headers={"X-API-Key": "plain-client-key"}).status_code == 401


def test_keys_are_made_only_under_the_admin_key(client, store, monkeypatch):
    monkeypatch.setattr(Config, "ADMIN_API_KEY", None)
    r = client.post("/admin/keys", json={"name": "worker-1"})
    assert r.status_code == 409 and "ADMIN_API_KEY" in r.json()["detail"]
    assert not store.made_any()


def test_the_warnings_know_client_keys_lock_the_client_routes(client, store):
    def warned():
        return [w for w in client.get("/admin/config", headers=ADMIN).json()["warnings"] if "unauthenticated" in w]

    assert warned()
    make(client)
    assert not warned()


# ------------------------------------------------------------------ the routes
def test_the_list_pages_newest_first_and_never_holds_a_token(client, store):
    first, second = make(client, "a"), make(client, "b")
    r = client.get("/admin/keys", params={"limit": 1}, headers=ADMIN)
    page = r.json()
    assert r.status_code == 200 and page["clients_need_a_key"] is True
    assert [k["id"] for k in page["keys"]] == [second["id"]]
    # A key made meanwhile goes at the top, and the next page goes on where the first stopped.
    third = make(client, "c")
    rest = client.get("/admin/keys", params={"limit": 1, "cursor": page["next"]}, headers=ADMIN).json()
    assert [k["id"] for k in rest["keys"]] == [first["id"]] and "next" not in rest
    shown = repr(page) + repr(rest)
    assert first["token"] not in shown and second["token"] not in shown and "hash" not in shown

    client.post(f"/admin/keys/{first['id']}/revoke", headers=ADMIN)
    live = client.get("/admin/keys", params={"state": "live"}, headers=ADMIN).json()["keys"]
    assert [k["id"] for k in live] == [third["id"], second["id"]]


def test_a_bad_request_is_refused(client, store):
    assert client.post("/admin/keys", json={"name": "   "}, headers=ADMIN).status_code == 422
    assert client.post("/admin/keys", json={"name": "x", "expires_in_days": 0}, headers=ADMIN).status_code == 422
    assert client.post("/admin/keys/nope/revoke", headers=ADMIN).status_code == 404


def test_the_audit_log_names_the_client_key(client, store):
    made = make(client)
    started = time.time()
    # A client key on an admin route: refused, and kept with the key it carried.
    assert client.post(f"/admin/keys/{made['id']}/revoke", headers=bearer(made["token"])).status_code == 401
    app_module.app.state.audit.flush()
    entries = client.get("/admin/audit", params={"since": started}, headers=ADMIN).json()["entries"]
    refused = [e for e in entries if e["status"] == 401 and e["route"] == "/admin/keys/{key_id}/revoke"]
    assert refused and refused[0]["actor"] == "client" and refused[0]["key_id"] == made["id"]
