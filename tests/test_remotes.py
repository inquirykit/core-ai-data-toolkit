"""Remotes: places outside the service that a bucket's files are copied to, through OpenDAL (buckets/remotes.py), the
copies (buckets/copies.py), and their routes (api/remote_routes.py, api/file_routes.py's _remote)."""

import hashlib
import io
import os
import threading
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("opendal")

import tests.fake_jobs  # noqa: E402,F401  registers test-env
from api import app as app_module  # noqa: E402
import buckets.remotes as remotes_module  # noqa: E402
from buckets.remotes import Stopped, _said, key_of  # noqa: E402
from core import procs  # noqa: E402
from tests import waits  # noqa: E402

SECRET_VALUE = "tk-remote-s3cret-value-9f8e7d"


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def workspace(client, request):
    """A workspace of the test's own, with a bucket, docs; dropped after it."""
    name = "rm_" + request.node.name.removeprefix("test_")[:30].rstrip("_")
    assert client.post("/db", json={"name": name}).status_code == 201
    assert client.post(f"/db/{name}", json={"name": "docs", "bucket": True}).status_code == 201
    yield name
    for bucket in client.get(f"/db/{name}").json()["tables"]:
        client.put(f"/db/{name}/{bucket['name']}/_remote", json={"remote": None})
    client.delete(f"/db/{name}")


def taken(client, name, options):
    """Whether an s3 remote with these options is taken; it is deleted again."""
    r = client.put(f"/remotes/{name}", json={"name": name, "scheme": "s3", "options": options})
    client.delete(f"/remotes/{name}")
    return r.status_code == 200


def put_remote(client, name, scheme="fs", options=None, **extra):
    r = client.put(f"/remotes/{name}", json={"name": name, "scheme": scheme, "options": options or {}, **extra})
    assert r.status_code == 200, r.text
    return r.json()


def fs_remote(client, name, root, **extra):
    return put_remote(client, name, "fs", {"root": str(root)}, **extra)


def upload(client, workspace, data, name="notes.txt", bucket="docs"):
    r = client.post(f"/db/{workspace}/{bucket}/_files", params={"name": name}, content=data,
                    headers={"Content-Type": "text/plain"})
    assert r.status_code == 201, r.text
    return r.json()


def state(client, workspace, bucket="docs"):
    r = client.get(f"/db/{workspace}/{bucket}/_remote")
    assert r.status_code == 200, r.text
    return r.json()


def set_remote(client, workspace, remote, bucket="docs"):
    r = client.put(f"/db/{workspace}/{bucket}/_remote", json={"remote": remote})
    assert r.status_code == 200, r.text
    return r.json()


def all_copied(client, workspace, count, bucket="docs"):
    """The bucket's state once its ``count`` files are copied and its latest copy has succeeded."""
    def done():
        s = state(client, workspace, bucket)
        return s if (s["copied"], s["waiting"]) == (count, 0) and s["task"]["status"] == "succeeded" else None
    return waits.until(done, 20, lambda: str(state(client, workspace, bucket)))


def copy_tasks(client, workspace, bucket="docs", statuses=("queued", "running")):
    """The bucket's copies with one of these statuses."""
    tasks = app_module.app.state.tasks
    return [t for status in statuses for t in client.portal.call(lambda status=status: tasks.list(200, kind="copy", status=status))
            if t["ref"] == f"{workspace}/{bucket}"]


@contextmanager
def held(target, name, only=lambda *args: True):
    """Hold each call of ``target.name`` for which ``only(*its args)`` is true, on its thread, until the gate it gives
    is set (at the latest when the block ends): ``(started, gate)``."""
    started, gate = threading.Event(), threading.Event()
    real = getattr(target, name)

    def call(*args):
        if only(*args):
            started.set()
            gate.wait(30)
        return real(*args)

    setattr(target, name, call)
    try:
        yield started, gate
    finally:
        gate.set()
        delattr(target, name)


def at_remote(client, name, key):
    """The bytes of ``key`` at the remote named ``name``, or None, read with its own operator."""
    remotes = app_module.app.state.remotes
    op = remotes.operator(client.portal.call(remotes.get, name))
    return bytes(op.read(key)) if op.exists(key) else None


