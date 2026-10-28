import asyncio
import sys
import time

import pytest
from fastapi.testclient import TestClient

import procs
from api import app as app_module
from errors import NotFound
from jobs.store import JobStore
from jobs.tasks import TaskRunner
from models import hub, memory
from models.generic.transformers_model import TransformersModel
from models.hub import Downloads
from tests.fakes import FakeModel, swapped
from vectorstore.tables import TableStore


async def wait_for(downloads, id, *statuses, timeout=10.0):
    """The download ``id`` once its status is one of ``statuses``."""
    deadline = time.monotonic() + timeout
    while True:
        found = next((d for d in await downloads.list() if d["id"] == id), None)
        if found is not None and found["status"] in statuses:
            return found
        assert time.monotonic() < deadline, f"still {found and found['status']}"
        await asyncio.sleep(0.02)


def spawning(code):
    """A _spawn stand-in that runs ``code`` in a child Python instead of a download."""
    async def spawn(self, repo_id):
        return await asyncio.create_subprocess_exec(
            sys.executable, "-c", code, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            start_new_session=True)
    return spawn


@pytest.fixture
def fake_hub(monkeypatch, tmp_path):
    # A folder of its own: the test database may sit in tmp_path too.
    repo = tmp_path / "repo"
    (repo / "blobs").mkdir(parents=True)
    (repo / "blobs" / "a").write_bytes(b"x" * 10)
    monkeypatch.setattr(hub, "repo_dir", lambda repo_id: repo)
    monkeypatch.setattr(hub, "repo_info", lambda repo_id: {"total_bytes": 40})
    monkeypatch.setattr(hub, "offline", lambda: False)
    return repo


def downloads_on(arcade):
    """Downloads, one at a time, in a task runner of their own."""
    tasks = TaskRunner(JobStore(arcade, TableStore(arcade)))
    return tasks, Downloads(tasks, max_running=1, poll_s=0.02)


def test_downloads_run_in_order_finish_and_fail(fake_hub, monkeypatch, arcade):
    monkeypatch.setattr(Downloads, "_spawn", spawning("import time; time.sleep(0.3)"))

    async def scenario():
        tasks, downloads = downloads_on(arcade)
        await tasks.start()
        first = await downloads.start("org/a")
        second = await downloads.start("org/b")
        with pytest.raises(ValueError):
            await downloads.start("org/a")
        assert (await downloads.active_for("org/a"))["id"] == first["id"]
        running = await wait_for(downloads, first["id"], "running")
        assert (await wait_for(downloads, second["id"], "queued"))["repo_id"] == "org/b"
        await asyncio.sleep(0.1)
        running = next(d for d in await downloads.list() if d["id"] == first["id"])
        assert running["total_bytes"] == 40 and running["done_bytes"] == 10
        done = await wait_for(downloads, first["id"], "done")
        assert done["done_bytes"] == 40 and done["finished"] is not None
        await wait_for(downloads, second["id"], "done")
        listed = await downloads.list()
        assert [d["repo_id"] for d in listed] == ["org/b", "org/a"], "newest first"
        assert "created" not in listed[0]

        monkeypatch.setattr(Downloads, "_spawn", spawning("import sys; sys.stderr.write('warn\\nboom: no\\n'); sys.exit(2)"))
        failed = await wait_for(downloads, (await downloads.start("org/c"))["id"], "failed")
        assert failed["error"] == "boom: no"

        await downloads.cancel(failed["id"])
        assert failed["id"] not in [d["id"] for d in await downloads.list()], "dismissed"
        with pytest.raises(NotFound):
            await downloads.cancel("nope")
        await tasks.stop()

    asyncio.run(scenario())


