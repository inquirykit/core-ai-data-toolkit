"""/queue (api/queue_routes.py): the tasks of every kind on the one queue, in one list, and their cancel."""

import asyncio

import pytest
from fastapi.testclient import TestClient

import api.app as app_module
from tasks.runner import Kind, TaskCancelled


@pytest.fixture
def client():
    with TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def held(client):
    """Two kinds of the test's own on the app's queue: "q-held" runs until it is stopped, "q-done" ends at once."""
    tasks = client.app.state.tasks
    release = {}

    async def hold(task):
        release[task["id"]] = asyncio.Event()
        await release[task["id"]].wait()
        raise TaskCancelled()

    async def stop(task):
        release[task["id"]].set()

    async def done(task):
        return {"ref": task["ref"]}

    tasks.add(Kind("q-held", run=hold, stop=stop))
    tasks.add(Kind("q-done", run=done, stop=stop))
    yield tasks
    # A new dict, not a change to the one the dispatcher may be going through.
    tasks.kinds = {name: kind for name, kind in tasks.kinds.items() if name not in ("q-held", "q-done")}


def test_the_queue_lists_every_kind_newest_first_and_pages(client, held):
    ids = [client.portal.call(lambda k=k, n=n: held.submit(k, ref=f"q-{n}"))["id"]
           for n, k in enumerate(["q-done", "q-done", "q-done"])]
    listed = client.get("/queue", params={"kind": "q-done"}).json()["tasks"]
    assert [t["id"] for t in listed] == ids[::-1]
    assert {t["kind"] for t in listed} == {"q-done"} and listed[0]["ref"] == "q-2"
    first = client.get("/queue", params={"kind": "q-done", "limit": 2}).json()
    assert [t["id"] for t in first["tasks"]] == ids[:0:-1] and first["next"]
    rest = client.get("/queue", params={"kind": "q-done", "limit": 2, "cursor": first["next"]}).json()
    assert [t["id"] for t in rest["tasks"]] == ids[:1] and "next" not in rest
    # Every kind at once, and the one record whatever the kind.
    held_task = client.portal.call(lambda: held.submit("q-held", ref="q-held-list"))
    every = client.get("/queue", params={"limit": 500}).json()["tasks"]
    assert {"q-done", "q-held"} <= {t["kind"] for t in every}
    assert all(set(t) == set(every[0]) for t in every)
    assert client.get("/queue", params={"ref": "nothing-works-on-this"}).json() == {"tasks": []}
    # A page goes on where the last stopped, however many came since; the work not done yet is asked for by status.
    first = client.get("/queue", params={"kind": "q-done", "limit": 2}).json()
    client.portal.call(lambda: held.submit("q-done", ref="q-new"))
    rest = client.get("/queue", params={"kind": "q-done", "limit": 2, "cursor": first["next"]}).json()["tasks"]
    assert [t["id"] for t in rest] == ids[:1]
    active = client.get("/queue", params=[("status", "queued"), ("status", "running")]).json()["tasks"]
    assert held_task["id"] in {t["id"] for t in active} and all(t["status"] in ("queued", "running") for t in active)
    assert client.get("/queue", params={"cursor": "eyJzIjogOTIyMzM3MjAzNjg1NDc3NTgwOH0"}).status_code == 400
    assert client.post(f"/queue/{held_task['id']}/cancel").json()["status"] == "cancelled"


def test_a_task_of_any_kind_is_cancelled_through_the_queue(client, held):
    task = client.portal.call(lambda: held.submit("q-held", ref="q-held-1"))
    from tests.waits import until

    until(lambda: client.get("/queue", params={"ref": "q-held-1"}).json()["tasks"][0]["status"] == "running", 20,
          "the task never started")
    cancelled = client.post(f"/queue/{task['id']}/cancel").json()
    assert cancelled["status"] == "cancelled" and cancelled["kind"] == "q-held"
    assert client.post("/queue/nope/cancel").status_code == 404
