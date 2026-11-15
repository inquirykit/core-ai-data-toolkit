"""Workflows (workflows/): specs checked on save, dry runs, runs a page at a time, error outputs, triggers."""

import asyncio
import threading
import time

import pytest
from fastapi.testclient import TestClient

from access import ClientKeys, Grants
from api import app as app_module
from core.config import Config
from tests.fakes import FakeModel, registered
from tests.waits import until

TICKETS = [
    {"id": "1", "body": "great service", "domain": "a.example"},
    {"id": "2", "body": "bad", "domain": "b.example"},
    {"id": "3", "body": "fine thanks", "domain": "c.example"},
    {"id": "4", "body": "spam offer", "domain": "d.example"},
    {"id": "5", "body": "lovely", "domain": "e.example"},
]


class Classifier(FakeModel):
    """Labels spam as SPAM, the rest as POSITIVE; a text that is only "bad" it cannot do."""

    async def predict(self, request):
        self.calls["predict"] += 1
        if request.text == "bad":
            raise RuntimeError("cannot read this one")
        label = "SPAM" if "spam" in request.text else "POSITIVE"
        return {"label": label, "confidence": 0.9, "scores": {label: 0.9}}


@pytest.fixture
def client():
    with registered(Classifier, task="text-classification", fallback=True), TestClient(app_module.app) as c:
        yield c


@pytest.fixture
def tickets(client):
    """A workspace with a tickets table, made afresh."""
    client.delete("/db/wf?confirm=wf")
    assert client.post("/db", json={"name": "wf"}).status_code == 201
    for row in TICKETS:
        assert client.put(f"/db/wf/tickets/{row['id']}", json=row).status_code == 201
    yield "wf"
    client.delete("/db/wf?confirm=wf")


def spec(nodes, edges, **more):
    return {"name": more.pop("name", "Tag tickets"), "nodes": nodes, "edges": edges, **more}


def read(**more):
    return {"id": "A", "type": "table.read", "settings": {"workspace": "wf", "table": "tickets", **more}}


CLASSIFY = {"id": "B", "type": "task", "settings": {"task": "text-classification", "model": "test/classifier",
                                                    "inputs": {"text": "body"}}}


def write(id, table, **more):
    return {"id": id, "type": "table.write", "settings": {"workspace": "wf", "table": table, **more}}


def edge(source, target, output="out"):
    return {"source": source, "output": output, "target": target}


# ------------------------------------------------------------------- the spec
def test_the_palette_lists_every_node_type_with_its_settings_schema(client):
    nodes = {n["type"]: n for n in client.get("/workflows/nodes").json()["nodes"]}
    assert set(nodes) == {"table.read", "task", "enrich", "if", "switch", "set", "merge", "sql",
                          "table.write"}
    assert nodes["if"]["outputs"] == ["true", "false"] and nodes["switch"]["outputs"] is None
    assert nodes["table.read"]["source"] and "workspace" in nodes["table.read"]["settings"]["properties"]


@pytest.mark.parametrize("nodes, edges, detail", [
    ([read(), CLASSIFY], [edge("A", "B"), edge("B", "A")], "no edge goes into it"),
    ([read(), {**read(), "id": "Z"}], [], "one table"),
    ([read(), CLASSIFY], [edge("A", "B", "nope")], "no output 'nope'"),
    ([read(), CLASSIFY, write("C", "out")], [edge("A", "B")], "Nothing leads to 'C'"),
    ([read(), {"id": "B", "type": "if", "settings": {"condition": "body =="}}], [edge("A", "B")], "not a JMESPath"),
    ([read(), {"id": "B", "type": "set", "settings": {"fields": {"x": "body"}}},
      {"id": "C", "type": "set", "settings": {"fields": {"y": "body"}}}],
     [edge("A", "B"), edge("B", "C"), edge("C", "B")], "circle"),
    ([read(), {**CLASSIFY, "settings": {**CLASSIFY["settings"], "task": "no-such-task"}}], [edge("A", "B")], "no task"),
    ([read(), {**CLASSIFY, "settings": {**CLASSIFY["settings"], "inputs": {"txt": "body"}}}], [edge("A", "B")],
     "takes no txt"),
    ([read(), {"id": "B", "type": "if", "settings": {}}], [edge("A", "B")], "condition"),
    ([read(only_new=True), write("K", "tickets")], [edge("A", "K")], "read back the rows the last one wrote"),
    ([read(), {"id": "Q", "type": "sql", "settings": {"workspace": "wf", "query": "DELETE FROM tickets"}}],
     [edge("A", "Q")], "Node 'Q': The SQL is a DELETE"),
    ([read(), {**CLASSIFY, "settings": {**CLASSIFY["settings"], "task": "question-answering",
                                        "inputs": {"question": "body"}}}], [edge("A", "B")], "needs context"),
    # A result read where it is not there: off another way, or a field the task does not give.
    ([read(), CLASSIFY, {"id": "I", "type": "if", "settings": {"condition": "B.label == 'SPAM'"}}],
     [edge("A", "B"), edge("A", "I")], "result of 'B', which is on no way to 'I'"),
    ([read(), CLASSIFY, {"id": "I", "type": "if", "settings": {"condition": "B.lable == 'SPAM' || body"}}],
     [edge("A", "B"), edge("B", "I")], "reads B.lable, but 'B' gives label, confidence"),
])
def test_a_spec_that_cannot_run_is_refused_saying_why(client, tickets, nodes, edges, detail):
    r = client.post("/workflows", json=spec(nodes, edges))
    assert r.status_code == 400, r.text
    assert detail in r.json()["detail"]


