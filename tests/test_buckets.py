"""Buckets: /db tables of files (db/tables.py), their bytes on disk (buckets/), and their routes (api/file_routes.py)."""

import asyncio
import hashlib
import os

import pytest
from fastapi.testclient import TestClient


from api import app as app_module  # noqa: E402
from api import audit  # noqa: E402
from core.config import Config  # noqa: E402
from core.errors import NotFound  # noqa: E402
from db.tables import FILES  # noqa: E402

FIELDS = {"name", "size", "sha256", "content_type", "created", "copied"}
# What an upload answers with: copied comes once the file reaches the bucket's remote.
UPLOADED = FIELDS - {"copied"}


@pytest.fixture(scope="module")
def client():
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture(scope="module")
def shelf(client):
    """A workspace with a bucket, docs, and a graph table, people, beside it."""
    assert client.post("/db", json={"name": "bk_shelf"}).status_code == 201
    r = client.post("/db/bk_shelf", json={"name": "docs", "bucket": True})
    assert r.status_code == 201, r.text
    assert client.put("/db/bk_shelf/people/ann", json={"name": "Ann"}).status_code == 201
    return "bk_shelf"


def files_dir():
    return app_module.app.state.buckets.root


def upload(client, workspace, bucket, data, name="notes.txt", content_type="text/plain", **kw):
    headers = {"Content-Type": content_type} if content_type else {}
    return client.post(f"/db/{workspace}/{bucket}/_files", params={"name": name}, content=data,
                       headers={**headers, **kw.pop("headers", {})}, **kw)


def leftovers(workspace, bucket):
    """The temporary files of uploads in the bucket's folder."""
    folder = files_dir() / workspace / bucket
    return [p.name for p in folder.iterdir() if p.name.startswith(".upload-")] if folder.exists() else []


def test_a_bucket_is_a_plain_table_of_fixed_fields(client, shelf):
    info = next(t for t in client.get(f"/db/{shelf}").json()["tables"] if t["name"] == "docs")
    assert info["bucket"] is True and info["graph"] is False and info["strict"] is True and info["kept"] is None
    assert {f["name"] for f in info["fields"]} == FIELDS
    assert {f["name"] for f in info["fields"] if f["indexed"]} == {"name", "sha256", "created", "copied"}
    assert not {"remote", "copied", "waiting"} & set(info)
    people = next(t for t in client.get(f"/db/{shelf}").json()["tables"] if t["name"] == "people")
    assert people["bucket"] is False
    assert client.get(f"/db/{shelf}/docs/_schema").json()["bucket"] is True
    # Kept with the table's schema, so a bucket stays one when the database is read again (a restart).
    app_module.app.state.tables.reload()
    assert next(t for t in client.get(f"/db/{shelf}").json()["tables"] if t["name"] == "docs")["bucket"] is True


def test_the_file_routes_write_buckets_and_nothing_else(client, shelf):
    tables = app_module.app.state.tables

    async def write_people():
        await tables.insert(shelf, "people", {"id": "bob"}, owner=FILES)

    with pytest.raises(NotFound):
        client.portal.call(write_people)
    assert client.get(f"/db/{shelf}/people/bob").status_code == 404


