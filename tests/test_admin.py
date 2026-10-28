import logging
import time

import pytest
from fastapi.testclient import TestClient

from api import app as app_module
from config import Config
from models.generic.transformers_model import TransformersModel
from tests.fakes import FakeModel, swapped


@pytest.fixture(scope="module")
def client():
    with swapped(TransformersModel, FakeModel), TestClient(app_module.app) as c:
        yield c


def flush(client):
    client.app.state.monitoring.flush()


def infer(client, model_id="admin/model", **headers):
    return client.post("/inference", headers=headers,
                       json={"model_id": model_id, "task": "text-generation", "input": {"text": "secret prompt"}})


def test_dashboard_page_is_served_without_a_key(client, monkeypatch, built_pages):
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "adm")
    r = client.get("/admin")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "AI &amp; Data Toolkit" in r.text


def test_stats_reflect_requests_and_loaded_models(client):
    assert infer(client).status_code == 200
    flush(client)
    body = client.get("/admin/stats?window=1h").json()
    assert body["monitoring"]["enabled"] is True
    assert "admin/model:text-generation" in body["cache"]["model_details"]
    model = next(m for m in body["models"] if m["model_key"] == "admin/model:text-generation")
    assert model["requests"] >= 1 and model["cold_starts"] >= 1
    assert body["totals"]["requests"] >= 1
    assert len(body["timeline"]) == 60
    assert body["process"]["pid"] > 0


def test_request_inputs_are_never_recorded(client):
    infer(client)
    flush(client)
    assert "secret prompt" not in client.get("/admin/requests").text
    assert "secret prompt" not in client.get("/admin/stats").text


def test_events_follow_load_and_unload(client):
    assert client.post("/models/load", json={"model_id": "ev/model", "task": "text-generation"}).status_code == 200
    assert client.delete("/models/ev/model/text-generation").status_code == 200
    flush(client)
    events = client.get("/admin/events").json()["events"]
    mine = [(e["kind"], e["detail"]) for e in events if e["model_key"] == "ev/model:text-generation"]
    assert mine[:2] == [("unload", "manual"), ("load", "FakeModel")]
    newest = events[0]["id"]
    assert client.get(f"/admin/events?after_id={newest}").json()["events"] == []


def test_bad_window_is_rejected(client):
    assert client.get("/admin/stats?window=1y").status_code == 422


def test_timeline_covers_any_range_at_any_resolution(client):
    assert infer(client, model_id="tl/model").status_code == 200
    flush(client)
    now = time.time()
    body = client.get(f"/admin/timeline?since={now - 120}&buckets=24").json()
    assert len(body["timeline"]) == 24
    assert body["bucket_s"] == pytest.approx(5, abs=0.5)
    assert body["totals"]["requests"] >= 1
    assert any(m["model_key"] == "tl/model:text-generation" for m in body["models"])
    # An explicit end, before the request: nothing in it.
    empty = client.get(f"/admin/timeline?since={now - 7200}&until={now - 3600}&buckets=10").json()
    assert empty["totals"]["requests"] == 0 and len(empty["timeline"]) == 10
    assert client.get(f"/admin/timeline?since={now}&until={now - 1}").status_code == 400
    assert client.get(f"/admin/timeline?since={now - 100 * 86400}").status_code == 400
    assert client.get(f"/admin/timeline?since={now}&buckets=0").status_code == 422
    assert client.get("/admin/timeline").status_code == 422


def test_requests_and_events_filter_by_range_and_kind(client):
    assert infer(client, model_id="flt/model").status_code == 200
    flush(client)
    now = time.time()
    rows = client.get(f"/admin/requests?since={now - 60}&model=FLT/&status=ok").json()["requests"]
    assert rows and all(r["model_id"] == "flt/model" and r["status"] == "ok" for r in rows)
    assert client.get(f"/admin/requests?until={now - 3600}&model=flt/").json()["requests"] == []
    assert client.get("/admin/requests?status=weird").status_code == 422
    events = client.get(f"/admin/events?since={now - 60}").json()["events"]
    assert any(e["model_key"] == "flt/model:text-generation" and e["kind"] == "load" for e in events)
    assert client.get(f"/admin/events?until={now - 3600}&limit=5").json()["events"] == []


def test_logs_are_served_from_the_buffer(client):
    logging.getLogger("tests.admin").warning("dashboard log line %d", 42)
    body = client.get("/admin/logs").json()
    mine = [l for l in body["lines"] if l["logger"] == "tests.admin"]
    assert mine and mine[-1]["message"] == "dashboard log line 42" and mine[-1]["level"] == "WARNING"
    assert body["capacity"] > 0 and body["level"] in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
    newest = body["lines"][-1]["id"]
    assert client.get(f"/admin/logs?after_id={newest}").json()["lines"] == []
    assert all(l["level"] in ("ERROR", "CRITICAL") for l in client.get("/admin/logs?level=ERROR").json()["lines"])
    assert client.get("/admin/logs?level=LOUD").status_code == 422


