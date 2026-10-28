import pytest
from fastapi.testclient import TestClient

from api import app as app_module
from config import Config

PAGES = [("/admin", "Admin"), ("/admin/setup", "Setup"), ("/playground", "Playground")]


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        yield c


@pytest.mark.parametrize("path, title", PAGES)
def test_built_pages_are_served_without_a_key_and_always_revalidated(client, monkeypatch, built_pages, path, title):
    monkeypatch.setattr(Config, "API_KEY", "secret")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-secret")
    r = client.get(path)
    assert r.status_code == 200
    assert f"AI &amp; Data Toolkit · {title}" in r.text
    assert r.headers["cache-control"] == "no-cache"


@pytest.mark.parametrize("path, title", PAGES)
def test_an_unbuilt_page_says_how_to_build_it(client, monkeypatch, tmp_path, path, title):
    from api import pages

    monkeypatch.setattr(pages, "STATIC_DIR", tmp_path / "missing")
    r = client.get(path)
    assert r.status_code == 503
    assert "npm run build" in r.text
