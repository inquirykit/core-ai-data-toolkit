import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient


from pydantic_ai.messages import ModelResponse, ToolCallPart  # noqa: E402
from pydantic_ai.models.function import FunctionModel  # noqa: E402
from pydantic_ai.retries import RetryConfig  # noqa: E402
from tenacity import stop_after_attempt  # noqa: E402

from api import app as app_module  # noqa: E402
from core.config import Config  # noqa: E402
from evals import scoring  # noqa: E402
from models.capture import compact, compacted, input_text, output_text, text_of  # noqa: E402
from evals.scoring import METRICS, score_items  # noqa: E402
from evals.scoring import classification_report, summarize_scores  # noqa: E402
from models.families.generic.transformers_model import TransformersModel  # noqa: E402
from models.manager import ModelManager  # noqa: E402
from tests.fakes import FakeModel, registered, swapped  # noqa: E402
from tests.waits import until, until_async  # noqa: E402


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
    with swapped(TransformersModel, FakeModel), registered(WordyFake, task="text-generation", match="wordy:*"), \
            registered(BrokenFake, task="text-generation", match="broken:*"), TestClient(app_module.app) as c:
        yield c


def flush(client):
    client.app.state.monitoring.flush()


def infer(client, text, model_id="ev/model", **headers):
    return client.post("/inference", headers=headers, json={"model_id": model_id, "task": "text-generation", "input": {"text": text}})


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
    assert summarize_scores([{"metric": "wer", "value": 0.25}])["wer"] == {"count": 1, "scored": 1, "mean": 0.25, "lower_is_better": True}


def test_word_error_rate_counts_the_words_to_change_per_reference_word():
    items = [
        # One word changed and one dropped, of four; case and punctuation do not count.
        {"id": "i1", "example_id": "e1", "status": "ok", "output": {"text": "The cat sat."}, "reference": "the dog sat down", "input": {}},
        {"id": "i2", "example_id": "e2", "status": "ok", "output": {"text": "Hello, world!"}, "reference": "hello world", "input": {}},
        # Words added: past 1.
        {"id": "i3", "example_id": "e3", "status": "ok", "output": {"text": "yes yes yes"}, "reference": "yes", "input": {}},
        {"id": "i4", "example_id": "e4", "status": "ok", "output": {"text": "anything"}, "reference": None, "input": {}},
    ]
    rows = {r["item_id"]: r["value"] for r in asyncio.run(score_items(items, METRICS["wer"][1]()))}
    assert rows == {"i1": 0.5, "i2": 0.0, "i3": 2.0, "i4": None}


def test_ranking_metrics_read_the_relevant_documents_from_the_reference():
    def ranked(order, reference):
        return {"id": "i", "example_id": "e", "status": "ok", "input": {"query": "q"}, "reference": reference,
                "output": {"results": [{"index": i, "score": 1.0 - n / 10} for n, i in enumerate(order)]}}

    def score(metric, item):
        return asyncio.run(score_items([item], METRICS[metric][1]()))[0]["value"]

    assert score("mrr", ranked([2, 0, 1], [0])) == 0.5
    assert score("mrr", ranked([2, 0, 1], {"relevant": [2, 1]})) == 1.0
    assert score("mrr", ranked([2, 1], [0])) == 0.0
    assert score("ndcg_at_10", ranked([0, 1, 2], [0, 1])) == 1.0
    # The one relevant document second: 1 / log2(3) of the best.
    assert score("ndcg_at_10", ranked([1, 0, 2], [0])) == pytest.approx(0.6309, abs=1e-4)
    # No relevant documents named, or no ranking: no score.
    assert score("mrr", ranked([0], [])) is None
    assert score("ndcg_at_10", {**ranked([0], [0]), "output": {"text": "not a ranking"}}) is None


def test_classification_report_counts_labels_against_references():
    def item(predicted, reference, status="ok"):
        return {"status": status, "output": {"label": predicted, "confidence": 0.9}, "reference": reference}

    report = classification_report([
        item("POSITIVE", "positive"), item("POSITIVE", "positive"), item("NEGATIVE", "positive"),
        item("NEGATIVE", {"label": "negative"}), item("POSITIVE", "negative"),
        item("NEGATIVE", None), item("POSITIVE", "positive", status="error"),
    ])
    assert report["count"] == 5 and report["accuracy"] == 0.6
    # Labels match ignoring case, shown as first seen (the reference's spelling here).
    assert [label["label"] for label in report["labels"]] == ["negative", "positive"]
    negative, positive = report["labels"]
    assert positive == {"label": "positive", "precision": 2 / 3, "recall": 2 / 3, "f1": pytest.approx(2 / 3), "support": 3}
    assert negative["precision"] == 0.5 and negative["recall"] == 0.5 and negative["support"] == 2
    assert report["macro_f1"] == pytest.approx((2 / 3 + 0.5) / 2)
    # A row per reference label, a column per predicted one.
    assert report["confusion"] == [[1, 1], [1, 2]]
    assert classification_report([{"status": "ok", "output": {"text": "free text"}, "reference": "x"}]) is None


