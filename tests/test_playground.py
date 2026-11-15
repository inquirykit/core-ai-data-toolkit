import pytest
from fastapi.testclient import TestClient

from api import app as app_module
from core.config import Config
from models.families.generic.transformers_model import TransformersModel
from tests.fakes import FakeModel, swapped


@pytest.fixture(scope="module")
def client():
    with swapped(TransformersModel, FakeModel), TestClient(app_module.app) as c:
        yield c


def test_limits_follow_the_config(client, monkeypatch):
    monkeypatch.setattr(Config, "MAX_MODELS", 2)
    assert client.get("/playground/limits").json() == {
        "max_models": 2,
        "max_generation_length": Config.MAX_GENERATION_LENGTH,
        "max_input_chars": Config.MAX_INPUT_CHARS,
    }


def test_limits_take_the_admin_key(client, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    assert client.get("/playground/limits").status_code == 401
    assert client.get("/playground/limits", headers={"X-API-Key": "client-key"}).status_code == 401
    assert client.get("/playground/limits", headers={"X-API-Key": "admin-key"}).status_code == 200
