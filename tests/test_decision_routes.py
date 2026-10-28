import base64
import io

import pytest
from fastapi.testclient import TestClient

from api import app as app_module
from config import Config
from models import tasks
from models.decision.vega import VegaDecisionModel
from tests.fakes import FakeModel, swapped


class FakeVega(FakeModel):
    """Answers like vegaml's engine: first option wins, probabilities fixed."""

    calls = []

    async def predict(self, request):
        # The image as the model reads it: decoded (model_dump() would re-encode it).
        input_data = {**request.model_dump(), "image": request.image}
        FakeVega.calls.append(input_data)
        answers = {}
        for name, q in input_data["questions"].items():
            common = {"confidence": 0.8, "abstain": False, "unbound": False}
            if q["type"] == "boolean":
                answers[name] = {**common, "boolean": 0.8, "probs": {"False": 0.2, "True": 0.8},
                                 "prediction_set": [True]}
            elif q["type"] == "choice":
                options = list(q["criteria"])
                probs = {o: (0.8 if i == 0 else 0.2 / (len(options) - 1)) for i, o in enumerate(options)}
                answers[name] = {**common, "choice": options[0], "probs": probs, "prediction_set": [options[0]]}
            else:
                answers[name] = {**common, "score": 1, "expected_score": 1.2,
                                 "probs": {str(i): 1 / len(q["criteria"]) for i in range(len(q["criteria"]))},
                                 "prediction_set": [1]}
        return {"answers": answers, "n_tokens": 42}


@pytest.fixture(scope="module")
def client():
    with swapped(VegaDecisionModel, FakeVega), TestClient(app_module.app) as c:
        yield c


@pytest.fixture(autouse=True)
def reset_calls():
    FakeVega.calls.clear()


def decide(client, **overrides):
    payload = {
        "model": "vega-0.8b",
        "state": {"subject": "Invoice overdue", "body": "Third notice. Pay now or I cancel."},
        "questions": {
            "team": {"type": "choice", "instructions": "Which team handles this?",
                     "criteria": {"billing": "payments", "technical": "bugs", "sales": None}},
            "churn": {"type": "noul", "instructions": "Is the customer about to leave?"},
            "urgency": {"type": "score", "instructions": "How urgent is it?",
                        "criteria": ["not urgent", "soon", "today"]},
        },
        **overrides,
    }
    return client.post("/v1/decisions", json=payload)


def png_data_url() -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def test_answers_come_back_in_perplexity_shape(client):
    r = decide(client)
    assert r.status_code == 200
    body = r.json()
    assert body["model"] == "vega-0.8b"
    assert body["usage"] == {"input_tokens": 42, "output_tokens": 0}

    team = body["answers"]["team"]
    assert team["type"] == "choice"
    assert team["choice"] == "billing"
    assert team["confidence"] == 0.8
    assert team["probabilities"]["billing"] == 0.8

    churn = body["answers"]["churn"]
    assert churn == {"type": "noul", "noul": 0.8, "abstain": False, "prediction_set": [True], "unbound": False}

    urgency = body["answers"]["urgency"]
    assert urgency["type"] == "score"
    assert urgency["score"] == 1.2
    assert urgency["legend"] == {"0": "not urgent", "1": "soon", "2": "today"}
    assert set(urgency["probabilities"]) == {"0", "1", "2"}


def test_questions_are_translated_to_vegaml(client):
    decide(client)
    questions = FakeVega.calls[0]["questions"]
    assert questions["churn"] == {"type": "boolean", "instructions": "Is the customer about to leave?",
                                  "criteria": None}
    assert questions["urgency"]["criteria"] == ["not urgent", "soon", "today"]
    assert FakeVega.calls[0]["state"]["subject"] == "Invoice overdue"
    assert FakeVega.calls[0]["image"] is None


def test_noul_with_only_criteria_gets_empty_instructions(client):
    r = decide(client, questions={"spam": {"type": "noul", "criteria": {"true": "an advert"}}})
    assert r.status_code == 200
    assert FakeVega.calls[0]["questions"]["spam"]["instructions"] == ""


