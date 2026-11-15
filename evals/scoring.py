"""
How a run's items are scored, on Pydantic Evals.

Each item is a case: its id is the case's input (the task looks the stored
output up, as text), the example's reference is the expected output, and the
words of the example's input ride in the metadata. One evaluator gives each
case a score (and maybe a label) with its reason, and the report's cases
become score rows, one per item. An item the model failed on scores None
with that reason; one the evaluator failed on, likewise.

METRICS need no model: they compare the output's text with the reference's
(word error rate is the one lower-is-better), or a ranking's documents with
the relevant ones the reference names. The classification report is the
run's, not an item's: precision and a confusion matrix are no sums of
per-item scores, so it is worked out from the items when the run is read.

The judge is a chat model asked through this service's own /v1, reached
in-process, so it is any model the service serves: one it runs, or one on
another server through a model server entry (JUDGE_MODEL is the default).
Pydantic-evals' grading agent asks it for a structured verdict (a score 0–1, pass or fail, and the reason), so there is
no reply parsing here; a judge call that fails is asked again, with backoff,
and then recorded on that item alone. For pairs the judge is asked twice,
with the outputs in both orders, and disagreement is a tie, so its
preference for the first (or second) answer it reads cancels out.
"""

import json
import math
import os
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Literal, Optional, Sequence, Tuple

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

from models.capture import input_text, output_text

from .store import reference_text

# pydantic-ai advertises Logfire on the first agent run of a process; this is a service.
os.environ.setdefault("PYDANTIC_AI_NO_BANNER", "1")

# What every evaluator here returns: the case's score (with its reason) and, when it has one, a label.
SCORE, LABEL = "score", "label"


# --------------------------------------------------------------- metrics
def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip().casefold().strip(".!?\"' ")


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


def _words(text: str) -> List[str]:
    """The words of ``text``, lowercased, without punctuation: what a word error rate counts."""
    return re.findall(r"\w+(?:['’]\w+)*", text.casefold())


def edit_distance(a: Sequence[Any], b: Sequence[Any]) -> int:
    """The fewest insertions, deletions and substitutions that turn ``a`` into ``b`` (Levenshtein)."""
    row = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        diagonal, row[0] = row[0], i
        for j, y in enumerate(b, 1):
            diagonal, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, diagonal + (x != y))
    return row[-1]


@dataclass(repr=False)
class WordErrorRate(Evaluator):
    """The words to insert, delete or change to turn the output into the reference, per word of the reference,
    ignoring case and punctuation: 0 is a perfect transcript, and it can pass 1; nothing without a reference."""

    def evaluate(self, ctx: EvaluatorContext) -> Dict[str, float]:
        reference = _words(ctx.expected_output or "")
        if not reference:
            return {}
        return {SCORE: edit_distance(reference, _words(ctx.output)) / len(reference)}


def _ranking(ctx: EvaluatorContext) -> Optional[Tuple[List[Any], set]]:
    """The documents a rerank output put first (their indexes, best first) and the relevant ones the reference names
    (a list of indexes, or {"relevant": [...]}); None for an output that is no ranking or a reference that names none."""
    result, relevant = ctx.metadata.get("result"), ctx.metadata.get("expected")
    if isinstance(relevant, dict):
        relevant = relevant.get("relevant")
    results = result.get("results") if isinstance(result, dict) else None
    relevant = {i for i in relevant if type(i) is int} if isinstance(relevant, list) else None
    if not isinstance(results, list) or not relevant:
        return None
    return [r.get("index") for r in results if isinstance(r, dict)], relevant


@dataclass(repr=False)
class ReciprocalRank(Evaluator):
    """1 over the rank of the first relevant document: 1 when it comes first, 0 when none is ranked."""

    def evaluate(self, ctx: EvaluatorContext) -> Dict[str, float]:
        ranking = _ranking(ctx)
        if ranking is None:
            return {}
        order, relevant = ranking
        return {SCORE: next((1 / rank for rank, index in enumerate(order, 1) if index in relevant), 0.0)}


@dataclass(repr=False)
class NDCG(Evaluator):
    """nDCG@10: the relevant documents among the first ten, the higher the more, against the best order possible."""

    def evaluate(self, ctx: EvaluatorContext) -> Dict[str, float]:
        ranking = _ranking(ctx)
        if ranking is None:
            return {}
        order, relevant = ranking
        gain = sum(1 / math.log2(rank + 1) for rank, index in enumerate(order[:10], 1) if index in relevant)
        best = sum(1 / math.log2(rank + 1) for rank in range(1, min(10, len(relevant)) + 1))
        return {SCORE: gain / best}


