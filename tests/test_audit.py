"""The audit log (api/audit.py, monitoring/audit.py): which requests it keeps, what an entry says, and reading it."""

import time

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("arcadedb_embedded")

from api import app as app_module  # noqa: E402
from api import audit  # noqa: E402
from core.config import Config  # noqa: E402
from monitoring import AuditLog  # noqa: E402


@pytest.fixture(scope="module")
def client():
    app_module.app.state.database.start_jvm()
    with TestClient(app_module.app) as c:
        yield c


def entries(client, headers=None, **params):
    app_module.app.state.audit.flush()
    r = client.get("/admin/audit", params=params, headers=headers or {})
    assert r.status_code == 200, r.text
    assert r.json()["on"] is True
    return r.json()["entries"]


def test_every_admin_change_and_every_delete_is_kept_but_reads_are_not():
    kept = audit.routes(app_module.app)
    for route in ("PUT /admin/limits", "POST /db", "PUT /db/{namespace}/{table}/_schema", "POST /jobs",
                  "PUT /enrichers/{name}/secret", "POST /models/load", "POST /admin/setup",
                  "DELETE /db/{namespace}/{table}/{row_id}", "DELETE /playground/conversations/{conversation_id}"):
        assert route in kept, route
    # The admin POSTs marked audit.reads are left out too.
    for route in ("POST /inference", "PUT /db/{namespace}/{table}/{row_id}", "POST /evidence/captures",
                  "GET /admin/config", "POST /db/{namespace}/_query", "POST /admin/model-servers/probe",
                  "POST /enrichers/{name}/test", "POST /jobs/webhooks/test"):
        assert route not in kept, route


def test_an_entry_says_what_was_asked_with_which_key_and_how_it_went(client, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    admin, as_client = {"X-API-Key": "admin-key"}, {"Authorization": "Bearer client-key"}
    started = time.time()
    assert client.post("/db", json={"name": "audited", "secret_marker": "s3cr3t"}, headers=admin).status_code == 422
    assert client.post("/db", json={"name": "audited"}, headers=admin).status_code == 201
    assert client.put("/db/audited/notes/n1", json={"text": "x"}, headers=as_client).status_code == 201
    assert client.get("/db/audited/notes/n1", headers=as_client).status_code == 200
    assert client.post("/db/audited/_query", json={"language": "sql", "query": "SELECT 1"}, headers=admin).status_code == 200
    assert client.delete("/db/audited/notes/n1?why=cleanup", headers=as_client).status_code == 204
    assert client.delete("/db/audited", headers=as_client).status_code == 401
    assert client.delete("/db/audited", headers={"X-API-Key": "nope"}).status_code == 401
    assert client.delete("/db/audited").status_code == 401
    assert client.delete("/db/audited", headers={**admin, "User-Agent": "audit-test", "X-Forwarded-For": "10.0.0.7"}).status_code == 204

    found = [e for e in entries(client, admin, since=started) if e["route"].startswith("/db")]
    # Oldest first: the refused body and the namespace made, then the deletes with each kind of key. The row a
    # client key wrote, the GET and the admin's read-only query are not kept.
    assert [(e["method"], e["path"], e["status"], e["actor"]) for e in reversed(found)] == [
        ("POST", "/db", 422, "admin"),
        ("POST", "/db", 201, "admin"),
        ("DELETE", "/db/audited/notes/n1?why=cleanup", 204, "client"),
        ("DELETE", "/db/audited", 401, "client"),
        ("DELETE", "/db/audited", 401, "wrong"),
        ("DELETE", "/db/audited", 401, "none"),
        ("DELETE", "/db/audited", 204, "admin"),
    ]
    last = found[0]
    assert last["route"] == "/db/{namespace}" and last["agent"] == "audit-test" and last["forwarded"] == "10.0.0.7"
    assert last["ip"] and last["duration_ms"] >= 0 and last["ts"] >= started
    # No body is kept.
    assert not any("s3cr3t" in str(e) for e in found)
    # A filter's ? is a question mark, not ArcadeDB's wildcard.
    assert [e["path"] for e in entries(client, admin, since=started, route="?why=")] == ["/db/audited/notes/n1?why=cleanup"]


def test_the_log_is_read_newest_first_by_filter_and_page(client):
    started = time.time()
    for name in ("a1", "a2", "a3"):
        assert client.post("/db", json={"name": name}).status_code in (201, 409)
    assert client.post("/db", json={"name": "a1"}).status_code == 409
    assert client.delete("/db/a2").status_code == 204
    recent = entries(client, since=started)
    assert [e["path"] for e in recent] == ["/db/a2", "/db", "/db", "/db", "/db"]
    assert [e["status"] for e in entries(client, since=started, outcome="refused")] == [409]
    assert [e["method"] for e in entries(client, since=started, method="DELETE")] == ["DELETE"]
    assert len(entries(client, since=started, route="{namespace}")) == 1 and entries(client, since=started, actor="open")
    assert entries(client, since=started, actor="admin") == []
    first = entries(client, since=started, limit=2)
    rest = entries(client, since=started, before_id=first[-1]["id"])
    assert [e["id"] for e in first + rest] == [e["id"] for e in recent]
    assert client.get("/admin/audit", params={"outcome": "maybe"}).status_code == 422


def test_the_log_needs_the_admin_key(client, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    assert client.get("/admin/audit", headers={"X-API-Key": "client-key"}).status_code == 401
    assert client.get("/admin/audit", headers={"X-API-Key": "admin-key"}).status_code == 200


def test_old_entries_are_pruned_and_a_log_switched_off_keeps_none(client, arcade, monkeypatch):
    import asyncio

    log = AuditLog(arcade, retention_days=1)
    for ts in (time.time() - 3 * 86400, time.time()):
        log.record(ts=ts, method="DELETE", route="/x", path="/x", status=204, duration_ms=1, actor="open")
    log.flush()
    assert asyncio.run(log.prune()) == 1
    assert [e["route"] for e in asyncio.run(log.entries())] == ["/x"]
    log.close()

    monkeypatch.setattr(app_module.app.state, "audit", None)
    assert client.delete("/db/never-there").status_code in (204, 404)
    r = client.get("/admin/audit")
    assert r.status_code == 200 and r.json() == {"entries": [], "on": False, "retention_days": Config.AUDIT_RETENTION_DAYS}