def test_text_parts_are_joined(client):
    r = decide(client, state=[{"type": "text", "text": "line one"}, {"type": "text", "text": "line two"}])
    assert r.status_code == 200
    assert FakeVega.calls[0]["state"] == "line one\nline two"


def test_plain_list_state_is_passed_through(client):
    r = decide(client, state=[{"sku": "A1", "qty": 2}])
    assert r.status_code == 200
    assert FakeVega.calls[0]["state"] == [{"sku": "A1", "qty": 2}]


def test_image_state_is_decoded_to_bytes(client):
    r = decide(client, state=[{"type": "image_url", "image_url": {"url": png_data_url()}}])
    assert r.status_code == 200
    call = FakeVega.calls[0]
    assert call["state"] is None
    assert call["image"].startswith(b"\x89PNG")


@pytest.mark.parametrize("state, status", [
    ([{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}], 400),
    ([{"type": "image_url", "image_url": {"url": "data:image/gif;base64,R0lGOD"}}], 400),
    ([{"type": "image_url", "image_url": {"url": "data:image/png;base64,not base64!"}}], 400),
    ([{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}}], 400),
])
def test_bad_image_states_are_rejected(client, state, status):
    assert decide(client, state=state).status_code == status
    assert FakeVega.calls == []


def test_image_size_follows_max_media_bytes(client, monkeypatch):
    monkeypatch.setattr(Config, "MAX_MEDIA_BYTES", 16)
    r = decide(client, state=[{"type": "image_url", "image_url": {"url": png_data_url()}}])
    assert r.status_code == 413
    assert FakeVega.calls == []


@pytest.mark.parametrize("questions", [
    {},
    {"q": {"type": "noul"}},
    {"q": {"type": "noul", "criteria": {"maybe": "?"}}},
    {"q": {"type": "choice", "instructions": "pick"}},
    {"q": {"type": "choice", "criteria": {f"o{i}": None for i in range(256)}}},
    {"q": {"type": "score", "criteria": {"a": "b"}}},
    {"q": {"type": "score", "criteria": [str(i) for i in range(11)]}},
    {"q": {"type": "rank", "criteria": ["a"]}},
    {"q": {"type": "noul", "instructions": "x", "extra": 1}},
    {f"q{i}": {"type": "noul", "instructions": "x"} for i in range(129)},
])
def test_invalid_questions_are_rejected(client, questions):
    assert decide(client, questions=questions).status_code == 422


def test_null_state_and_unknown_fields_are_rejected(client):
    assert decide(client, state=None).status_code == 422
    assert decide(client, temperature=0).status_code == 422


def test_long_state_is_rejected(client):
    assert decide(client, state="x" * (Config.MAX_INPUT_CHARS + 1)).status_code == 413


def test_allowed_models_is_enforced(client, monkeypatch):
    monkeypatch.setattr(Config, "ALLOWED_MODELS", ["vega-4b"])
    assert decide(client).status_code == 403


def test_vega_models_are_listed_under_decision(client):
    data = client.get("/v1/models").json()["data"]
    tasks = {m["id"]: m["task"] for m in data}
    assert tasks["vega-0.8b"] == "decision"
    assert tasks["vega-4b"] == "decision"


def test_non_vega_model_is_rejected(client):
    assert decide(client, model="gpt2").status_code == 400
    assert FakeVega.calls == []


def test_decision_input_needs_questions_and_one_of_state_or_image():
    contract = tasks.get("decision")
    questions = {"q": {"type": "boolean", "instructions": "x"}}
    with pytest.raises(ValueError):
        contract.parse_input("just text")
    with pytest.raises(ValueError, match="either state or image"):
        contract.parse_input({"questions": questions})
    with pytest.raises(ValueError, match="either state or image"):
        contract.parse_input({"state": "s", "image": "AA==", "questions": questions})
    # Over /inference the image arrives as base64.
    assert contract.parse_input({"image": "iVBORw==", "questions": questions}).image == b"\x89PNG"


def test_open_image_rejects_garbage():
    with pytest.raises(ValueError):
        VegaDecisionModel._open_image(b"not an image")