def test_config_hides_keys_and_lists_warnings(client, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    body = client.get("/admin/config", headers={"X-API-Key": "admin-key"}).json()
    assert body["settings"]["API_KEY"] == "set"
    assert "client-key" not in str(body) and "admin-key" not in str(body)
    assert any("ALLOWED_MODELS" in w for w in body["warnings"])


ADMIN_ROUTES = [
    ("get", "/admin/stats"),
    ("get", f"/admin/timeline?since={time.time() - 60}"),
    ("get", "/admin/events"),
    ("get", "/admin/requests"),
    ("get", "/admin/logs"),
    ("get", "/admin/config"),
    ("get", "/models/status"),
    ("delete", "/models/clear"),
]


@pytest.mark.parametrize("method, path", ADMIN_ROUTES)
def test_admin_routes_need_the_admin_key(client, monkeypatch, method, path):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    call = getattr(client, method)
    assert call(path).status_code == 401
    assert call(path, headers={"X-API-Key": "client-key"}).status_code == 401
    assert call(path, headers={"Authorization": "Bearer admin-key"}).status_code == 200


def test_loading_needs_the_admin_key(client, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    payload = {"model_id": "k/model", "task": "text-generation"}
    assert client.post("/models/load", json=payload, headers={"X-API-Key": "client-key"}).status_code == 401
    assert client.post("/models/load", json=payload, headers={"X-API-Key": "admin-key"}).status_code == 200
    assert client.delete("/models/k/model/text-generation", headers={"X-API-Key": "client-key"}).status_code == 401


def test_client_routes_accept_either_key(client, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    assert infer(client, **{"X-API-Key": "client-key"}).status_code == 200
    assert infer(client, **{"X-API-Key": "admin-key"}).status_code == 200
    assert infer(client, **{"X-API-Key": "nope"}).status_code == 401
    assert client.get("/v1/models", headers={"X-API-Key": "admin-key"}).status_code == 200


def test_without_an_admin_key_api_key_guards_admin_routes(client, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    assert client.get("/admin/stats").status_code == 401
    assert client.get("/admin/stats", headers={"X-API-Key": "client-key"}).status_code == 200


@pytest.fixture
def limits(client, tmp_path, monkeypatch):
    """Restore the cache's limits and settings after a test changes them."""
    import os

    import envfile

    manager = client.app.state.manager
    saved = (manager.cache.max_models, manager.cache.max_memory_gb, Config.MAX_MODELS, Config.MAX_MEMORY_GB)
    env = {k: os.environ.get(k) for k in ("MAX_MODELS", "MAX_MEMORY_GB")}
    env_file = tmp_path / "settings.env"
    monkeypatch.setattr(Config, "ENV_FILE", env_file)
    yield manager, lambda: envfile.parse(env_file.read_text())
    manager.cache.max_models, manager.cache.max_memory_gb, Config.MAX_MODELS, Config.MAX_MEMORY_GB = saved
    for key, value in env.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def test_limits_apply_now_and_are_saved(client, limits):
    manager, saved_file = limits
    for name in ("lim/a", "lim/b", "lim/c"):
        assert infer(client, model_id=name).status_code == 200
    r = client.put("/admin/limits", json={"max_models": 1, "max_memory_gb": 4.5})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["max_models"], body["max_memory_gb"]) == (1, 4.5)
    # Lowering the count unloaded the least recently used models at once.
    assert manager.get_cache_status()["cache_size"] == 1
    assert "lim/a:text-generation" in body["evicted"] and "lim/b:text-generation" in body["evicted"]
    assert saved_file() == {"MAX_MODELS": "1", "MAX_MEMORY_GB": "4.5"}
    assert client.get("/playground/limits").json()["max_models"] == 1
    assert client.get("/admin/config").json()["settings"]["MAX_MEMORY_GB"] == 4.5
    flush(client)
    events = client.get("/admin/events").json()["events"]
    assert any(e["kind"] == "limits" and "MAX_MODELS=1" in e["detail"] for e in events)


def test_limits_one_at_a_time_and_validated(client, limits):
    manager, saved_file = limits
    assert client.put("/admin/limits", json={"max_memory_gb": 0}).status_code == 200
    assert manager.cache.budget is None  # 0: no budget
    assert saved_file() == {"MAX_MEMORY_GB": "0.0"}
    assert client.put("/admin/limits", json={}).status_code == 400
    assert client.put("/admin/limits", json={"max_models": 0}).status_code == 422
    assert client.put("/admin/limits", json={"max_memory_gb": -1}).status_code == 422
    assert client.put("/admin/limits", json={"max_models": 2, "colour": "red"}).status_code == 422


def test_limits_warn_when_the_environment_wins(client, limits, monkeypatch):
    import config

    monkeypatch.setenv("MAX_MEMORY_GB", "8.0")
    monkeypatch.setattr(config, "_FROM_FILE", set())
    notes = client.put("/admin/limits", json={"max_memory_gb": 6}).json()["notes"]
    assert any("MAX_MEMORY_GB is also set in the environment" in n for n in notes)


def test_limits_need_the_admin_key(client, limits, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    assert client.put("/admin/limits", json={"max_models": 2}, headers={"X-API-Key": "client-key"}).status_code == 401
    assert client.put("/admin/limits", json={"max_models": 2}, headers={"X-API-Key": "admin-key"}).status_code == 200