def test_a_model_outside_the_allow_list_and_a_missing_workspace_are_refused(client, tickets, monkeypatch):
    monkeypatch.setattr(Config, "ALLOWED_MODELS", ["allowed/model"])
    for path in ("/workflows", "/workflows/dry-run"):
        body = spec([read(), CLASSIFY], [edge("A", "B")])
        r = client.post(path, json=body if path == "/workflows" else {"spec": body})
        assert r.status_code == 403 and "ALLOWED_MODELS" in r.json()["detail"]
    monkeypatch.setattr(Config, "ALLOWED_MODELS", [])
    missing = {**read(), "settings": {"workspace": "nowhere", "table": "t"}}
    assert client.post("/workflows", json=spec([missing], [])).status_code == 404


# ------------------------------------------------------------------ dry runs
def test_a_dry_run_shows_rows_along_each_edge_and_writes_nothing(client, tickets):
    body = spec([read(), CLASSIFY,
                 {"id": "S", "type": "switch", "settings": {"cases": ["B.label == 'SPAM'"]}},
                 write("T", "trash"), write("K", "kept", fields={"text": "body", "label": "B.label"}),
                 write("F", "failed")],
                [edge("A", "B"), edge("B", "S"), edge("B", "F", "error"), edge("S", "T", "0"), edge("S", "K", "default")])
    r = client.post("/workflows/dry-run", json={"spec": body, "limit": 10})
    assert r.status_code == 200, r.text
    out = r.json()
    along = {(e["source"], e["output"], e["target"]): e["rows"] for e in out["edges"]}
    assert out["rows"] == 5
    assert along == {("A", "out", "B"): 5, ("B", "out", "S"): 4, ("B", "error", "F"): 1, ("S", "0", "T"): 1,
                     ("S", "default", "K"): 3}
    assert out["nodes"]["B"]["failed"] == 1 and "cannot read this one" in out["nodes"]["B"]["error"]
    assert out["nodes"]["K"]["written"] == 3
    # The rows as they left the classifier carry its result under its id.
    assert out["nodes"]["B"]["samples"][0]["B"]["label"] == "POSITIVE"
    # Nothing was written.
    assert client.get("/db/wf/kept").status_code == 404 or client.get("/db/wf/kept").json()["rows"] == []


# ---------------------------------------------------------------------- runs
def finished(client, run_id):
    return until(lambda: (r := client.get(f"/workflows/runs/{run_id}").json())["status"] in
                 ("succeeded", "failed", "cancelled") and r, what=f"run {run_id} to finish")


