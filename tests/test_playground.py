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


def test_page_is_served_without_a_key(client, monkeypatch, built_pages):
    monkeypatch.setattr(Config, "API_KEY", "secret")
    r = client.get("/playground")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "Playground" in r.text


def test_limits_follow_the_config(client, monkeypatch):
    monkeypatch.setattr(Config, "MAX_MODELS", 2)
    assert client.get("/playground/limits").json() == {
        "max_models": 2,
        "max_generation_length": Config.MAX_GENERATION_LENGTH,
        "max_input_chars": Config.MAX_INPUT_CHARS,
    }


def test_limits_take_the_client_or_admin_key(client, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    assert client.get("/playground/limits").status_code == 401
    for key in ("client-key", "admin-key"):
        assert client.get("/playground/limits", headers={"X-API-Key": key}).status_code == 200
