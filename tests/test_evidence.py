import asyncio
import hashlib
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("arcadedb_embedded")

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402

from api import app as app_module  # noqa: E402
from config import Config  # noqa: E402
from evidence.enrich import hosts_of  # noqa: E402
from evidence import GENESIS_HASH, verify_uploads  # noqa: E402
from errors import Refused  # noqa: E402
from evidence.record import is_valid_artifact_name  # noqa: E402
from evidence.store import CAPTURE  # noqa: E402
from evidence.tsa import SHA256_OID, TsaError, build_request, inspect  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"fake image bytes " * 100
HTML = b"<html><body>hello</body></html>"
FIXED_KEY = Ed25519PrivateKey.from_private_bytes(bytes(range(32)))
FIXED_FINGERPRINT = hashlib.sha256(
    FIXED_KEY.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
).hexdigest()


class FakeEnrich:
    """Stands in for the enrichers runner: answers at once, except for one host that never does."""

    def __init__(self):
        self.calls = []

    async def enrich(self, value, type_=None, names=None, refresh=False):
        self.calls.append(value)
        if value == "slow.example":
            await asyncio.sleep(3600)
        kind = "ipv4" if value[0].isdigit() else "domain"
        return {"value": value, "type": kind, "results": [{"enricher": "fake", "status": "ok", "outputs": {"seen": True}}],
                "merged": {"seen": True}, "sources": {"seen": "fake"}}