def test_downloads_are_cancelled_and_stopped(fake_hub, monkeypatch, arcade):
    monkeypatch.setattr(Downloads, "_spawn", spawning("import time; time.sleep(30)"))

    async def scenario():
        tasks, downloads = downloads_on(arcade)
        await tasks.start()
        running = await downloads.start("org/a")
        queued = await downloads.start("org/b")
        await wait_for(downloads, running["id"], "running")
        while running["id"] not in downloads._processes:
            await asyncio.sleep(0.02)
        await downloads.cancel(queued["id"])
        assert (await wait_for(downloads, queued["id"], "cancelled"))["finished"] is not None
        await downloads.cancel(running["id"])
        await wait_for(downloads, running["id"], "cancelled")
        assert running["id"] not in downloads._processes

        third = await downloads.start("org/c")
        await wait_for(downloads, third["id"], "running")
        while third["id"] not in downloads._processes:
            await asyncio.sleep(0.02)
        process = downloads._processes[third["id"]]
        # The service stopping kills it; the download, its partial files kept, waits to go on when the service is back.
        await tasks.stop()
        assert process.returncode is not None
        stopped = next(d for d in await downloads.list() if d["id"] == third["id"])
        assert stopped["status"] == "queued" and stopped["error"] is None

    asyncio.run(scenario())


def test_a_download_cancelled_while_it_looks_up_the_size_never_downloads(fake_hub, monkeypatch, arcade):
    monkeypatch.setattr(hub, "repo_info", lambda repo_id: time.sleep(0.3) or {"total_bytes": 40})
    monkeypatch.setattr(Downloads, "_spawn", spawning("import time; time.sleep(30)"))

    async def scenario():
        tasks, downloads = downloads_on(arcade)
        await tasks.start()
        started = await downloads.start("org/a")
        await wait_for(downloads, started["id"], "running")
        assert started["id"] not in downloads._processes, "still asking for the size"
        began = time.monotonic()
        await downloads.cancel(started["id"])
        # Its process is stopped as soon as it starts, not left to download for 30 s.
        assert (await wait_for(downloads, started["id"], "cancelled"))["finished"] is not None
        assert time.monotonic() - began < 5
        await tasks.stop()

    asyncio.run(scenario())


def test_a_download_gets_the_environment_without_the_services_secrets(monkeypatch, arcade):
    monkeypatch.setenv("TK_TEST_SECRET", "s3cret")
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setattr(procs, "hidden", {"TK_TEST_SECRET"})
    monkeypatch.setattr(hub, "_DOWNLOAD_SCRIPT", "import os, sys; sys.stderr.write(repr(sorted(k for k in os.environ "
                        "if k.startswith(('TK_TEST', 'HF_')))))")

    async def scenario():
        _, downloads = downloads_on(arcade)
        process = await downloads._spawn("org/a")
        _, err = await process.communicate()
        return err.decode()

    assert asyncio.run(scenario()) == repr(["HF_HUB_DISABLE_PROGRESS_BARS", "HF_TOKEN"])


# ------------------------------------------------------------------- routes
@pytest.fixture(scope="module")
def client():
    with swapped(TransformersModel, FakeModel), TestClient(app_module.app) as c:
        yield c


RESULTS = [
    {"id": "org/a", "pipeline_tag": "text-generation", "library_name": "transformers", "downloads": 5, "likes": 1,
     "last_modified": "2026-01-01T00:00:00+00:00", "gated": False, "tags": ["chat"]},
    {"id": "org/b", "pipeline_tag": None, "library_name": None, "downloads": 0, "likes": 0,
     "last_modified": None, "gated": True, "tags": []},
]


def test_search_marks_what_is_on_disk(client, monkeypatch):
    calls = []
    monkeypatch.setattr(hub, "offline", lambda: False)
    monkeypatch.setattr(hub, "search", lambda q, task, limit: calls.append((q, task, limit)) or [dict(r) for r in RESULTS])
    monkeypatch.setattr(memory, "cached_weights", lambda: {"org/a": 123})
    r = client.get("/hub/search?q=llama&task=embedding&limit=5")
    assert r.status_code == 200, r.text
    assert calls == [("llama", "embedding", 5)]
    body = r.json()
    assert body["query"] == "llama" and body["task"] == "embedding"
    assert [(x["id"], x["on_disk"]) for x in body["results"]] == [("org/a", True), ("org/b", False)]
    assert client.get("/hub/search?task=poetry").status_code == 422

    monkeypatch.setattr(hub, "offline", lambda: True)
    assert client.get("/hub/search?q=x").status_code == 503


