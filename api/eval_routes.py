"""
The routes of the /admin Evals section (see evals/).

Datasets gather examples, copied from the traces the request log keeps
(monitoring/) or given directly (or as JSON Lines); a run is one model over a
dataset, scored by metrics or an LLM judge, and two runs compare example by
example. Every route needs the admin key.
"""

import json
from collections import Counter
from typing import Annotated, Any, Dict, List, Optional

from fastapi import APIRouter, Depends, Query
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from core.config import Config
from evals.scoring import DEFAULT_RUBRIC, METRICS, Judge, classification_report, score_items, summarize_scores
from core.errors import NotFound, Refused
from db.tables import decode_cursor, encode_cursor
from . import deps
from . import responses

router = APIRouter(prefix="/admin", tags=["evals"], dependencies=deps.ADMIN)


# -------------------------------------------------------------- datasets
class CreateDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=2000)


Label = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]


class UpdateDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    labels: List[Label] = Field(..., max_length=100, description="The choices a reference is picked from when labeling; "
                                                                  "empty: typed")


class UpdateExample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reference: Any = Field(..., description="The expected answer, as a label or text, or the expected result; null takes "
                                            "it away, so the example is to label again")


class Example(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: Dict[str, Any] = Field(..., description="The request input, as POST /inference takes it")
    reference: Optional[Any] = Field(None, description="The expected answer: text, or the expected result")
    task: Optional[str] = Field(None, max_length=64)
    tags: List[str] = Field(default_factory=list, max_length=16)


class AddExamples(BaseModel):
    model_config = ConfigDict(extra="forbid")

    examples: List[Example] = Field(default_factory=list, max_length=5000)
    trace_ids: List[str] = Field(default_factory=list, max_length=1000, description="Traces to copy in as examples")
    jsonl: Optional[str] = Field(None, max_length=50_000_000, description="Examples as JSON Lines, as the export writes them")


@router.get("/datasets", **responses.answering(responses.Datasets))
async def list_datasets(store: deps.Evals):
    """Every dataset."""
    return {"datasets": await store.datasets()}


@router.post("/datasets", **responses.answering(responses.Dataset))
async def create_dataset(store: deps.Evals, body: CreateDataset):
    """Make an empty dataset."""
    return await store.create_dataset(body.name, body.description)


@router.get("/datasets/{dataset_id}", **responses.answering(responses.DatasetDetail))
async def get_dataset(
    store: deps.Evals,
    runs: deps.Runs,
    dataset_id: str,
):
    """A dataset with its runs and what each run is doing now."""
    dataset = await store.dataset(dataset_id)
    run_list = await runs.runs(dataset_id)
    activity = await runs.activity(run_list)
    return {**dataset, "run_list": [{**r, "activity": activity.get(r["id"])} for r in run_list]}


@router.patch("/datasets/{dataset_id}", **responses.answering(responses.Dataset))
async def update_dataset(store: deps.Evals, dataset_id: str, body: UpdateDataset):
    """Set the dataset's label choices."""
    return await store.set_labels(dataset_id, list(dict.fromkeys(body.labels)))


@router.delete("/datasets/{dataset_id}", **responses.answering(responses.Deleted))
async def delete_dataset(store: deps.Evals, runs: deps.Runs, dataset_id: str):
    """The dataset with its examples, runs and scores; a run of it under way, or being judged, is stopped first."""
    for run in await store.runs(dataset_id):
        await runs.delete(run["id"])
    await store.delete_dataset(dataset_id)
    return {"deleted": dataset_id}


@router.get("/datasets/{dataset_id}/examples", **responses.answering(responses.DatasetExamples))
async def list_examples(store: deps.Evals, dataset_id: str, limit: int = Query(100, ge=1, le=1000),
                        cursor: deps.Cursor = None):
    """A page of the dataset's examples, oldest first, and how many it holds."""
    dataset = await store.dataset(dataset_id)
    offset = decode_cursor(cursor) if cursor else 0
    examples = await store.examples(dataset_id, limit + 1, offset)
    after = encode_cursor(offset + limit) if len(examples) > limit else None
    return {**responses.page("examples", examples[:limit], after), "total": dataset["examples"]}


def _parse_jsonl(text: str) -> List[Dict[str, Any]]:
    examples = []
    for n, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except ValueError as e:
            raise Refused(f"Line {n} is not JSON: {e}")
        if not isinstance(row, dict) or not isinstance(row.get("input"), dict):
            raise Refused(f"Line {n} needs an object with an \"input\" object")
        examples.append(Example(input=row["input"], reference=row.get("reference"), task=row.get("task"),
                                tags=list(row.get("tags") or [])[:16]).model_dump())
    return examples


@router.post("/datasets/{dataset_id}/examples", **responses.answering(responses.ExamplesAdded))
async def add_examples(store: deps.Evals, requests: deps.Monitoring, dataset_id: str, body: AddExamples):
    """Add examples given directly, copied from traces, or parsed from JSON Lines; any mix."""
    examples = [e.model_dump() for e in body.examples]
    if body.jsonl:
        examples += _parse_jsonl(body.jsonl)
    if not examples and not body.trace_ids:
        raise Refused("Give examples, trace_ids or jsonl")
    added = 0
    if examples:
        added += await store.add_examples(dataset_id, examples)
    if body.trace_ids:
        if requests is None:
            raise NotFound("The request history is off: MONITORING_RETENTION_DAYS is 0")
        added += await store.add_from_traces(dataset_id, [await requests.trace(id) for id in body.trace_ids])
    return {"added": added}


@router.patch("/datasets/{dataset_id}/examples/{example_id}", **responses.answering(responses.DatasetExample))
async def update_example(store: deps.Evals, dataset_id: str, example_id: str, body: UpdateExample):
    """Label an example: set its reference. A null reference makes it one to label again."""
    return await store.set_reference(dataset_id, example_id, body.reference)


@router.get("/datasets/{dataset_id}/to-label", **responses.answering(responses.ExamplesToLabel))
async def examples_to_label(store: deps.Evals, runs: deps.Runs, dataset_id: str, run_id: Optional[str] = None,
                            limit: int = Query(20, ge=1, le=200),
                            skip: int = Query(0, ge=0, description="How many the labeler passed over at the front")):
    """The examples without a reference, the least confident suggestion first: each with what run ``run_id``
    answered for it, else what the traced model did. A labeled example leaves the queue, so the next page is after
    those passed over (``skip``), not after the page."""
    if run_id is not None and (await runs.run(run_id))["dataset_id"] != dataset_id:
        raise Refused("The run is of another dataset")
    examples, left = await store.to_label(dataset_id, run_id, limit, skip)
    return {"examples": examples, "left": left}


@router.delete("/datasets/{dataset_id}/examples/{example_id}", **responses.answering(responses.Deleted))
async def delete_example(store: deps.Evals, dataset_id: str, example_id: str):
    """Delete one example of the dataset."""
    await store.delete_example(dataset_id, example_id)
    return {"deleted": example_id}


@router.get("/datasets/{dataset_id}/export", response_class=PlainTextResponse)
async def export_dataset(store: deps.Evals, dataset_id: str):
    """The examples as JSON Lines."""
    dataset = await store.dataset(dataset_id)
    text = await store.export(dataset_id)
    name = "".join(c if c.isalnum() or c in "-_" else "_" for c in dataset["name"]) or "dataset"
    return PlainTextResponse(text, media_type="application/jsonl",
                             headers={"Content-Disposition": f'attachment; filename="{name}.jsonl"'})


@router.post("/datasets/{dataset_id}/training-file", **responses.answering(responses.TrainingFile))
async def write_training_file(store: deps.Evals, jobs: deps.Jobs, dataset_id: str):
    """Write the dataset's labeled examples (their input's text and their reference, as text and label) to
    DATA_DIR/datasets/evals/<dataset id>.jsonl, in place of what an earlier call wrote: the train_file of the
    text-classifier job."""
    rows = await store.training_rows(dataset_id)
    if not rows:
        raise Refused("No example has both a text input and a reference: label some first")
    path = f"evals/{dataset_id}.jsonl"
    await jobs.data.write_lines(path, rows)
    return {"path": path, "rows": len(rows), "labels": dict(sorted(Counter(row["label"] for row in rows).items()))}


# ------------------------------------------------------------------ runs
class CreateRun(BaseModel):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())

    dataset_id: str = Field(..., min_length=1, max_length=64)
    model_id: str = Field(..., min_length=1, max_length=256)
    task: Optional[str] = Field(None, max_length=64, description="Unset: each example's own task, else text-generation")
    params: Dict[str, Any] = Field(default_factory=dict, description="Added to every example's input (temperature, max_length...)")
    name: str = Field("", max_length=120)
    webhook_url: Optional[str] = Field(None, max_length=2000, description="POSTed to when the run finishes, besides WEBHOOK_URLS")
    priority: responses.Priority = Field(
        "normal", description="Queued runs start the most urgent first, then one whose model is loaded, then the oldest; "
                              "the run's requests carry it when models compete for room")


class ScoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metrics: List[responses.MetricName] = Field(..., min_length=1)


class JudgeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    against: Optional[str] = Field(None, description="Another run of the same dataset to compare with, pair by pair; unset grades by rubric")
    model: str = Field("", max_length=256, description="The judge model, as /v1 names it (a model server entry's for one on another server); unset: JUDGE_MODEL")
    rubric: str = Field(DEFAULT_RUBRIC, max_length=4000)
    concurrency: int = Field(4, ge=1, le=32, description="Items judged at once")
    priority: responses.Priority = Field(
        "normal", description="Queued judges start the most urgent first; a judge this service runs asks it at this priority")


@router.get("/evals/metrics", **responses.answering(responses.MetricsInfo))
async def list_metrics(store: deps.Evals):
    """The metrics POST /admin/runs/{id}/score takes, and the judge's defaults."""
    # The store goes unused, but this answers 503 with evals off, as the rest do.
    return {
        "metrics": [{"name": name, "description": desc} for name, (desc, _) in METRICS.items()],
        "judge": {"model": Config.JUDGE_MODEL, "rubric": DEFAULT_RUBRIC},
    }


@router.get("/runs", **responses.answering(responses.Runs))
async def list_runs(runs: deps.Runs, dataset_id: Optional[str] = None, limit: int = Query(100, ge=1, le=500),
                    cursor: deps.Cursor = None):
    """The runs (of one dataset with ``dataset_id``), newest first, a page at a time, each with what it is doing
    now."""
    run_list, after = await runs.page(dataset_id, limit, cursor)
    activity = await runs.activity(run_list)
    return responses.page("runs", [{**r, "activity": activity.get(r["id"])} for r in run_list], after)