def test_an_upload_and_its_download_hash_alike_and_ranges_put_together_do_too(client, shelf):
    data = os.urandom(100)
    r = upload(client, shelf, "docs", data, name="blob.bin", content_type="application/x-thing")
    assert r.status_code == 201, r.text
    row = r.json()
    assert set(row) == {"id", *UPLOADED}
    assert len(row["id"]) == 32 and row["size"] == 100 and row["sha256"] == hashlib.sha256(data).hexdigest()
    assert row["name"] == "blob.bin" and row["content_type"] == "application/x-thing" and row["created"] > 0
    assert r.headers["location"].endswith(f"/db/{shelf}/docs/_files/{row['id']}")
    assert (files_dir() / shelf / "docs" / row["id"]).read_bytes() == data

    whole = client.get(f"/db/{shelf}/docs/_files/{row['id']}")
    assert whole.status_code == 200
    assert hashlib.sha256(whole.content).hexdigest() == row["sha256"] and len(whole.content) == row["size"]
    assert whole.headers["content-type"] == "application/x-thing"
    assert whole.headers["accept-ranges"] == "bytes"

    head = client.get(f"/db/{shelf}/docs/_files/{row['id']}", headers={"Range": "bytes=0-9"})
    tail = client.get(f"/db/{shelf}/docs/_files/{row['id']}", headers={"Range": "bytes=10-"})
    assert (head.status_code, tail.status_code) == (206, 206)
    assert head.headers["content-range"] == "bytes 0-9/100" and tail.headers["content-range"] == "bytes 10-99/100"
    assert len(head.content) == 10 and len(tail.content) == 90
    assert hashlib.sha256(head.content + tail.content).hexdigest() == row["sha256"]

    # The list of files is the bucket's list of rows.
    listed = client.get(f"/db/{shelf}/docs", params={"_sort": "-created"}).json()["rows"]
    assert row in listed
    assert client.get(f"/db/{shelf}/docs", params={"sha256": row["sha256"]}).json()["rows"] == [row]
    assert client.get(f"/db/{shelf}/docs/{row['id']}").json() == row


def test_a_download_is_an_attachment_that_never_runs_as_a_page(client, shelf):
    page = b"<script>alert(document.cookie)</script>"
    row = upload(client, shelf, "docs", page, name="page.html", content_type="text/html").json()
    r = client.get(f"/db/{shelf}/docs/_files/{row['id']}")
    assert r.content == page
    assert r.headers["content-disposition"] == 'attachment; filename="page.html"'
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["content-security-policy"] == "sandbox"
    # A name the header cannot carry as it is goes encoded.
    row = upload(client, shelf, "docs", b"x", name='a "quoted" näme.txt').json()
    r = client.get(f"/db/{shelf}/docs/_files/{row['id']}")
    assert r.headers["content-disposition"] == "attachment; filename*=utf-8''a%20%22quoted%22%20n%C3%A4me.txt"


def test_an_empty_body_is_an_empty_file_and_a_type_left_out_is_octet_stream(client, shelf):
    r = upload(client, shelf, "docs", b"", name="empty", content_type=None)
    assert r.status_code == 201, r.text
    row = r.json()
    assert row["size"] == 0 and row["sha256"] == hashlib.sha256(b"").hexdigest()
    assert row["content_type"] == "application/octet-stream"
    got = client.get(f"/db/{shelf}/docs/_files/{row['id']}")
    assert got.status_code == 200 and got.content == b""


def test_a_name_is_1_to_255_characters_without_control_characters(client, shelf):
    assert upload(client, shelf, "docs", b"x", name="n" * 255).status_code == 201
    assert upload(client, shelf, "docs", b"x", name="n" * 256).status_code == 422
    assert client.post(f"/db/{shelf}/docs/_files", content=b"x").status_code == 422
    r = upload(client, shelf, "docs", b"x", name="two\nlines")
    assert r.status_code == 400 and "control" in r.json()["detail"]
    assert upload(client, shelf, "docs", b"x", name="   ").status_code == 400
    assert leftovers(shelf, "docs") == []


def test_a_delete_removes_the_row_and_the_bytes(client, shelf):
    row = upload(client, shelf, "docs", b"bye").json()
    path = files_dir() / shelf / "docs" / row["id"]
    assert path.exists()
    assert client.delete(f"/db/{shelf}/docs/_files/{row['id']}").status_code == 204
    assert not path.exists()
    assert client.get(f"/db/{shelf}/docs/{row['id']}").status_code == 404
    assert client.get(f"/db/{shelf}/docs/_files/{row['id']}").status_code == 404
    # Not there: no error.
    assert client.delete(f"/db/{shelf}/docs/_files/{row['id']}").status_code == 204
    # Deletes go in the audit log, as every delete does; an upload writes rows with the client key, as a PUT does.
    kept = audit.routes(app_module.app)
    assert "DELETE /db/{workspace}/{bucket}/_files/{file_id}" in kept
    assert "POST /db/{workspace}/{bucket}/_files" not in kept


