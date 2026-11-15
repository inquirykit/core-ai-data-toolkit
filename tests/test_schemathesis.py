"""
The API against its own OpenAPI schema: every operation, fed requests generated from the schema, must not answer
5xx, and must answer with a body the schema describes for its status (an error status any route may answer is the
default response's: {detail}). A few examples per operation, so it is a
smoke test of the contract rather than a fuzzing campaign; `pytest -m "not schemathesis"` skips it.

The models are the fakes the other tests use; operations that reach outside the test database (the network, the
Hub cache, child processes, the .env file) are left out below, each with its reason.
"""

import contextlib

import pytest
import schemathesis
from hypothesis import HealthCheck, Phase, assume, settings
from schemathesis.checks import not_a_server_error, response_schema_conformance
from schemathesis.python import asgi

from api import app as app_module
from models import registry
from models.families.custom.custom_model import CustomModel
from tests.fakes import FakeModel, sample_result, swapped

CHECKS = [not_a_server_error, response_schema_conformance]

# Checks skipped for one operation (a bug in the route or its schema, not in this test): none at the moment.
KNOWN_BUGS: dict = {}

# (method, path regex): operations not called, and why.
EXCLUDED = [
    # Writes the .env file and changes Config for every later test.
    ("PUT", r"^/admin/limits$"),
    ("POST", r"^/admin/setup"),
    ("PUT", r"^/enrichers/[^/]+/secret$"),
    # Probes reach the network; edits write the saved servers file and may start a model server.
    ("PUT|DELETE", r"^/admin/model-servers/"),
    ("POST", r"^/admin/model-servers/probe$"),
    # A run posts to its webhook_url when it ends, and a judge loads its model.
    ("POST", r"^/admin/runs$"),
    ("POST", r"^/admin/runs/[^/]+/(resume|judge)$"),
    # The enrichers call outside APIs.
    ("POST", r"^/enrich$"),
    ("POST", r"^/enrichers/[^/]+/test$"),
    # Multipart uploads, signing, and the origin check the browser extension passes.
    ("GET|POST|PUT", r"^/evidence/"),
    # Jobs are child processes: submitting, cancelling, deleting, schedules (which submit on their own clock), and
    # the uploads and deletes of their input files.
    ("POST", r"^/jobs$"),
    ("POST|DELETE", r"^/jobs/\{id\}"),
    ("POST|DELETE", r"^/jobs/data"),
    ("POST|PUT", r"^/jobs/schedules"),
    ("POST", r"^/queue/webhooks/test$"),
    # Drops a whole workspace, table or edge type.
    ("DELETE", r"^/db/\{workspace\}$"),
    ("DELETE", r"^/db/\{workspace\}/\{table\}$"),
    ("DELETE", r"^/db/\{workspace\}/_edges/\{edge_type\}$"),
]


schema = schemathesis.openapi.from_dict(
    app_module.app.openapi(),
    config=schemathesis.Config.from_dict({"phases": {"coverage": {"enabled": False}}}),
)
for method, path in EXCLUDED:
    schema = schema.exclude(method_regex=f"^({method})$", path_regex=path)
# What from_asgi() sets: requests go to the app in-process.
schema.app = app_module.app


class ContractFake(FakeModel):
    """FakeModel with a result each task's contract accepts (FakeModel echoes the request for tasks it does not know)."""

    async def predict(self, request):
        await super().predict(request)
        if getattr(request, "json_schema", None) is not None:
            # A JSON reply was asked for (/v1/chat/completions' response_format): one that fits any JSON.
            return {"text": "{}"}
        if self.task == "question-answering":
            return {"answer": request.context[:1], "confidence": 0.9, "start_position": 0, "end_position": 1}
        if self.task == "embedding":
            return {"embeddings": [[float(len(t)), 0.5] for t in request.texts], "tokens": 1}
        if self.task == "speech-to-text":
            return {"text": "heard", "language": request.language}
        if self.task == "decision":
            answer = {"boolean": 0.5, "choice": None, "expected_score": 0.0, "confidence": 0.5, "probs": {}}
            return {"answers": {name: answer for name in request.questions}, "n_tokens": 1}
        if self.task == "tabular-prediction":
            return {"predictions": [None] * len(request.rows)}
        if self.task == "timeseries-forecast":
            return {"forecast": []}
        return sample_result(self.task, request)


# The app's own enrichers service, taken at collection, before any test runs: tests/test_evidence.py leaves its
# FakeEnrich on app.state.enrichers for the modules after it.
ENRICHERS = app_module.app.state.enrichers


@pytest.fixture(scope="module", autouse=True)
def fake_models():
    """Every model class but the pure-Python echo answered by ContractFake: no weights, no Hub, no processes."""
    registry.discover()
    with contextlib.ExitStack() as stack:
        stack.enter_context(pytest.MonkeyPatch.context()).setattr(app_module.app.state, "enrichers", ENRICHERS)
        for cls in set(registry.classes()) - {CustomModel}:
            stack.enter_context(swapped(cls, ContractFake))
        yield
        # Schemathesis's pytest plugin ends the app's lifespan after each test, so every test starts the app and opens
        # the database, and shuts them down; this ends the last one, as the modules after this one start their own,
        # as each TestClient does.
        asgi.shutdown_lifespans()



def _not_sent(case):
    """Inputs left out: a reply the fakes cannot give."""
    body = case.body if isinstance(case.body, dict) else {}
    # A JSON reply to a schema of the client's, which ContractFake's {} need not fit (the 502 is then right); a
    # schema that is no JSON Schema is refused before the model runs (422), which the schema documents.
    fmt = body.get("response_format")
    inner = fmt.get("json_schema") if isinstance(fmt, dict) else None
    if isinstance(inner, dict) and inner.get("schema"):
        return True
    return False


@pytest.mark.schemathesis
@schema.parametrize()
@settings(
    max_examples=5,
    phases=[Phase.explicit, Phase.generate],
    deadline=None,
    derandomize=True,
    database=None,
    suppress_health_check=list(HealthCheck),
)
def test_api_answers_as_its_schema_says(case):
    assume(not _not_sent(case))
    case.call_and_validate(checks=CHECKS, excluded_checks=KNOWN_BUGS.get(case.operation.label, []))