# ------------------------------------- traces (the request log's, monitoring/: datasets copy them)
def test_requests_are_traced_with_their_input_and_output(client):
    r = infer(client, "What is the capital of France?", **{"X-Trace-Tag": "playground, demo", "X-Session-Id": "sess-1"})
    assert r.status_code == 200, r.text
    assert r.json()["trace_id"]
    flush(client)
    body = client.get("/admin/requests?q=capital").json()
    assert body["capture"]["enabled"] is True and body["capture"]["retention_days"] == Config.TRACE_RETENTION_DAYS
    found = [row for row in body["requests"] if "capital of France" in (row["preview"] or "")]
    assert found, body
    head = found[0]
    assert head["trace_id"] == r.json()["trace_id"]
    assert head["model_id"] == "ev/model" and head["status"] == "ok" and head["tags"] == ["playground", "demo"]
    assert head["session"] == "sess-1" and "input" not in head
    trace = client.get(f"/admin/traces/{head['trace_id']}").json()
    assert trace["input"]["text"] == "What is the capital of France?"
    assert trace["output"]["text"] == "echo: What is the capital of France?"
    assert trace["duration_ms"] >= 0 and trace["input_chars"] > 0 and trace["tags"] == ["playground", "demo"]
    assert client.get("/admin/traces/nope").status_code == 404


def test_requests_filter_by_words_model_status_tag_and_session(client):
    assert infer(client, "okapi crossing rules", model_id="ev/other").status_code == 200
    # A request the model rejects: traced with its error.
    assert infer(client, "please fail", model_id="broken:x").status_code == 400
    flush(client)
    rows = client.get("/admin/requests?q=okapi").json()["requests"]
    assert len(rows) == 1 and rows[0]["model_id"] == "ev/other"
    assert all(r["model_id"] == "ev/other" for r in client.get("/admin/requests?model=EV/OTHER").json()["requests"])
    failed = client.get("/admin/requests?status=client_error&model=broken").json()["requests"]
    assert failed and failed[0]["error"] == "ValueError"
    trace = client.get(f"/admin/traces/{failed[0]['trace_id']}").json()
    assert trace["input"]["text"] == "please fail" and trace["error"] == "ValueError: the model refused this input"
    assert client.get("/admin/requests?tag=demo").json()["requests"][0]["tags"] == ["playground", "demo"]
    assert client.get("/admin/requests?session=sess-1&model=text-generation").json()["requests"]
    assert client.get("/admin/requests?session=sess-1&model=embedding").json()["requests"] == []
    assert client.get("/admin/requests?status=weird").status_code == 422


def test_request_rows_link_to_traces(client):
    r = infer(client, "linked request")
    flush(client)
    rows = client.get("/admin/requests?limit=5").json()["requests"]
    assert any(row["trace_id"] == r.json()["trace_id"] for row in rows)


def test_traces_past_the_retention_are_deleted_and_never_outlive_their_request(client):
    from monitoring.store import REQUEST

    store = client.app.state.monitoring
    old = {"id": 1, "ts": time.time() - 40 * 86400, "model_id": "m", "task": "t", "status": "ok", "duration_ms": 1.0,
           "cold": False, "error": None, "trace_id": "old-trace", "prompt_tokens": None, "completion_tokens": None,
           "tags": [], "session": None, "backend": None}
    store._write([(REQUEST, old, [("old-trace", {"text": "x"}, {"text": "y"})], None)])
    assert client.get("/admin/traces/old-trace").status_code == 200
    assert client.portal.call(store.prune) >= 2  # the request and its trace
    assert client.get("/admin/traces/old-trace").status_code == 404
    # Today's traces stay.
    assert client.get("/admin/requests?q=capital").json()["requests"]
    assert client.portal.call(store.prune) == 0