def test_a_run_goes_a_page_at_a_time_writes_by_id_and_failed_rows_take_their_way(client, tickets):
    body = spec([read(), CLASSIFY, write("K", "tagged", fields={"text": "body", "label": "B.label"}),
                 write("F", "failed", fields={"why": "_error.message"})],
                [edge("A", "B"), edge("B", "K"), edge("B", "F", "error")], page_size=2)
    made = client.post("/workflows", json=body)
    assert made.status_code == 201, made.text
    workflow = made.json()
    run = client.post(f"/workflows/{workflow['id']}/run", json={}).json()
    done = finished(client, run["id"])
    assert done["status"] == "succeeded", done
    assert done["rows"] == 5 and done["pages"] == 3
    along = {(e["source"], e["output"], e["target"]): e["rows"] for e in done["edges"]}
    assert along[("B", "out", "K")] == 4 and along[("B", "error", "F")] == 1
    tagged = {r["id"]: r for r in client.get("/db/wf/tagged").json()["rows"]}
    assert tagged["4"]["label"] == "SPAM"
    assert tagged["1"] == {"id": "1", "text": "great service", "label": "POSITIVE", "_run": run["id"]}
    assert "cannot read this one" in client.get("/db/wf/failed/2").json()["why"]
    # The run is the workflow's, newest first, and so are its requests in the request log.
    assert [r["id"] for r in client.get(f"/workflows/{workflow['id']}/runs").json()["runs"]] == [run["id"]]
    client.app.state.monitoring.flush()
    assert "workflow" in {r["source"] for r in client.get("/admin/requests?limit=20").json()["requests"]}


def test_a_row_a_node_cannot_do_with_no_error_edge_fails_the_run_at_its_last_page(client, tickets):
    body = spec([read(), CLASSIFY, write("K", "tagged2")], [edge("A", "B"), edge("B", "K")], page_size=1)
    workflow = client.post("/workflows", json=body).json()
    done = finished(client, client.post(f"/workflows/{workflow['id']}/run", json={}).json()["id"])
    assert done["status"] == "failed" and "Node 'B'" in done["error"] and "cannot read this one" in done["error"]
    # The first page was written before the second failed.
    assert [r["id"] for r in client.get("/db/wf/tagged2").json()["rows"]] == ["1"]


def test_a_run_stamps_the_rows_it_writes_and_keeps_its_spec_and_model_revisions(client, tickets, monkeypatch):
    from models import hub
    commit = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setattr(hub, "commit_of", lambda repo_id, revision=None: commit)
    client.delete("/models/clear")  # loaded afresh, so its revision is read
    body = spec([read(), CLASSIFY, write("K", "stamped", fields={"label": "B.label"}),
                 write("U", "unstamped", fields={"label": "B.label"}, stamp=False), write("F", "failed3")],
                [edge("A", "B"), edge("B", "K"), edge("B", "U"), edge("B", "F", "error")])
    workflow = client.post("/workflows", json=body).json()
    run = finished(client, client.post(f"/workflows/{workflow['id']}/run", json={}).json()["id"])
    assert client.get("/db/wf/stamped/1").json() == {"id": "1", "label": "POSITIVE", "_run": run["id"]}
    assert client.get("/db/wf/unstamped/1").json() == {"id": "1", "label": "POSITIVE"}
    # The run a row names says what made it: the spec as it ran, and the commit of the model's weights.
    assert run["spec"]["nodes"][1]["settings"]["model"] == "test/classifier"
    assert run["nodes"]["B"]["revisions"] == [commit]
    # A dry run writes nothing, so stamps nothing; it says the revisions too.
    out = client.post("/workflows/dry-run", json={"spec": body}).json()
    assert out["nodes"]["B"]["revisions"] == [commit] and "_run" not in out["nodes"]["B"]["samples"][0]
    client.delete("/models/clear")


def test_a_write_into_its_own_table_keeps_the_fields_it_does_not_set_and_the_vector(client, tickets):
    """A Table write merges into the row of its id: a run that tags its rows in place keeps the rest of each, the
    vector among them, which a run reads the rows without."""
    made = client.post("/db/wf", json={"name": "points", "fields": [
        {"name": "label", "type": "string"}, {"name": "v", "type": "vector", "dimensions": 3}]})
    assert made.status_code == 201, made.text
    assert client.put("/db/wf/points/a", json={"label": "x", "v": [1, 0, 0]}).status_code == 201
    body = spec([{"id": "A", "type": "table.read", "settings": {"workspace": "wf", "table": "points"}},
                 {"id": "S", "type": "set", "settings": {"fields": {"tag": "'seen'"}}},
                 write("K", "points", fields={"tag": "tag"}), write("W", "points")],
                [edge("A", "S"), edge("S", "K"), edge("S", "W")])
    workflow = client.post("/workflows", json=body).json()
    run = finished(client, client.post(f"/workflows/{workflow['id']}/run", json={}).json()["id"])
    assert run["status"] == "succeeded", run
    row = client.get("/db/wf/points/a").json()
    assert row["label"] == "x" and row["tag"] == "seen" and row["v"] == pytest.approx([1, 0, 0])