@pytest.fixture(scope="module")
def client():
    key_file = Path(Config.EVIDENCE_KEY_FILE)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_bytes(FIXED_KEY.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    # The JVM must start on the main thread (see Database.start_jvm).
    app_module.app.state.database.start_jvm()
    real_enrichers = app_module.app.state.enrichers
    app_module.app.state.enrichers = FakeEnrich()
    try:
        with TestClient(app_module.app) as c:
            yield c
    finally:
        app_module.app.state.enrichers = real_enrichers


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def metadata(case_id="inbox", artifacts=None, **extra):
    base = {
        "caseId": case_id,
        "artifacts": artifacts if artifacts is not None else [{"name": "screenshot.png", "sha256": sha(PNG), "size": len(PNG), "mediaType": "image/png"}],
        "page": {"documentUrl": "https://example.com/page?x=1", "tabUrl": "https://example.com/page?x=1", "title": "Example"},
        "network": {"mainFrame": {"ip": "93.184.216.34", "status": 200,
                                  "redirects": [{"from": "http://example.com/", "to": "https://example.com/page?x=1"}]}},
        "note": "a note",
    }
    base.update(extra)
    return base


def upload(client, meta, files=None, headers=None, workspace=None):
    files = files if files is not None else {"screenshot.png": (PNG, "image/png")}
    parts = [("file", (name, data, ctype)) for name, (data, ctype) in files.items()]
    path = f"/evidence/captures?workspace={workspace}" if workspace else "/evidence/captures"
    return client.post(path, data={"metadata": json.dumps(meta)}, files=parts, headers=headers or {})


# ------------------------------------------------------------ pure parts
def test_artifact_names():
    assert is_valid_artifact_name("screenshot.png")
    assert is_valid_artifact_name("page.mhtml") and is_valid_artifact_name("dom-1_2.html")
    for bad in ("", ".hidden", "Upper.png", "a/b", "a b", "ä", "x" * 101, None, 3):
        assert not is_valid_artifact_name(bad)


def test_verify_uploads_checks_sizes_and_order():
    meta = {"artifacts": [{"name": "b.txt", "sha256": sha(b"b")}, {"name": "a.txt", "sha256": sha(b"a"), "size": 1}]}
    verified = verify_uploads(meta, {"a.txt": b"a", "b.txt": b"b"})
    assert [a.name for a in verified] == ["b.txt", "a.txt"]
    assert verified[0].media_type == "application/octet-stream"
    with pytest.raises(Refused) as e:
        verify_uploads({"artifacts": [{"name": "a.txt", "sha256": sha(b"a"), "size": 2}]}, {"a.txt": b"a"})
    assert e.value.status == 400 and "size mismatch" in str(e.value)
    with pytest.raises(Refused):
        verify_uploads({"artifacts": []}, {})


def test_hosts_to_enrich_are_deduplicated_in_order():
    meta = metadata()
    meta["page"]["tabUrl"] = "https://other.example/"
    assert hosts_of(meta) == [("ip", "93.184.216.34"), ("host", "example.com"), ("host", "other.example")]
    assert hosts_of({"page": {"documentUrl": "about:blank"}}) == []
    assert hosts_of("junk") == []


def test_tsa_request_imprint_is_the_record_hash():
    req = build_request(PNG)
    assert req.message_imprint.hash_algorithm == SHA256_OID
    assert req.message_imprint.message == hashlib.sha256(PNG).digest()
    assert req.nonce is not None and req.cert_req


def test_tsa_inspect_rejects_garbage():
    with pytest.raises(TsaError):
        inspect(b"\x30\x03\x02\x01\x00", b"\x00" * 32)


# ----------------------------------------------------------------- routes
def test_cases(client):
    cases = client.get("/evidence/cases").json()["cases"]
    assert any(c["id"] == "inbox" and c["name"] == "Inbox" for c in cases)
    made = client.post("/evidence/cases", json={"name": "  Case one  "})
    assert made.status_code == 200
    case = made.json()["case"]
    assert case["name"] == "Case one" and case["id"] and case["createdAt"].endswith("Z")
    assert case["id"] in [c["id"] for c in client.get("/evidence/cases").json()["cases"]]
    bad = client.post("/evidence/cases", json={"name": "   "})
    assert bad.status_code == 422 and "case name" in bad.json()["detail"][0]["msg"]
    assert client.post("/evidence/cases", json={"name": "x" * 201}).status_code == 422
    assert client.get("/evidence/cases/nope/captures").status_code == 404


def test_upload_chains_signs_and_enriches(client):
    first = upload(client, metadata())
    assert first.status_code == 200, first.text
    a = first.json()
    assert a["seq"] >= 1 and a["captureId"] and a["receivedAt"].endswith("Z")
    assert len(a["signature"]) == 128 and len(a["recordHash"]) == 64
    assert a["timestamp"] == {"status": "disabled"}

    second = upload(client, metadata(), files={"screenshot.png": (PNG, "image/png")})
    assert second.status_code == 200
    b = second.json()
    assert b["seq"] == a["seq"] + 1

    detail = client.get(f"/evidence/captures/{b['captureId']}").json()
    record = detail["record"]
    assert record["schema"] == "evidence-capture/record/v3" and record["workspaceId"] == "default"
    assert record["prevRecordHash"] == a["recordHash"]
    assert record["seq"] == b["seq"] and record["captureId"] == b["captureId"] and record["caseId"] == "inbox"
    assert record["service"] == {"name": "ai-toolkit-evidence", "version": app_module.APP_VERSION, "signingKeySha256": FIXED_FINGERPRINT}
    assert record["artifacts"] == [{"name": "screenshot.png", "mediaType": "image/png", "size": len(PNG), "sha256": sha(PNG)}]
    assert record["client"]["note"] == "a note" and record["client"]["caseId"] == "inbox"
    assert detail["recordHash"] == b["recordHash"] and detail["signature"] == b["signature"]
    assert detail["timestamp"] == {"genTime": None, "error": None}

    items = record["enrichment"]["items"]
    assert [i["value"] for i in items] == ["93.184.216.34", "example.com"]
    assert items[0]["type"] == "ipv4" and items[0]["results"]["merged"] == {"seen": True}
    assert items[1]["type"] == "domain"
    assert record["enrichment"]["startedAt"] <= record["enrichment"]["finishedAt"]

    # The first record of the ledger starts the chain from the genesis hash.
    chain = client.get("/evidence/verify").json()
    assert chain["ok"] and chain["records"] >= 2 and chain["headHash"] == b["recordHash"]
    first_record = next(r for r in [client.get(f"/evidence/captures/{a['captureId']}").json()["record"]] if r["seq"] == 1)
    assert first_record["prevRecordHash"] == GENESIS_HASH

    # The files on disk: the exact signed bytes, the raw signature and the read-only artifact.
    directory = Path(Config.EVIDENCE_DIR) / "captures" / b["captureId"]
    canonical = (directory / "record.json").read_bytes()
    assert sha(canonical) == b["recordHash"]
    assert json.loads(canonical) == record
    assert (directory / "record.sig").read_bytes().hex() == b["signature"]
    FIXED_KEY.public_key().verify(bytes.fromhex(b["signature"]), canonical)
    assert (directory / "artifacts" / "screenshot.png").read_bytes() == PNG
    assert oct(os.stat(directory / "artifacts" / "screenshot.png").st_mode)[-3:] == "444"
    assert not (directory / "record.tsr").exists()


def test_listing_and_artifacts(client):
    case = client.post("/evidence/cases", json={"name": "Listing"}).json()["case"]
    one = upload(client, metadata(case_id=case["id"])).json()
    two_meta = metadata(case_id=case["id"], artifacts=[
        {"name": "screenshot.png", "sha256": sha(PNG), "mediaType": "image/png"},
        {"name": "dom.html", "sha256": sha(HTML), "mediaType": "text/html"},
    ])
    two = upload(client, two_meta, files={"screenshot.png": (PNG, "image/png"), "dom.html": (HTML, "text/html")}).json()
    listed = client.get(f"/evidence/cases/{case['id']}/captures").json()["captures"]
    assert [c["captureId"] for c in listed] == [two["captureId"], one["captureId"]]
    assert listed[0] == {
        "seq": two["seq"], "captureId": two["captureId"], "receivedAt": two["receivedAt"],
        "url": "https://example.com/page?x=1", "title": "Example", "recordHash": two["recordHash"], "timestampedAt": None,
    }
    assert client.get("/evidence/cases/inbox/captures").json()["captures"][0]["captureId"] != two["captureId"]

    got = client.get(f"/evidence/captures/{two['captureId']}/artifacts/dom.html")
    assert got.status_code == 200 and got.content == HTML and got.headers["content-type"].startswith("text/html")
    assert client.get(f"/evidence/captures/{two['captureId']}/artifacts/other.html").status_code == 404
    assert client.get(f"/evidence/captures/{two['captureId']}/artifacts/%2e%2e").status_code == 404
    assert client.get(f"/evidence/captures/{two['captureId']}/artifacts/Screenshot.png").status_code == 404
    assert client.get("/evidence/captures/nope").status_code == 404
    assert client.get("/evidence/captures/nope").json() == {"error": "unknown capture", "detail": "unknown capture"}


def test_upload_rejections(client):
    before = client.get("/evidence/verify").json()["records"]
    meta = metadata(artifacts=[{"name": "screenshot.png", "sha256": "ab" * 32, "mediaType": "image/png"}])
    r = upload(client, meta)
    assert r.status_code == 400
    assert r.json()["error"] == f"hash mismatch for screenshot.png: client said {'ab' * 32}, received {sha(PNG)}"

    r = upload(client, metadata(), files={"screenshot.png": (PNG, "image/png"), "extra.bin": (b"x", "application/octet-stream")})
    assert r.status_code == 400 and r.json()["error"] == "uploaded file extra.bin was not declared"

    r = upload(client, metadata(), files={"dom.html": (HTML, "text/html")})
    assert r.status_code == 400 and "declared but not uploaded" in r.json()["error"]

    r = upload(client, metadata(case_id="no-such-case"))
    assert r.status_code == 404 and r.json()["error"] == "unknown case"

    r = upload(client, metadata(artifacts=[{"name": "../x", "sha256": sha(PNG)}]))
    assert r.status_code == 400 and "invalid artifact name" in r.json()["error"]

    r = client.post("/evidence/captures", data={"metadata": json.dumps(metadata()), "other": "1"},
                    files=[("file", ("screenshot.png", PNG, "image/png"))])
    assert r.status_code == 400 and "unexpected field" in r.json()["error"]

    r = client.post("/evidence/captures", files=[("file", ("screenshot.png", PNG, "image/png"))])
    assert r.status_code == 400 and r.json()["error"] == "missing metadata field"

    r = client.post("/evidence/captures", data={"metadata": "{not json"}, files=[("file", ("screenshot.png", PNG, "image/png"))])
    assert r.status_code == 400 and r.json()["error"].startswith("metadata is not JSON")

    r = client.post("/evidence/captures", data={"metadata": json.dumps({"artifacts": [{"name": "a", "sha256": "0"}]})})
    assert r.status_code == 400 and r.json()["error"] == "metadata.caseId is required"
    # Nothing was appended.
    assert client.get("/evidence/verify").json()["records"] == before


def test_origin_guard(client, monkeypatch):
    assert client.get("/evidence/cases", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/evidence/cases", headers={"Origin": "https://evil.example"}).json()["error"] == "origin not allowed"
    assert client.get("/evidence/cases", headers={"Origin": "chrome-extension://abcdefghijklmnop"}).status_code == 200
    assert client.get("/evidence/cases", headers={"Origin": "moz-extension://1234-5678/"}).status_code == 200
    assert client.get("/evidence/cases", headers={"Origin": "null"}).status_code == 403
    monkeypatch.setattr(Config, "EVIDENCE_ALLOWED_ORIGINS", ["chrome-extension://pinned"])
    assert client.get("/evidence/cases", headers={"Origin": "chrome-extension://pinned/"}).status_code == 200
    assert client.get("/evidence/cases", headers={"Origin": "chrome-extension://other"}).status_code == 403
    r = upload(client, metadata(), headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_api_key_is_required(client, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    r = client.get("/evidence/cases")
    assert r.status_code == 401 and r.json()["error"] and r.headers["www-authenticate"] == "Bearer"
    assert client.get("/evidence/cases", headers={"X-API-Key": "client-key"}).status_code == 200
    assert client.get("/evidence/cases", headers={"Authorization": "Bearer admin-key"}).status_code == 200
    assert client.post("/evidence/timestamp-pending", headers={"X-API-Key": "client-key"}).status_code == 401
    # The admin key, but no TSA configured.
    r = client.post("/evidence/timestamp-pending", headers={"X-API-Key": "admin-key"})
    assert r.status_code == 400 and "disabled" in r.json()["error"]


def test_public_key(client):
    r = client.get("/evidence/public-key")
    assert r.status_code == 200 and r.headers["content-type"].startswith("application/x-pem-file")
    assert r.text.startswith("-----BEGIN PUBLIC KEY-----")
    key = serialization.load_pem_public_key(r.content)
    raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    assert hashlib.sha256(raw).hexdigest() == FIXED_FINGERPRINT
    assert client.get("/evidence/verify").json()["signingKeySha256"] == FIXED_FINGERPRINT
    assert Path(Config.EVIDENCE_KEY_FILE).with_name("ed25519.pub.pem").read_text() == r.text


def test_enrichment_timeout_and_skip(client, monkeypatch):
    monkeypatch.setattr(Config, "EVIDENCE_ENRICH_TIMEOUT", 0.2)
    meta = metadata()
    meta["page"]["tabUrl"] = "https://slow.example/"
    r = upload(client, meta)
    assert r.status_code == 200
    items = client.get(f"/evidence/captures/{r.json()['captureId']}").json()["record"]["enrichment"]["items"]
    assert items[-1] == {"value": "slow.example", "type": "host", "error": "timeout"}
    assert "results" in items[0]

    monkeypatch.setattr(Config, "EVIDENCE_ENRICH", False)
    r = upload(client, metadata())
    assert client.get(f"/evidence/captures/{r.json()['captureId']}").json()["record"]["enrichment"] == {"skipped": True}


def test_verify_detects_tampering(client):
    report = client.get("/evidence/verify").json()
    assert report["ok"] and report["records"] > 0 and report["timestamped"] == 0
    assert report["issues"] and all(i["severity"] == "warning" and i["message"] == "no trusted timestamp" for i in report["issues"])

    store = client.portal.call(client.app.state.evidence.ledger, "default")
    db = store._db()
    row = store._rows(db, f"SELECT record_json FROM {CAPTURE} WHERE seq = 1")[0]
    original = row["record_json"]
    tampered = original.replace('"note":"a note"', '"note":"a different note"')
    assert tampered != original
    store._commit(db, lambda: db.command("sql", f"UPDATE {CAPTURE} SET record_json = :j WHERE seq = 1", {"j": tampered}))
    try:
        report = client.get("/evidence/verify").json()
        assert not report["ok"]
        errors = [i["message"] for i in report["issues"] if i["severity"] == "error" and i["seq"] == 1]
        assert "record bytes do not match stored record hash" in errors
        assert "signature does not verify" in errors
        assert "record.json on disk differs from the log" in errors
        # One broken record gives one set of errors, not a cascade: record 2 still chains to the claimed hash.
        assert not [i for i in report["issues"] if i["severity"] == "error" and i["seq"] != 1]
    finally:
        store._commit(db, lambda: db.command("sql", f"UPDATE {CAPTURE} SET record_json = :j WHERE seq = 1", {"j": original}))
    assert client.get("/evidence/verify").json()["ok"]

    # A modified artifact on disk is found too.
    capture_id = store._rows(db, f"SELECT capture_id FROM {CAPTURE} WHERE seq = 1")[0]["capture_id"]
    path = Path(Config.EVIDENCE_DIR) / "captures" / capture_id / "artifacts" / "screenshot.png"
    os.chmod(path, 0o644)
    path.write_bytes(PNG + b"!")
    try:
        report = client.get("/evidence/verify").json()
        assert [i["message"] for i in report["issues"] if i["severity"] == "error"] == ["artifact screenshot.png has been modified"]
    finally:
        path.write_bytes(PNG)
        os.chmod(path, 0o444)
    assert client.get("/evidence/verify").json()["ok"]


def test_annotations_stay_apart_from_the_record(client):
    a = upload(client, metadata()).json()
    cid = a["captureId"]
    before = client.get("/evidence/verify").json()
    assert before["ok"]
    assert cid not in [x["captureId"] for x in client.get("/evidence/cases/inbox/annotations").json()["annotations"]]

    # Tags: trimmed, inner whitespace collapsed, de-duplicated case-insensitively (first spelling wins).
    r = client.put(f"/evidence/captures/{cid}/annotations", json={"tags": [" Phishing ", "phishing", "a  b", "A B", "x"]})
    assert r.status_code == 200, r.text
    first = r.json()
    assert first == {"captureId": cid, "tags": ["Phishing", "a b", "x"], "notes": [], "updatedAt": first["updatedAt"]}
    assert first["updatedAt"].endswith("Z")

    # Notes get ids and createdAt; a missing key keeps the other list.
    r = client.put(f"/evidence/captures/{cid}/annotations", json={"notes": [{"text": "  first  "}, {"text": "second"}]})
    assert r.status_code == 200, r.text
    second = r.json()
    assert second["tags"] == ["Phishing", "a b", "x"]
    n1, n2 = second["notes"]
    assert n1["text"] == "first" and n1["id"] and n1["createdAt"].endswith("Z") and n1["updatedAt"] is None
    assert n2["text"] == "second" and n2["id"] != n1["id"]

    # Known ids keep createdAt; a changed text sets updatedAt; unknown ids are new; left-out notes go.
    r = client.put(f"/evidence/captures/{cid}/annotations", json={
        "tags": [], "notes": [{"id": n1["id"], "text": "first, edited"}, {"id": "nope", "text": "third"}]})
    third = r.json()
    assert third["tags"] == []
    e1, e3 = third["notes"]
    assert e1["id"] == n1["id"] and e1["createdAt"] == n1["createdAt"] and e1["updatedAt"] is not None
    assert e3["id"] not in ("nope", n1["id"], n2["id"]) and e3["updatedAt"] is None
    # Same text again: updatedAt unchanged.
    again = client.put(f"/evidence/captures/{cid}/annotations", json={"notes": third["notes"]}).json()
    assert again["notes"][0]["updatedAt"] == e1["updatedAt"]

    # Listed by case, only captures that have annotations; unknown case or capture is 404.
    listed = client.get("/evidence/cases/inbox/annotations").json()["annotations"]
    mine = [x for x in listed if x["captureId"] == cid]
    assert mine == [again]
    assert all(x["tags"] or x["notes"] or x["updatedAt"] for x in listed)
    assert client.get("/evidence/cases/nope/annotations").status_code == 404
    assert client.put("/evidence/captures/nope/annotations", json={"tags": ["x"]}).status_code == 404

    # Validation: FastAPI's 422, as on every route.
    for body in ({"tags": "x"}, {"tags": [""]}, {"tags": ["x" * 65]}, {"tags": [str(i) for i in range(65)]},
                 {"notes": [{"text": "   "}]}, {"notes": [{"text": "x" * 20001}]}, {"notes": [{"text": "x"}] * 201},
                 {"notes": ["x"]}, {"notes": [{"id": 3, "text": "x"}]}, {"other": 1}, []):
        r = client.put(f"/evidence/captures/{cid}/annotations", json=body)
        assert r.status_code == 422 and r.json()["detail"], body

    # None of it touched the record, its hash, the files or the chain.
    detail = client.get(f"/evidence/captures/{cid}").json()
    assert detail["recordHash"] == a["recordHash"] and detail["signature"] == a["signature"]
    assert "tags" not in detail["record"] and "annotations" not in detail["record"]
    canonical = (Path(Config.EVIDENCE_DIR) / "captures" / cid / "record.json").read_bytes()
    assert sha(canonical) == a["recordHash"]
    after = client.get("/evidence/verify").json()
    assert after["ok"] and after["records"] == before["records"] and after["headHash"] == before["headHash"]


def test_todos_belong_to_the_case(client):
    case_id = client.post("/evidence/cases", json={"name": "Todo case"}).json()["case"]["id"]
    a = upload(client, metadata(case_id)).json()
    before = client.get("/evidence/verify").json()
    assert client.get(f"/evidence/cases/{case_id}/todos").json() == {"caseId": case_id, "todos": [], "updatedAt": None}

    # New todos get ids and createdAt; texts are trimmed; done defaults to false, and a done one gets doneAt.
    r = client.put(f"/evidence/cases/{case_id}/todos", json={"todos": [
        {"text": "  check the registrar  "}, {"text": "ask for logs", "done": True}]})
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["caseId"] == case_id and first["updatedAt"].endswith("Z")
    t1, t2 = first["todos"]
    assert t1["text"] == "check the registrar" and t1["id"] and not t1["done"] and t1["doneAt"] is None
    assert t1["createdAt"].endswith("Z") and t1["updatedAt"] is None
    assert t2["done"] and t2["doneAt"].endswith("Z") and t2["id"] != t1["id"]

    # Known ids keep createdAt; a changed text sets updatedAt, checking sets doneAt and unchecking clears it;
    # the order is the one given; unknown ids and repeats are new; left-out todos go.
    r = client.put(f"/evidence/cases/{case_id}/todos", json={"todos": [
        {"id": t2["id"], "text": "ask for logs", "done": False},
        {"id": t1["id"], "text": "check the registrar again", "done": True},
        {"id": t1["id"], "text": "a repeat"},
        {"id": "nope", "text": "third"}]})
    second = r.json()
    e2, e1, repeat, e3 = second["todos"]
    assert e2["id"] == t2["id"] and not e2["done"] and e2["doneAt"] is None and e2["updatedAt"] is None
    assert e1["id"] == t1["id"] and e1["createdAt"] == t1["createdAt"] and e1["updatedAt"] and e1["done"] and e1["doneAt"]
    assert repeat["id"] not in (t1["id"], t2["id"]) and e3["id"] not in ("nope", t1["id"], t2["id"])
    # Same again: nothing changes but the list's updatedAt.
    again = client.put(f"/evidence/cases/{case_id}/todos", json={"todos": second["todos"]}).json()
    assert again["todos"] == second["todos"]
    assert client.get(f"/evidence/cases/{case_id}/todos").json() == again

    # Another case's list is its own; an unknown case is 404.
    assert client.get("/evidence/cases/inbox/todos").json()["caseId"] == "inbox"
    assert e1["id"] not in [t["id"] for t in client.get("/evidence/cases/inbox/todos").json()["todos"]]
    assert client.get("/evidence/cases/nope/todos").status_code == 404
    assert client.put("/evidence/cases/nope/todos", json={"todos": []}).status_code == 404

    # Validation: FastAPI's 422, as on every route.
    for body in ({}, {"todos": "x"}, {"todos": ["x"]}, {"todos": [{"text": "  "}]}, {"todos": [{"text": "x" * 2001}]},
                 {"todos": [{"text": "x"}] * 501}, {"todos": [{"id": 3, "text": "x"}]},
                 {"todos": [{"text": "x", "done": "yes"}]}, {"todos": [], "other": 1}, []):
        r = client.put(f"/evidence/cases/{case_id}/todos", json=body)
        assert r.status_code == 422 and r.json()["detail"], body

    # An empty list clears it.
    assert client.put(f"/evidence/cases/{case_id}/todos", json={"todos": []}).json()["todos"] == []

    # None of it touched the records or the chain.
    detail = client.get(f"/evidence/captures/{a['captureId']}").json()
    assert detail["recordHash"] == a["recordHash"] and "todos" not in detail["record"]
    after = client.get("/evidence/verify").json()
    assert after["ok"] and after["records"] == before["records"] and after["headHash"] == before["headHash"]


def test_off_when_no_directory(client, monkeypatch):
    monkeypatch.setattr(client.app.state, "evidence", None)
    r = client.get("/evidence/cases")
    assert r.status_code == 503 and "EVIDENCE_DIR" in r.json()["error"]


# ------------------------------------------------------------- workspaces
WORKSPACE = "0199f3a1-7c2e-7d4b-9a1e-2f3c4d5e6f70"  # a UUIDv7


def test_a_workspace_has_its_own_ledger(client):
    listed = client.get("/evidence/workspaces").json()["workspaces"]
    assert listed[0] == {"id": "default", "name": "Default", "createdAt": listed[0]["createdAt"]}
    made = client.post("/evidence/workspaces", json={"id": WORKSPACE, "name": "  Fraud ring  "})
    assert made.status_code == 201, made.text
    assert made.json()["workspace"]["name"] == "Fraud ring"
    assert WORKSPACE in [w["id"] for w in client.get("/evidence/workspaces").json()["workspaces"]]
    assert client.post("/evidence/workspaces", json={"id": WORKSPACE, "name": "again"}).status_code == 409

    # Its own inbox, its own chain from the genesis hash, its own files.
    assert [c["id"] for c in client.get(f"/evidence/cases?workspace={WORKSPACE}").json()["cases"]] == ["inbox"]
    r = upload(client, metadata(), workspace=WORKSPACE)
    assert r.status_code == 200, r.text
    a = r.json()
    assert a["seq"] == 1
    record = client.get(f"/evidence/captures/{a['captureId']}?workspace={WORKSPACE}").json()["record"]
    assert record["schema"] == "evidence-capture/record/v3" and record["workspaceId"] == WORKSPACE
    assert record["prevRecordHash"] == GENESIS_HASH
    directory = Path(Config.EVIDENCE_DIR) / "workspaces" / WORKSPACE / "captures" / a["captureId"]
    assert sha((directory / "record.json").read_bytes()) == a["recordHash"]
    artifact = client.get(f"/evidence/captures/{a['captureId']}/artifacts/screenshot.png?workspace={WORKSPACE}")
    assert artifact.status_code == 200 and artifact.content == PNG

    # A capture is found in its own workspace only.
    assert client.get(f"/evidence/captures/{a['captureId']}").status_code == 404
    listed = client.get(f"/evidence/cases/inbox/captures?workspace={WORKSPACE}").json()["captures"]
    assert [c["captureId"] for c in listed] == [a["captureId"]]
    assert a["captureId"] not in [c["captureId"] for c in client.get("/evidence/cases/inbox/captures").json()["captures"]]
    report = client.get(f"/evidence/verify?workspace={WORKSPACE}").json()
    assert report["workspace"] == WORKSPACE and report["ok"] and report["records"] == 1
    assert report["headHash"] == a["recordHash"]
    assert client.get("/evidence/verify").json()["workspace"] == "default"

    # Its namespace in /db is the service's: listed as such, read-only there, and its prefix reserved.
    namespace = "evidence_" + WORKSPACE.replace("-", "_")
    listing = {n["name"]: n for n in client.get("/db").json()["namespaces"]}
    assert listing[namespace]["managed"] and {"cases", "captures"} <= {t["name"] for t in listing[namespace]["tables"]}
    assert client.put(f"/db/{namespace}/cases/x", json={"name": "sneaky"}).status_code == 403
    assert client.post("/db", json={"name": "evidence_mine"}).status_code == 409


def test_workspace_ids_and_names_are_checked(client):
    for bad in ("Upper", "a--b", "-a", "a_b"):
        r = client.post("/evidence/workspaces", json={"id": bad, "name": "x"})
        assert r.status_code == 400, (bad, r.text)
    for bad in ("x" * 37, 5):  # the body's own checks
        assert client.post("/evidence/workspaces", json={"id": bad, "name": "x"}).status_code == 422, bad
    assert client.post("/evidence/workspaces", json={"name": " "}).status_code == 422
    made = client.post("/evidence/workspaces", json={"name": "Made up"}).json()["workspace"]
    assert len(made["id"]) == 36 and made["name"] == "Made up"
    assert client.get("/evidence/cases?workspace=nope").status_code == 404
    assert client.get("/evidence/cases?workspace=Bad_Id").status_code == 400
    assert upload(client, metadata(), workspace="nope").status_code == 404


def test_workspaces_are_made_with_the_admin_key(client, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    as_client = {"X-API-Key": "client-key"}
    assert client.post("/evidence/workspaces", json={"name": "x"}, headers=as_client).status_code == 401
    assert client.get("/evidence/workspaces", headers=as_client).status_code == 200


def test_a_record_moved_to_another_workspace_is_found(client):
    """A record signed for one workspace, copied into another's ledger, still verifies by its signature: the
    workspace it names gives it away."""
    source, target = (client.post("/evidence/workspaces", json={"name": n}).json()["workspace"]["id"] for n in "st")
    a = upload(client, metadata(), workspace=source).json()
    from_ledger = client.portal.call(client.app.state.evidence.ledger, source)
    to_ledger = client.portal.call(client.app.state.evidence.ledger, target)
    row = from_ledger._get(a["captureId"])
    db = to_ledger._db()
    to_ledger._commit(db, lambda: to_ledger._insert(db, to_ledger._captures, {"id": row["capture_id"], **row}))
    report = client.get(f"/evidence/verify?workspace={target}").json()
    errors = [i["message"] for i in report["issues"] if i["severity"] == "error"]
    assert "workspaceId inside record does not match its workspace" in errors
    assert "signature does not verify" not in errors
    assert client.get(f"/evidence/verify?workspace={source}").json()["ok"]


def test_a_ledger_from_before_workspaces_carries_on(arcade, tmp_path):
    """A database with v2 records (no workspaceId) and no workspaces: they are the default workspace's, which goes
    on chaining from them."""
    from evidence import CaptureFiles, EvidenceStore, Signer, VerifiedArtifact, verify_all
    from evidence.record import build_record, canonical, now_iso, sha256_hex
    from evidence.store import CASE, TABLES
    from vectorstore.tables import TableStore

    signer, files = Signer(tmp_path / "key.pem"), CaptureFiles(tmp_path / "ev")
    artifacts = [VerifiedArtifact("a.txt", "text/plain", sha256_hex(b"a"), b"a")]
    old = build_record(seq=1, capture_id="c-old", workspace_id="default", case_id="inbox", received_at=now_iso(),
                       prev_hash=GENESIS_HASH, version="0.9", signing_key_sha256=signer.fingerprint,
                       artifacts=artifacts, client={"caseId": "inbox"}, enrichment={"skipped": True})
    del old["workspaceId"]
    old["schema"] = "evidence-capture/record/v2"
    data = canonical(old)
    files.write_capture("c-old", data, signer.sign(data), artifacts)
    tables = TableStore(arcade)
    tables.manage("evidence", "the ledger, as an earlier version left it", TABLES)
    tables.ensure_managed("evidence")
    db = arcade.open()
    with db.transaction():
        db.command("sql", f"INSERT INTO {CASE} CONTENT :row", {"row": {"id": "inbox", "name": "Inbox", "created_at": now_iso()}})
        db.command("sql", f"INSERT INTO {CAPTURE} CONTENT :row", {"row": {
            "id": "c-old", "seq": 1, "capture_id": "c-old", "case_id": "inbox", "received_at": old["receivedAt"],
            "record_json": data.decode(), "record_hash": sha256_hex(data), "signature": signer.sign(data).hex()}})

    store = EvidenceStore(arcade, TableStore(arcade), files, signer, "1.0")
    assert [w["id"] for w in store._list_workspaces()] == ["default"]
    ledger = store._ledger("default")
    assert verify_all(ledger)["ok"]
    appended = ledger._append("inbox", {"caseId": "inbox"}, artifacts, {"skipped": True})
    record = json.loads(appended["recordJson"])
    assert appended["seq"] == 2 and record["prevRecordHash"] == sha256_hex(data)
    assert record["schema"] == "evidence-capture/record/v3" and record["workspaceId"] == "default"
    report = verify_all(ledger)
    assert report["ok"] and report["records"] == 2 and report["workspace"] == "default"
