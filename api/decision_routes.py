"""
Perplexity-compatible decision route, so clients of Perplexity's Decisions
API (https://docs.perplexity.ai/docs/decisions/quickstart) can run the same
requests against local vegaml models.

POST /v1/decisions takes a state and named noul / choice / score questions
and returns probabilities instead of text. Differences from Perplexity:

- models are the toolkit's (vega-0.8b, vega-4b), not pplx-decider-*;
- validation errors are FastAPI's 422 {"detail": ...}, like every other route;
- an image state is one image with no text parts, since vegaml reads the
  image in place of the state text;
- each answer also carries vegaml's abstain, prediction_set and unbound.
"""

import json
from typing import Any, Dict, List, Literal, Optional, Tuple, Union

from fastapi import APIRouter
from pydantic import BaseModel, Field, model_validator

from config import Config
from errors import Refused, ServiceError, TooLarge
from models import tasks
from . import deps

router = APIRouter(prefix="/v1", dependencies=deps.CLIENT)

DECISION_TASK = "decision"
# The formats vegaml reads (models/decision/vega.py).
IMAGE_FORMATS = ("png", "jpeg", "webp")
# Perplexity's yes/no type is "noul"; vegaml calls it "boolean".
VEGA_TYPES = {"noul": "boolean", "choice": "choice", "score": "score"}


class DecisionQuestion(BaseModel):
    model_config = {"extra": "forbid"}

    type: Literal["noul", "choice", "score"]
    instructions: Optional[str] = Field(None, max_length=Config.MAX_INPUT_CHARS)
    criteria: Optional[Union[Dict[str, Optional[str]], List[str]]] = None

    @model_validator(mode="after")
    def check_criteria(self):
        crit = self.criteria
        if self.type == "noul":
            if not self.instructions and not crit:
                raise ValueError("a noul question needs instructions, criteria, or both")
            if crit is not None and (not isinstance(crit, dict) or not set(crit) <= {"true", "false"}):
                raise ValueError('noul criteria must be an object with "true" and/or "false" keys')
        elif self.type == "choice":
            if not isinstance(crit, dict) or not 1 <= len(crit) <= 255:
                raise ValueError("choice criteria must be an object of 1 to 255 options")
        elif not isinstance(crit, list) or not 1 <= len(crit) <= 10:
            raise ValueError("score criteria must be an array of 1 to 10 levels")
        return self


class DecisionRequest(BaseModel):
    # Perplexity rejects unknown top-level fields; so do we.
    model_config = {"extra": "forbid", "protected_namespaces": ()}

    model: str = Field(..., min_length=1, max_length=256)
    state: Union[str, Dict[str, Any], List[Any]]
    questions: Dict[str, DecisionQuestion] = Field(..., min_length=1, max_length=128)


def _is_content_parts(state: Any) -> bool:
    return (
        isinstance(state, list)
        and bool(state)
        and all(isinstance(p, dict) and p.get("type") in ("text", "image_url") for p in state)
    )


def _image_base64(part: Dict[str, Any]) -> str:
    """The checked base64 of an image part; the decision input (models/tasks.py) decodes it."""
    url = (part.get("image_url") or {}).get("url") or ""
    try:
        return tasks.image_data_url(url, IMAGE_FORMATS)
    except ServiceError:
        raise  # MediaTooLarge: 413
    except ValueError as e:
        raise Refused(str(e))


def split_state(state: Any) -> Tuple[Any, Optional[str]]:
    """Return (state for vegaml, base64 image or None), the image checked to decode."""
    if not _is_content_parts(state):
        return state, None
    images = [p for p in state if p["type"] == "image_url"]
    texts = [str(p.get("text", "")) for p in state if p["type"] == "text"]
    if not images:
        return "\n".join(texts), None
    if len(images) > 1 or texts:
        raise Refused("An image state must be a single image with no text parts")
    return None, _image_base64(images[0])


def to_vega_questions(questions: Dict[str, DecisionQuestion]) -> Dict[str, Dict[str, Any]]:
    return {
        name: {"type": VEGA_TYPES[q.type], "instructions": q.instructions or "", "criteria": q.criteria}
        for name, q in questions.items()
    }


def to_perplexity_answer(question: DecisionQuestion, answer: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {"type": question.type}
    if question.type == "noul":
        out["noul"] = answer["boolean"]
    elif question.type == "choice":
        out["choice"] = answer["choice"]
        out["confidence"] = answer["confidence"]
        out["probabilities"] = answer["probs"]
    else:
        out["score"] = answer["expected_score"]
        out["confidence"] = answer["confidence"]
        out["legend"] = {str(i): level for i, level in enumerate(question.criteria)}
        out["probabilities"] = answer["probs"]
    # vegaml extras: whether the engine stands behind the answer, the
    # conformal answer set, and whether the input looked out of distribution.
    for key in ("abstain", "prediction_set", "unbound"):
        if key in answer:
            out[key] = answer[key]
    return out


@router.post("/decisions")
async def decisions(manager: deps.Manager, request: DecisionRequest):
    state, image = split_state(request.state)
    if image is None:
        text = state if isinstance(state, str) else json.dumps(state, ensure_ascii=False)
        if len(text) > Config.MAX_INPUT_CHARS:
            raise TooLarge("state is longer than MAX_INPUT_CHARS")

    input_data = {"state": state, "image": image, "questions": to_vega_questions(request.questions)}
    output = await manager.run_inference(request.model, DECISION_TASK, input_data)

    result = output["result"]
    return {
        "model": request.model,
        "answers": {name: to_perplexity_answer(q, result.answers[name]) for name, q in request.questions.items()},
        "usage": {"input_tokens": result.n_tokens, "output_tokens": 0},
    }