def test_only_new_reads_what_came_since_the_last_run_and_reset_reads_all_again(client, tickets):
    body = spec([read(only_new=True), {"id": "S", "type": "set", "settings": {"fields": {"seen": "`true`"}}},
                 write("K", "seen")], [edge("A", "S"), edge("S", "K")])
    workflow = client.post("/workflows", json=body).json()
    start = lambda: finished(client, client.post(f"/workflows/{workflow['id']}/run", json={}).json()["id"])
    assert start()["rows"] == 5
    assert client.put("/db/wf/tickets/6", json={"id": "6", "body": "new one"}).status_code == 201
    assert start()["rows"] == 1
    assert start()["rows"] == 0
    assert client.post(f"/workflows/{workflow['id']}/reset").json()["last_read"] == 0
    assert start()["rows"] == 6


async def _slowly(self, request):
    """A classifier's answer, a tenth of a second late: a run of the five tickets a page at a time takes half a second."""
    await asyncio.sleep(0.1)
    return {"label": "POSITIVE", "confidence": 0.9, "scores": {"POSITIVE": 0.9}}


def test_a_run_waiting_takes_the_workflow_as_saved_when_it_starts(client, tickets, monkeypatch):
    """Asking for a run while one waits gives that one: it runs what is saved by the time it starts, not what was
    saved when it was first asked for."""
    monkeypatch.setattr(Classifier, "predict", _slowly)
    v1 = spec([read(), CLASSIFY, write("K", "v1")], [edge("A", "B"), edge("B", "K")], page_size=1)
    workflow = client.post("/workflows", json=v1).json()
    going = client.post(f"/workflows/{workflow['id']}/run", json={}).json()
    until(lambda: client.get(f"/workflows/runs/{going['id']}").json()["status"] == "running")
    waiting = client.post(f"/workflows/{workflow['id']}/run", json={}).json()
    assert waiting["status"] == "queued"
    v2 = {**v1, "nodes": [read(), CLASSIFY, write("K", "v2")]}
    assert client.put(f"/workflows/{workflow['id']}", json=v2).status_code == 200
    assert client.post(f"/workflows/{workflow['id']}/run", json={}).json()["id"] == waiting["id"]
    done = finished(client, waiting["id"])
    assert done["status"] == "succeeded" and done["spec"]["nodes"][2]["settings"]["table"] == "v2", done
    assert len(client.get("/db/wf/v2").json()["rows"]) == len(TICKETS)
    assert finished(client, going["id"])["spec"]["nodes"][2]["settings"]["table"] == "v1"


def test_deleting_a_workflow_cancels_its_run_waiting_and_its_run_going(client, tickets, monkeypatch):
    """One run may wait while one runs: both go with the workflow, and their tasks (which hold what they stored)."""
    monkeypatch.setattr(Classifier, "predict", _slowly)
    workflow = client.post("/workflows", json=spec([read(), CLASSIFY, write("K", "slow")], [edge("A", "B"),
                                                                                          edge("B", "K")],
                                                   page_size=1)).json()
    going = client.post(f"/workflows/{workflow['id']}/run", json={}).json()
    until(lambda: client.get(f"/workflows/runs/{going['id']}").json()["status"] == "running")
    waiting = client.post(f"/workflows/{workflow['id']}/run", json={}).json()
    assert waiting["id"] != going["id"] and waiting["status"] == "queued"
    assert client.delete(f"/workflows/{workflow['id']}").status_code == 204
    # The running one has ended (a cancel waits for it), and the runs went with the workflow.
    assert client.app.state.tasks.running_on("workflow", workflow["id"]) is None
    assert [client.get(f"/workflows/runs/{run['id']}").status_code for run in (going, waiting)] == [404, 404]
    assert client.portal.call(client.app.state.tasks.lookup, going["id"]) is None