def test_a_file_past_max_file_bytes_is_refused_and_leaves_nothing(client, shelf, monkeypatch):
    monkeypatch.setattr(app_module.app.state.buckets, "max_bytes", 50)
    count = client.get(f"/db/{shelf}/docs/_count").json()["count"]
    # Its Content-Length says so: refused before a byte is read.
    r = upload(client, shelf, "docs", b"x" * 51)
    assert r.status_code == 413 and "MAX_FILE_BYTES" in r.json()["detail"]
    # Streamed without one: refused when the body gets there.

    def chunks():
        for _ in range(6):
            yield b"y" * 10

    r = client.post(f"/db/{shelf}/docs/_files", params={"name": "streamed"}, content=chunks())
    assert r.status_code == 413, r.text
    assert leftovers(shelf, "docs") == []
    assert client.get(f"/db/{shelf}/docs/_count").json()["count"] == count
    # At the limit is fine.
    assert upload(client, shelf, "docs", b"z" * 50).status_code == 201


def test_an_upload_that_fails_on_the_way_leaves_no_temporary_file(client, shelf):
    files = app_module.app.state.buckets

    async def broken():
        yield b"a" * 10
        raise RuntimeError("the network went away")

    async def run():
        with pytest.raises(RuntimeError):
            await files.upload(shelf, "docs", "broken", "text/plain", broken())

    count = client.get(f"/db/{shelf}/docs/_count").json()["count"]
    client.portal.call(run)
    assert leftovers(shelf, "docs") == []
    assert client.get(f"/db/{shelf}/docs/_count").json()["count"] == count


def test_a_row_whose_write_fails_takes_its_bytes_with_it(client, shelf, monkeypatch):
    files = app_module.app.state.buckets
    before = {p.name for p in (files_dir() / shelf / "docs").iterdir()}

    async def refused(*args, **kwargs):
        raise RuntimeError("the database is gone")

    monkeypatch.setattr(app_module.app.state.tables, "insert", refused)

    async def body():
        yield b"data"

    async def run():
        with pytest.raises(RuntimeError):
            await files.upload(shelf, "docs", "lost", None, body())

    client.portal.call(run)
    assert {p.name for p in (files_dir() / shelf / "docs").iterdir()} == before


def test_only_the_file_routes_write_a_buckets_rows(client, shelf):
    row = upload(client, shelf, "docs", b"keep me").json()
    attempts = [
        client.put(f"/db/{shelf}/docs/x1", json={"name": "x"}),
        client.post(f"/db/{shelf}/docs", json={"name": "x"}),
        client.patch(f"/db/{shelf}/docs/{row['id']}", json={"name": "renamed"}),
        client.delete(f"/db/{shelf}/docs/{row['id']}"),
        client.put(f"/db/{shelf}/docs/_bulk", json={"rows": [{"id": "x2", "name": "x"}]}),
        client.post(f"/db/{shelf}/docs/_bulk-delete", json={"ids": [row["id"]]}),
        client.post(f"/db/{shelf}/_transaction", json={"puts": {"docs": [{"id": "x3"}]}}),
        client.post(f"/db/{shelf}/_transaction", json={"deletes": {"docs": [row["id"]]}}),
        client.put(f"/db/{shelf}/_edges", json={"edges": [{"type": "wrote", "from": "people/ann", "to": f"docs/{row['id']}"}]}),
        client.post(f"/db/{shelf}/_edges/_delete", json={"edges": [{"type": "wrote", "from": f"docs/{row['id']}", "to": "people/ann"}]}),
    ]
    for r in attempts:
        assert r.status_code == 403, (r.request.method, r.request.url, r.text)
        assert "is a bucket" in r.json()["detail"] and "_files" in r.json()["detail"]
    assert client.get(f"/db/{shelf}/docs/{row['id']}").json() == row
    # Its fields are fixed.
    r = client.put(f"/db/{shelf}/docs/_schema", json={"fields": [{"name": "name", "type": "string"}]})
    assert r.status_code == 400 and "fixed" in r.json()["detail"]