def hub_error(cls, status):
    """One of huggingface_hub's HTTP errors, as a failed request would raise it."""
    import httpx
    from huggingface_hub import errors

    response = httpx.Response(status, request=httpx.Request("GET", "https://huggingface.co/api/models/x"))
    return getattr(errors, cls)("refused", response=response)


def test_repo_info_and_hub_errors(client, monkeypatch):

    monkeypatch.setattr(hub, "offline", lambda: False)
    monkeypatch.setattr(memory, "cached_weights", lambda: {})
    info = {"id": "org/a", "sha": "abc", "pipeline_tag": None, "gated": False, "license": "mit",
            "files": [{"path": "model.safetensors", "size": 30}, {"path": "config.json", "size": 2}],
            "total_bytes": 32, "weight_bytes": 30}
    monkeypatch.setattr(hub, "repo_info", lambda repo_id: dict(info))
    r = client.get("/hub/repos/org/a")
    assert r.status_code == 200, r.text
    assert r.json() == {**info, "on_disk": False}
    assert client.get("/hub/repos/org/a/b").status_code == 400

    def missing(repo_id):
        raise hub_error("RepositoryNotFoundError", 404)
    monkeypatch.setattr(hub, "repo_info", missing)
    assert client.get("/hub/repos/org/missing").status_code == 404

    def gated(repo_id):
        raise hub_error("GatedRepoError", 403)
    monkeypatch.setattr(hub, "repo_info", gated)
    assert client.get("/hub/repos/org/gated").status_code == 403


def test_download_routes(client, monkeypatch, fake_hub):
    monkeypatch.setattr(Downloads, "_spawn", spawning("import time; time.sleep(30)"))
    r = client.post("/hub/downloads", json={"repo_id": "org/a"})
    assert r.status_code == 202, r.text
    download = r.json()
    assert download["repo_id"] == "org/a" and download["status"] in ("queued", "running")
    assert client.post("/hub/downloads", json={"repo_id": "org/a"}).status_code == 409
    assert client.post("/hub/downloads", json={"repo_id": "bad id"}).status_code == 400
    listed = client.get("/hub/downloads").json()["downloads"]
    assert [d["id"] for d in listed] == [download["id"]]
    # Deleting the weights of a repo being downloaded is refused.
    assert client.delete("/hub/local/org/a").status_code == 409

    assert client.delete(f"/hub/downloads/{download['id']}").status_code == 204
    assert client.get("/hub/downloads").json()["downloads"][0]["status"] == "cancelled"
    assert client.delete(f"/hub/downloads/{download['id']}").status_code == 204, "dismissed"
    assert client.get("/hub/downloads").json()["downloads"] == []
    assert client.delete("/hub/downloads/nope").status_code == 404


def test_local_repos_and_deletion(client, monkeypatch):
    repos = [{"id": "org/a", "size_bytes": 100, "weight_bytes": 90, "files": 3, "last_accessed": 1.0, "last_modified": 1.0},
             {"id": "org/b", "size_bytes": 50, "weight_bytes": 0, "files": 1, "last_accessed": None, "last_modified": None}]
    monkeypatch.setattr(hub, "local_repos", lambda: [dict(r) for r in repos])
    deleted = []
    monkeypatch.setattr(hub, "delete_repo", lambda repo_id: deleted.append(repo_id) or 100)

    assert client.post("/models/load", json={"model_id": "org/a", "task": "text-generation"}).status_code == 200
    body = client.get("/hub/local").json()
    assert body["total_bytes"] == 150
    assert body["repos"][0]["loaded"] is True and body["repos"][0]["loaded_tasks"] == ["text-generation"]
    assert body["repos"][1]["loaded"] is False and body["repos"][1]["tasks"] == []
    assert client.delete("/hub/local/org/a").status_code == 409

    assert client.delete("/models/org/a/text-generation").status_code == 200
    assert client.delete("/hub/local/org/a").status_code == 204
    assert deleted == ["org/a"]

    def gone(repo_id):
        raise NotFound("Not in the Hub cache")
    monkeypatch.setattr(hub, "delete_repo", gone)
    assert client.delete("/hub/local/org/zzz").status_code == 404