# ---------------------------------------------------------------- datasets
def test_datasets_gather_examples_from_traces_and_files(client):
    r = client.post("/admin/datasets", json={"name": "capitals", "description": "geography"})
    assert r.status_code == 200, r.text
    dataset = r.json()
    assert client.post("/admin/datasets", json={"name": "capitals"}).status_code == 400
    trace_id = client.get("/admin/requests?q=capital").json()["requests"][0]["trace_id"]
    long_text = "y" * (Config.TRACE_MAX_CHARS + 500)
    r = client.post(f"/admin/datasets/{dataset['id']}/examples", json={
        "examples": [{"input": {"text": "Capital of Spain?"}, "reference": "Madrid", "tags": ["easy"]},
                     {"input": {"text": long_text}, "reference": "whole"}],
        "trace_ids": [trace_id],
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
    assert copied["source_trace_id"] == trace_id and copied["source_model_id"] == "ev/model"
    assert copied["source_output"]["text"].startswith("echo:")
    assert client.get("/admin/datasets").json()["datasets"][0]["examples"] == 4
    # A trace whose input was cut when recorded is not copied: the example would not be what the client sent.
    infer(client, long_text, model_id="long/model")
    flush(client)
    cut = client.get("/admin/requests?model=long").json()["requests"][0]["trace_id"]
    r = client.post(f"/admin/datasets/{dataset['id']}/examples", json={"trace_ids": [cut]})
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
    return until(lambda: (lambda r: r if r["status"] in ("succeeded", "failed", "cancelled") else None)(client.get(f"/admin/runs/{run_id}").json()))


def test_runs_execute_score_judge_and_compare(client, dataset, monkeypatch):
    # The judge is a pydantic-ai model; here a function, in place of the endpoint. No waiting between its retries.
    monkeypatch.setattr(scoring, "judge_model", lambda judge, local: fake_judge(failing_on="three"))
    monkeypatch.setattr(scoring, "JUDGE_RETRY", RetryConfig(stop=stop_after_attempt(2), reraise=True))
    traced = lambda: [r for r in client.get("/admin/requests?model=wordy").json()["requests"] if r["trace_id"]]  # noqa: E731
    before = traced()
    a = client.post("/admin/runs", json={"dataset_id": dataset["id"], "model_id": "ev/model", "task": "text-generation", "name": "plain"})
    assert a.status_code == 200, a.text
    b = client.post("/admin/runs", json={"dataset_id": dataset["id"], "model_id": "wordy:big", "params": {"temperature": 0.5}})
    assert b.status_code == 200, b.text
    run_a, run_b = finished(client, a.json()["id"]), finished(client, b.json()["id"])
    assert (run_a["status"], run_a["total"], run_a["done"], run_a["failed"]) == ("succeeded", 3, 3, 0)
    assert run_b["status"] == "succeeded" and run_b["name"] == "wordy:big" and run_b["failed"] == 0
    # Its requests are the eval's in the request log.
    flush(client)
    assert "eval" in {r["source"] for r in client.get("/admin/requests?model=wordy:big").json()["requests"]}
    outputs = {i["input"]["text"]: i["output"]["text"] for i in run_a["items"]}
    assert outputs == {"one": "echo: one", "two": "echo: two", "three": "echo: three"}
    assert run_a["items"][0]["reference"] == "echo: one" and run_a["items"][0]["duration_ms"] >= 0
    # The runner's own requests are counted, and leave no traces.
    flush(client)
    assert traced() == before and len(client.get("/admin/requests?model=wordy").json()["requests"]) >= 3

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
    r = client.post(f"/admin/runs/{run_a['id']}/judge", json={"model": "judge:v1"})
    assert r.status_code == 202, r.text
    judged = until(lambda: (lambda s: s if "judge" in s else None)(client.get(f"/admin/runs/{run_a['id']}").json()["summary"]))
    assert judged["judge"] == {"count": 3, "scored": 2, "mean": 0.75}
    detail = client.get(f"/admin/runs/{run_a['id']}").json()
    assert detail["activity"] is None
    scores = {i["input"]["text"]: s for i in detail["items"] for s in i["scores"] if s["metric"] == "judge"}
    assert scores["one"]["label"] == "pass" and scores["one"]["rationale"] == "fine" and scores["one"]["judge"] == "judge:v1"
    assert scores["three"]["value"] is None and scores["three"]["rationale"].startswith("The judge failed: RuntimeError")
    # With the judge back, judging again replaces the scores.
    monkeypatch.setattr(scoring, "judge_model", lambda judge, local: fake_judge())
    client.post(f"/admin/runs/{run_a['id']}/judge", json={"model": "judge:v1"})
    until(lambda: client.get(f"/admin/runs/{run_a['id']}").json()["summary"]["judge"]["scored"] == 3)

    r = client.post(f"/admin/runs/{run_a['id']}/judge", json={"model": "judge:v1", "against": run_b["id"]})
    assert r.status_code == 202, r.text
    metric = f"judge_vs_{run_b['id']}"
    pairwise = until(lambda: client.get(f"/admin/runs/{run_a['id']}").json()["summary"].get(metric))
    # The wordy model's answers are longer, which this judge prefers, in either order.
    assert (pairwise["wins"], pairwise["ties"], pairwise["losses"]) == (0, 0, 3) and pairwise["mean"] == 0.0

    compare = client.get(f"/admin/runs/compare/{run_a['id']}/{run_b['id']}").json()
    assert len(compare["rows"]) == 3
    assert compare["rows"][0]["a"]["output"]["text"] == "echo: one" and compare["rows"][0]["b"]["output"]["text"].startswith("echo: one, and")
    assert compare["a"]["summary"]["exact_match"]["mean"] == pytest.approx(2 / 3)
    assert client.get(f"/admin/datasets/{dataset['id']}").json()["run_list"][0]["id"] == run_b["id"]


def test_a_classifier_run_reports_its_labels_against_the_references(client):
    d = client.post("/admin/datasets", json={"name": "sentiment"}).json()
    client.post(f"/admin/datasets/{d['id']}/examples", json={"examples": [
        {"input": {"text": "great"}, "reference": "positive"},
        {"input": {"text": "awful"}, "reference": "negative"},
        {"input": {"text": "no label yet"}},
    ]})
    # The fake classifier answers POSITIVE to everything.
    run = client.post("/admin/runs", json={"dataset_id": d["id"], "model_id": "ev/cls", "task": "text-classification"}).json()
    detail = finished(client, run["id"])
    report = detail["classification"]
    assert report["count"] == 2 and report["accuracy"] == 0.5 and report["confusion"] == [[0, 1], [0, 1]]
    assert {label["label"]: label["recall"] for label in report["labels"]} == {"negative": 0.0, "positive": 1.0}
    compare = client.get(f"/admin/runs/compare/{run['id']}/{run['id']}").json()
    assert compare["a"]["classification"]["accuracy"] == 0.5
    # A run whose outputs are text has none.
    text = client.post("/admin/runs", json={"dataset_id": d["id"], "model_id": "ev/model", "task": "text-generation"}).json()
    assert finished(client, text["id"])["classification"] is None


class UnsureFake(FakeModel):
    """A classifier whose confidence grows with the text's length."""

    async def predict(self, request):
        confidence = len(request.text) / 100
        return {"label": "positive", "confidence": confidence, "scores": {"positive": confidence, "negative": 1 - confidence}}


def test_labeling_goes_through_the_unlabeled_examples_least_confident_first(client):
    d = client.post("/admin/datasets", json={"name": "to label"}).json()
    assert d["labels"] == []
    r = client.patch(f"/admin/datasets/{d['id']}", json={"labels": ["positive", " negative ", "positive"]})
    assert r.status_code == 200, r.text
    assert r.json()["labels"] == ["positive", "negative"] and r.json()["name"] == "to label"
    assert client.patch(f"/admin/datasets/{d['id']}", json={"name": "renamed"}).status_code == 422
    client.post(f"/admin/datasets/{d['id']}/examples", json={"examples": [
        {"input": {"text": "a"}}, {"input": {"text": "bbbb"}}, {"input": {"text": "cc"}},
        {"input": {"text": "done already"}, "reference": "negative"},
    ]})
    queue = lambda **q: client.get(f"/admin/datasets/{d['id']}/to-label", params=q).json()  # noqa: E731
    texts = lambda page: [e["input"]["text"] for e in page["examples"]]  # noqa: E731
    # With no run and no traced answers there is nothing to suggest: oldest first.
    first = queue()
    assert texts(first) == ["a", "bbbb", "cc"] and first["left"] == 3 and first["examples"][0]["suggestion"] is None

    with registered(UnsureFake, task="text-classification", match="unsure:*"):
        run = client.post("/admin/runs", json={"dataset_id": d["id"], "model_id": "unsure:x", "task": "text-classification"}).json()
        finished(client, run["id"])
    page = queue(run_id=run["id"])
    assert texts(page) == ["a", "cc", "bbbb"] and page["examples"][0]["suggestion"]["confidence"] == 0.01

    a = page["examples"][0]
    r = client.patch(f"/admin/datasets/{d['id']}/examples/{a['id']}", json={"reference": "negative"})
    assert r.status_code == 200, r.text
    assert r.json()["reference"] == "negative" and r.json()["input"] == {"text": "a"}
    # Passing over "cc" leaves it at the front: the next one is after it.
    after = queue(run_id=run["id"], skip=1)
    assert texts(after) == ["bbbb"] and after["left"] == 2
    # A reference taken away puts the example back.
    r = client.patch(f"/admin/datasets/{d['id']}/examples/{a['id']}", json={"reference": None})
    assert r.json()["reference"] is None and queue()["left"] == 3
    assert client.patch(f"/admin/datasets/{d['id']}/examples/{a['id']}", json={}).status_code == 422
    assert client.patch(f"/admin/datasets/{d['id']}/examples/nope", json={"reference": "x"}).status_code == 404
    other = client.post("/admin/datasets", json={"name": "not labeled here"}).json()
    assert client.get(f"/admin/datasets/{other['id']}/to-label", params={"run_id": run["id"]}).status_code == 400


def test_a_datasets_labeled_examples_become_the_classifier_jobs_training_file(client):
    d = client.post("/admin/datasets", json={"name": "for training/v1"}).json()
    assert client.post(f"/admin/datasets/{d['id']}/training-file").status_code == 400  # nothing labeled
    client.post(f"/admin/datasets/{d['id']}/examples", json={"examples": [
        {"input": {"text": "great"}, "reference": "positive"},
        {"input": {"text": "awful"}, "reference": {"label": " negative "}},
        {"input": {"text": "fine"}, "reference": "Positive"},
        {"input": {"text": "not yet"}},
        {"input": {"question": "q", "context": "c"}, "reference": "no text"},
    ]})
    r = client.post(f"/admin/datasets/{d['id']}/training-file")
    assert r.status_code == 200, r.text
    # Named by the dataset's id; labels that differ only in case are one.
    assert r.json() == {"path": f"evals/{d['id']}.jsonl", "rows": 3, "labels": {"negative": 1, "positive": 2}}
    written = client.app.state.jobs.data_dir / r.json()["path"]
    rows = [json.loads(line) for line in written.read_text().splitlines()]
    assert rows == [{"text": "great", "label": "positive"}, {"text": "awful", "label": "negative"}, {"text": "fine", "label": "positive"}]
    # It is a file the job takes.
    r = client.post("/jobs", json={"job": "text-classifier", "params": {"train_file": r.json()["path"], "epochs": 0}})
    assert r.status_code == 400 and "epochs" in r.text


def test_runs_cancel_delete_and_validate(client, dataset):
    r = client.post("/admin/runs", json={"dataset_id": dataset["id"], "model_id": "ev/model"})
    run_id = r.json()["id"]
    client.post(f"/admin/runs/{run_id}/cancel")
    assert finished(client, run_id)["status"] in ("cancelled", "succeeded")
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
    assert {m["name"] for m in metrics["metrics"]} == {"exact_match", "contains", "json_valid", "wer", "mrr", "ndcg_at_10"}
    assert metrics["judge"]["model"] == "" and "rubric" in metrics["judge"]
    # Only a run that stopped short resumes.
    assert client.post(f"/admin/runs/{far['id']}/resume").status_code == 400
    # Deleting a dataset takes its runs with it.
    assert client.delete(f"/admin/datasets/{other['id']}").status_code == 200
    assert client.get(f"/admin/runs/{far['id']}").status_code == 404


def test_eval_routes_need_the_admin_key(client, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "client-key")
    monkeypatch.setattr(Config, "ADMIN_API_KEY", "admin-key")
    for path in ("/admin/datasets", "/admin/runs", "/admin/evals/metrics"):
        assert client.get(path).status_code == 401
        assert client.get(path, headers={"X-API-Key": "client-key"}).status_code == 401
        assert client.get(path, headers={"X-API-Key": "admin-key"}).status_code == 200


def task_runner(own_db):
    """The task queue runs wait in (tasks/runner.py), on a database of its own."""
    from tasks.store import TaskStore
    from tasks.runner import TaskRunner
    from db.tables import TableStore

    return TaskRunner(TaskStore(own_db, TableStore(own_db)))


async def run_is(tasks, *statuses, run_id="r1"):
    """Whether the run's newest task is in one of ``statuses``: the run's status (RunExecutor.with_status)."""
    task = await tasks.find("run", run_id)
    return task is not None and task["status"] in statuses


async def note_status(tasks, store, run_id="r1"):
    """The fake store's row with its run's status and error, as RunExecutor.with_status gives them: its task's."""
    task = await tasks.find("run", run_id)
    store.rows[run_id].update(status=task["status"], error=task["error"])


def test_a_run_cancelled_while_queued_is_reported_once(own_db):
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

        async def send(self, event, data, extra_urls=()):
            self.sent.append((event, data["status"]))

    async def scenario():
        hooks = Hooks()
        store = Store()
        tasks = task_runner(own_db)
        runner = RunExecutor(store, manager=None, tasks=tasks, webhooks=hooks)
        store.rows["r1"] = {"id": "r1"}  # as POST /admin/runs creates it
        await runner.submit("r1")  # the task runner is not started, so it stays queued
        await runner.cancel("r1")
        return hooks.sent, (await runner.run("r1"))["status"]

    sent, status = asyncio.run(scenario())
    assert sent == [("run.finished", "cancelled")] and status == "cancelled"


def test_a_resumed_run_skips_the_examples_it_answered(own_db):
    from evals.runner import RunExecutor

    examples = [{"id": f"e{n}", "input": {"text": f"q{n}"}, "task": None} for n in (1, 2, 3)]

    class Store:
        def __init__(self):
            # A run that stopped short with one example answered already, which POST /admin/runs/{id}/resume queues.
            self.rows = {"r1": {"id": "r1", "dataset_id": "d", "model_id": "m", "failed": 0}}
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
        width = ModelManager.width  # the real gate, over this fake's parallelism

        def parallelism(self, model_id, task):
            return 32

        async def run_inference(self, model_id, task, request):
            return {"result": {"text": "ok"}}

    async def scenario():
        store = Store()
        tasks = task_runner(own_db)
        runner = RunExecutor(store, manager=Manager(), tasks=tasks)
        await runner.submit("r1")
        await tasks.start()
        await until_async(lambda: run_is(tasks, "succeeded"), 2, lambda: f"run {store.rows['r1']}")
        await tasks.stop()
        return store

    store = asyncio.run(scenario())
    assert store.items == [("e2", "ok"), ("e3", "ok")] and store.rows["r1"]["done"] == 3




def test_a_judge_queued_before_a_restart_runs_after_it(own_db, monkeypatch):
    """A judge is a model ID, asked through this service's own /v1: its task holds all it needs and nothing is kept in
    memory, so one queued when the service stopped runs when it is back; one that fails says why in the run's
    activity."""
    from evals.runner import RunExecutor
    from evals.scoring import Judge

    seen = []

    async def judging(self, task, judge, local):
        seen.append((task["ref"], judge.model, task["model_id"]))
        return "RuntimeError: the judge's server is down"

    monkeypatch.setattr(RunExecutor, "_judge", judging)

    def executor(tasks):
        runner = RunExecutor(store=None, manager=None, tasks=tasks)
        runner.local_client = lambda priority: None
        return runner

    async def scenario():
        await executor(task_runner(own_db)).judge("r1", Judge(model="judge:v1"))
        # The service stopped before it ran; the next one runs it.
        again = task_runner(own_db)
        runner = executor(again)
        queued = await again.find("judge", "r1")
        await again.start()
        await until_status(again, queued["id"], "failed")
        await again.stop()
        return await runner.activity([{"id": "r1", "status": "succeeded", "done": 3, "total": 3}])

    activity = asyncio.run(scenario())
    assert seen == [("r1", "judge:v1", "judge:v1")]
    assert activity["r1"]["kind"] == "judging" and "server is down" in activity["r1"]["error"]


def test_cancelling_a_judge_stops_its_scoring(own_db, monkeypatch):
    from evals.runner import RunExecutor
    from evals.scoring import Judge

    async def judging(self, task, judge, local):
        await asyncio.sleep(30)

    monkeypatch.setattr(RunExecutor, "_judge", judging)

    async def scenario():
        tasks = task_runner(own_db)
        runner = RunExecutor(store=None, manager=None, tasks=tasks)
        runner.local_client = lambda priority: None
        await tasks.start()
        await runner.judge("r1", Judge(model="local/judge"), priority="low")
        task = await tasks.find("judge", "r1")
        await until_status(tasks, task["id"], "running")
        run = {"id": "r1", "status": "succeeded", "done": 3, "total": 3}
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
    async def reached():
        return (await tasks.get(id))["status"] == status
    await until_async(reached, timeout, f"task {id} never became {status}")


def test_a_run_the_service_stopped_under_goes_on_by_itself(own_db):
    from evals.runner import RunExecutor

    examples = [{"id": f"e{n}", "input": {"text": f"q{n}"}, "task": None} for n in (1, 2, 3)]

    class Store:
        def __init__(self):
            self.rows = {"r1": {"id": "r1", "dataset_id": "d", "model_id": "m", "failed": 0}}
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
        width = ModelManager.width  # the real gate, over this fake's parallelism

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
        tasks = task_runner(own_db)
        first = Manager(stall_on="q2")
        runner = RunExecutor(store, manager=first, tasks=tasks)
        await runner.submit("r1")
        await tasks.start()
        await until_async(lambda: len(first.answered) == 2, 4, lambda: f"answered {first.answered}")
        await tasks.stop()
        stopped, items = (await runner.run("r1"))["status"], list(store.items)
        # The next one goes on from there.
        tasks = task_runner(own_db)
        second = Manager()
        runner = RunExecutor(store, manager=second, tasks=tasks)
        await tasks.start()
        await until_async(lambda: run_is(tasks, "succeeded"), 4, lambda: f"run {store.rows['r1']}")
        await tasks.stop()
        return stopped, items, second.answered, store

    stopped, items, asked_again, store = asyncio.run(scenario())
    # The answers in when it stopped were kept; only the one in flight was asked again.
    assert stopped == "queued" and items == [("e1", "ok"), ("e3", "ok")]
    assert asked_again == ["q2"]
    assert store.rows["r1"]["done"] == 3


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


async def run_to_the_end(runner, tasks, store, run_id="r1", until=("succeeded", "cancelled", "failed")):
    store.rows[run_id] = {"id": run_id, "dataset_id": "d", "model_id": "m", "task": "t", "failed": 0}
    await runner.submit(run_id)
    await tasks.start()
    await until_async(lambda: run_is(tasks, *until, run_id=run_id), 10, lambda: f"run {store.rows[run_id]}")
    await tasks.stop()
    await note_status(tasks, store, run_id)


def test_a_run_sends_its_examples_a_page_at_a_time_so_they_share_forward_passes(own_db):
    from evals.runner import RunExecutor
    from models.manager import ModelManager
    from tests.test_batching import BatchingModel

    async def scenario():
        store = PageStore(64)
        tasks = task_runner(own_db)
        runner = RunExecutor(store, manager=ModelManager(device="cpu", batch_wait_s=0.005), tasks=tasks)
        await run_to_the_end(runner, tasks, store)
        return store

    BatchingModel.batch_calls.clear()
    with registered(BatchingModel, task="t", fallback=True):
        store = asyncio.run(scenario())
    assert store.rows["r1"]["status"] == "succeeded" and store.rows["r1"]["done"] == 64
    # In the dataset's order, each with its own answer; one at a time would have been 64 passes.
    assert store.items == [(f"e{i}", "ok") for i in range(64)]
    assert BatchingModel.batch_calls["requests"] == 64 and BatchingModel.batch_calls["calls"] <= 4


def test_cancelling_a_run_drops_the_requests_of_its_page_still_waiting(own_db):
    from evals.runner import RunExecutor

    class Manager:
        width = ModelManager.width  # the real gate, over this fake's parallelism

        def parallelism(self, model_id, task):
            return 32

        async def run_inference(self, model_id, task, request):
            if request["text"] != "q0":
                await asyncio.sleep(30)
            return {"result": {"text": "ok"}}

    async def scenario():
        store = PageStore(5)
        tasks = task_runner(own_db)
        runner = RunExecutor(store, manager=Manager(), tasks=tasks)
        store.rows["r1"] = {"id": "r1", "dataset_id": "d", "model_id": "m", "failed": 0}
        await runner.submit("r1")
        await tasks.start()
        await until_async(lambda: run_is(tasks, "running"), 5, "the run never started")
        await asyncio.sleep(0.05)  # q0 is answered; the rest wait
        started = time.monotonic()
        await runner.cancel("r1")
        took = time.monotonic() - started
        await tasks.stop()
        await note_status(tasks, store)
        return store, took

    store, took = asyncio.run(scenario())
    assert store.rows["r1"]["status"] == "cancelled" and took < 5
    assert store.items == [("e0", "ok")]


def test_a_model_that_answers_one_at_a_time_gets_the_run_one_at_a_time(own_db):
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
        tasks = task_runner(own_db)
        runner = RunExecutor(store, manager=manager, tasks=tasks)
        await run_to_the_end(runner, tasks, store)
        return store

    with registered(OneAtATime, task="t", fallback=True):
        store = asyncio.run(scenario())
    assert store.rows["r1"]["status"] == "succeeded" and len(store.items) == 40
    assert max(waiting) == 1


def test_a_run_the_service_has_no_room_for_stops_keeping_its_answers_and_resumes(own_db):
    """No room for the model is the service's, not the example's: no error item that a resume would skip."""
    from evals.runner import RunExecutor
    from models.cache import CapacityError

    class Manager:
        width = ModelManager.width  # the real gate, over this fake's parallelism

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
        tasks = task_runner(own_db)
        await run_to_the_end(RunExecutor(store, manager=manager, tasks=tasks), tasks, store)
        stopped = dict(store.rows["r1"]), list(store.items)
        # Resumed (run_to_the_end queues it again): the answers kept are skipped, the refused one is asked again.
        manager.busy = False
        tasks = task_runner(own_db)
        await run_to_the_end(RunExecutor(store, manager=manager, tasks=tasks), tasks, store)
        return stopped, store

    (stopped, items), store = asyncio.run(scenario())
    assert stopped["status"] == "failed" and "resume the run" in stopped["error"]
    assert sorted(items) == [("e0", "ok"), ("e1", "ok"), ("e3", "ok"), ("e4", "ok")]
    assert store.rows["r1"]["status"] == "succeeded" and ("e2", "ok") in store.items and len(store.items) == 5


def test_a_page_widens_once_an_answer_has_loaded_the_model(own_db):
    """The first example fails before the model loads (a bad input): the second loads it, and the rest go together."""
    from evals.runner import RunExecutor

    class Manager:
        width = ModelManager.width  # the real gate, over this fake's parallelism

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
        tasks = task_runner(own_db)
        await run_to_the_end(RunExecutor(store, manager=manager, tasks=tasks), tasks, store)
        return store

    store = asyncio.run(scenario())
    assert store.rows["r1"]["status"] == "succeeded" and store.items[0] == ("e0", "error") and len(store.items) == 20
    assert manager.most > 1


def test_resumes_at_once_queue_one_task_and_a_run_never_queued_resumes(own_db):
    """However many resumes come at once, the run has one task waiting or running, which a cancel reaches; and a run
    whose task was never made (the service stopped between the two) is cancelled, so it can be resumed."""
    from evals.runner import RunExecutor

    class Store:
        rows = {"r1": {"id": "r1", "model_id": "m"}, "r2": {"id": "r2", "model_id": "m"}}

        async def run(self, run_id):
            return dict(self.rows[run_id])

    async def scenario():
        tasks = task_runner(own_db)
        runner = RunExecutor(Store(), manager=None, tasks=tasks)
        never = (await runner.run("r2"))["status"]
        await runner.submit("r1")
        await runner.cancel("r1")
        resumed = await asyncio.gather(*(runner.resume("r1") for _ in range(4)), return_exceptions=True)
        waiting = await tasks.list(10, kind="run", ref="r1", status="queued")
        await runner.cancel("r1")
        return never, resumed, waiting, (await runner.run("r1"))["status"], await runner.resume("r2")

    never, resumed, waiting, after_cancel, again = asyncio.run(scenario())
    assert never == "cancelled" and again["status"] == "queued"
    assert sum(1 for r in resumed if isinstance(r, dict)) == 1 and len(waiting) == 1
    assert all("resumes" in str(r) for r in resumed if not isinstance(r, dict))
    assert after_cancel == "cancelled"


def test_a_run_whose_model_is_no_longer_allowed_stops(own_db):
    """A model taken out of ALLOWED_MODELS while the run goes refuses each request (Forbidden): that is the run's, as
    for a workflow, not an error of every example after it."""
    from core.errors import Forbidden
    from evals.runner import RunExecutor

    class Manager:
        width = ModelManager.width

        def parallelism(self, model_id, task):
            return 32

        async def run_inference(self, model_id, task, request):
            if request["text"] == "q2":
                raise Forbidden("model_id is not in ALLOWED_MODELS")
            return {"result": {"text": "ok"}}

    async def scenario():
        store = PageStore(5)
        tasks = task_runner(own_db)
        await run_to_the_end(RunExecutor(store, manager=Manager(), tasks=tasks), tasks, store)
        return store

    store = asyncio.run(scenario())
    assert store.rows["r1"]["status"] == "failed" and "ALLOWED_MODELS" in store.rows["r1"]["error"]
    assert ("e2", "error") not in store.items


def test_runs_and_examples_page_with_a_cursor(client):
    """A page goes on where the last stopped, newest runs first, oldest examples first; a new run meanwhile does not
    make the next page repeat one."""
    d = client.post("/admin/datasets", json={"name": "paged"}).json()
    client.post(f"/admin/datasets/{d['id']}/examples", json={"examples": [{"input": {"text": f"e{i}"}} for i in range(3)]})
    first = client.get(f"/admin/datasets/{d['id']}/examples", params={"limit": 2}).json()
    rest = client.get(f"/admin/datasets/{d['id']}/examples", params={"limit": 2, "cursor": first["next"]}).json()
    assert [e["input"]["text"] for e in first["examples"] + rest["examples"]] == ["e0", "e1", "e2"] and "next" not in rest
    made = [client.post("/admin/runs", json={"dataset_id": d["id"], "model_id": "ev/model"}).json()["id"] for _ in range(3)]
    page = client.get("/admin/runs", params={"dataset_id": d["id"], "limit": 2}).json()
    assert [r["id"] for r in page["runs"]] == made[:0:-1]
    client.post("/admin/runs", json={"dataset_id": d["id"], "model_id": "ev/model"})
    older = client.get("/admin/runs", params={"dataset_id": d["id"], "limit": 2, "cursor": page["next"]}).json()
    assert [r["id"] for r in older["runs"]] == made[:1] and "_seq" not in older["runs"][0]
    assert client.delete(f"/admin/datasets/{d['id']}").status_code == 200