# ------------------------------------------------------------------ remotes
def test_a_remote_is_put_got_listed_and_deleted(client, tmp_path):
    made = fs_remote(client, "rm_one", tmp_path, label="A folder")
    assert made["name"] == "rm_one" and made["scheme"] == "fs" and made["options"] == {"root": str(tmp_path)}
    assert made["secrets"] == {} and made["label"] == "A folder" and made["buckets"] == [] and made["missing"] == []
    assert made["created"] > 0 and made["updated"] >= made["created"]
    assert client.get("/remotes/rm_one").json() == made
    assert "rm_one" in [r["name"] for r in client.get("/remotes").json()["remotes"]]
    again = fs_remote(client, "rm_one", tmp_path, label="Another")
    assert again["label"] == "Another" and again["created"] == made["created"]

    r = client.put("/remotes/rm_one", json={"name": "rm_two", "scheme": "fs"})
    assert r.status_code == 400 and "rm_two" in r.json()["detail"]
    assert client.put("/remotes/Bad", json={"name": "Bad", "scheme": "fs"}).status_code == 422
    assert client.put("/remotes/rm_x", json={"name": "rm_x", "scheme": "fs", "nope": 1}).status_code == 422

    assert client.delete("/remotes/rm_one").status_code == 204
    assert client.get("/remotes/rm_one").status_code == 404
    assert client.delete("/remotes/rm_one").status_code == 404


def test_a_remote_a_bucket_names_is_neither_deleted_nor_moved(client, workspace, tmp_path):
    fs_remote(client, "rm_named", tmp_path / "a")
    assert set_remote(client, workspace, "rm_named")["remote"] == "rm_named"
    assert client.get("/remotes/rm_named").json()["buckets"] == [f"{workspace}/docs"]
    r = client.delete("/remotes/rm_named")
    assert r.status_code == 409 and f"{workspace}/docs" in r.json()["detail"]
    # Its root says where the copies are: not while a bucket names it.
    r = client.put("/remotes/rm_named", json={"name": "rm_named", "scheme": "fs", "options": {"root": str(tmp_path / "b")}})
    assert r.status_code == 409 and "new remote" in r.json()["detail"]
    # Its label can change.
    assert fs_remote(client, "rm_named", tmp_path / "a", label="Kept")["label"] == "Kept"
    # A remote that is not there is not named, and a table of rows has no remote.
    r = client.put(f"/db/{workspace}/docs/_remote", json={"remote": "rm_nowhere"})
    assert r.status_code == 404
    assert client.put(f"/db/{workspace}/rows/r1", json={"x": 1}).status_code == 201
    assert client.put(f"/db/{workspace}/rows/_remote", json={"remote": "rm_named"}).status_code == 400
    assert client.get(f"/db/{workspace}/rows/_remote").status_code == 400
    assert set_remote(client, workspace, None)["remote"] is None
    assert client.delete("/remotes/rm_named").status_code == 204


def test_a_location_read_from_a_variable_cannot_move_either(client, workspace, monkeypatch):
    for name, value in (("TK_TEST_ROOT_A", "/a"), ("TK_TEST_ROOT_B", "/b"), ("TK_TEST_KEY_A", "ka"),
                        ("TK_TEST_KEY_B", "kb")):
        monkeypatch.setenv(name, value)
    put_remote(client, "rm_var", "memory", secrets={"root": "TK_TEST_ROOT_A", "password": "TK_TEST_KEY_A"})
    set_remote(client, workspace, "rm_var")
    # Its root, read from another variable, would be another place: not while a bucket names it.
    r = client.put("/remotes/rm_var", json={"name": "rm_var", "scheme": "memory",
                                            "secrets": {"root": "TK_TEST_ROOT_B", "password": "TK_TEST_KEY_A"}})
    assert r.status_code == 409 and "variables" in r.json()["detail"]
    # A key read from another variable is a key rotated.
    put_remote(client, "rm_var", "memory", secrets={"root": "TK_TEST_ROOT_A", "password": "TK_TEST_KEY_B"})
    set_remote(client, workspace, None)
    assert client.delete("/remotes/rm_var").status_code == 204


def test_a_folder_remote_never_overlaps_files_dir(client, monkeypatch):
    files_root = app_module.app.state.buckets.root
    for root in (files_root, files_root / "backup", files_root.parent):
        r = client.put("/remotes/rm_here", json={"name": "rm_here", "scheme": "fs", "options": {"root": str(root)}})
        assert r.status_code == 400 and "buckets' folder" in r.json()["detail"], root
    # One read from a variable is refused where it is used.
    monkeypatch.setenv("TK_TEST_FILES_ROOT", str(files_root))
    put_remote(client, "rm_here", "fs", secrets={"root": "TK_TEST_FILES_ROOT"})
    r = client.post("/remotes/rm_here/test")
    assert r.status_code == 503 and "buckets' folder" in r.json()["detail"]
    assert list(files_root.glob(".toolkit-probe*")) == []
    assert client.delete("/remotes/rm_here").status_code == 204