def test_a_finished_run_is_kept_workflow_run_days(client, tickets):
    """Each run is a task, which holds its progress, a trigger every minute half a million a year: they go once
    older than WORKFLOW_RUN_DAYS, in the task runner's hourly prune."""
    workflow = client.post("/workflows", json=spec([read(), write("K", "kept")], [edge("A", "K")])).json()
    run = finished(client, client.post(f"/workflows/{workflow['id']}/run", json={}).json()["id"])
    assert run["rows"] > 0
    keep_s = Config.WORKFLOW_RUN_DAYS * 86400
    assert client.app.state.tasks.kinds["workflow"].keep_s == keep_s
    store = client.app.state.tasks.store
    assert client.portal.call(store.prune, "workflow", time.time() - keep_s) == 0
    assert client.portal.call(store.prune, "workflow", time.time() + 60) >= 1
    assert client.get(f"/workflows/runs/{run['id']}").status_code == 404


def test_a_workflow_changed_to_read_another_table_reads_it_all(client, tickets):
    """How far runs read is a write number of the table they read: of no meaning to another one."""
    for i in range(3):
        assert client.put(f"/db/wf/other/{i}", json={"body": f"other {i}"}).status_code == 201
    assert client.put("/db/wf/tickets/9", json={"body": "the newest"}).status_code == 201
    body = spec([read(only_new=True), write("K", "seen")], [edge("A", "K")])
    workflow = client.post("/workflows", json=body).json()
    assert finished(client, client.post(f"/workflows/{workflow['id']}/run", json={}).json()["id"])["rows"] == 6
    other = {**body, "nodes": [read(only_new=True, table="other"), body["nodes"][1]]}
    assert client.put(f"/workflows/{workflow['id']}", json=other).json()["last_read"] == 0
    assert finished(client, client.post(f"/workflows/{workflow['id']}/run", json={}).json()["id"])["rows"] == 3


def test_only_new_waits_for_a_write_under_way_so_its_rows_are_not_passed(client, tickets):
    """A write takes its rows' numbers as it starts, and may commit after a later write: a run that read up to the
    later one would pass its rows for good. A run reads how far to go once no write to its table is under way."""
    tables = app_module.app.state.tables
    started, release = threading.Event(), threading.Event()

    def slow_write():
        with tables._writing("wf", ["tickets"]) as db:
            tables._put(db, "wf", "tickets", [{"id": "late", "body": "late"}])
            started.set()
            release.wait(5)

    workflow = client.post("/workflows", json=spec([read(only_new=True), write("K", "seen")], [edge("A", "K")])).json()
    writer = threading.Thread(target=slow_write)
    writer.start()
    assert started.wait(5)
    assert client.put("/db/wf/tickets/early", json={"body": "early"}).status_code == 201
    threading.Timer(0.5, release.set).start()
    run = finished(client, client.post(f"/workflows/{workflow['id']}/run", json={}).json()["id"])
    writer.join(5)
    assert run["status"] == "succeeded" and run["rows"] == len(TICKETS) + 2, run
    assert "late" in {r["id"] for r in client.get("/db/wf/seen?_limit=100").json()["rows"]}


def test_a_lookup_that_could_not_be_made_is_the_rows_error_not_an_empty_answer(client, tickets, monkeypatch):
    # By domain: what each enricher answered for it.
    answers = {
        "a.example": [{"enricher": "dns", "status": "ok", "outputs": {"ip": "1.2.3.4"}}],
        "b.example": [{"enricher": "dns", "status": "not_found", "outputs": {}}],
        "c.example": [{"enricher": "dns", "status": "ok", "outputs": {"ip": "5.6.7.8"}},
                      {"enricher": "vt", "status": "rate_limited", "error": "2/min"}],
        "d.example": [{"enricher": "vt", "status": "no_key", "error": "VT_KEY is not set"}],
        "e.example": [{"enricher": "vt", "status": "disabled"}],
    }

    async def enrich(value, names=None):
        results = answers[value]
        merged = {k: v for r in results if r["status"] == "ok" for k, v in r["outputs"].items()}
        return {"value": value, "results": results, "merged": merged}

    monkeypatch.setattr(client.app.state.workflows, "enrich", enrich)
    body = spec([read(), {"id": "E", "type": "enrich", "settings": {"value": "domain"}}, write("K", "found"),
                 write("F", "failed")], [edge("A", "E"), edge("E", "K"), edge("E", "F", "error")])
    out = client.post("/workflows/dry-run", json={"spec": body}).json()
    # Found, and found nothing, go on; a rate limit, even beside an answer, a missing key, and no enricher asked fail.
    assert out["nodes"]["E"]["rows_out"] == 2 and out["nodes"]["E"]["failed"] == 3
    assert "vt: 2/min" in out["nodes"]["E"]["error"]