def _allowed_run(body: CreateRun) -> CreateRun:
    """The body, once its model is allowed: so a 403 comes before any other work."""
    deps.check_model_allowed(body.model_id)
    return body


@router.post("/runs", **responses.answering(responses.Run))
async def create_run(body: Annotated[CreateRun, Depends(_allowed_run)], store: deps.Evals, runs: deps.Runs):
    """Queue a run; it starts when the runs before it have finished (by priority, then loaded model, then age)."""
    run = await store.create_run(body.dataset_id, body.model_id, body.task, body.params, body.name, body.webhook_url)
    await runs.submit(run["id"], body.priority)
    return await runs.run(run["id"])


def _by_item(scores: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Scores grouped by the item they score."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for s in scores:
        out.setdefault(s["item_id"], []).append(s)
    return out


@router.get("/runs/{run_id}", **responses.answering(responses.RunDetail))
async def get_run(store: deps.Evals, runs: deps.Runs, run_id: str):
    """The run, its items (with each example's input and reference), their scores, a summary per metric, and for a
    run whose outputs are labels the classification report (accuracy, per-label precision, recall and F1, the
    confusion matrix) over the items with a reference."""
    run = await runs.run(run_id)
    items = await store.items(run_id)
    scores = await store.scores(run_id)
    by_item = _by_item(scores)
    return {
        **run,
        "activity": (await runs.activity([run])).get(run_id),
        "items": [{**i, "scores": by_item.get(i["id"], [])} for i in items],
        "summary": summarize_scores(scores),
        "classification": classification_report(items),
    }


@router.delete("/runs/{run_id}", **responses.answering(responses.Deleted))
async def delete_run(runs: deps.Runs, run_id: str):
    """Delete a run with its items and scores; one queued or under way is cancelled first."""
    await runs.delete(run_id)
    return {"deleted": run_id}


@router.post("/runs/{run_id}/cancel", **responses.answering(responses.Run))
async def cancel_run(runs: deps.Runs, run_id: str):
    """Cancel the run if it is queued or under way."""
    await runs.cancel(run_id)
    return await runs.run(run_id)


@router.post("/runs/{run_id}/resume", **responses.answering(responses.Run))
async def resume_run(runs: deps.Runs, run_id: str):
    """Queue a failed or cancelled run again, at its priority; the examples it answered are skipped."""
    deps.check_model_allowed((await runs.run(run_id))["model_id"])
    return await runs.resume(run_id)


@router.post("/runs/{run_id}/score", **responses.answering(responses.Scored))
async def score_run(store: deps.Evals, run_id: str, body: ScoreRequest):
    """Score the run's items with metrics that need no model; replaces those metrics' earlier scores."""
    items = await store.items(run_id)
    for metric in body.metrics:
        await store.replace_scores(run_id, metric, await score_items(items, METRICS[metric][1]()))
    return {"summary": summarize_scores(await store.scores(run_id))}


@router.post("/runs/{run_id}/judge", status_code=202, **responses.answering(responses.JudgeStarted))
async def judge_run(store: deps.Evals, runs: deps.Runs, run_id: str, body: JudgeRequest):
    """Queue the LLM judge on the run (two judges at once); GET /admin/runs/{id} shows its progress and then its scores."""
    model = body.model or Config.JUDGE_MODEL
    if not model:
        raise Refused("Name the judge model, or set JUDGE_MODEL")
    deps.check_model_allowed(model)
    run = await runs.run(run_id)
    if run["status"] != "succeeded":
        raise Refused("Only a finished run can be judged")
    if body.against:
        other = await runs.run(body.against)
        if other["dataset_id"] != run["dataset_id"]:
            raise Refused("The runs must be of the same dataset")
        if other["status"] != "succeeded":
            raise Refused("The other run has not finished")
    await runs.judge(run_id, Judge(model=model, rubric=body.rubric), body.against, body.concurrency, body.priority)
    return {"started": run_id, "against": body.against, "judge": model}


@router.get("/runs/compare/{run_a}/{run_b}", **responses.answering(responses.Comparison))
async def compare_runs(store: deps.Evals, runs: deps.Runs, run_a: str, run_b: str):
    """The two runs' outputs and scores side by side, example by example, with each run's summary and
    classification report."""
    a, b = await runs.run(run_a), await runs.run(run_b)
    if a["dataset_id"] != b["dataset_id"]:
        raise Refused("The runs must be of the same dataset")
    items_a, items_b = await store.items(run_a), await store.items(run_b)
    scores_a, scores_b = await store.scores(run_a), await store.scores(run_b)

    sa, sb = _by_item(scores_a), _by_item(scores_b)

    def side(item: Dict[str, Any], scores: Dict[str, List[Dict[str, Any]]]) -> Dict[str, Any]:
        return {**{k: item.get(k) for k in ("id", "output", "status", "error", "duration_ms")}, "scores": scores.get(item["id"], [])}

    other = {i["example_id"]: i for i in items_b}
    rows = []
    for ia in items_a:
        ib = other.get(ia["example_id"])
        rows.append({
            "example_id": ia["example_id"], "input": ia["input"], "reference": ia["reference"],
            "a": side(ia, sa), "b": None if ib is None else side(ib, sb),
        })
    return {"a": {**a, "summary": summarize_scores(scores_a), "classification": classification_report(items_a)},
            "b": {**b, "summary": summarize_scores(scores_b), "classification": classification_report(items_b)},
            "rows": rows}