def test_an_option_that_looks_secret_is_refused(client):
    for option in ("secret_access_key", "access_key_id", "password", "token", "credential", "account_key",
                   "connection_string"):
        r = client.put("/remotes/rm_leak", json={"name": "rm_leak", "scheme": "s3", "options": {option: "x"}})
        assert r.status_code == 400, option
        assert "secrets" in r.json()["detail"] and option in r.json()["detail"]
    # A URL that carries a password, whatever the option's name.
    for value in ("https://user:pa55@s3.example.com", "mongodb://u:p@db.example.com:27017/files"):
        r = client.put("/remotes/rm_leak", json={"name": "rm_leak", "scheme": "s3", "options": {"endpoint": value}})
        assert r.status_code == 400 and "password" in r.json()["detail"] and "pa55" not in r.json()["detail"], value
    assert taken(client, "rm_leak", {"endpoint": "https://user@s3.example.com:9000/x"})
    r = client.put("/remotes/rm_leak", json={"name": "rm_leak", "scheme": "s3", "options": {"bucket": "b"},
                                             "secrets": {"bucket": "TK_BUCKET"}})
    assert r.status_code == 400 and "once" in r.json()["detail"]
    r = client.put("/remotes/rm_leak", json={"name": "rm_leak", "scheme": "s3", "secrets": {"password": "lower"}})
    assert r.status_code == 422
    assert client.get("/remotes/rm_leak").status_code == 404


@pytest.mark.parametrize("variable", ["ADMIN_API_KEY", "SECRETS_MASTER_KEY", "PROVIDER_CLOUD_API_KEY", "HF_TOKEN",
                                      "OLLAMA_HOST"])
def test_a_remote_cannot_name_a_setting_a_providers_key_or_what_children_get(client, variable):
    """S3 sends its access key ID in the clear, so such a remote would hand the value to whatever endpoint it names."""
    r = client.put("/remotes/rm_own", json={"name": "rm_own", "scheme": "s3", "secrets": {"access_key_id": variable}})
    assert r.status_code == 400 and variable in r.json()["detail"]
    assert client.get("/remotes/rm_own").status_code == 404


def test_a_secrets_value_is_never_answered_stored_or_logged(client, monkeypatch, caplog):
    monkeypatch.setenv("TK_TEST_REMOTE_KEY", SECRET_VALUE)
    monkeypatch.setenv("TK_TEST_REMOTE_ID", "tk-test-id")
    spec = put_remote(client, "rm_s3", "s3", {"bucket": "b", "region": "auto", "endpoint": "http://127.0.0.1:9"},
                      secrets={"secret_access_key": "TK_TEST_REMOTE_KEY", "access_key_id": "TK_TEST_REMOTE_ID"})
    assert spec["secrets"] == {"secret_access_key": "TK_TEST_REMOTE_KEY", "access_key_id": "TK_TEST_REMOTE_ID"}
    assert spec["missing"] == []
    tested = client.post("/remotes/rm_s3/test")
    assert tested.status_code == 503 and "rm_s3" in tested.json()["detail"]
    answers = [client.get("/remotes").text, client.get("/remotes/rm_s3").text, tested.text]
    assert all(SECRET_VALUE not in text for text in answers)
    remotes = app_module.app.state.remotes
    stored = client.portal.call(remotes._run, remotes._specs)
    assert SECRET_VALUE not in str(stored)
    assert SECRET_VALUE not in caplog.text
    # A remote's error that holds a secret's value says *** in its place.
    assert _said(RuntimeError(f"denied for {SECRET_VALUE}\n\nSource:\n   it said {SECRET_VALUE}"), [SECRET_VALUE]) \
        == "denied for ***: it said ***"
    monkeypatch.delenv("TK_TEST_REMOTE_KEY")
    assert client.get("/remotes/rm_s3").json()["missing"] == ["TK_TEST_REMOTE_KEY"]
    assert client.delete("/remotes/rm_s3").status_code == 204


