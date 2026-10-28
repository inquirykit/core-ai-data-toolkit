import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

pytest.importorskip("arcadedb_embedded")

from pydantic_ai.messages import ModelResponse, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402
from pydantic_ai.retries import RetryConfig  # noqa: E402
from tenacity import stop_after_attempt  # noqa: E402

from api import app as app_module  # noqa: E402
from config import Config  # noqa: E402
from evals import scoring  # noqa: E402
from evals.capture import compact, compacted, input_text, output_text, text_of  # noqa: E402
from evals.scoring import METRICS, score_items  # noqa: E402
from evals.store import bucket_for, expired_buckets, summarize_scores  # noqa: E402
from models.generic.transformers_model import TransformersModel  # noqa: E402
from tests.fakes import FakeModel, registered, swapped  # noqa: E402


def fake_judge(failing_on: str = ""):
    """A judge (a pydantic-ai FunctionModel) that scores every answer 0.75 and prefers the longer of two, and that
    fails on a prompt containing ``failing_on``."""

    def reply(messages, info):
        prompt = "".join(str(getattr(part, "content", "")) for m in messages for part in m.parts)
        if failing_on and failing_on in prompt:
            raise RuntimeError("the judge is down")
        tool = info.output_tools[0]
        if "<AnswerA>" in prompt:
            a = prompt.split("<AnswerA>")[1].split("</AnswerA>")[0]
            b = prompt.split("<AnswerB>")[1].split("</AnswerB>")[0]
            winner = "tie" if len(a) == len(b) else "A" if len(a) > len(b) else "B"
            return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"winner": winner, "reason": "longer is better"})])
        return ModelResponse(parts=[ToolCallPart(tool_name=tool.name, args={"reason": "fine", "pass": True, "score": 0.75})])

    return FunctionModel(reply)


class WordyFake(FakeModel):
    async def predict(self, request):
        return {"text": f"echo: {request.text}, and then some more words"}


class BrokenFake(FakeModel):
    async def predict(self, request):
        raise ValueError("the model refused this input")


@pytest.fixture(scope="module")
def client():
    # The JVM must start on the main thread (see Database.start_jvm).
    app_module.app.state.database.start_jvm()
    with swapped(TransformersModel, FakeModel), registered(WordyFake, task="text-generation", match="wordy:*"), \
            registered(BrokenFake, task="text-generation", match="broken:*"), TestClient(app_module.app) as c:
        yield c


def flush(client):
    client.app.state.evals.flush()


def infer(client, text, model_id="ev/model", **headers):
    return client.post("/inference", headers=headers, json={"model_id": model_id, "task": "text-generation", "input": {"text": text}})


