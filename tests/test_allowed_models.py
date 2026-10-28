"""ALLOWED_MODELS holds on every route a client names a model to: a route added without the check fails here.

The model manager refuses a model outside the list on every load, run and estimate (ModelManager.check_allowed), so
this test sends each such route a model that is not allowed and expects 403. The routes are found from the API
schema (a ``model`` or ``model_id`` in the body or the query), so a new one must be given a request below.
"""

import pytest
from fastapi.testclient import TestClient

from api import app as app_module
from config import Config
from models.generic.transformers_model import TransformersModel
from .fakes import FakeModel, swapped

OTHER = "someone/other-model"
# Routes that name a model without loading one: they list, filter or edit by name, and refuse nothing.
NAMES_ONLY = {
    "GET /admin/requests", "GET /admin/traces",  # filters of the request history
    "DELETE /models/{model_id}/{task}", "PUT /models/{model_id}/{task}/pin",  # a loaded model's own routes
    "DELETE /admin/model-servers/models/{model_id}", "PUT /admin/model-servers/models/{model_id}",  # the operator's entries
    "POST /admin/model-servers/probe",
}
# Every route that loads or runs the model it is given, with a request the model's name is the only fault of.
REQUESTS = {
    "POST /inference": dict(json={"model_id": OTHER, "task": "text-generation", "input": {"text": "hi"}}),
    "POST /inference/batch": dict(json={"model_id": OTHER, "task": "text-generation", "inputs": [{"text": "hi"}]}),
    "POST /vision-language": dict(json={"model_id": OTHER, "input_text": "hi"}),
    "POST /models/load": dict(json={"model_id": OTHER, "task": "text-generation"}),
    "GET /models/estimate": dict(params={"model_id": OTHER, "task": "text-generation"}),
    "POST /v1/chat/completions": dict(json={"model": OTHER, "messages": [{"role": "user", "content": "hi"}]}),
    "POST /v1/completions": dict(json={"model": OTHER, "prompt": "hi"}),
    "POST /v1/embeddings": dict(json={"model": OTHER, "input": "hi"}),
    "POST /v1/rerank": dict(json={"model": OTHER, "query": "q", "documents": ["a"]}),
    "POST /v1/audio/transcriptions": dict(data={"model": OTHER}, files={"file": ("a.wav", b"RIFF", "audio/wav")}),
    "POST /v1/decisions": dict(json={"model": OTHER, "state": "s", "questions": {"ok": {"type": "noul", "instructions": "Is it fine?"}}}),
    "POST /collections": dict(json={"name": "c1", "model": OTHER}),
    "POST /admin/runs": dict(json={"dataset_id": "d1", "model_id": OTHER}),
    "POST /admin/runs/{run_id}/judge": dict(json={"model": OTHER}),
}


def _names_a_model(spec, op):
    schemas = spec["components"]["schemas"]

    def deref(s):
        while "$ref" in s:
            s = schemas[s["$ref"].split("/")[-1]]
        return s

    if any(p["name"] in ("model", "model_id") for p in op.get("parameters", [])):
        return True
    for media in op.get("requestBody", {}).get("content", {}).values():
        s = deref(media["schema"])
        for v in [deref(x) for x in s.get("oneOf", s.get("anyOf", []))] or [s]:
            if {"model", "model_id"} & set(v.get("properties", {})):
                return True
    return False


def model_routes():
    spec = app_module.app.openapi()
    return sorted(f"{method.upper()} {path}" for path, ops in spec["paths"].items()
                  for method, op in ops.items() if _names_a_model(spec, op))


def test_every_route_naming_a_model_is_listed_here():
    unlisted = [r for r in model_routes() if r not in NAMES_ONLY and r not in REQUESTS]
    assert unlisted == [], f"routes that take a model: add a request (or NAMES_ONLY) for {unlisted}"


@pytest.mark.parametrize("route", sorted(REQUESTS))
def test_a_model_outside_the_list_is_forbidden(route, monkeypatch):
    monkeypatch.setattr(Config, "ALLOWED_MODELS", ["allowed/model"])
    method, path = route.split(" ", 1)
    path = path.replace("{run_id}", "r1")
    with swapped(TransformersModel, FakeModel), TestClient(app_module.app) as client:
        r = client.request(method, path, **REQUESTS[route])
    assert r.status_code == 403, f"{route}: {r.status_code} {r.text[:200]}"
    assert "ALLOWED_MODELS" in r.json()["detail"]