def test_a_named_variable_stays_out_of_child_processes_and_jobs(client, monkeypatch):
    variable = "TK_TEST_REMOTE_CHILD"
    monkeypatch.setenv(variable, SECRET_VALUE)
    put_remote(client, "rm_child", "memory", secrets={"root": variable})
    assert variable not in procs.child_env() and variable not in procs.python_env(False)
    r = client.post("/jobs", json={"job": "test-env", "params": {"name": variable}})
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]

    def ended():
        job = client.get(f"/jobs/{job_id}").json()
        return job if job["status"] in ("succeeded", "failed", "cancelled") else None

    job = waits.until(ended, 30, "the test-env job never ended")
    assert job["status"] == "succeeded", job
    assert job["result"] == {"present": False}
    assert client.delete("/remotes/rm_child").status_code == 204


def test_the_test_route_says_what_worked_or_the_remotes_error(client, tmp_path, monkeypatch):
    fs_remote(client, "rm_test", tmp_path / "root")
    r = client.post("/remotes/rm_test/test")
    assert r.status_code == 200, r.text
    assert [s["step"] for s in r.json()["steps"]] == ["write", "read", "list", "delete"]
    assert r.json()["scheme"] == "fs"
    # The probe leaves nothing behind.
    assert list((tmp_path / "root").iterdir()) == []

    # A root that is a file: nothing can be written under it.
    (tmp_path / "a-file").write_bytes(b"x")
    fs_remote(client, "rm_test", tmp_path / "a-file")
    r = client.post("/remotes/rm_test/test")
    assert r.status_code == 503
    assert "rm_test" in r.json()["detail"] and "write a probe object" in r.json()["detail"]
    assert "not a directory" in r.json()["detail"].lower()

    # A variable its secrets name that is not set is named.
    monkeypatch.delenv("TK_TEST_NOT_SET", raising=False)
    put_remote(client, "rm_test", "memory", secrets={"root": "TK_TEST_NOT_SET"})
    r = client.post("/remotes/rm_test/test")
    assert r.status_code == 503 and "TK_TEST_NOT_SET" in r.json()["detail"]
    # A scheme OpenDAL does not know.
    put_remote(client, "rm_test", "nosuch")
    r = client.post("/remotes/rm_test/test")
    assert r.status_code == 503 and "nosuch" in r.json()["detail"]
    assert client.post("/remotes/rm_nowhere/test").status_code == 404
    assert client.delete("/remotes/rm_test").status_code == 204


# ------------------------------------------------------------------- copies
def test_files_are_copied_deleted_there_and_read_back(client, workspace, tmp_path):
    root = tmp_path / "remote"
    fs_remote(client, "rm_copy", root)
    before = upload(client, workspace, b"uploaded before the remote", name="before.txt")
    named = set_remote(client, workspace, "rm_copy")
    assert named["remote"] == "rm_copy" and named["count"] == 1
    rows = [before] + [upload(client, workspace, os.urandom(3000) + bytes([i]), name=f"f{i}.bin") for i in range(3)]
    all_copied(client, workspace, 4)
    for row in rows:
        copy = root / workspace / "docs" / row["id"]
        assert hashlib.sha256(copy.read_bytes()).hexdigest() == row["sha256"]
        assert client.get(f"/db/{workspace}/docs/{row['id']}").json()["copied"] > row["created"] - 1
    s = state(client, workspace)
    assert (s["remote"], s["copied"], s["waiting"]) == ("rm_copy", 4, 0)
    assert client.get(f"/db/{workspace}/docs", params={"copied": "null"}).json()["rows"] == []
    task = state(client, workspace)["task"]
    assert task["result"]["remote"] == "rm_copy" and task["status"] == "succeeded"

    # A file deleted here is deleted there.
    gone = rows[1]
    assert client.delete(f"/db/{workspace}/docs/_files/{gone['id']}").status_code == 204
    waits.until(lambda: not (root / workspace / "docs" / gone["id"]).exists(), 20, "its copy is still at the remote")
    assert (root / workspace / "docs" / rows[2]["id"]).exists()

    # A file whose bytes are gone from disk is read back from the remote, a Range too.
    lost = rows[2]
    local = app_module.app.state.buckets.path(workspace, "docs", lost["id"])
    data = local.read_bytes()
    local.unlink()
    r = client.get(f"/db/{workspace}/docs/_files/{lost['id']}", headers={"Range": "bytes=10-99"})
    assert r.status_code == 206 and r.content == data[10:100]
    assert local.read_bytes() == data
    assert [p.name for p in local.parent.iterdir() if p.name.startswith(".")] == []

    # What the remote holds must be the file: one that differs is not taken.
    (root / workspace / "docs" / lost["id"]).write_bytes(b"not the file")
    local.unlink()
    r = client.get(f"/db/{workspace}/docs/_files/{lost['id']}")
    assert r.status_code == 503 and "differs" in r.json()["detail"]
    assert not local.exists()

    # Without a remote, missing bytes are a 404, as before.
    assert set_remote(client, workspace, None) == {"remote": None, "count": 3, "copied": None, "waiting": None,
                                                   "task": state(client, workspace)["task"]}
    r = client.get(f"/db/{workspace}/docs/_files/{lost['id']}")
    assert r.status_code == 404 and "missing from disk" in r.json()["detail"]
    assert client.delete("/remotes/rm_copy").status_code == 204