def test_branches_of_one_row_are_merged_by_row(client, tickets):
    body = spec([read(), {"id": "L", "type": "set", "settings": {"fields": {"short": "length(body) < `6`"}}},
                 {"id": "R", "type": "set", "settings": {"fields": {"loud": "contains(body, 'great')"}}},
                 {"id": "M", "type": "merge"}, write("K", "both")],
                [edge("A", "L"), edge("A", "R"), edge("L", "M"), edge("R", "M"), edge("M", "K")])
    out = client.post("/workflows/dry-run", json={"spec": body}).json()
    assert out["nodes"]["M"]["rows_in"] == 10 and out["nodes"]["M"]["rows_out"] == 5
    merged = out["nodes"]["M"]["samples"][0]
    # Each way's own field, on its own copy of the row: both, once merged.
    assert merged["short"] is False and merged["loud"] is True


# ----------------------------------------------------------------------- SQL
def test_a_sql_node_joins_the_rows_with_a_table_filters_and_reshapes_them(client, tickets):
    for domain, owner in (("a.example", "ann"), ("d.example", "dan")):
        assert client.put(f"/db/wf/owners/{domain}", json={"id": domain, "owner": owner}).status_code == 201
    # A field the table does not declare is in its _rest; a result of a node before is JSON.
    query = ("SELECT r.id, r._row, r.B->>'label' AS label, o._rest->>'owner' AS owner FROM rows r "
             "JOIN owners o ON o.id = r.domain ORDER BY r.id")
    body = spec([read(), CLASSIFY, {"id": "Q", "type": "sql", "settings": {"workspace": "wf", "query": query}},
                 write("K", "owned")], [edge("A", "B"), edge("B", "Q"), edge("Q", "K")])
    out = client.post("/workflows/dry-run", json={"spec": body}).json()
    # The classifier's rows (but the one it failed), the two with an owner left, as the SQL made them.
    assert out["nodes"]["Q"]["rows_in"] == 4 and out["nodes"]["Q"]["rows_out"] == 2
    assert [{k: r[k] for k in ("id", "label", "owner")} for r in out["nodes"]["Q"]["samples"]] == [
        {"id": "1", "label": "POSITIVE", "owner": "ann"}, {"id": "4", "label": "SPAM", "owner": "dan"}]
    workflow = client.post("/workflows", json={**body, "edges": [*body["edges"], edge("B", "F", "error")],
                                               "nodes": [*body["nodes"], write("F", "unowned_failed")]}).json()
    run = finished(client, client.post(f"/workflows/{workflow['id']}/run", json={}).json()["id"])
    assert run["status"] == "succeeded"
    assert client.get("/db/wf/owned/4").json() == {"id": "4", "label": "SPAM", "owner": "dan", "_run": run["id"]}
    # A count is a row the SQL made: it comes back from an invoke after the rows given.
    count = {"id": "C", "type": "sql", "settings": {"workspace": "wf", "query": "SELECT count(*) AS n FROM rows"}}
    counted = client.post("/workflows", json=spec([read(), count], [edge("A", "C")], name="Count",
                                                  invokable=True)).json()
    rows = client.post(f"/workflows/{counted['id']}/invoke", json={"rows": [{"x": 1}, {"x": 2}]}).json()["rows"]
    assert rows == [{"x": 1}, {"x": 2}, {"n": 2}]


def test_a_sql_that_fails_says_which_node(client, tickets):
    bad = {"id": "Q", "type": "sql", "settings": {"workspace": "wf", "query": "SELECT nope FROM rows"}}
    r = client.post("/workflows/dry-run", json={"spec": spec([read(), bad], [edge("A", "Q")])})
    assert r.status_code == 400 and r.json()["detail"].startswith("Node 'Q': ") and "nope" in r.json()["detail"]


