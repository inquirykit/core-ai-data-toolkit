import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from api import app as app_module
from config import Config
from models.events import HIGH, LOW
from models.generic.transformers_model import TransformersModel
from models.vision.fastvlm import FastVLMModel
from tests.fakes import FakeModel, swapped

IMAGES = Path(os.environ["IMAGE_DIR"])


@pytest.fixture(scope="module")
def client():
    IMAGES.mkdir(exist_ok=True)
    (IMAGES / "ok.png").write_bytes(b"not really a png")
    with swapped(TransformersModel, FakeModel), swapped(FastVLMModel, FakeModel), TestClient(app_module.app) as c:
        yield c
    (IMAGES / "ok.png").unlink()


def test_health(client):
    body = client.get("/health").json()
    assert body["status"] == "healthy"
    assert "memory_backend" in body["cache_status"]


def generate(text="hi", model_id="some/model", **params):
    return {"model_id": model_id, "task": "text-generation", "input": {"text": text, **params}}


def test_inference_roundtrip_and_cache(client):
    r = client.post("/inference", json=generate(max_length=5))
    assert r.status_code == 200, r.text
    assert r.json()["result"] == {"text": "echo: hi", "tool_calls": None, "prompt_tokens": None, "completion_tokens": None, "logprobs": None}
    assert r.json()["task"] == "text-generation"
    assert r.json()["model_used"] == "some/model:text-generation"
    assert FakeModel.last_request.max_length == 5
    assert FakeModel.last_request.temperature == 0.7, "unset fields get the contract's default"
    assert "some/model:text-generation" in client.get("/models/status").json()["cached_models"]


def test_x_priority_says_how_urgent_a_request_is(client):
    seen = []
    sinks = client.app.state.manager.sinks
    sinks.append(lambda event: seen.append(event.context.priority))
    try:
        assert client.post("/inference", json=generate(), headers={"X-Priority": "Low"}).status_code == 200
        assert client.post("/inference", json=generate()).status_code == 200
    finally:
        sinks.pop()
    assert seen == [LOW, HIGH]
    r = client.post("/inference", json=generate(), headers={"X-Priority": "urgent"})
    assert r.status_code == 400 and r.json()["detail"] == "X-Priority must be high, normal or low"


@pytest.mark.parametrize("bad", [
    {"max_length": -1},
    {"max_length": 10 ** 6},
    {"temperature": 0},
    {"top_p": 1.5},
    {"text": ""},
    {"context": "not a text-generation field"},
])
def test_request_validation(client, bad):
    assert client.post("/inference", json=generate(model_id="m", **bad)).status_code == 422


@pytest.mark.parametrize("task, bad_input", [
    ("text-classification", {"text": "hi", "max_length": 10}),
    ("question-answering", {"question": "Who?"}),
    ("vision-language", {"text": "hi", "image": "../conftest.py"}),
    ("decision", {"state": "s", "questions": {}}),
])
def test_each_task_checks_its_own_input(client, task, bad_input):
    r = client.post("/inference", json={"model_id": "m", "task": task, "input": bad_input})
    assert r.status_code == 422, r.text


def test_tasks_lists_each_contract(client):
    listed = client.get("/tasks").json()["tasks"]
    assert set(listed) >= {"text-generation", "text-classification", "question-answering", "vision-language", "decision"}
    classification = listed["text-classification"]
    assert classification["input"]["required"] == ["text"]
    assert set(classification["output"]["required"]) == {"label", "confidence", "scores"}


def test_tasks_list_the_limits_schemas_cannot_show(client):
    limits = client.get("/tasks").json()["limits"]
    assert limits["max_audio_bytes"] == Config.MAX_AUDIO_BYTES
    assert limits["max_media_bytes"] == Config.MAX_MEDIA_BYTES
    assert limits["max_input_chars"] == Config.MAX_INPUT_CHARS


def test_openapi_shows_each_task(client):
    schemas = client.get("/openapi.json").json()["components"]["schemas"]
    assert "QuestionAnsweringInferenceRequest" in schemas
    assert "QuestionAnsweringInferenceResponse" in schemas
    assert "TextClassificationBatchInferenceRequest" in schemas
    assert "TextClassificationBatchInferenceResponse" in schemas


def classify(*texts, model_id="org/classifier"):
    return {"model_id": model_id, "task": "text-classification", "inputs": [{"text": t} for t in texts]}


def test_batch_inference_answers_each_input_in_order(client):
    r = client.post("/inference/batch", json=classify("good", "bad", "fine"))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["task"] == "text-classification"
    assert [res["label"] for res in body["results"]] == ["POSITIVE"] * 3
    assert len(body["trace_ids"]) == 3 and len(set(body["trace_ids"])) == 3
    assert body["model_used"] == "org/classifier:text-classification"


