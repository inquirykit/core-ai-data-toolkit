"""Schedules (cron and interval) and webhooks on finished jobs and runs."""

import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

import tests.fake_jobs  # noqa: F401  registers the test-* jobs
from api import app as app_module
from jobs import schedule
from tests.test_jobs import wait


# ---------------------------------------------------------------------- cron
def test_cron_fields_are_parsed():
    minutes, hours, days, months, weekdays = schedule.parse_cron("*/15 3,4 1-5 * 7")
    assert minutes == {0, 15, 30, 45} and hours == {3, 4} and days == {1, 2, 3, 4, 5}
    assert months == set(range(1, 13)) and weekdays == {0}, "7 is Sunday, like 0"
    assert schedule.parse_cron("5/20 * * * *")[0] == {5, 25, 45}
    for bad in ("* * *", "60 * * * *", "a * * * *", "*/0 * * * *", "5-3 * * * *", "* * 0 * *"):
        with pytest.raises(ValueError):
            schedule.parse_cron(bad)


def ts(*parts):
    return datetime(*parts).timestamp()


def test_next_run_follows_cron_in_local_time():
    # Daily at 03:00: from 2026-10-17 10:00 the next is the 18th at 03:00.
    assert schedule.next_run("0 3 * * *", ts(2026, 10, 17, 10, 0)) == ts(2026, 10, 18, 3, 0)
    # Every 15 minutes: the next quarter after 10:07 is 10:15; exactly on 10:15 moves to 10:30.
    assert schedule.next_run("*/15 * * * *", ts(2026, 10, 17, 10, 7)) == ts(2026, 10, 17, 10, 15)
    assert schedule.next_run("*/15 * * * *", ts(2026, 10, 17, 10, 15)) == ts(2026, 10, 17, 10, 30)
    # Mondays at 09:00 (2026-10-17 is a Saturday).
    assert schedule.next_run("0 9 * * 1", ts(2026, 10, 17, 10, 0)) == ts(2026, 10, 19, 9, 0)
    # Both day fields restricted: the 1st of the month OR a Monday, whichever first.
    assert schedule.next_run("0 9 1 * 1", ts(2026, 10, 17, 10, 0)) == ts(2026, 10, 19, 9, 0)
    assert schedule.next_run("0 9 18 * 1", ts(2026, 10, 17, 10, 0)) == ts(2026, 10, 18, 9, 0)
    with pytest.raises(ValueError):
        schedule.next_run("0 0 30 2 *", ts(2026, 1, 1))
    assert schedule.compute_next(interval_s=600, after=100.0) == 700.0
    assert schedule.describe("0 3 * * *") == "cron 0 3 * * *" and schedule.describe(interval_s=7200) == "every 2h"


# -------------------------------------------------------------------- routes
@pytest.fixture
def client():
    with TestClient(app_module.app) as c:
        yield c


