"""Schedules (cron and interval) and webhooks on finished jobs and runs."""

import asyncio
import json
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

import tests.fake_jobs  # noqa: F401  registers the test-* jobs
from api import app as app_module
from tasks import schedule
from tests.test_jobs import wait
from tests.waits import until


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

    assert sched["retries"] == 0 and sched["timeout_s"] is None

    # Run now: the job carries the schedule, its webhook URL and retries; the planned next run stays.
    client.put(f"/jobs/schedules/{sched['id']}", json={**{k: sched[k] for k in ("name", "job", "params", "cron", "webhook_url")},
                                                       "retries": 2, "retry_delay_s": 5, "timeout_s": 600})
    r = client.post(f"/jobs/schedules/{sched['id']}/run")
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["schedule_id"] == sched["id"] and job["webhook_url"] == "http://127.0.0.1:9/hook"
    assert (job["retries"], job["retry_delay_s"], job["timeout_s"]) == (2, 5, 600)
    wait(client, job["id"])
    again = client.get(f"/jobs/schedules/{sched['id']}").json()
    assert again["last_task_id"] == job["id"] and again["last_status"] == "succeeded"
    assert again["next_run"] == sched["next_run"]

    # Update: switch to an interval, disabled, which clears the next run.
    r = client.put(f"/jobs/schedules/{sched['id']}", json={"job": "test-echo", "params": {"text": "x"}, "interval_s": 3600, "enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["cron"] is None and r.json()["interval_s"] == 3600 and r.json()["next_run"] is None
    assert r.json()["name"] == "test-echo", "an empty name falls back to the job's"

    assert client.delete(f"/jobs/schedules/{sched['id']}").status_code == 204
    assert client.get(f"/jobs/schedules/{sched['id']}").status_code == 404
    assert client.get("/jobs/schedules").json()["schedules"] == []


def test_a_change_that_leaves_the_cadence_keeps_the_next_run(client):
    """Each save pushed the next run a whole period out: an hourly schedule saved more often never ran."""
    body = {"job": "test-echo", "params": {"text": "x"}, "interval_s": 3600}
    sched = client.post("/jobs/schedules", json=body).json()
    hooked = client.put(f"/jobs/schedules/{sched['id']}", json={**body, "webhook_url": "http://127.0.0.1:9/hook"})
    assert hooked.json()["next_run"] == sched["next_run"]
    # A new cadence counts from now; so does a schedule switched off and on again.
    assert client.put(f"/jobs/schedules/{sched['id']}", json={**body, "interval_s": 7200}).json()["next_run"] \
        == pytest.approx(time.time() + 7200, abs=5)
    assert client.put(f"/jobs/schedules/{sched['id']}", json={**body, "enabled": False}).json()["next_run"] is None
    assert client.put(f"/jobs/schedules/{sched['id']}", json=body).json()["next_run"] \
        == pytest.approx(time.time() + 3600, abs=5)
    client.delete(f"/jobs/schedules/{sched['id']}")


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
    client.portal.call(runner.tasks.store.mark_schedule_run, sched["id"], 0, None, time.time() - 1)
    submitted = client.portal.call(runner.scheduler.tick)
    assert [j["schedule_id"] for j in submitted] == [sched["id"]]
    row = client.get(f"/jobs/schedules/{sched['id']}").json()
    assert row["last_task_id"] == submitted[0]["id"] and row["next_run"] > time.time() + 50
    # Due again while that job is still going: skipped, next run moved on.
    client.portal.call(runner.tasks.store.mark_schedule_run, sched["id"], 0, None, time.time() - 1)
    client.portal.call(runner.tasks.store.claim, submitted[0]["id"])  # pretend it runs
    assert client.portal.call(runner.scheduler.tick) == []
    assert client.get(f"/jobs/schedules/{sched['id']}").json()["next_run"] > time.time() + 50
    client.portal.call(runner.tasks.store.finish, submitted[0]["id"], "succeeded")
    client.delete(f"/jobs/schedules/{sched['id']}")


def test_any_kind_that_declares_itself_is_scheduled_and_one_undeclared_is_left_alone(own_db):
    from core.errors import Refused
    from db.tables import TableStore
    from tasks.store import TaskStore

    store = TaskStore(own_db, TableStore(own_db))

    async def submit(name, params, **fields):
        return await store.insert(f"t{time.time_ns()}", name, params, schedule_id=fields["schedule_id"], kind="echo")

    def check(name, params):
        if not name.strip():
            raise ValueError("an echo needs something to say")
        return name.upper(), {**params, "checked": True}

    echo = schedule.Schedulable("echo", check=check, submit=submit)
    job = schedule.Schedulable("job", check=lambda name, params: (name, params), submit=submit)

    async def go():
        both = schedule.Scheduler(store)
        both.add(echo)
        both.add(job)
        said = await both.create(schedule.ScheduleSpec(job="hello", params={"n": 1}, interval_s=60), "echo")
        assert (said["kind"], said["job"], said["params"], said["name"]) == ("echo", "HELLO", {"n": 1, "checked": True}, "HELLO")
        for kind, runs, detail in (("nope", "x", "cannot be scheduled"), ("echo", " ", "something to say")):
            with pytest.raises(Refused, match=detail):
                await both.create(schedule.ScheduleSpec(job=runs, interval_s=60), kind)
        # A schedule made without a kind is a job's.
        jobs = await both.create(schedule.ScheduleSpec(job="nightly", interval_s=60))
        assert jobs["kind"] == "job"

        # A service that declares only echo: the job's schedule is neither submitted nor moved on.
        only_echo = schedule.Scheduler(store)
        only_echo.add(echo)
        later = time.time() + 120
        submitted = await only_echo.tick(now=later)
        assert [(t["kind"], t["job"], t["schedule_id"]) for t in submitted] == [("echo", "HELLO", said["id"])]
        assert (await store.schedule(jobs["id"]))["next_run"] == jobs["next_run"]
        with pytest.raises(Refused, match="cannot be scheduled"):
            await only_echo.run(jobs["id"])

    asyncio.run(go())


def test_interval_schedules_keep_their_pace(client):
    runner = client.app.state.jobs
    sched = client.post("/jobs/schedules", json={"job": "test-echo", "params": {"text": "pace", "steps": 1}, "interval_s": 600}).json()
    # The tick finds the run 12 s late (ticks come every POLL_S): the next run is still due + 600, not now + 600.
    due = time.time() - 12
    client.portal.call(runner.tasks.store.mark_schedule_run, sched["id"], 0, None, due)
    [job] = client.portal.call(runner.scheduler.tick)
    assert client.get(f"/jobs/schedules/{sched['id']}").json()["next_run"] == pytest.approx(due + 600, abs=0.01)
    wait(client, job["id"])
    client.delete(f"/jobs/schedules/{sched['id']}")
    # Runs missed while the service was down: one is made, and the next is the first on the grid still to come.
    assert schedule.following(None, 600, due=1000.0, now=2900.0) == 3400.0
    assert schedule.following(None, 600, due=1000.0, now=1000.0) == 1600.0
    # Cron goes by the clock, as before.
    assert schedule.following("*/15 * * * *", None, ts(2026, 10, 17, 10, 15), ts(2026, 10, 17, 10, 15, 12)) == ts(2026, 10, 17, 10, 30)


# ------------------------------------------------------------------ webhooks
class Receiver(BaseHTTPRequestHandler):
    received = []
    raw = []  # the bodies as sent, which the signatures cover
    status = 200
    statuses = []  # answered in turn before ``status``

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        Receiver.received.append((dict(self.headers), json.loads(body)))
        Receiver.raw.append(body)
        self.send_response(Receiver.statuses.pop(0) if Receiver.statuses else Receiver.status)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def receiver():
    server = HTTPServer(("127.0.0.1", 0), Receiver)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    Receiver.received = []
    Receiver.raw = []
    Receiver.status = 200
    Receiver.statuses = []
    yield f"http://127.0.0.1:{server.server_port}/hook"
    server.shutdown()


def wait_received(n, timeout=10.0):
    until(lambda: len(Receiver.received) >= n, timeout, lambda: f"received {Receiver.received}")


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

    describe = client.get("/queue/webhooks").json()
    assert describe["urls"] == [receiver] and describe["secret_set"] is True and "job.finished" in describe["events"]


def delivery(client, url, status):
    """The delivery to ``url`` once it is ``status``."""
    tasks = client.app.state.tasks
    return until(lambda: (task := client.portal.call(tasks.find, "webhook", url)) and task["status"] == status and task,
                 15, lambda: f"delivery {client.portal.call(tasks.find, 'webhook', url)}, received {Receiver.received}")


def test_a_failed_delivery_is_tried_again_with_its_id_and_a_fresh_signature(client, receiver, monkeypatch):
    import base64, hashlib, hmac
    import tasks.webhooks

    hooks = client.app.state.webhooks
    monkeypatch.setattr(hooks, "secret", "s3cret")
    monkeypatch.setattr(tasks.webhooks, "RETRY_DELAY_S", 1.0)
    # Down, then a redirect, which is not a delivery either (Standard Webhooks: only a 2xx is), then taken.
    Receiver.statuses = [503, 302]
    assert client.portal.call(hooks.send, "job.finished", {"id": "j1"}, [receiver]) == 1
    task = delivery(client, receiver, "succeeded")
    assert task["retried"] == 2 and task["result"] == {"status": 200} and len(Receiver.received) == 3

    # One message: the same id and body each time. Each try has its own time, signed with it, since a receiver
    # refuses a time far from its clock and later retries come hours after the first.
    ids = {headers["webhook-id"] for headers, _ in Receiver.received}
    times = [int(headers["webhook-timestamp"]) for headers, _ in Receiver.received]
    assert len(ids) == 1 and len(set(Receiver.raw)) == 1 and times == sorted(times) and times[-1] > times[0]
    for (headers, _), body in zip(Receiver.received, Receiver.raw):
        signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + body
        assert headers["webhook-signature"] == "v1," + base64.b64encode(hmac.new(b"s3cret", signed, hashlib.sha256).digest()).decode()


def test_a_delivery_answered_410_is_not_tried_again(client, receiver):
    Receiver.statuses = [410]
    client.portal.call(client.app.state.webhooks.send, "run.finished", {"id": "r1"}, [receiver])
    task = delivery(client, receiver, "failed")
    assert task["error"] == "HTTP 410" and task["retried"] == 0 and len(Receiver.received) == 1
    # The dashboard lists it among the latest deliveries.
    listed = {d["id"]: d for d in client.get("/queue/webhooks").json()["deliveries"]}
    assert {k: listed[task["id"]][k] for k in ("event", "about", "url", "status", "error", "retries")} == {
        "event": "run.finished", "about": "r1", "url": receiver, "status": "failed", "error": "HTTP 410", "retries": 10}


def test_a_delivery_outlives_a_restart(own_db, receiver):
    import asyncio
    from db.tables import TableStore
    from tasks.runner import TaskRunner
    from tasks.store import TaskStore
    from tasks.webhooks import Webhooks
    from tests.waits import until_async

    store = TaskStore(own_db, TableStore(own_db))

    async def scenario():
        # Two sent before the runner started: the service stopped first, and died while the second was going out.
        await Webhooks([receiver], tasks=TaskRunner(store)).send("job.finished", {"id": "j1"})
        await Webhooks([receiver], tasks=TaskRunner(store)).send("run.finished", {"id": "r1"})
        sent = await store.queued("webhook")
        assert await store.claim(sent[1]["id"])

        # The next start sends both.
        runner = TaskRunner(store)
        hooks = Webhooks([receiver], tasks=runner)
        await runner.start()
        try:
            async def delivered():
                rows = [await store.get(row["id"]) for row in sent]
                return all(row["status"] == "succeeded" for row in rows) and rows

            return await until_async(delivered, 15, lambda: f"received {Receiver.received}")
        finally:
            await runner.stop()
            await hooks.close()

    rows = asyncio.run(scenario())
    assert sorted(body["event"] for _, body in Receiver.received) == ["job.finished", "run.finished"]
    assert rows[0]["attempts"] == 0 and rows[1]["attempts"] == 1, "the one under way counts as resumed"


def test_finished_deliveries_are_deleted_after_their_days(own_db):
    import asyncio
    from db.tables import TableStore
    from tasks.runner import TaskRunner
    from tasks.store import TaskStore
    from tasks.webhooks import Webhooks

    store = TaskStore(own_db, TableStore(own_db))
    runner = TaskRunner(store)
    Webhooks(tasks=runner)

    async def scenario():
        delivered = await runner.submit("webhook", params={})
        waiting = await runner.submit("webhook", params={})
        job = await store.insert("j1", "test-echo", {})
        for row in (delivered, job):
            await store.claim(row["id"])
            await store.finish(row["id"], "succeeded")
        kept = await runner.prune()
        runner.kinds["webhook"].keep_s = 0
        pruned = await runner.prune()
        return kept, pruned, [await store.get(row["id"]) for row in (delivered, waiting, job)]

    kept, pruned, (delivered, waiting, job) = asyncio.run(scenario())
    assert (kept, pruned) == (0, 1), "kept KEEP_DAYS, then deleted"
    assert delivered is None and waiting["status"] == "queued" and job["status"] == "succeeded", "jobs are kept"


def test_webhook_test_reports_the_answer(client, receiver):
    r = client.post("/queue/webhooks/test", json={"url": receiver})
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "status": 200, "error": None}
    assert Receiver.received[0][1]["event"] == "test"
    Receiver.status = 500
    assert client.post("/queue/webhooks/test", json={"url": receiver}).json()["ok"] is False
    assert client.post("/queue/webhooks/test", json={"url": "http://127.0.0.1:9/nothing"}).json()["ok"] is False
    assert client.post("/queue/webhooks/test", json={"url": "ftp://x"}).status_code == 400


def test_signature_is_hmac_of_the_body(own_db):
    from db.tables import TableStore
    from tasks.runner import TaskRunner
    from tasks.store import TaskStore
    from tasks.webhooks import Webhooks
    import base64, hashlib, hmac
    tasks = TaskRunner(TaskStore(own_db, TableStore(own_db)))
    hooks = Webhooks(["http://x"], secret="k", tasks=tasks)
    body = hooks.body("test", {"a": 1})
    headers = hooks.headers("test", body, message_id="msg_1", sent_at=1700000000)
    assert headers["X-Toolkit-Signature"] == "sha256=" + hmac.new(b"k", body, hashlib.sha256).hexdigest()
    # Standard Webhooks: v1,<base64 HMAC of "id.timestamp.body">, with the id and time in their own headers.
    assert headers["webhook-id"] == "msg_1" and headers["webhook-timestamp"] == "1700000000"
    signed = hmac.new(b"k", b"msg_1.1700000000." + body, hashlib.sha256).digest()
    assert headers["webhook-signature"] == "v1," + base64.b64encode(signed).decode()
    assert hooks.standard_secret == "whsec_" + base64.b64encode(b"k").decode()
    # Each message gets its own id; without a secret, no signature.
    plain = Webhooks(["http://x"], tasks=tasks)
    first, second = plain.headers("test", body), plain.headers("test", body)
    assert first["webhook-id"].startswith("msg_") and first["webhook-id"] != second["webhook-id"]
    assert "X-Toolkit-Signature" not in first and "webhook-signature" not in first and plain.standard_secret is None
    assert Webhooks(["http://x", " ", "http://y"], tasks=tasks).urls == ["http://x", "http://y"]


def test_a_cron_run_more_than_an_hour_late_is_skipped(client):
    runner = client.app.state.jobs
    sched = client.post("/jobs/schedules", json={"job": "test-echo", "params": {"text": "late", "steps": 1},
                                                 "cron": "0 3 * * *"}).json()
    # Two hours late (the service was down): skipped, its next run the next 03:00.
    client.portal.call(runner.tasks.store.mark_schedule_run, sched["id"], 0, None, time.time() - 7200)
    assert client.portal.call(runner.scheduler.tick) == []
    row = client.get(f"/jobs/schedules/{sched['id']}").json()
    assert row["next_run"] > time.time() and row["last_task_id"] is None
    # Ten minutes late: made, once.
    client.portal.call(runner.tasks.store.mark_schedule_run, sched["id"], 0, None, time.time() - 600)
    submitted = client.portal.call(runner.scheduler.tick)
    assert [j["schedule_id"] for j in submitted] == [sched["id"]]
    wait(client, submitted[0]["id"])
    client.delete(f"/jobs/schedules/{sched['id']}")


def test_a_schedule_with_an_endless_timeout_or_interval_is_refused(client):
    """Infinity is no number of seconds: refused as the body is read (the 422 cannot echo it back, so a 400), rather
    than saved to fail at each run."""
    before = len(client.get("/jobs/schedules").json()["schedules"])
    for field in ("timeout_s", "interval_s"):
        body = '{"job": "test-echo", "params": {"text": "x"}, "interval_s": 600, "%s": Infinity}' % field
        r = client.post("/jobs/schedules", content=body.replace('"interval_s": 600, "interval_s"', '"interval_s"'),
                        headers={"Content-Type": "application/json"})
        assert r.status_code in (400, 422), (field, r.text)
    assert len(client.get("/jobs/schedules").json()["schedules"]) == before