@pytest.mark.parametrize("body", [
    classify(),                                     # no inputs
    classify("ok", ""),                             # one bad input
    classify(*["t"] * (Config.MAX_BATCH_INPUTS + 1)),
    {"model_id": "m", "task": "text-classification", "input": {"text": "one"}},  # /inference's shape
])
def test_batch_inference_checks_its_inputs(client, body):
    r = client.post("/inference/batch", json=body)
    assert r.status_code == 422, r.text


def test_batch_inference_error_points_at_the_input(client):
    detail = client.post("/inference/batch", json=classify("ok", "")).json()["detail"]
    assert any(e["loc"][:3] == ["body", "text-classification", "inputs"] and e["loc"][3] == 1 for e in detail), detail


def test_image_path_is_confined_to_image_dir(client):
    base = {"model_id": "apple/FastVLM-1.5B", "input_text": "describe"}
    assert client.post("/vision-language", json={**base, "image_path": "../conftest.py"}).status_code == 400
    assert client.post("/vision-language", json={**base, "image_path": "/etc/passwd"}).status_code == 400
    assert client.post("/vision-language", json={**base, "image_path": "missing.png"}).status_code == 404
    r = client.post("/vision-language", json={**base, "image_path": "ok.png", "max_new_tokens": 7})
    assert r.status_code == 200, r.text
    assert FakeModel.last_request.image == str(IMAGES / "ok.png")
    assert FakeModel.last_request.max_length == 7
    assert r.json()["result"]["text"] == "echo: describe"
    assert r.json()["processing_time"] >= 0
    # /inference reaches the same model, with the same confinement.
    vision = {"model_id": "apple/FastVLM-1.5B", "task": "vision-language"}
    assert client.post("/inference", json={**vision, "input": {"text": "x", "image": "/etc/passwd"}}).status_code == 422
    assert client.post("/inference", json={**vision, "input": {"text": "x", "image": "ok.png"}}).status_code == 200


def test_errors_do_not_leak_details(client):
    class Failing(FakeModel):
        async def predict(self, *a, **k):
            raise RuntimeError("secret /path/to/weights")

    with swapped(FakeModel, Failing):
        r = client.post("/inference", json=generate(model_id="other"))
    assert r.status_code == 500
    assert "secret" not in r.text
    assert "RuntimeError" in r.text


def test_load_unload_with_slash_in_model_id_and_clear(client):
    r = client.post("/models/load", json={"model_id": "org/name", "task": "text-classification", "torch_dtype": "float16"})
    assert r.status_code == 200, r.text
    assert client.delete("/models/org/name/text-classification").status_code == 200
    assert client.delete("/models/org/name/text-classification").status_code == 404
    assert client.delete("/models/clear").status_code == 200
    assert client.get("/models/status").json()["cache_size"] == 0


def test_bad_dtype_rejected(client):
    r = client.post("/models/load", json={"model_id": "m", "task": "t", "torch_dtype": "int8"})
    assert r.status_code == 422