def test_schedules_are_created_run_and_deleted(client):
    r = client.post("/jobs/schedules", json={"name": "nightly", "job": "test-echo", "params": {"text": "hi", "steps": 1},
                                             "cron": "0 3 * * *", "webhook_url": "http://127.0.0.1:9/hook"})
    assert r.status_code == 201, r.text
    sched = r.json()
    assert sched["name"] == "nightly" and sched["enabled"] is True and sched["last_status"] is None
    assert sched["next_run"] > time.time() and sched["interval_s"] is None
    assert [s["id"] for s in client.get("/jobs/schedules").json()["schedules"]] == [sched["id"]]

    # Run now: the job carries the schedule and its webhook URL; the planned next run stays.
    r = client.post(f"/jobs/schedules/{sched['id']}/run")
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["schedule_id"] == sched["id"] and job["webhook_url"] == "http://127.0.0.1:9/hook"
    wait(client, job["id"])
    again = client.get(f"/jobs/schedules/{sched['id']}").json()
    assert again["last_job_id"] == job["id"] and again["last_status"] == "succeeded"
    assert again["next_run"] == sched["next_run"]

    # Update: switch to an interval, disabled, which clears the next run.
    r = client.put(f"/jobs/schedules/{sched['id']}", json={"job": "test-echo", "params": {"text": "x"}, "interval_s": 3600, "enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["cron"] is None and r.json()["interval_s"] == 3600 and r.json()["next_run"] is None
    assert r.json()["name"] == "test-echo", "an empty name falls back to the job's"

    assert client.delete(f"/jobs/schedules/{sched['id']}").status_code == 204
    assert client.get(f"/jobs/schedules/{sched['id']}").status_code == 404
    assert client.get("/jobs/schedules").json()["schedules"] == []


@pytest.mark.parametrize("body, detail", [
    ({"job": "test-echo", "params": {"text": "x"}}, "cron or interval_s"),
    ({"job": "test-echo", "params": {"text": "x"}, "cron": "0 3 * * *", "interval_s": 60}, "not both"),
    ({"job": "test-echo", "params": {"text": "x"}, "cron": "* *"}, "five fields"),
    ({"job": "test-echo", "params": {"text": "x"}, "interval_s": 5}, "at least 60"),
    ({"job": "test-echo", "params": {}, "interval_s": 60}, "text"),
    ({"job": "no-such-job", "params": {}, "interval_s": 60}, "no-such-job"),
])
def test_bad_schedules_are_refused(client, body, detail):
    r = client.post("/jobs/schedules", json=body)
    assert r.status_code == 400, r.text
    assert detail in r.json()["detail"]


def test_due_schedules_submit_their_job_once(client):
    runner = client.app.state.jobs
    sched = client.post("/jobs/schedules", json={"job": "test-echo", "params": {"text": "tick", "steps": 1}, "interval_s": 60}).json()
    # Not due yet.
    assert client.portal.call(runner.scheduler.tick) == []
    client.portal.call(runner.store.mark_schedule_run, sched["id"], 0, None, time.time() - 1)
    submitted = client.portal.call(runner.scheduler.tick)
    assert [j["schedule_id"] for j in submitted] == [sched["id"]]
    row = client.get(f"/jobs/schedules/{sched['id']}").json()
    assert row["last_job_id"] == submitted[0]["id"] and row["next_run"] > time.time() + 50
    # Due again while that job is still going: skipped, next run moved on.
    client.portal.call(runner.store.mark_schedule_run, sched["id"], 0, None, time.time() - 1)
    client.portal.call(runner.store.claim, submitted[0]["id"])  # pretend it runs
    assert client.portal.call(runner.scheduler.tick) == []
    assert client.get(f"/jobs/schedules/{sched['id']}").json()["next_run"] > time.time() + 50
    client.portal.call(runner.store.finish, submitted[0]["id"], "succeeded")
    client.delete(f"/jobs/schedules/{sched['id']}")


# ------------------------------------------------------------------ webhooks
class Receiver(BaseHTTPRequestHandler):
    received = []
    status = 200

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Receiver.received.append((dict(self.headers), json.loads(body)))
        self.send_response(Receiver.status)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def receiver():
    server = HTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Receiver.received = []
    Receiver.status = 200
    yield f"http://127.0.0.1:{server.server_port}/hook"
    server.shutdown()


def wait_received(n, timeout=10.0):
    deadline = time.time() + timeout
    while len(Receiver.received) < n and time.time() < deadline:
        time.sleep(0.02)
    assert len(Receiver.received) >= n, Receiver.received


def test_finished_jobs_are_posted_to_webhooks(client, receiver, monkeypatch):
    webhooks = client.app.state.webhooks
    monkeypatch.setattr(webhooks, "secret", "s3cret")
    r = client.post("/jobs", json={"job": "test-echo", "params": {"text": "hook", "steps": 1}, "webhook_url": receiver})
    assert r.status_code == 202, r.text
    job = wait(client, r.json()["id"])
    wait_received(1)
    headers, body = Receiver.received[0]
    assert headers["X-Toolkit-Event"] == "job.finished" and body["event"] == "job.finished"
    assert body["data"]["id"] == job["id"] and body["data"]["status"] == "succeeded" and body["data"]["result"]["echo"] == "hook"
    assert headers["X-Toolkit-Signature"].startswith("sha256=")
    assert len(headers["X-Toolkit-Signature"]) == len("sha256=") + 64

    # Cancelled jobs are reported too: one running, one still queued behind it.
    monkeypatch.setattr(webhooks, "urls", [receiver])
    ids = [client.post("/jobs", json={"job": "test-sleep", "params": {}}).json()["id"] for _ in range(2)]
    time.sleep(0.3)
    for id in ids:
        client.post(f"/jobs/{id}/cancel")
    wait_received(3)
    assert {b["data"]["status"] for _, b in Receiver.received[1:]} == {"cancelled"}

    describe = client.get("/jobs/webhooks").json()
    assert describe["urls"] == [receiver] and describe["secret_set"] is True and "job.finished" in describe["events"]


def test_webhook_test_reports_the_answer(client, receiver):
    r = client.post("/jobs/webhooks/test", json={"url": receiver})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "status": 200, "error": None}
    assert Receiver.received[0][1]["event"] == "test"
    Receiver.status = 500
    assert client.post("/jobs/webhooks/test", json={"url": receiver}).json()["ok"] is False
    assert client.post("/jobs/webhooks/test", json={"url": "http://127.0.0.1:9/nothing"}).json()["ok"] is False
    assert client.post("/jobs/webhooks/test", json={"url": "ftp://x"}).status_code == 400


def test_signature_is_hmac_of_the_body():
    from jobs.webhooks import Webhooks
    import base64, hashlib, hmac
    hooks = Webhooks(["http://x"], secret="k")
    body = hooks.body("test", {"a": 1})
    headers = hooks.headers("test", body, message_id="msg_1", sent_at=1700000000)
    assert headers["X-Toolkit-Signature"] == "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    # Standard Webhooks: v1,<base64 HMAC of "id.timestamp.body">, with the id and time in their own headers.
    assert headers["webhook-id"] == "msg_1" and headers["webhook-timestamp"] == "1700000000"
    signed = hmac.new(b"k", b"msg_1.1700000000." + body, hashlib.sha256).digest()
    assert headers["webhook-signature"] == "v1," + base64.b64encode(signed).decode()
    assert hooks.standard_secret == "whsec_" + base64.b64encode(b"k").decode()
    # Each message gets its own id; without a secret, no signature.
    plain = Webhooks(["http://x"])
    first, second = plain.headers("test", body), plain.headers("test", body)
    assert first["webhook-id"].startswith("msg_") and first["webhook-id"] != second["webhook-id"]
    assert "X-Toolkit-Signature" not in first and "webhook-signature" not in first and plain.standard_secret is None
    assert Webhooks(["http://x", " ", "http://y"]).urls == ["http://x", "http://y"]


def test_a_cron_run_more_than_an_hour_late_is_skipped(client):
    runner = client.app.state.jobs
    sched = client.post("/jobs/schedules", json={"job": "test-echo", "params": {"text": "late", "steps": 1},
                                                 "cron": "0 3 * * *"}).json()
    # Two hours late (the service was down): skipped, its next run the next 03:00.
    client.portal.call(runner.store.mark_schedule_run, sched["id"], 0, None, time.time() - 7200)
    assert client.portal.call(runner.scheduler.tick) == []
    row = client.get(f"/jobs/schedules/{sched['id']}").json()
    assert row["next_run"] > time.time() and row["last_job_id"] is None
    # Ten minutes late: made, once.
    client.portal.call(runner.store.mark_schedule_run, sched["id"], 0, None, time.time() - 600)
    submitted = client.portal.call(runner.scheduler.tick)
    assert [j["schedule_id"] for j in submitted] == [sched["id"]]
    wait(client, submitted[0]["id"])
    client.delete(f"/jobs/schedules/{sched['id']}")