def test_a_new_remote_gets_every_file_and_the_old_keeps_its_copies(client, workspace, tmp_path):
    fs_remote(client, "rm_old", tmp_path / "old")
    fs_remote(client, "rm_new", tmp_path / "new")
    set_remote(client, workspace, "rm_old")
    rows = [upload(client, workspace, f"file {i}".encode(), name=f"{i}.txt") for i in range(2)]
    all_copied(client, workspace, 2)
    moved = set_remote(client, workspace, "rm_new")
    assert moved["remote"] == "rm_new" and moved["copied"] + moved["waiting"] == 2
    all_copied(client, workspace, 2)
    for row in rows:
        assert (tmp_path / "new" / workspace / "docs" / row["id"]).read_bytes() == (
            tmp_path / "old" / workspace / "docs" / row["id"]).read_bytes()
    # Dropping the bucket leaves its copies at the remote.
    set_remote(client, workspace, None)
    assert client.delete(f"/db/{workspace}/docs").status_code == 204
    assert (tmp_path / "new" / workspace / "docs" / rows[0]["id"]).exists()
    for name in ("rm_old", "rm_new"):
        assert client.delete(f"/remotes/{name}").status_code == 204


def test_a_dropped_bucket_forgets_its_remote_and_one_made_again_names_none(client, workspace, tmp_path):
    fs_remote(client, "rm_forget", tmp_path / "remote")
    set_remote(client, workspace, "rm_forget")
    upload(client, workspace, b"kept at the remote")
    all_copied(client, workspace, 1)
    assert client.delete("/remotes/rm_forget").status_code == 409
    # Dropped while it names the remote: the remote is free, and its copies stay there.
    assert client.delete(f"/db/{workspace}/docs").status_code == 204
    assert client.get("/remotes/rm_forget").json()["buckets"] == []
    assert any((tmp_path / "remote" / workspace / "docs").iterdir())
    assert client.post(f"/db/{workspace}", json={"name": "docs", "bucket": True}).status_code == 201
    assert state(client, workspace)["remote"] is None
    upload(client, workspace, b"stays on this disk")
    assert copy_tasks(client, workspace, statuses=("queued", "running")) == []
    # The naming's number goes on from the one before, across a new store over the same database.
    namings = app_module.app.state.copies.namings
    set_remote(client, workspace, "rm_forget")
    first = client.portal.call(namings.named, workspace, "docs")
    set_remote(client, workspace, None)
    set_remote(client, workspace, "rm_forget")
    from buckets import Namings
    again = client.portal.call(Namings(namings.database, namings.tables).named, workspace, "docs")
    assert first[0] == again[0] == "rm_forget" and again[1] == first[1] + 2
    all_copied(client, workspace, 1)
    set_remote(client, workspace, None)
    # A naming that comes after the drop (a PUT that found the bucket just before it) is refused, not kept.
    assert client.delete(f"/db/{workspace}/docs").status_code == 204
    from core.errors import NotFound
    with pytest.raises(NotFound):
        client.portal.call(namings.name, workspace, "docs", "rm_forget")
    assert client.get("/remotes/rm_forget").json()["buckets"] == []
    assert client.delete("/remotes/rm_forget").status_code == 204