def test_api_key_required_when_configured(client, monkeypatch):
    monkeypatch.setattr(Config, "API_KEY", "s3cret")
    assert client.get("/models/status").status_code == 401
    assert client.get("/models/status", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/models/status", headers={"X-API-Key": "s3cret"}).status_code == 200
    assert client.get("/models/status", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert client.post("/inference", json=generate(model_id="m")).status_code == 401
    # Container health checks run without credentials.
    assert client.get("/health").status_code == 200


def test_allowed_models_enforced(client, monkeypatch):
    monkeypatch.setattr(Config, "ALLOWED_MODELS", ["ok/model"])
    assert client.post("/inference", json=generate(model_id="evil/model")).status_code == 403
    assert client.post("/inference", json=generate(model_id="ok/model")).status_code == 200
    assert client.post("/models/load", json={"model_id": "evil/model", "task": "t"}).status_code == 403
    r = client.post("/vision-language", json={"model_id": "evil/x-fastvlm", "input_text": "hi"})
    assert r.status_code == 403


def test_qa_question_and_context_reach_the_model(client):
    r = client.post("/inference", json={
        "model_id": "qa/model", "task": "question-answering",
        "input": {"question": "What is the capital?", "context": "Paris is the capital."},
    })
    assert r.status_code == 200, r.text
    assert FakeModel.last_request.context == "Paris is the capital."
    assert r.json()["result"]["answer"] == "Paris"


def test_a_result_that_breaks_the_contract_is_a_server_error(client):
    class Sloppy(FakeModel):
        async def predict(self, request):
            return {"label": "POSITIVE"}  # no confidence or scores

    with swapped(FakeModel, Sloppy):
        r = client.post("/inference", json={"model_id": "sloppy", "task": "text-classification", "input": {"text": "x"}})
    assert r.status_code == 500
    assert "ContractViolation" in r.text


def test_wrong_task_for_a_claimed_model_is_a_client_error(client):
    assert client.post("/inference", json=generate(model_id="vega-0.8b")).status_code == 400
    r = client.post("/inference", json={"model_id": "any/model", "task": "no-such-task", "input": "hi"})
    assert r.status_code == 400
    cached = client.get("/models/status").json()["cached_models"]
    assert not any(key.startswith(("vega-0.8b:", "any/model:")) for key in cached)


def test_model_types_describe_the_registry(client):
    types = client.get("/models/types").json()["model_types"]
    vega = next(t for t in types if t["class"].endswith("VegaDecisionModel"))
    assert vega["task"] == "decision" and "vega-4b" in vega["ids"]


def by_task(catalog):
    return {t["task"]: t for t in catalog["tasks"]}


def test_catalog_lists_each_task_with_cards_and_status(client):
    tasks = by_task(client.get("/models/catalog").json())
    generation = tasks["text-generation"]
    assert generation["any_id"] and generation["description"]
    qwen = next(m for m in generation["models"] if m["id"] == "Qwen/Qwen2.5-0.5B-Instruct")
    assert "chat" in qwen["tags"] and qwen["description"]
    assert qwen["served_by"] == "FakeModel" and qwen["kind"] == "in-process"
    assert tasks["decision"]["patterns"] == ["vega-*", "*/vega-*"] and not tasks["decision"]["any_id"]

    # A model loaded by ID shows up loaded, under its task, though nobody lists it.
    assert client.post("/models/load", json={"model_id": "loaded/one", "task": "text-generation"}).status_code == 200
    models = {m["id"]: m for m in by_task(client.get("/models/catalog").json())["text-generation"]["models"]}
    assert models["loaded/one"]["status"] == "loaded" and models["loaded/one"]["generic"]
    assert models["Qwen/Qwen2.5-0.5B-Instruct"]["status"] == "unloaded"
    client.delete("/models/loaded/one/text-generation")


def test_catalog_follows_allowed_models(client, monkeypatch):
    monkeypatch.setattr(Config, "ALLOWED_MODELS", ["gpt2", "my/model"])
    tasks = by_task(client.get("/models/catalog").json())
    assert [m["id"] for m in tasks["text-generation"]["models"]] == ["gpt2", "my/model"]
    assert not tasks["text-generation"]["any_id"] and tasks["text-generation"]["patterns"] == []
    assert tasks["embedding"]["models"] == []


def test_estimate_plans_a_load_without_loading(client, monkeypatch):
    monkeypatch.setattr(FakeModel, "size_bytes", 2 * 1024 ** 3)
    r = client.get("/models/estimate", params={"model_id": "plan/me", "task": "text-generation"})
    assert r.status_code == 200, r.text
    plan = r.json()
    assert plan["estimate_gb"] == 2 and plan["outcome"] in ("fits", "evicts")
    assert "plan/me:text-generation" not in client.get("/models/status").json()["cached_models"]
    params = {"model_id": "vega-0.8b", "task": "text-generation"}
    assert client.get("/models/estimate", params=params).status_code == 400
    params = {"model_id": "x", "task": "text-generation", "torch_dtype": "int3"}
    assert client.get("/models/estimate", params=params).status_code == 422
    monkeypatch.setattr(Config, "ALLOWED_MODELS", ["gpt2"])
    assert client.get("/models/estimate", params={"model_id": "evil/model", "task": "t"}).status_code == 403


def test_models_can_be_pinned(client):
    r = client.post("/models/load", json={"model_id": "pin/me", "task": "text-generation", "pin": True})
    assert r.status_code == 200, r.text
    details = client.get("/models/status").json()["model_details"]["pin/me:text-generation"]
    assert details["pinned"] is True and details["queued"] == 0

    assert client.put("/models/pin/me/text-generation/pin", json={"pinned": False}).status_code == 200
    assert client.get("/models/status").json()["model_details"]["pin/me:text-generation"]["pinned"] is False
    assert client.put("/models/not/loaded/text-generation/pin", json={"pinned": True}).status_code == 404
    assert client.delete("/models/pin/me/text-generation").status_code == 200