# ------------------------------------------------------------------- invoke
def test_an_invoke_answers_each_row_with_its_results_and_writes_nothing(client, tickets, monkeypatch):
    body = spec([read(), CLASSIFY, {"id": "I", "type": "if", "settings": {"condition": "B.label == 'SPAM'"}},
                 {"id": "S", "type": "set", "settings": {"fields": {"flag": "`true`"}}}, write("K", "invoked")],
                [edge("A", "B"), edge("B", "I"), edge("I", "S", "true"), edge("S", "K"), edge("I", "K", "false")],
                invokable=True)
    workflow = client.post("/workflows", json=body).json()
    rows = [{"body": "spam offer"}, {"body": "bad"}, {"body": "lovely", "id": "x"}]
    r = client.post(f"/workflows/{workflow['id']}/invoke", json={"rows": rows})
    assert r.status_code == 200, r.text
    out = r.json()
    # In the order given, each with what it got on its way; a row a node failed says so, and the rest go on.
    spam, bad, lovely = out["rows"]
    assert spam["B"]["label"] == "SPAM" and spam["flag"] is True
    # An error nobody expected is named by its type to a client, as any answer of the service is: its text, which
    # may hold a path or a key, stays in the log. DEBUG gives the text, as it does for any answer.
    assert bad["_error"]["message"] == "The node could not do this row (RuntimeError)" and "B" not in bad
    assert lovely == {"body": "lovely", "id": "x", "B": lovely["B"]} and lovely["B"]["label"] == "POSITIVE"
    assert out["nodes"]["K"]["written"] == 1 and out["nodes"]["K"]["failed"] == 1  # the row with no id
    assert client.get("/db/wf/invoked").status_code == 404 or client.get("/db/wf/invoked").json()["rows"] == []
    assert client.post("/workflows/0000000000/invoke", json={"rows": rows}).status_code == 404
    monkeypatch.setattr(client.app.state.workflows, "debug", True)
    debugged = client.post(f"/workflows/{workflow['id']}/invoke", json={"rows": rows}).json()["rows"][1]
    assert "cannot read this one" in debugged["_error"]["message"]