@pytest.mark.parametrize("failing",["an fs root that is a file", "a variable not set"])
def test_files_uploaded_while_the_remote_fails_are_copied_after_a_restart(client, workspace, tmp_path, monkeypatch,
                                                                         failing):
    copies = app_module.app.state.copies
    monkeypatch.setattr(copies, "retries", 0)
    variable = "TK_TEST_REMOTE_ROOT"
    if failing == "a variable not set":
        monkeypatch.delenv(variable, raising=False)
        put_remote(client, "rm_failing", "memory", secrets={"root": variable})
    else:
        (tmp_path / "remote").write_bytes(b"in the way")
        fs_remote(client, "rm_failing", tmp_path / "remote")
    set_remote(client, workspace, "rm_failing")
    rows = [upload(client, workspace, f"waiting {i}".encode(), name=f"w{i}.txt") for i in range(3)]

    def failed():
        s = state(client, workspace)
        return s if s["task"] and s["task"]["status"] == "failed" and s["waiting"] == 3 else None

    s = waits.until(failed, 20, lambda: str(state(client, workspace)))
    assert "rm_failing" in s["task"]["error"] and s["copied"] == 0
    assert (variable if failing == "a variable not set" else "not a directory") in s["task"]["error"].lower() \
        or variable in s["task"]["error"]
    for row in rows:
        assert client.get(f"/db/{workspace}/docs/{row['id']}").json().get("copied") is None

    # The remote works again; the service starts again.
    if failing == "a variable not set":
        monkeypatch.setenv(variable, "/" + workspace)
    else:
        (tmp_path / "remote").unlink()
    client.portal.call(copies.start)
    all_copied(client, workspace, 3)
    for row in rows:
        assert hashlib.sha256(at_remote(client, "rm_failing", key_of(workspace, "docs", row["id"]))).hexdigest() == row["sha256"]
    set_remote(client, workspace, None)
    assert client.delete("/remotes/rm_failing").status_code == 204


def test_one_copy_per_bucket_is_queued_at_a_time(client, workspace, tmp_path, monkeypatch):
    tasks = app_module.app.state.tasks
    fs_remote(client, "rm_queue", tmp_path / "remote")
    # Nothing starts meanwhile: every change finds the copy queued.
    monkeypatch.setattr(tasks.kinds["copy"], "concurrency", 0)
    set_remote(client, workspace, "rm_queue")
    rows = [upload(client, workspace, f"q{i}".encode(), name=f"q{i}") for i in range(4)]
    client.delete(f"/db/{workspace}/docs/_files/{rows[0]['id']}")
    client.portal.call(app_module.app.state.copies.start)
    queued = [t for t in client.portal.call(lambda: tasks.list(200, kind="copy", status="queued")) if t["ref"] == f"{workspace}/docs"]
    assert len(queued) == 1
    monkeypatch.setattr(tasks.kinds["copy"], "concurrency", 2)
    tasks._poke()
    all_copied(client, workspace, 3)
    set_remote(client, workspace, None)
    assert client.delete("/remotes/rm_queue").status_code == 204


def test_changes_during_a_copy_wait_in_one_copy_that_leaves_the_other_place_free(client, workspace, tmp_path):
    copies = app_module.app.state.copies
    tasks = app_module.app.state.tasks
    fs_remote(client, "rm_slot", tmp_path / "remote")
    assert client.post(f"/db/{workspace}", json={"name": "more", "bucket": True}).status_code == 201
    for bucket in ("docs", "more"):
        set_remote(client, workspace, "rm_slot", bucket)
        all_copied(client, workspace, 0, bucket)
    with held(copies, "_write_file", lambda spec, op, ws, bucket, *rest: bucket == "docs") as (started, gate):
        first = upload(client, workspace, b"held at the remote")
        assert started.wait(20)
        [running] = copy_tasks(client, workspace)
        assert running["status"] == "running"
        # Two more changes to docs while its copy is held: one more copy of docs, queued, which waits for it
        # rather than taking the kind's other place.
        rows = [first, upload(client, workspace, b"second"), upload(client, workspace, b"third")]
        [waiting] = copy_tasks(client, workspace, statuses=("queued",))
        # Asked for again, it is the same one.
        again = client.portal.call(lambda: tasks.submit("copy", ref=f"{workspace}/docs",
                                                        params={"workspace": workspace, "bucket": "docs"}))
        assert again["id"] == waiting["id"]
        # So another bucket's copy runs meanwhile.
        upload(client, workspace, b"in another bucket", bucket="more")
        all_copied(client, workspace, 1, "more")
        assert client.portal.call(tasks.get, waiting["id"])["status"] == "queued"
        assert state(client, workspace)["task"]["id"] == running["id"]
        gate.set()
        all_copied(client, workspace, 3)
    # Between them, the two copies copied every file once.
    ended = [client.portal.call(tasks.get, t["id"]) for t in (running, waiting)]
    assert [t["status"] for t in ended] == ["succeeded", "succeeded"]
    assert sum(t["result"]["copied"] for t in ended) == 3
    assert copy_tasks(client, workspace) == []
    for row in rows:
        assert hashlib.sha256((tmp_path / "remote" / workspace / "docs" / row["id"]).read_bytes()).hexdigest() \
            == row["sha256"]
    for bucket in ("docs", "more"):
        set_remote(client, workspace, None, bucket)
    assert client.delete("/remotes/rm_slot").status_code == 204