def wait_for(fn, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError("Timed out")


# ------------------------------------------------------------- pure parts
def test_compact_cuts_long_strings_and_keeps_only_media_hashes():
    doc = compact({"text": "x" * 20, "image": "data:image/png;base64," + "A" * 600, "raw": b"\x00" * 10,
                   "items": list(range(300)), "n": 1.5}, max_chars=10)
    assert doc["text"] == "xxxxxxxxxx… [10 more chars]"
    assert doc["image"]["$media"]["kind"] == "image/png" and doc["image"]["$media"]["bytes"] == 600
    assert doc["raw"]["$media"] == {"kind": "bytes", "bytes": 10, "sha256": compact(b"\x00" * 10)["$media"]["sha256"]}
    assert len(doc["items"]) == 257 and doc["items"][-1] == "… 44 more items"
    assert doc["n"] == 1.5
    # Only a data URL is media: a long run of letters is text (a DNA sequence, a token), kept up to the budget.
    assert compact("ACGT" * 200, 5000) == "ACGT" * 200
    assert compact("word " * 200, 5000) == "word " * 200
    # What was cut is told apart from what was not.
    assert compacted(doc) and compacted({"a": [1, "… 3 more items"]}) and compacted("abc… [4 more chars]")
    assert not compacted({"text": "whole", "items": [1, 2, "… more"], "n": 1.5})


def test_text_helpers_pick_the_words_a_reader_wants():
    inp = {"messages": [{"role": "system", "content": "be brief"}, {"role": "user", "content": "hello there"}], "embedding": [0.1] * 5}
    assert input_text(inp) == "hello there"
    assert input_text({"text": "a prompt", "temperature": 0.7}) == "a prompt"
    assert "0.1" not in text_of(inp) and "be brief" in text_of(inp)
    assert text_of({"$media": {"kind": "bytes"}}) == ""
    assert output_text({"text": "the answer", "tool_calls": None}) == "the answer"
    assert output_text({"label": "POSITIVE"}) == "POSITIVE"
    assert output_text({"results": [1]}) == '{"results": [1]}'


def test_buckets_expire_by_whole_days():
    assert bucket_for(0) == "tk_trace_d19700101"
    day = 86400
    now = 20 * day + 3600
    names = ["tk_trace_d19700105", "tk_trace_d19700117", "tk_trace_d19700120", "tk_trace_0", "other"]
    assert expired_buckets(names, 3, now) == ["tk_trace_d19700105", "tk_trace_d19700117"]
    assert expired_buckets(names, 2.5, now) == ["tk_trace_d19700105", "tk_trace_d19700117"]
    assert expired_buckets(names, 30, now) == []


def test_metrics_score_items_and_summaries_count_them():
    items = [
        {"id": "i1", "example_id": "e1", "status": "ok", "output": {"text": "Paris."}, "reference": "paris", "input": {"text": "q"}},
        {"id": "i2", "example_id": "e2", "status": "ok", "output": {"text": '{"a": 1}'}, "reference": None, "input": {"text": "q"}},
        {"id": "i3", "example_id": "e3", "status": "error", "output": None, "reference": "x", "input": {"text": "q"}},
    ]
    score = lambda metric: asyncio.run(score_items(items, METRICS[metric][1]()))  # noqa: E731
    by_value = lambda rows: [r["value"] for r in sorted(rows, key=lambda r: r["item_id"])]  # noqa: E731
    assert by_value(score("exact_match")) == [1.0, None, None]
    assert by_value(score("contains")) == [1.0, None, None]
    assert by_value(score("json_valid")) == [0.0, 1.0, None]
    failed = [r for r in score("exact_match") if r["item_id"] == "i3"][0]
    assert failed["rationale"] == "This model failed on this example" and failed["example_id"] == "e3"
    assert "bleu" not in METRICS
    summary = summarize_scores([
        {"metric": "exact_match", "value": 1.0}, {"metric": "exact_match", "value": 0.0}, {"metric": "exact_match", "value": None},
        {"metric": "judge_vs_b", "value": 1.0, "label": "win", "against": "b"}, {"metric": "judge_vs_b", "value": 0.5, "label": "tie", "against": "b"},
    ])
    assert summary["exact_match"] == {"count": 3, "scored": 2, "mean": 0.5}
    assert summary["judge_vs_b"] == {"count": 2, "scored": 2, "mean": 0.75, "wins": 1, "ties": 1, "losses": 0}


# ------------------------------------------------------------------ traces
def test_requests_are_traced_with_their_input_and_output(client):
    r = infer(client, "What is the capital of France?", **{"X-Trace-Tag": "playground, demo", "X-Session-Id": "sess-1"})
    assert r.status_code == 200, r.text
    assert r.json()["trace_id"]
    flush(client)
    body = client.get("/admin/traces?q=capital").json()
    assert body["capture"]["enabled"] is True and body["capture"]["retention_days"] == Config.TRACE_RETENTION_DAYS
    found = [t for t in body["traces"] if "capital of France" in t["preview"]]
    assert found, body
    head = found[0]
    assert head["id"] == r.json()["trace_id"]
    assert head["model_id"] == "ev/model" and head["status"] == "ok" and head["tags"] == ["playground", "demo"]
    assert head["session"] == "sess-1" and "input" not in head
    trace = client.get(f"/admin/traces/{head['id']}").json()
    assert trace["input"]["text"] == "What is the capital of France?"
    assert trace["output"]["text"] == "echo: What is the capital of France?"
    assert trace["duration_ms"] >= 0 and trace["input_chars"] > 0
    assert client.get("/admin/traces/nope").status_code == 404


def test_traces_filter_by_words_model_status_tag_and_time(client):
    assert infer(client, "zebra crossing rules", model_id="ev/other").status_code == 200
    # A request the model rejects: traced with its error.
    assert infer(client, "please fail", model_id="broken:x").status_code == 400
    flush(client)
    traces = client.get("/admin/traces?q=zebra").json()["traces"]
    assert len(traces) == 1 and traces[0]["model_id"] == "ev/other"
    assert all(t["model_id"] == "ev/other" for t in client.get("/admin/traces?model=EV/OTHER").json()["traces"])
    failed = client.get("/admin/traces?status=client_error&model=broken").json()["traces"]
    assert failed and failed[0]["error"] == "ValueError: the model refused this input"
    assert client.get(f"/admin/traces/{failed[0]['id']}").json()["input"]["text"] == "please fail"
    assert client.get("/admin/traces?tag=demo").json()["traces"][0]["tags"] == ["playground", "demo"]
    assert client.get("/admin/traces?session=sess-1&task=text-generation").json()["traces"]
    assert client.get("/admin/traces?session=sess-1&task=embedding").json()["traces"] == []
    newest = client.get("/admin/traces?limit=1").json()["traces"][0]
    older = client.get(f"/admin/traces?before={newest['ts']}").json()["traces"]
    assert newest["id"] not in {t["id"] for t in older}
    assert client.get("/admin/traces?status=weird").status_code == 422


def test_monitoring_rows_link_to_traces(client):
    r = infer(client, "linked request")
    client.app.state.monitoring.flush()
    rows = client.get("/admin/requests?limit=5").json()["requests"]
    assert any(row["trace_id"] == r.json()["trace_id"] for row in rows)


def test_expired_day_buckets_are_dropped(client):
    store = client.app.state.evals
    old = {
        "id": "old-trace", "ts": time.time() - 40 * 86400, "model_id": "m", "task": "t", "status": "ok", "duration_ms": 1.0,
        "cold": False, "error": None, "tags": [], "session": None, "input": {"text": "x"}, "output": {"text": "y"},
        "text": "x y", "input_chars": 1, "output_chars": 1,
    }
    store._write([old])
    assert client.get("/admin/traces/old-trace").status_code == 200
    dropped = client.portal.call(store.prune)
    assert dropped == [bucket_for(old["ts"])]
    assert client.get("/admin/traces/old-trace").status_code == 404
    # Today's traces stay.
    assert client.get("/admin/traces?q=capital").json()["traces"]
    assert client.portal.call(store.prune) == []


# ---------------------------------------------------------------- datasets
def test_datasets_gather_examples_from_traces_and_files(client):
    r = client.post("/admin/datasets", json={"name": "capitals", "description": "geography"})
    assert r.status_code == 200, r.text
    dataset = r.json()
    assert client.post("/admin/datasets", json={"name": "capitals"}).status_code == 400
    trace = client.get("/admin/traces?q=capital").json()["traces"][0]
    long_text = "y" * (Config.TRACE_MAX_CHARS + 500)
    r = client.post(f"/admin/datasets/{dataset['id']}/examples", json={
        "examples": [{"input": {"text": "Capital of Spain?"}, "reference": "Madrid", "tags": ["easy"]},
                     {"input": {"text": long_text}, "reference": "whole"}],
        "trace_ids": [trace["id"]],
        "jsonl": '{"input": {"text": "Capital of Italy?"}, "reference": {"text": "Rome"}}\n\n',
    })
    assert r.status_code == 200, r.text
    assert r.json()["added"] == 4
    body = client.get(f"/admin/datasets/{dataset['id']}/examples").json()
    assert body["total"] == 4
    by_text = {e["input"]["text"]: e for e in body["examples"]}
    assert by_text["Capital of Spain?"]["reference"] == "Madrid" and by_text["Capital of Spain?"]["tags"] == ["easy"]
    assert by_text["Capital of Italy?"]["reference"] == {"text": "Rome"}
    # An example is kept as given, however long: a run sends it to the model whole.
    assert by_text[long_text]["reference"] == "whole"
    copied = by_text["What is the capital of France?"]
    assert copied["source_trace_id"] == trace["id"] and copied["source_model_id"] == "ev/model"
    assert copied["source_output"]["text"].startswith("echo:")
    assert client.get("/admin/datasets").json()["datasets"][0]["examples"] == 4
    # A trace whose input was cut when recorded is not copied: the example would not be what the client sent.
    infer(client, long_text, model_id="long/model")
    flush(client)
    cut = client.get("/admin/traces?model=long").json()["traces"][0]
    r = client.post(f"/admin/datasets/{dataset['id']}/examples", json={"trace_ids": [cut["id"]]})
    assert r.status_code == 400 and "cut" in r.json()["detail"]
    assert client.delete(f"/admin/datasets/{dataset['id']}/examples/{by_text[long_text]['id']}").status_code == 200

    export = client.get(f"/admin/datasets/{dataset['id']}/export")
    assert export.status_code == 200 and export.headers["content-disposition"].endswith('capitals.jsonl"')
    lines = [json.loads(l) for l in export.text.splitlines()]
    assert len(lines) == 3 and all("input" in l for l in lines)
    other = client.post("/admin/datasets", json={"name": "copy"}).json()
    assert client.post(f"/admin/datasets/{other['id']}/examples", json={"jsonl": export.text}).json()["added"] == 3
    assert client.post(f"/admin/datasets/{other['id']}/examples", json={"jsonl": "not json"}).status_code == 400
    assert client.post(f"/admin/datasets/{other['id']}/examples", json={}).status_code == 400
    assert client.post(f"/admin/datasets/{other['id']}/examples", json={"trace_ids": ["nope"]}).status_code == 404

    example_id = by_text["Capital of Spain?"]["id"]
    assert client.delete(f"/admin/datasets/{dataset['id']}/examples/{example_id}").status_code == 200
    assert client.get(f"/admin/datasets/{dataset['id']}/examples").json()["total"] == 2
    assert client.delete(f"/admin/datasets/{other['id']}").status_code == 200
    assert client.get(f"/admin/datasets/{other['id']}").status_code == 404


# -------------------------------------------------------------------- runs
@pytest.fixture(scope="module")
def dataset(client):
    d = client.post("/admin/datasets", json={"name": "bench"}).json()
    client.post(f"/admin/datasets/{d['id']}/examples", json={"examples": [
        {"input": {"text": "one"}, "reference": "echo: one"},
        {"input": {"text": "two"}, "reference": "echo: TWO"},
        {"input": {"text": "three"}, "reference": "something else"},
    ]})
    return d


def finished(client, run_id):
    return wait_for(lambda: (lambda r: r if r["status"] in ("done", "failed", "cancelled") else None)(client.get(f"/admin/runs/{run_id}").json()))


def test_runs_execute_score_judge_and_compare(client, dataset, monkeypatch):
    # The judge is a pydantic-ai model; here a function, in place of the endpoint. No waiting between its retries.
    monkeypatch.setattr(scoring, "judge_model", lambda judge, local=None: fake_judge(failing_on="three"))
    monkeypatch.setattr(scoring, "JUDGE_RETRY", RetryConfig(stop=stop_after_attempt(2), reraise=True))
    before = client.get("/admin/traces?model=wordy").json()["traces"]
    a = client.post("/admin/runs", json={"dataset_id": dataset["id"], "model_id": "ev/model", "task": "text-generation", "name": "plain"})
    assert a.status_code == 200, a.text
    b = client.post("/admin/runs", json={"dataset_id": dataset["id"], "model_id": "wordy:big", "params": {"temperature": 0.5}})
    assert b.status_code == 200, b.text
    run_a, run_b = finished(client, a.json()["id"]), finished(client, b.json()["id"])
    assert (run_a["status"], run_a["total"], run_a["done"], run_a["failed"]) == ("done", 3, 3, 0)
    assert run_b["status"] == "done" and run_b["name"] == "wordy:big" and run_b["failed"] == 0
    outputs = {i["input"]["text"]: i["output"]["text"] for i in run_a["items"]}
    assert outputs == {"one": "echo: one", "two": "echo: two", "three": "echo: three"}
    assert run_a["items"][0]["reference"] == "echo: one" and run_a["items"][0]["duration_ms"] >= 0
    # The runner's own requests leave no traces.
    flush(client)
    assert client.get("/admin/traces?model=wordy").json()["traces"] == before

    r = client.post(f"/admin/runs/{run_a['id']}/score", json={"metrics": ["exact_match", "contains", "json_valid"]})
    assert r.status_code == 200, r.text
    summary = r.json()["summary"]
    assert summary["exact_match"] == {"count": 3, "scored": 3, "mean": pytest.approx(2 / 3)}
    assert summary["contains"]["mean"] == pytest.approx(2 / 3)
    assert summary["json_valid"]["mean"] == 0.0
    assert client.post(f"/admin/runs/{run_a['id']}/score", json={"metrics": ["bleu"]}).status_code == 422
    # Scoring again replaces, not duplicates.
    client.post(f"/admin/runs/{run_a['id']}/score", json={"metrics": ["exact_match"]})
    assert client.get(f"/admin/runs/{run_a['id']}").json()["summary"]["exact_match"]["count"] == 3

    assert client.post(f"/admin/runs/{run_a['id']}/judge", json={}).status_code == 400  # no JUDGE_MODEL
    # A judge call that keeps failing is that item's failure, not the run's: the others are scored.
    r = client.post(f"/admin/runs/{run_a['id']}/judge", json={"model": "judge:v1", "base_url": ""})
    assert r.status_code == 202, r.text
    judged = wait_for(lambda: (lambda s: s if "judge" in s else None)(client.get(f"/admin/runs/{run_a['id']}").json()["summary"]))
    assert judged["judge"] == {"count": 3, "scored": 2, "mean": 0.75}
    detail = client.get(f"/admin/runs/{run_a['id']}").json()
    assert detail["activity"] is None
    scores = {i["input"]["text"]: s for i in detail["items"] for s in i["scores"] if s["metric"] == "judge"}
    assert scores["one"]["label"] == "pass" and scores["one"]["rationale"] == "fine" and scores["one"]["judge"] == "judge:v1"
    assert scores["three"]["value"] is None and scores["three"]["rationale"].startswith("The judge failed: RuntimeError")
    # With the judge back, judging again replaces the scores.
    monkeypatch.setattr(scoring, "judge_model", lambda judge, local=None: fake_judge())
    client.post(f"/admin/runs/{run_a['id']}/judge", json={"model": "judge:v1", "base_url": ""})
    wait_for(lambda: client.get(f"/admin/runs/{run_a['id']}").json()["summary"]["judge"]["scored"] == 3)

    r = client.post(f"/admin/runs/{run_a['id']}/judge", json={"model": "judge:v1", "base_url": "", "against": run_b["id"]})
    assert r.status_code == 202, r.text
    metric = f"judge_vs_{run_b['id']}"
    pairwise = wait_for(lambda: client.get(f"/admin/runs/{run_a['id']}").json()["summary"].get(metric))
    # The wordy model's answers are longer, which this judge prefers, in either order.
    assert (pairwise["wins"], pairwise["ties"], pairwise["losses"]) == (0, 0, 3) and pairwise["mean"] == 0.0

    compare = client.get(f"/admin/runs/compare/{run_a['id']}/{run_b['id']}").json()
    assert len(compare["rows"]) == 3
    assert compare["rows"][0]["a"]["output"]["text"] == "echo: one" and compare["rows"][0]["b"]["output"]["text"].startswith("echo: one, and")
    assert compare["a"]["summary"]["exact_match"]["mean"] == pytest.approx(2 / 3)
    assert client.get(f"/admin/datasets/{dataset['id']}").json()["run_list"][0]["id"] == run_b["id"]


def test_runs_cancel_delete_and_validate(client, dataset):
    r = client.post("/admin/runs", json={"dataset_id": dataset["id"], "model_id": "ev/model"})
    run_id = r.json()["id"]
    client.post(f"/admin/runs/{run_id}/cancel")
    assert finished(client, run_id)["status"] in ("cancelled", "done")
    assert client.delete(f"/admin/runs/{run_id}").status_code == 200
    assert client.get(f"/admin/runs/{run_id}").status_code == 404
    assert client.post("/admin/runs", json={"dataset_id": "nope", "model_id": "ev/model"}).status_code == 404
    assert client.post(f"/admin/runs/{run_id}/judge", json={"model": "judge:v1"}).status_code == 404
    other = client.post("/admin/datasets", json={"name": "elsewhere"}).json()
    a = client.get("/admin/runs").json()["runs"][0]["id"]
    far = client.post("/admin/runs", json={"dataset_id": other["id"], "model_id": "ev/model"}).json()
    finished(client, far["id"])
    assert client.get(f"/admin/runs/compare/{a}/{far['id']}").status_code == 400
    metrics = client.get("/admin/evals/metrics").json()
    assert {m["name"] for m in metrics["metrics"]} == {"exact_match", "contains", "json_valid"}
    assert metrics["judge"]["model"] == "" and "rubric" in metrics["judge"]
    # Only a run that stopped short resumes.
    assert client.post(f"/admin/runs/{far['id']}/resume").status_code == 400
    # Deleting a dataset takes its runs with it.
    assert client.delete(f"/admin/datasets/{other['id']}").status_code == 200
    assert client.get(f"/admin/runs/{far['id']}").status_code == 404


def test_eval_routes_need_the_admin_key(client, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    for path in ("/admin/traces", "/admin/datasets", "/admin/runs", "/admin/evals/metrics"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-API-Key": "client-key"}).status_code == 401
        assert client.get(path, headers={"X-API-Key": "admin-key"}).status_code == 200


def task_runner(arcade):
    """The task queue runs wait in (jobs/tasks.py), on a database of its own."""
    from jobs.store import JobStore
    from jobs.tasks import TaskRunner
    from vectorstore.tables import TableStore

    return TaskRunner(JobStore(arcade, TableStore(arcade)))


def test_a_run_cancelled_while_queued_is_reported_once(arcade):
    from evals.runner import RunExecutor

    class Store:
        def __init__(self):
            self.rows = {}

        async def runs(self):
            return list(self.rows.values())

        async def run(self, run_id):
            return dict(self.rows[run_id])

        async def update_run(self, run_id, **fields):
            self.rows[run_id].update(fields)

    class Hooks:
        sent = []

        def send(self, event, data, extra_urls=()):
            self.sent.append((event, data["status"]))

    async def scenario():
        hooks = Hooks()
        store = Store()
        tasks = task_runner(arcade)
        runner = RunExecutor(store, manager=None, tasks=tasks, webhooks=hooks)
        await runner.start()
        store.rows["r1"] = {"id": "r1", "status": "queued"}  # as POST /admin/runs creates it
        await runner.submit("r1")  # the task runner is not started, so it stays queued
        await runner.cancel("r1")
        return hooks.sent, store.rows["r1"]["status"], await tasks.find("run", "r1")

    sent, status, task = asyncio.run(scenario())
    assert sent == [("run.finished", "cancelled")]
    assert status == "cancelled" and task["status"] == "cancelled"


def test_a_resumed_run_skips_the_examples_it_answered(arcade):
    from evals.runner import RunExecutor

    examples = [{"id": f"e{n}", "input": {"text": f"q{n}"}, "task": None} for n in (1, 2, 3)]

    class Store:
        def __init__(self):
            # As POST /admin/runs/{id}/resume leaves an interrupted run: queued, one example answered already.
            self.rows = {"r1": {"id": "r1", "status": "queued", "dataset_id": "d", "model_id": "m", "failed": 0, "started": 5.0}}
            self.items = []

        async def runs(self):
            return list(self.rows.values())

        async def run(self, run_id):
            return dict(self.rows[run_id])

        async def update_run(self, run_id, **fields):
            self.rows[run_id].update(fields)

        async def dataset(self, dataset_id):
            return {"examples": 3}

        async def answered(self, run_id):
            return {"e1"}

        async def examples(self, dataset_id, limit, skip):
            return examples if skip == 0 else []

        async def add_items(self, run_id, items):
            self.items.extend((item["example_id"], item["status"]) for item in items)

    class Manager:
        def parallelism(self, model_id, task):
            return 32

        async def run_inference(self, model_id, task, request):
            return {"result": {"text": "ok"}}

    async def scenario():
        store = Store()
        tasks = task_runner(arcade)
        runner = RunExecutor(store, manager=Manager(), tasks=tasks)
        await runner.start()
        store.rows["r1"]["status"] = "queued"  # start() marks a queued run with no task interrupted; resume re-queues
        await runner.submit("r1")
        await tasks.start()
        for _ in range(100):
            if store.rows["r1"]["status"] == "done":
                break
            await asyncio.sleep(0.02)
        await tasks.stop()
        return store

    store = asyncio.run(scenario())
    assert store.items == [("e2", "ok"), ("e3", "ok")]
    assert store.rows["r1"]["status"] == "done" and store.rows["r1"]["done"] == 3 and store.rows["r1"]["started"] == 5.0




def test_a_judge_keeps_its_api_key_in_memory_only(arcade, monkeypatch):
    from evals.runner import RunExecutor
    from evals.scoring import Judge

    seen = []

    async def judging(self, task, judge, local):
        seen.append((task["ref"], judge.api_key))

    monkeypatch.setattr(RunExecutor, "_judge", judging)

    async def scenario():
        tasks = task_runner(arcade)
        runner = RunExecutor(store=None, manager=None, tasks=tasks)
        await runner.judge("r1", Judge(model="judge:v1", base_url="https://judge.test/v1", api_key="sk-secret"))
        queued = await tasks.find("judge", "r1")
        await tasks.start()
        await until_status(tasks, queued["id"], "succeeded")
        # Queued again, then the service restarts before it runs: the key is gone with the old process.
        await runner.judge("r1", Judge(model="judge:v1", base_url="https://judge.test/v1", api_key="sk-secret"))
        await tasks.stop()
        again = task_runner(arcade)
        RunExecutor(store=None, manager=None, tasks=again)
        lost = await again.find("judge", "r1")
        await again.start()
        await until_status(again, lost["id"], "failed")
        await again.stop()
        # The dashboard learns why from the judge's task, the process that failed it gone or not.
        activity = await RunExecutor(store=None, manager=None, tasks=task_runner(arcade)).activity(
            [{"id": "r1", "status": "done", "done": 3, "total": 3}])
        return queued, await again.get(lost["id"]), activity

    queued, lost, activity = asyncio.run(scenario())
    assert seen == [("r1", "sk-secret")]
    assert "sk-secret" not in json.dumps(queued) and queued["model_id"] is None
    assert "judge the run again" in lost["error"]
    assert activity["r1"]["kind"] == "judging" and "judge the run again" in activity["r1"]["error"]


def test_cancelling_a_judge_stops_its_scoring(arcade, monkeypatch):
    from evals.runner import RunExecutor
    from evals.scoring import Judge

    async def judging(self, task, judge, local):
        await asyncio.sleep(30)

    monkeypatch.setattr(RunExecutor, "_judge", judging)

    async def scenario():
        tasks = task_runner(arcade)
        runner = RunExecutor(store=None, manager=None, tasks=tasks)
        runner.local_client = lambda priority: None
        await tasks.start()
        await runner.judge("r1", Judge(model="local/judge"), priority="low")
        task = await tasks.find("judge", "r1")
        await until_status(tasks, task["id"], "running")
        run = {"id": "r1", "status": "done", "done": 3, "total": 3}
        judging = await runner.activity([run])
        started = time.monotonic()
        await runner.cancel("r1")
        took = time.monotonic() - started
        await tasks.stop()
        return await tasks.get(task["id"]), took, judging, await runner.activity([run])

    task, took, judging, activity = asyncio.run(scenario())
    assert task["status"] == "cancelled" and task["priority"] == "low" and task["model_id"] == "local/judge"
    assert judging == {"r1": {"kind": "judging", "done": 0, "total": 3}}
    assert took < 5 and "r1" not in activity


async def until_status(tasks, id, status, timeout=10.0):
    deadline = time.monotonic() + timeout
    while (await tasks.get(id))["status"] != status:
        assert time.monotonic() < deadline, f"task {id} is still {(await tasks.get(id))['status']}"
        await asyncio.sleep(0.02)


def test_a_run_the_service_stopped_under_goes_on_by_itself(arcade):
    from evals.runner import RunExecutor

    examples = [{"id": f"e{n}", "input": {"text": f"q{n}"}, "task": None} for n in (1, 2, 3)]

    class Store:
        def __init__(self):
            self.rows = {"r1": {"id": "r1", "status": "queued", "dataset_id": "d", "model_id": "m", "failed": 0}}
            self.items = []

        async def runs(self):
            return list(self.rows.values())

        async def run(self, run_id):
            return dict(self.rows[run_id])

        async def update_run(self, run_id, **fields):
            self.rows[run_id].update(fields)

        async def dataset(self, dataset_id):
            return {"examples": 3}

        async def answered(self, run_id):
            return {example_id for example_id, _ in self.items}

        async def examples(self, dataset_id, limit, skip):
            return examples if skip == 0 else []

        async def add_items(self, run_id, items):
            self.items.extend((item["example_id"], item["status"]) for item in items)

    class Manager:
        def parallelism(self, model_id, task):
            return 32

        def __init__(self, stall_on=None):
            self.stall_on = stall_on
            self.answered = []

        async def run_inference(self, model_id, task, request):
            if request["text"] == self.stall_on:
                await asyncio.sleep(30)
            self.answered.append(request["text"])
            return {"result": {"text": "ok"}}

    async def scenario():
        store = Store()
        # The first service answers e1 and e3 (a page goes at once), then stops while it works on e2.
        tasks = task_runner(arcade)
        first = Manager(stall_on="q2")
        runner = RunExecutor(store, manager=first, tasks=tasks)
        await runner.start()
        store.rows["r1"]["status"] = "queued"  # as POST /admin/runs leaves it, once the service is up
        await runner.submit("r1")
        await tasks.start()
        for _ in range(200):
            if len(first.answered) == 2:
                break
            await asyncio.sleep(0.02)
        await tasks.stop()
        stopped, items = dict(store.rows["r1"]), list(store.items)
        # The next one goes on from there.
        tasks = task_runner(arcade)
        second = Manager()
        runner = RunExecutor(store, manager=second, tasks=tasks)
        await runner.start()
        await tasks.start()
        for _ in range(200):
            if store.rows["r1"]["status"] == "done":
                break
            await asyncio.sleep(0.02)
        await tasks.stop()
        return stopped, items, second.answered, store

    stopped, items, asked_again, store = asyncio.run(scenario())
    # The answers in when it stopped were kept; only the one in flight was asked again.
    assert stopped["status"] == "queued" and items == [("e1", "ok"), ("e3", "ok")]
    assert asked_again == ["q2"]
    assert store.rows["r1"]["status"] == "done" and store.rows["r1"]["done"] == 3


class PageStore:
    """An eval store with one queued run over ``n`` examples, for the run executor alone."""

    def __init__(self, n):
        self.dataset_examples = [{"id": f"e{i}", "input": {"text": f"q{i}"}, "task": None} for i in range(n)]
        self.rows = {}
        self.items = []

    async def runs(self):
        return list(self.rows.values())

    async def run(self, run_id):
        return dict(self.rows[run_id])

    async def update_run(self, run_id, **fields):
        self.rows[run_id].update(fields)

    async def dataset(self, dataset_id):
        return {"examples": len(self.dataset_examples)}

    async def answered(self, run_id):
        return {example_id for example_id, _ in self.items}

    async def examples(self, dataset_id, limit, skip):
        return self.dataset_examples[skip:skip + limit]

    async def add_items(self, run_id, items):
        self.items.extend((item["example_id"], item["status"]) for item in items)


async def run_to_the_end(runner, tasks, store, run_id="r1", until=("done", "cancelled", "failed")):
    await runner.start()
    store.rows[run_id] = {"id": run_id, "status": "queued", "dataset_id": "d", "model_id": "m", "task": "t", "failed": 0}
    await runner.submit(run_id)
    await tasks.start()
    for _ in range(500):
        if store.rows[run_id]["status"] in until:
            break
        await asyncio.sleep(0.02)
    await tasks.stop()


def test_a_run_sends_its_examples_a_page_at_a_time_so_they_share_forward_passes(arcade):
    from evals.runner import RunExecutor
    from models.manager import ModelManager
    from tests.test_batching import BatchingModel

    async def scenario():
        store = PageStore(64)
        tasks = task_runner(arcade)
        runner = RunExecutor(store, manager=ModelManager(device="cpu", batch_wait_s=0.005), tasks=tasks)
        await run_to_the_end(runner, tasks, store)
        return store

    BatchingModel.batch_calls.clear()
    with registered(BatchingModel, task="t", fallback=True):
        store = asyncio.run(scenario())
    assert store.rows["r1"]["status"] == "done" and store.rows["r1"]["done"] == 64
    # In the dataset's order, each with its own answer; one at a time would have been 64 passes.
    assert store.items == [(f"e{i}", "ok") for i in range(64)]
    assert BatchingModel.batch_calls["requests"] == 64 and BatchingModel.batch_calls["calls"] <= 4


def test_cancelling_a_run_drops_the_requests_of_its_page_still_waiting(arcade):
    from evals.runner import RunExecutor

    class Manager:
        def parallelism(self, model_id, task):
            return 32

        async def run_inference(self, model_id, task, request):
            if request["text"] != "q0":
                await asyncio.sleep(30)
            return {"result": {"text": "ok"}}

    async def scenario():
        store = PageStore(5)
        tasks = task_runner(arcade)
        runner = RunExecutor(store, manager=Manager(), tasks=tasks)
        await runner.start()
        store.rows["r1"] = {"id": "r1", "status": "queued", "dataset_id": "d", "model_id": "m", "failed": 0}
        await runner.submit("r1")
        await tasks.start()
        for _ in range(250):
            if store.rows["r1"]["status"] != "queued":
                break
            await asyncio.sleep(0.02)
        assert store.rows["r1"]["status"] == "running"
        await asyncio.sleep(0.05)  # q0 is answered; the rest wait
        started = time.monotonic()
        await runner.cancel("r1")
        took = time.monotonic() - started
        await tasks.stop()
        return store, took

    store, took = asyncio.run(scenario())
    assert store.rows["r1"]["status"] == "cancelled" and took < 5
    assert store.items == [("e0", "ok")]


def test_a_model_that_answers_one_at_a_time_gets_the_run_one_at_a_time(arcade):
    """So a live request for it waits behind one of the run's requests, not a page of them."""
    from evals.runner import RunExecutor
    from models.manager import ModelManager

    waiting = []

    class OneAtATime(FakeModel):
        predict_delay = 0.005

        async def predict(self, request):
            waiting.append(manager.cache.entries["m:t"].inflight)
            return await super().predict(request)

    manager = ModelManager(device="cpu", batch_wait_s=0.005)

    async def scenario():
        store = PageStore(40)
        tasks = task_runner(arcade)
        runner = RunExecutor(store, manager=manager, tasks=tasks)
        await run_to_the_end(runner, tasks, store)
        return store

    with registered(OneAtATime, task="t", fallback=True):
        store = asyncio.run(scenario())
    assert store.rows["r1"]["status"] == "done" and len(store.items) == 40
    assert max(waiting) == 1


def test_a_run_the_service_has_no_room_for_stops_keeping_its_answers_and_resumes(arcade):
    """No room for the model is the service's, not the example's: no error item that a resume would skip."""
    from evals.runner import RunExecutor
    from models.cache import CapacityError

    class Manager:
        busy = True

        def parallelism(self, model_id, task):
            return 32

        async def run_inference(self, model_id, task, request):
            if self.busy and request["text"] == "q2":
                raise CapacityError("No room for another model (max_models) and every loaded model is busy")
            return {"result": {"text": "ok"}}

    async def scenario():
        store = PageStore(5)
        manager = Manager()
        tasks = task_runner(arcade)
        await run_to_the_end(RunExecutor(store, manager=manager, tasks=tasks), tasks, store)
        stopped = dict(store.rows["r1"]), list(store.items)
        # Resumed (run_to_the_end queues it again): the answers kept are skipped, the refused one is asked again.
        manager.busy = False
        tasks = task_runner(arcade)
        await run_to_the_end(RunExecutor(store, manager=manager, tasks=tasks), tasks, store)
        return stopped, store

    (stopped, items), store = asyncio.run(scenario())
    assert stopped["status"] == "failed" and "resume the run" in stopped["error"]
    assert sorted(items) == [("e0", "ok"), ("e1", "ok"), ("e3", "ok"), ("e4", "ok")]
    assert store.rows["r1"]["status"] == "done" and ("e2", "ok") in store.items and len(store.items) == 5


def test_a_page_widens_once_an_answer_has_loaded_the_model(arcade):
    """The first example fails before the model loads (a bad input): the second loads it, and the rest go together."""
    from evals.runner import RunExecutor

    class Manager:
        loaded = False
        running = most = 0

        def parallelism(self, model_id, task):
            return 8 if self.loaded else None

        async def run_inference(self, model_id, task, request):
            if request["text"] == "q0":
                raise ValueError("a bad input, refused before anything loads")
            self.loaded = True
            self.running += 1
            self.most = max(self.most, self.running)
            await asyncio.sleep(0.01)
            self.running -= 1
            return {"result": {"text": "ok"}}

    manager = Manager()

    async def scenario():
        store = PageStore(20)
        tasks = task_runner(arcade)
        await run_to_the_end(RunExecutor(store, manager=manager, tasks=tasks), tasks, store)
        return store

    store = asyncio.run(scenario())
    assert store.rows["r1"]["status"] == "done" and store.items[0] == ("e0", "error") and len(store.items) == 20
    assert manager.most > 1