def test_a_key_held_to_other_models_invokes_a_workflow_with_the_admins(client, tickets, own_db, monkeypatch):
    """A key's models (access/grants.py) hold its own requests; an invokable workflow's nodes run the models the admin
    chose for it."""
    body = spec([read(), CLASSIFY], [edge("A", "B")], invokable=True)
    workflow = client.post("/workflows", json=body).json()
    keys = ClientKeys(own_db)
    monkeypatch.setattr(client.app.state, "client_keys", keys)
    _, token = keys._make("worker", grants=Grants(models=["someone/else"]))
    r = client.post(f"/workflows/{workflow['id']}/invoke", json={"rows": [{"body": "lovely"}]},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["rows"][0]["B"]["label"] == "POSITIVE"


def test_only_a_workflow_made_invokable_takes_a_client_key(client, tickets, monkeypatch):
    """Its SQL nodes read as the admin: a workflow over the service's own tables, which /db opens to the admin key
    only, would hand them to any client. So an invoke takes only a workflow the admin opened to it."""
    secret = {"id": "Q", "type": "sql", "settings": {"workspace": "evals", "query": "SELECT * FROM datasets"}}
    assert client.post("/admin/datasets", json={"name": "private"}).status_code == 200
    body = spec([read(), secret], [edge("A", "Q")])
    closed = client.post("/workflows", json=body).json()
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    as_client, as_admin = {"Authorization": "Bearer client-key"}, {"Authorization": "Bearer admin-key"}
    assert client.get("/db/evals/datasets", headers=as_client).status_code == 401
    r = client.post(f"/workflows/{closed['id']}/invoke", json={"rows": [{"x": 1}]}, headers=as_client)
    assert r.status_code == 403 and "not invokable" in r.json()["detail"] and "private" not in r.text
    # The admin's choice: once made invokable, it is a call as any other.
    opened = client.post("/workflows", json={**body, "invokable": True}, headers=as_admin).json()
    r = client.post(f"/workflows/{opened['id']}/invoke", json={"rows": [{"x": 1}]}, headers=as_client)
    assert r.status_code == 200 and "private" in r.text


# ------------------------------------------------------------------ triggers
@pytest.mark.parametrize("trigger, detail", [
    ({"cron": "99 * * * *"}, "outside 0-59"), ({"interval_s": 5}, "at least 60"),
    ({"cron": "0 0 30 2 *"}, "never matches"), ({}, "not both")])
def test_a_trigger_its_schedule_would_refuse_is_refused_before_anything_is_saved(client, tickets, trigger, detail):
    name = f"Trigger {detail}"
    r = client.post("/workflows", json=spec([read(), write("K", "x")], [edge("A", "K")], name=name, trigger=trigger))
    assert r.status_code == 400 and detail in r.json()["detail"], r.text
    assert name not in {w["name"] for w in client.get("/workflows").json()["workflows"]}
    hourly = spec([read(), write("K", "x")], [edge("A", "K")], name=f"{name}, hourly", trigger={"interval_s": 3600})
    made = client.post("/workflows", json=hourly).json()
    r = client.put(f"/workflows/{made['id']}", json={**hourly, "trigger": trigger})
    assert r.status_code == 400 and detail in r.json()["detail"], r.text
    assert client.get(f"/workflows/{made['id']}").json()["trigger"] == {"cron": None, "interval_s": 3600}
    client.delete(f"/workflows/{made['id']}")


def test_saving_a_workflow_leaves_its_next_run_where_it_was(client, tickets):
    body = spec([read(), write("K", "x")], [edge("A", "K")], trigger={"interval_s": 3600})
    made = client.post("/workflows", json=body).json()
    scheduler = client.app.state.workflows.scheduler
    due = client.portal.call(scheduler.get, made["schedule_id"])["next_run"]
    moved = {**body, "nodes": [{**body["nodes"][0], "ui": {"x": 40, "y": 40}}, body["nodes"][1]]}
    assert client.put(f"/workflows/{made['id']}", json=moved).status_code == 200
    assert client.portal.call(scheduler.get, made["schedule_id"])["next_run"] == due
    client.delete(f"/workflows/{made['id']}")


def test_a_trigger_is_a_schedule_of_its_own_kind_kept_with_the_workflow(client, tickets):
    body = spec([read(), write("K", "copied")], [edge("A", "K")], trigger={"interval_s": 3600})
    workflow = client.post("/workflows", json=body).json()
    schedule_id = workflow["schedule_id"]
    scheduler = client.app.state.workflows.scheduler
    row = client.portal.call(scheduler.get, schedule_id)
    assert row["kind"] == "workflow" and row["job"] == workflow["id"]
    # Not a job's: the Jobs section neither lists it nor edits it.
    assert schedule_id not in [s["id"] for s in client.get("/jobs/schedules").json()["schedules"]]
    assert client.get(f"/jobs/schedules/{schedule_id}").status_code == 404
    # Due: the tick starts a run.
    client.portal.call(client.app.state.tasks.store.mark_schedule_run, schedule_id, 0, None, 1.0)
    [task] = client.portal.call(scheduler.tick)
    assert task["kind"] == "workflow" and task["ref"] == workflow["id"]
    assert finished(client, task["id"])["status"] == "succeeded"
    # Without a trigger, its schedule goes; and so does the workflow, with a 404 after.
    updated = client.put(f"/workflows/{workflow['id']}", json={**body, "trigger": None}).json()
    assert updated["schedule_id"] is None
    with pytest.raises(Exception):
        client.portal.call(scheduler.get, schedule_id)
    assert client.delete(f"/workflows/{workflow['id']}").status_code == 204
    assert client.get(f"/workflows/{workflow['id']}").status_code == 404


def test_workflows_page_by_name_with_a_cursor(client, tickets):
    made = [client.post("/workflows", json={**spec([read(), write("K", "paged")], [edge("A", "K")]),
                                            "name": f"zz paged {i}"}).json() for i in range(3)]
    names = []
    cursor = None
    while True:
        page = client.get("/workflows", params={"limit": 2, **({"cursor": cursor} if cursor else {})}).json()
        names += [w["name"] for w in page["workflows"]]
        cursor = page.get("next")
        if not cursor:
            break
    assert [n for n in names if n.startswith("zz paged")] == ["zz paged 0", "zz paged 1", "zz paged 2"]
    for w in made:
        assert client.delete(f"/workflows/{w['id']}").status_code == 204


def test_a_trigger_too_long_for_a_schedule_is_refused_before_anything_is_saved(client, tickets):
    """A cron the schedule could not hold: refused as the body is read (422), never saved without its schedule."""
    name = "Trigger too long"
    every_minute = ",".join(str(m) for m in range(60)) + " * * * *"
    r = client.post("/workflows", json=spec([read(), write("K", "x")], [edge("A", "K")], name=name,
                                            trigger={"cron": every_minute}))
    assert r.status_code == 422 and len(every_minute) > 120
    assert name not in {w["name"] for w in client.get("/workflows").json()["workflows"]}