def test_a_copy_begun_before_the_remote_was_named_again_marks_nothing(client, workspace, tmp_path):
    copies = app_module.app.state.copies
    fs_remote(client, "rm_race", tmp_path / "r1")
    rows = [upload(client, workspace, f"race {i}".encode(), name=f"{i}.txt") for i in range(2)]
    with held(copies, "_write_file") as (started, gate):
        set_remote(client, workspace, "rm_race")
        assert started.wait(20)
        # While it writes to r1: the remote cleared, moved to r2 (no bucket names it then), and named again.
        set_remote(client, workspace, None)
        fs_remote(client, "rm_race", tmp_path / "r2")
        set_remote(client, workspace, "rm_race")
        gate.set()
        all_copied(client, workspace, 2)
    # Each file marked copied is at the place the remote names now.
    for row in rows:
        assert hashlib.sha256((tmp_path / "r2" / workspace / "docs" / row["id"]).read_bytes()).hexdigest() \
            == row["sha256"]
    set_remote(client, workspace, None)
    assert client.delete("/remotes/rm_race").status_code == 204


def test_a_write_that_fails_or_stops_part_way_leaves_nothing_at_the_remote(client, tmp_path, monkeypatch):
    # Small parts, so a folder has written some of them to the key before the write ends.
    monkeypatch.setattr(remotes_module, "WRITE_CHUNK", 64 * 1024)
    monkeypatch.setattr(remotes_module, "READ_BYTES", 16 * 1024)
    remotes = app_module.app.state.remotes
    fs_remote(client, "rm_part", tmp_path / "root")
    spec = client.portal.call(remotes.get, "rm_part")
    op = remotes.operator(spec)
    key = key_of("ws", "b", "a" * 32)

    class Source(io.BytesIO):
        """1 MiB, that sets ``stop`` (or fails) once 256 KiB are read."""

        def __init__(self, stop=None):
            super().__init__(os.urandom(1024 * 1024))
            self.stop = stop

        def read(self, size=-1):
            if self.tell() >= 256 * 1024:
                if self.stop is None:
                    raise OSError("the disk failed")
                self.stop.set()
            return super().read(size)

    stop = threading.Event()
    with pytest.raises(Stopped):
        remotes.write_file(spec, op, key, Source(stop), stop)
    assert not op.exists(key)
    with pytest.raises(OSError):
        remotes.write_file(spec, op, key, Source(), threading.Event())
    assert not op.exists(key)
    # Whole, it is there.
    remotes.write_file(spec, op, key, io.BytesIO(b"whole"), threading.Event())
    assert bytes(op.read(key)) == b"whole"
    assert client.delete("/remotes/rm_part").status_code == 204


def test_a_read_back_that_a_delete_or_a_drop_overtakes_leaves_no_bytes(client, workspace, tmp_path):
    remotes = app_module.app.state.remotes
    buckets = app_module.app.state.buckets
    fs_remote(client, "rm_back", tmp_path / "remote")
    set_remote(client, workspace, "rm_back")
    rows = [upload(client, workspace, os.urandom(5000), name=f"{i}.bin") for i in range(2)]
    all_copied(client, workspace, 2)
    folder = buckets.folder(workspace, "docs")
    for row in rows:
        buckets.path(workspace, "docs", row["id"]).unlink()

    def read_back_overtaken_by(row, overtake):
        answers = []
        with held(remotes, "read_file") as (started, gate):
            reader = threading.Thread(target=lambda: answers.append(
                client.get(f"/db/{workspace}/docs/_files/{row['id']}")))
            reader.start()
            assert started.wait(20)
            overtake()
            gate.set()
            reader.join(30)
        return answers[0]

    # Deleted while its bytes come back: they go too, and the download is a 404.
    gone = rows[0]
    r = read_back_overtaken_by(gone, lambda: client.delete(f"/db/{workspace}/docs/_files/{gone['id']}"))
    assert r.status_code == 404 and "deleted while" in r.json()["detail"]
    assert client.get(f"/db/{workspace}/docs/{gone['id']}").status_code == 404
    assert sorted(p.name for p in folder.iterdir()) == []
    # Its bucket dropped meanwhile: a 404 too, not an internal error.
    r = read_back_overtaken_by(rows[1], lambda: client.delete(f"/db/{workspace}/docs"))
    assert r.status_code == 404, r.text
    assert not (folder / rows[1]["id"]).exists()
    assert client.delete("/remotes/rm_back").status_code == 204