METRICS: Dict[str, Tuple[str, Callable[[], Evaluator]]] = {
    "exact_match": ("The output equals the reference, ignoring case and spacing", ExactMatch),
    "contains": ("The output contains the reference", Contains),
    "json_valid": ("The output parses as JSON", JsonValid),
    "wer": ("Word error rate of a transcript against the reference, lower is better", WordErrorRate),
    "mrr": ("A ranking's reciprocal rank of the first relevant document (the reference lists their indexes)",
            ReciprocalRank),
    "ndcg_at_10": ("A ranking's nDCG@10 against the relevant documents (the reference lists their indexes)", NDCG),
}
# The metrics where less is better; the pages read their summary the other way round.
LOWER_IS_BETTER = {"wer"}


def summarize_scores(scores: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Per metric: how many items were scored, the mean value, and for pairwise judgments the wins, ties and losses;
    ``lower_is_better`` for a metric where less is better."""
    summary: Dict[str, Dict[str, Any]] = {}
    for s in scores:
        m = summary.setdefault(s["metric"], {"count": 0, "scored": 0, "mean": None, "_sum": 0.0, "wins": 0, "ties": 0, "losses": 0})
        m["count"] += 1
        if s.get("value") is not None:
            m["scored"] += 1
            m["_sum"] += s["value"]
        if s.get("against"):
            label = s.get("label")
            if label == "win":
                m["wins"] += 1
            elif label == "tie":
                m["ties"] += 1
            elif label == "loss":
                m["losses"] += 1
    for metric, m in summary.items():
        m["mean"] = m.pop("_sum") / m["scored"] if m["scored"] else None
        if not m["wins"] and not m["ties"] and not m["losses"]:
            for k in ("wins", "ties", "losses"):
                m.pop(k)
        if metric in LOWER_IS_BETTER:
            m["lower_is_better"] = True
    return summary


# A confusion matrix is given for at most this many labels (a row and a column each).
MAX_CONFUSION_LABELS = 50


def classification_report(items: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """For a run whose outputs are labels (text or zero-shot classification), over its items with a reference:
    accuracy, each label's precision, recall, F1 and support (its references), their mean F1 (macro), and the
    confusion matrix (a row per reference label, a column per predicted one, in ``labels``' order). Labels match
    ignoring case and spacing, each shown as a reference spells it (else as predicted). None when no item has both a
    label and a reference."""
    raw: List[Tuple[str, str]] = []
    for item in items:
        output, reference = item.get("output"), reference_text(item.get("reference"))
        if item.get("status") == "ok" and isinstance(output, dict) and isinstance(output.get("label"), str) \
                and reference is not None:
            raw.append((reference, output["label"]))
    if not raw:
        return None
    shown: Dict[str, str] = {}
    for spelling in [r for r, _ in raw] + [p for _, p in raw]:
        shown.setdefault(_norm(spelling), spelling.strip())
    pairs = [(_norm(r), _norm(p)) for r, p in raw]
    keys = sorted(shown, key=lambda k: shown[k])
    at = {k: i for i, k in enumerate(keys)}
    matrix = [[0] * len(keys) for _ in keys]
    for actual, predicted in pairs:
        matrix[at[actual]][at[predicted]] += 1
    labels = []
    for i, key in enumerate(keys):
        hits, predicted, support = matrix[i][i], sum(row[i] for row in matrix), sum(matrix[i])
        precision = hits / predicted if predicted else 0.0
        recall = hits / support if support else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        labels.append({"label": shown[key], "precision": precision, "recall": recall, "f1": f1, "support": support})
    return {
        "count": len(pairs),
        "accuracy": sum(matrix[i][i] for i in range(len(keys))) / len(pairs),
        "macro_f1": sum(label["f1"] for label in labels) / len(labels),
        "labels": labels,
        "confusion": matrix if len(keys) <= MAX_CONFUSION_LABELS else None,
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
    rubric: str = DEFAULT_RUBRIC

    @property
    def name(self) -> str:
        return self.model


# Makes the HTTP client that reaches this service's own API in-process, with the admin's key (api/app.py gives it to
# the run executor).
LocalClient = Callable[[], httpx.AsyncClient]


def judge_model(judge: Judge, local: LocalClient) -> Model:
    """The pydantic-ai model that asks the judge, through this service's own /v1."""
    # The local client signs its requests; the provider's key is a placeholder it must be given.
    provider = OpenAIProvider(base_url="http://toolkit/v1", api_key="in-process", http_client=local())
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
        # The output and reference as they are ride along too, for the metrics that read a ranking.
        cases.append(Case(name=item["id"], inputs=item["id"], expected_output=reference_text(item.get("reference")),
                          metadata={"prompt": input_text(item["input"]), "other": output_text(other["output"]) if other else None,
                                    "result": item["output"], "expected": item.get("reference")}))
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
