"""ALLOWED_MODELS holds on every route a client names a model to: a route added without the check fails here.

The model manager refuses a model outside the list on every load, run and estimate (ModelManager.check_allowed), so
this test sends each such route a model that is not allowed and expects 403. The routes are found from the API
schema (a ``model`` or ``model_id`` in the query or anywhere in the body, as a /db vector field's model is in a
table's fields), so a new one must be given a request below.
"""

import pytest
from fastapi.testclient import TestClient

from api import app as app_module
from core.config import Config
from models.families.generic.transformers_model import TransformersModel
from .fakes import FakeModel, swapped

OTHER = "someone/other-model"
# Routes that name a model without loading one: they list, filter or edit by name, and refuse nothing.
NAMES_ONLY = {
    "GET /admin/requests",  # a filter of the request log
    "DELETE /models/{model_id}/{task}", "PUT /models/{model_id}/{task}/pin",  # a loaded model's own routes
    "DELETE /admin/model-servers/models/{model_id}", "PUT /admin/model-servers/models/{model_id}",  # the operator's entries
    "POST /admin/model-servers/probe",
    # A saved chat names the models its columns used.
    "POST /playground/conversations", "PUT /playground/conversations/{conversation_id}",
}
# A /db table's text, and its vector field embedded from it with a model.
VECTOR_FIELDS = [{"name": "text", "type": "string"}, {"name": "v", "type": "vector", "model": OTHER, "from": "text"}]
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
    "POST /db/{workspace}": dict(json={"name": "t1", "fields": VECTOR_FIELDS}),
    "PUT /db/{workspace}/{table}/_schema": dict(json={"fields": VECTOR_FIELDS}),
    "POST /admin/runs": dict(json={"dataset_id": "d1", "model_id": OTHER}),
    "POST /admin/runs/{run_id}/judge": dict(json={"model": OTHER}),
}


def _names_a_model(spec, op):
    schemas = spec["components"]["schemas"]

    def deref(s):
        while "$ref" in s:
            s = schemas[s["$ref"].split("/")[-1]]
        return s

    def names(s, seen):
        """Whether the schema, or one within it (a property's, a list's items, a choice's), has a model."""
        s = deref(s)
        if id(s) in seen:
            return False
        seen.add(id(s))
        if {"model", "model_id"} & set(s.get("properties", {})):
            return True
        inner = [*s.get("properties", {}).values(), *s.get("oneOf", []), *s.get("anyOf", []), *s.get("allOf", [])]
        if isinstance(s.get("items"), dict):
            inner.append(s["items"])
        if isinstance(s.get("additionalProperties"), dict):
            inner.append(s["additionalProperties"])
        return any(names(x, seen) for x in inner)

    if any(p["name"] in ("model", "model_id") for p in op.get("parameters", [])):
        return True
    return any(names(media["schema"], set()) for media in op.get("requestBody", {}).get("content", {}).values())


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
    path = path.replace("{run_id}", "r1").replace("{workspace}", "default").replace("{table}", "t1")
    with swapped(TransformersModel, FakeModel), TestClient(app_module.app) as client:
        r = client.request(method, path, **REQUESTS[route])
    assert r.status_code == 403, f"{route}: {r.status_code} {r.text[:200]}"
    assert "ALLOWED_MODELS" in r.json()["detail"]
