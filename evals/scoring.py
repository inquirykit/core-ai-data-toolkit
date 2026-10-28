"""
How a run's items are scored, on Pydantic Evals.

Each item is a case: its id is the case's input (the task looks the stored
output up, as text), the example's reference is the expected output, and the
words of the example's input ride in the metadata. One evaluator gives each
case a score (and maybe a label) with its reason, and the report's cases
become score rows, one per item. An item the model failed on scores None
with that reason; one the evaluator failed on, likewise.

METRICS need no model. The judge is a chat model behind an OpenAI-compatible
endpoint (JUDGE_* give the defaults; no base URL means this service's own
/v1, reached in-process), asked by pydantic-evals' grading agent for a
structured verdict (a score 0–1, pass or fail, and the reason), so there is
no reply parsing here; a judge call that fails is asked again, with backoff,
and then recorded on that item alone. For pairs the judge is asked twice,
with the outputs in both orders, and disagreement is a tie, so its
preference for the first (or second) answer it reads cancels out.
"""

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Tuple

import httpx
from pydantic import BaseModel, Field
from pydantic_ai import Agent
from pydantic_ai.models import Model
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.retries import RetryConfig
from pydantic_ai.settings import ModelSettings
from pydantic_evals import Case, Dataset
from pydantic_evals.evaluators import EvaluationReason, Evaluator, EvaluatorContext
from pydantic_evals.evaluators.llm_as_a_judge import judge_input_output, judge_input_output_expected
from pydantic_evals.lifecycle import CaseLifecycle
from tenacity import stop_after_attempt, wait_exponential

from .capture import input_text, output_text

# pydantic-ai advertises Logfire on the first agent run of a process; this is a service.
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

# What every evaluator here returns: the case's score (with its reason) and, when it has one, a label.
SCORE, LABEL = "score", "label"


# --------------------------------------------------------------- metrics
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold().strip(".!?\"' ")


def reference_text(reference: Any) -> Optional[str]:
    if reference is None:
        return None
    if isinstance(reference, str):
        return reference
    if isinstance(reference, dict):
        for key in ("text", "answer", "label", "output"):
            if isinstance(reference.get(key), str):
                return reference[key]
    return json.dumps(reference, ensure_ascii=False, default=str)


@dataclass(repr=False)
class ExactMatch(Evaluator):
    """1 when the output equals the reference, ignoring case and spacing; nothing without a reference."""

    def evaluate(self, ctx: EvaluatorContext) -> Dict[str, float]:
        if ctx.expected_output is None:
            return {}
        return {SCORE: float(_norm(ctx.output) == _norm(ctx.expected_output))}


@dataclass(repr=False)
class Contains(Evaluator):
    """1 when the output contains the reference, ignoring case and spacing; nothing without a reference."""

    def evaluate(self, ctx: EvaluatorContext) -> Dict[str, float]:
        if ctx.expected_output is None:
            return {}
        return {SCORE: float(_norm(ctx.expected_output) in _norm(ctx.output))}


@dataclass(repr=False)
class JsonValid(Evaluator):
    """1 when the output parses as JSON (code fences allowed)."""

    def evaluate(self, ctx: EvaluatorContext) -> Dict[str, float]:
        body = re.sub(r"^```(?:json)?\s*|\s*```$", "", ctx.output.strip())
        try:
            json.loads(body)
            return {SCORE: 1.0}
        except ValueError:
            return {SCORE: 0.0}


METRICS: Dict[str, Tuple[str, Callable[[], Evaluator]]] = {
    "exact_match": ("The output equals the reference, ignoring case and spacing", ExactMatch),
    "contains": ("The output contains the reference", Contains),
    "json_valid": ("The output parses as JSON", JsonValid),
}


# ------------------------------------------------------------------ judge
DEFAULT_RUBRIC = (
    "The answer responds to the prompt correctly and completely, follows its instructions, and is no more "
    "verbose than needed. If a reference answer is given, the answer agrees with it in substance."
)
SETTINGS = ModelSettings(temperature=0.0)
# A judge call that fails (a busy or flaky endpoint) is asked again, twice, with backoff; then the item records it.
JUDGE_RETRY = RetryConfig(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=1, min=1, max=8), reraise=True)


@dataclass
class Judge:
    model: str
    # Empty: this service's own /v1.
    base_url: str = ""
    api_key: Optional[str] = None
    rubric: str = DEFAULT_RUBRIC

    @property
    def name(self) -> str:
        return f"{self.model} @ {self.base_url}" if self.base_url else self.model


# Makes the HTTP client that reaches this service's own API in-process (api/app.py gives it to the run executor).
LocalClient = Callable[[], httpx.AsyncClient]


def judge_model(judge: Judge, local: Optional[LocalClient] = None) -> Model:
    """The pydantic-ai model that asks the judge: an OpenAI-compatible endpoint, or this service's own /v1."""
    if judge.base_url:
        provider = OpenAIProvider(base_url=judge.base_url, api_key=judge.api_key or "none")
    elif local is not None:
        provider = OpenAIProvider(base_url="http://toolkit/v1", api_key=judge.api_key or "none", http_client=local())
    else:
        raise ValueError("The judge needs a base URL: this service cannot ask itself here")
    return OpenAIChatModel(judge.model, provider=provider)


def _clamp(score: float) -> float:
    return max(0.0, min(1.0, float(score)))