def test_a_bucket_is_made_with_no_fields_and_never_as_a_graph_table(client, shelf):
    r = client.post(f"/db/{shelf}", json={"name": "b1", "bucket": True, "fields": [{"name": "x", "type": "string"}]})
    assert r.status_code == 400 and "fixed" in r.json()["detail"]
    r = client.post(f"/db/{shelf}", json={"name": "b1", "bucket": True, "graph": True})
    assert r.status_code == 400
    r = client.post(f"/db/{shelf}", json={"name": "b1", "bucket": True, "graph": False})
    assert r.status_code == 201 and r.json()["bucket"] is True
    # Not a bucket: no files.
    assert upload(client, shelf, "people", b"x").status_code == 400
    assert upload(client, shelf, "nope", b"x").status_code == 404
    assert upload(client, "bk_nowhere", "docs", b"x").status_code == 404
    assert client.get(f"/db/{shelf}/docs/_files/{'0' * 32}").status_code == 404
    assert client.get(f"/db/{shelf}/docs/_files/not-an-id").status_code == 400
    assert client.delete(f"/db/{shelf}/docs/_files/..%2F..%2Fx").status_code in (400, 404)


def test_a_row_whose_bytes_are_gone_says_so(client, shelf):
    row = upload(client, shelf, "docs", b"soon gone").json()
    (files_dir() / shelf / "docs" / row["id"]).unlink()
    r = client.get(f"/db/{shelf}/docs/_files/{row['id']}")
    assert r.status_code == 404 and "missing from disk" in r.json()["detail"]


def test_a_buckets_folder_is_its_own(client, shelf):
    # Type names are case-sensitive, a disk may not be: Docs would share docs's folder there.
    r = client.post(f"/db/{shelf}", json={"name": "Docs", "bucket": True})
    assert r.status_code == 409 and "case" in r.json()["detail"]
    # A folder that holds files already (left by hand, say) is not taken over.
    leftover = files_dir() / shelf / "leftover"
    leftover.mkdir(parents=True)
    (leftover / "old").write_bytes(b"old")
    r = client.post(f"/db/{shelf}", json={"name": "leftover", "bucket": True})
    assert r.status_code == 409 and "holds files" in r.json()["detail"]
    assert (leftover / "old").exists()


def test_dropping_a_bucket_or_its_workspace_removes_its_files(client):
    assert client.post("/db", json={"name": "bk_gone"}).status_code == 201
    for bucket in ("one", "two"):
        assert client.post("/db/bk_gone", json={"name": bucket, "bucket": True}).status_code == 201
        assert upload(client, "bk_gone", bucket, b"bytes").status_code == 201
    assert client.delete("/db/bk_gone/one").status_code == 204
    assert not (files_dir() / "bk_gone" / "one").exists()
    assert (files_dir() / "bk_gone" / "two").exists()
    # Made again, it starts empty.
    assert client.post("/db/bk_gone", json={"name": "one", "bucket": True}).status_code == 201
    assert client.get("/db/bk_gone/one").json()["rows"] == []
    assert client.delete("/db/bk_gone").status_code == 204
    assert not (files_dir() / "bk_gone").exists()


def test_the_client_key_uploads_lists_downloads_and_deletes(client, shelf, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    key = {"X-API-Key": "client-key"}
    assert upload(client, shelf, "docs", b"x").status_code == 401
    r = upload(client, shelf, "docs", b"mine", headers=key)
    assert r.status_code == 201, r.text
    row = r.json()
    assert row in client.get(f"/db/{shelf}/docs", headers=key, params={"_limit": 1000}).json()["rows"]
    assert client.get(f"/db/{shelf}/docs/_files/{row['id']}", headers=key).content == b"mine"
    assert client.delete(f"/db/{shelf}/docs/_files/{row['id']}", headers=key).status_code == 204
    # Making a bucket is the admin's, as making any table is.
    assert client.post(f"/db/{shelf}", json={"name": "b2", "bucket": True}, headers=key).status_code == 401


def test_files_upload_concurrently(client, shelf):
    files = app_module.app.state.buckets

    async def one(i):
        async def body():
            yield str(i).encode() * 1000
        return await files.upload(shelf, "docs", f"c{i}", None, body())

    async def run():
        return await asyncio.gather(*(one(i) for i in range(8)))

    rows = client.portal.call(run)
    assert len({r["id"] for r in rows}) == 8
    for i, r in enumerate(rows):
        assert r["sha256"] == hashlib.sha256(str(i).encode() * 1000).hexdigest()
    assert leftovers(shelf, "docs") == []
