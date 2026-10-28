import pytest
from fastapi.testclient import TestClient

import envfile
from api import app as app_module
from config import Config
from models.generic.transformers_model import TransformersModel
from tests.fakes import FakeModel, swapped

ADMIN = "admin-key-0123456789abcdef"
CLIENT = "client-key-0123456789abcdef"


@pytest.fixture(scope="module")
def client():
    with swapped(TransformersModel, FakeModel), TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def fresh(client, tmp_path, monkeypatch):
    """An unconfigured instance writing to a throwaway settings file; restored afterwards."""
    env_file = tmp_path / "settings.env"
    monkeypatch.setattr(Config, "ENV_FILE", env_file)
    for name in ("API_KEY", "ADMIN_API_KEY", "ALLOWED_MODELS", "SETUP_WIZARD", "CORS_ORIGINS"):
        monkeypatch.setattr(Config, name, getattr(Config, name))
        monkeypatch.setenv(name, "")
    monkeypatch.setattr(Config, "SETUP_WIZARD", True)
    monkeypatch.setattr(client.app.state, "setup_code", "the-setup-code")
    return env_file


def setup(client, **overrides):
    body = {"code": "the-setup-code", "admin_api_key": ADMIN, "api_key": CLIENT,
            "allowed_models": ["gpt2", "org/model"], "cors_origins": ["http://localhost:3000"], **overrides}
    return client.post("/admin/setup", json=body)


def test_status_and_pages_are_public(client, fresh, built_pages):
    assert client.get("/admin/setup/status").json() == {"needed": True}
    assert client.get("/admin/setup").status_code == 200


def test_setup_writes_the_file_and_locks_the_api(client, fresh):
    r = setup(client)
    assert r.status_code == 200, r.text
    assert r.json()["env_file"] == str(fresh)

    written = envfile.parse(fresh.read_text())
    assert written == {
        "ADMIN_API_KEY": ADMIN,
        "API_KEY": CLIENT,
        "ALLOWED_MODELS": "gpt2,org/model",
        "CORS_ORIGINS": "http://localhost:3000",
    }
    # Keys and the allow-list apply at once; CORS needs a restart.
    assert r.json()["restart_required"] is True
    assert client.get("/admin/setup/status").json() == {"needed": False}
    assert client.get("/admin/stats").status_code == 401
    assert client.get("/admin/stats", headers={"X-API-Key": ADMIN}).status_code == 200
    assert client.get("/v1/models").status_code == 401
    r = client.post("/inference", headers={"X-API-Key": CLIENT},
                    json={"model_id": "evil/model", "task": "text-generation", "input": {"text": "x"}})
    assert r.status_code == 403

    # It runs once.
    assert setup(client).status_code == 409


def test_wrong_or_missing_code_is_refused(client, fresh):
    assert setup(client, code="guess").status_code == 403
    assert not fresh.exists()
    client.app.state.setup_code = None
    assert setup(client).status_code == 403


def test_any_model_and_no_client_key(client, fresh):
    r = setup(client, api_key=None, allowed_models=None, cors_origins=[])
    assert r.status_code == 200, r.text
    written = envfile.parse(fresh.read_text())
    assert written["API_KEY"] == "" and written["ALLOWED_MODELS"] == "" and written["CORS_ORIGINS"] == ""
    assert client.get("/v1/models").status_code == 200  # clients need no key
    assert client.get("/admin/stats").status_code == 401  # the admin still does


@pytest.mark.parametrize("overrides", [
    {"admin_api_key": "short"},
    {"admin_api_key": "has spaces in it 0123456789"},
    {"admin_api_key": "line\nINJECTED=1-0123456789"},
    {"api_key": ADMIN},
    {"allowed_models": []},
    {"allowed_models": ["a,b"]},
    {"allowed_models": ["model\nAPI_KEY="]},
    {"cors_origins": ["http://site.example/path"]},
    {"cors_origins": ["javascript:alert(1)"]},
    {"extra": 1},
])
def test_unsafe_values_are_rejected(client, fresh, overrides):
    assert setup(client, **overrides).status_code == 422
    assert not fresh.exists()


def test_skip_turns_the_wizard_off(client, fresh):
    assert client.post("/admin/setup/skip", json={"code": "nope"}).status_code == 403
    assert client.post("/admin/setup/skip", json={"code": "the-setup-code"}).status_code == 200
    assert envfile.parse(fresh.read_text()) == {"SETUP_WIZARD": "false"}
    assert client.get("/admin/setup/status").json() == {"needed": False}
    assert client.get("/admin/stats").status_code == 200  # still open: no keys


def test_a_configured_key_means_no_setup(client, fresh, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "already-configured")
    assert client.get("/admin/setup/status").json() == {"needed": False}
    assert setup(client).status_code == 409