def test_a_copy_whose_workspace_is_dropped_meanwhile_ends_quietly(client, tmp_path, caplog):
    """Dropped between two of its files, its workspace gone from the table store: the copy ends as for a dropped
    bucket, not failed and tried again."""
    copies, tasks = app_module.app.state.copies, app_module.app.state.tasks
    ws = "rm_dropped_meanwhile"
    assert client.post("/db", json={"name": ws}).status_code == 201
    assert client.post(f"/db/{ws}", json={"name": "docs", "bucket": True}).status_code == 201
    fs_remote(client, "rm_dropped", tmp_path / "remote")
    set_remote(client, ws, "rm_dropped")
    with held(copies, "_write_file") as (started, gate):
        upload(client, ws, b"held at the remote")
        assert started.wait(20)
        [running] = copy_tasks(client, ws, statuses=("running",))
        assert client.delete(f"/db/{ws}").status_code == 204
        gate.set()
        ended = waits.until(lambda: (lambda t: t if t["status"] != "running" else None)(
            client.portal.call(tasks.get, running["id"])), 20, "the copy never ended")
    assert (ended["status"], ended["error"], ended["retried"]) == ("succeeded", None, 0), ended
    assert ended["result"]["skipped"] == 1
    assert not [r for r in caplog.records if r.name.startswith("tasks") and r.levelname == "ERROR"]
    assert client.delete("/remotes/rm_dropped").status_code == 204


def test_a_copy_that_gave_up_goes_again_when_the_admin_asks(client, workspace, tmp_path, monkeypatch):
    copies = app_module.app.state.copies
    monkeypatch.setattr(copies, "retries", 0)
    (tmp_path / "remote").write_bytes(b"in the way")
    fs_remote(client, "rm_late", tmp_path / "remote")
    set_remote(client, workspace, "rm_late")
    rows = [upload(client, workspace, f"late {i}".encode(), name=f"{i}.txt") for i in range(2)]

    def failed():
        s = state(client, workspace)
        return s if s["task"]["status"] == "failed" and s["waiting"] == 2 else None

    waits.until(failed, 20, lambda: str(state(client, workspace)))
    (tmp_path / "remote").unlink()
    # Named again, the same remote: its copy goes now (the page's Copy now).
    set_remote(client, workspace, "rm_late")
    all_copied(client, workspace, 2)

    # Saving the remote asks too: a variable it reads its root from, set since.
    variable = "TK_TEST_LATE_ROOT"
    monkeypatch.delenv(variable, raising=False)
    put_remote(client, "rm_late_var", "fs", secrets={"root": variable})
    set_remote(client, workspace, "rm_late_var")
    waits.until(failed, 20, lambda: str(state(client, workspace)))
    monkeypatch.setenv(variable, str(tmp_path / "from-variable"))
    put_remote(client, "rm_late_var", "fs", secrets={"root": variable}, label="Its variable is set now")
    all_copied(client, workspace, 2)
    for row in rows:
        assert (tmp_path / "from-variable" / workspace / "docs" / row["id"]).exists()

    # A retry that waits starts at once when the bucket is given a remote that works.
    monkeypatch.setattr(copies, "retries", 1)
    monkeypatch.setattr(copies, "retry_delay_s", 3600)
    (tmp_path / "blocked").write_bytes(b"in the way")
    fs_remote(client, "rm_late_blocked", tmp_path / "blocked")
    set_remote(client, workspace, "rm_late_blocked")
    waits.until(lambda: (lambda t: t["status"] == "queued" and t["retried"] == 1)(state(client, workspace)["task"]),
                20, lambda: str(state(client, workspace)))
    set_remote(client, workspace, "rm_late")
    all_copied(client, workspace, 2)
    set_remote(client, workspace, None)
    for name in ("rm_late", "rm_late_var", "rm_late_blocked"):
        assert client.delete(f"/remotes/{name}").status_code == 204