@dataclass(repr=False)
class RubricJudge(Evaluator):
    """The judge's score 0–1 for one output against the rubric (and the reference, when the example has one)."""

    judge: Judge
    model: Model

    async def evaluate(self, ctx: EvaluatorContext) -> Dict[str, Any]:
        prompt = ctx.metadata["prompt"]
        if ctx.expected_output is None:
            graded = await judge_input_output(prompt, ctx.output, self.judge.rubric, self.model, SETTINGS)
        else:
            graded = await judge_input_output_expected(prompt, ctx.output, ctx.expected_output, self.judge.rubric, self.model, SETTINGS)
        return {SCORE: EvaluationReason(value=_clamp(graded.score), reason=graded.reason), LABEL: "pass" if graded.pass_ else "fail"}

    def build_serialization_arguments(self) -> Dict[str, Any]:
        return {"judge": self.judge.name}


class Verdict(BaseModel):
    winner: Literal["A", "B", "tie"]
    reason: str = Field(description="One or two sentences")


_pair_agent: Agent[None, Verdict] = Agent(
    name="judge_pair",
    system_prompt=(
        "You are comparing two models' answers to the same prompt by a user-specified rubric. Say which answer "
        "meets the rubric better, or \"tie\" when neither does, with a one or two sentence reason."
    ),
    output_type=Verdict,
)

PAIR_PROMPT = """<Rubric>{rubric}</Rubric>
<Prompt>{prompt}</Prompt>{reference}
<AnswerA>{a}</AnswerA>
<AnswerB>{b}</AnswerB>"""


@dataclass(repr=False)
class PairJudge(Evaluator):
    """Whether the output beats the other run's (the case's ``other``): win 1, tie 0.5, loss 0; asked in both orders."""

    judge: Judge
    model: Model

    async def evaluate(self, ctx: EvaluatorContext) -> Dict[str, Any]:
        prompt, other = ctx.metadata["prompt"], ctx.metadata["other"]
        reference = f"\n<Reference>{ctx.expected_output}</Reference>" if ctx.expected_output is not None else ""
        first = await self._ask(prompt, reference, ctx.output, other)
        second = await self._ask(prompt, reference, other, ctx.output)
        # In the second round the output was B, so a B there is a vote for it.
        votes = (first.winner, {"A": "B", "B": "A"}.get(second.winner, "tie"))
        label = "win" if votes == ("A", "A") else "loss" if votes == ("B", "B") else "tie"
        reason = first.reason if votes[0] == votes[1] else f"The judge disagreed with itself when the order was swapped. {first.reason}"
        return {SCORE: EvaluationReason(value={"win": 1.0, "tie": 0.5, "loss": 0.0}[label], reason=reason), LABEL: label}

    async def _ask(self, prompt: str, reference: str, a: str, b: str) -> Verdict:
        text = PAIR_PROMPT.format(rubric=self.judge.rubric, prompt=prompt, reference=reference, a=a, b=b)
        return (await _pair_agent.run(text, model=self.model, model_settings=SETTINGS)).output

    def build_serialization_arguments(self) -> Dict[str, Any]:
        return {"judge": self.judge.name}


# ---------------------------------------------------------------- scoring
def _failed(item: Dict[str, Any]) -> bool:
    return item.get("status") != "ok" or item.get("output") is None


async def score_items(items: List[Dict[str, Any]], evaluator: Evaluator, *, others: Optional[Dict[str, Dict[str, Any]]] = None,
                      concurrency: Optional[int] = None, retry: Optional[RetryConfig] = None,
                      progress: Optional[Callable[[int], Awaitable[None]]] = None) -> List[Dict[str, Any]]:
    """One score row (item_id, example_id, value, label, rationale) per item, by ``evaluator``.

    ``others`` (the other run's items by example id) makes it pairwise: an example the other run never answered is
    skipped, and one either model failed on is decided without asking. ``progress`` hears how many are scored so far.
    """
    rows: List[Dict[str, Any]] = []
    cases: List[Case] = []
    outputs: Dict[str, str] = {}
    by_id = {item["id"]: item for item in items}
    for item in items:
        row = {"item_id": item["id"], "example_id": item["example_id"], "value": None, "label": None, "rationale": None}
        other = None
        if others is not None:
            other = others.get(item["example_id"])
            if other is None:
                continue
        failed, other_failed = _failed(item), other is not None and _failed(other)
        if failed or other_failed:
            if failed and other_failed:
                row["rationale"] = "Both models failed on this example"
            elif failed:
                row.update(rationale="This model failed on this example", **({"value": 0.0, "label": "loss"} if other else {}))
            else:
                row.update(value=1.0, label="win", rationale="The other model failed on this example")
            rows.append(row)
            continue
        outputs[item["id"]] = output_text(item["output"])
        cases.append(Case(name=item["id"], inputs=item["id"], expected_output=reference_text(item.get("reference")),
                          metadata={"prompt": input_text(item["input"]), "other": output_text(other["output"]) if other else None}))
    if not cases:
        return rows

    done = 0

    class Counting(CaseLifecycle):
        async def teardown(self, result: Any) -> None:
            nonlocal done
            done += 1
            if progress:
                await progress(done)

    report = await Dataset(name="run", cases=cases, evaluators=[evaluator]).evaluate(
        lambda item_id: outputs[item_id], progress=False, max_concurrency=concurrency, retry_evaluators=retry, lifecycle=Counting)
    for case in report.cases:
        row = {"item_id": case.name, "example_id": by_id[case.name]["example_id"], "value": None, "label": None, "rationale": None}
        score, label = case.scores.get(SCORE), case.labels.get(LABEL)
        if score is not None:
            row.update(value=float(score.value), rationale=score.reason)
        elif case.evaluator_failures:
            row["rationale"] = f"The judge failed: {case.evaluator_failures[0].error_message}"[:1000]
        if label is not None:
            row["label"] = label.value
        rows.append(row)
    return rows
