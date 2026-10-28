"""
The routes of traces (Activity & logs' request detail) and the /admin Evals
section (see evals/).

Traces are each request's input and output, kept TRACE_RETENTION_DAYS and
searchable by their words. Datasets gather examples, from traces or given
directly (or as JSON Lines); a run is one model over a dataset, scored by
metrics or an LLM judge, and two runs compare example by example. Every
route needs the admin key, and the ArcadeDB database (ARCADEDB_PATH).
"""

import json
from typing import Annotated, Any, Dict, List, Literal, Optional

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, ConfigDict, Field

from config import Config
from evals import EvalError, RunExecutor
from evals.scoring import DEFAULT_RUBRIC, METRICS, Judge, score_items
from evals.store import summarize_scores
from errors import Refused
from . import deps
from . import responses

# The largest integer the database takes (a Java long): a query integer past it would otherwise be a 500.
INT64_MAX = 2**63 - 1

router = APIRouter(prefix="/admin", tags=["evals"], dependencies=deps.ADMIN)

Status = Literal["ok", "client_error", "error"]


# ---------------------------------------------------------------- traces
@router.get("/traces", **responses.answering(responses.Traces))
async def list_traces(
    store: deps.Evals,
    since: Optional[float] = None,
    until: Optional[float] = None,
    before: Optional[float] = Query(None, description="Only traces older than this time: the cursor for the next page"),
    model: Optional[str] = Query(None, max_length=200, description="Part of the model ID"),
    task: Optional[str] = Query(None, max_length=64),
    status: Optional[Status] = None,
    tag: Optional[str] = Query(None, max_length=64),
    session: Optional[str] = Query(None, max_length=128),
    q: Optional[str] = Query(None, max_length=200, description="Words to search the inputs and outputs for"),
    limit: int = Query(100, ge=1, le=500),
):
    """Traces, newest first, without their inputs and outputs (GET /admin/traces/{id} has them)."""
    traces = await store.traces(limit, since=since, until=until, before=before, model=model, task=task, status=status,
                                tag=tag, session=session, q=q)
    return {
        "traces": traces,
        "capture": {"enabled": store.capturing, "retention_days": store.retention_days, "max_chars": store.max_chars,
                    "dropped": store.dropped},
    }


@router.get("/traces/{trace_id}", **responses.answering(responses.Trace))
async def get_trace(store: deps.Evals, trace_id: str):
    return await store.trace(trace_id)


# -------------------------------------------------------------- datasets
class CreateDataset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, max_length=120)
    description: str = Field("", max_length=2000)


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
    return {"datasets": await store.datasets()}


@router.post("/datasets", **responses.answering(responses.Dataset))
async def create_dataset(store: deps.Evals, body: CreateDataset):
    return await store.create_dataset(body.name, body.description)


def _runs_or_none(request: Request) -> Optional[RunExecutor]:
    """The run executor, or None while it is off: deps.Runs without its 503."""
    return request.app.state.runs


@router.get("/datasets/{dataset_id}", **responses.answering(responses.DatasetDetail))
async def get_dataset(
    store: deps.Evals,
    # Not deps.Runs: without the executor the dataset still shows, its runs without their activity.
    runs: Annotated[Optional[RunExecutor], Depends(_runs_or_none)],
    dataset_id: str,
):
    dataset = await store.dataset(dataset_id)
    run_list = await store.runs(dataset_id)
    activity = await runs.activity(run_list) if runs else {}
    return {**dataset, "run_list": [{**r, "activity": activity.get(r["id"])} for r in run_list]}


@router.delete("/datasets/{dataset_id}", **responses.answering(responses.Deleted))
async def delete_dataset(store: deps.Evals, runs: Annotated[Optional[RunExecutor], Depends(_runs_or_none)], dataset_id: str):
    """The dataset with its examples, runs and scores; a run of it under way, or being judged, is stopped first."""
    if runs is not None:
        for run in await store.runs(dataset_id):
            await runs.cancel(run["id"])
    await store.delete_dataset(dataset_id)
    return {"deleted": dataset_id}


@router.get("/datasets/{dataset_id}/examples", **responses.answering(responses.DatasetExamples))
async def list_examples(store: deps.Evals, dataset_id: str, limit: int = Query(100, ge=1, le=1000), skip: int = Query(0, ge=0, le=INT64_MAX)):
    dataset = await store.dataset(dataset_id)
    return {"examples": await store.examples(dataset_id, limit, skip), "total": dataset["examples"]}


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
async def add_examples(store: deps.Evals, dataset_id: str, body: AddExamples):
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
        added += await store.add_from_traces(dataset_id, body.trace_ids)
    return {"added": added}


@router.delete("/datasets/{dataset_id}/examples/{example_id}", **responses.answering(responses.Deleted))
async def delete_example(store: deps.Evals, dataset_id: str, example_id: str):
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
    model: str = Field("", max_length=256, description="The judge model; unset: JUDGE_MODEL")
    base_url: Optional[str] = Field(None, max_length=500, description="An OpenAI-compatible endpoint; unset: JUDGE_BASE_URL; empty: this service's own /v1")
    api_key: Optional[str] = Field(None, max_length=500, description="Unset: JUDGE_API_KEY for JUDGE_BASE_URL, this service's key for its own /v1, none elsewhere")
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
        "judge": {"model": Config.JUDGE_MODEL, "base_url": Config.JUDGE_BASE_URL, "has_api_key": bool(Config.JUDGE_API_KEY),
                  "rubric": DEFAULT_RUBRIC},
    }


@router.get("/runs", **responses.answering(responses.Runs))
async def list_runs(store: deps.Evals, runs: deps.Runs, dataset_id: Optional[str] = None):
    run_list = await store.runs(dataset_id)
    activity = await runs.activity(run_list)
    return {"runs": [{**r, "activity": activity.get(r["id"])} for r in run_list]}


def _allowed_run(body: CreateRun) -> CreateRun:
    """The body, once its model is allowed: so a 403 comes before the 503 of evals being off."""
    deps.check_model_allowed(body.model_id)
    return body


@router.post("/runs", **responses.answering(responses.Run))
async def create_run(body: Annotated[CreateRun, Depends(_allowed_run)], store: deps.Evals, runs: deps.Runs):
    """Queue a run; it starts when the runs before it have finished (by priority, then loaded model, then age)."""
    run = await store.create_run(body.dataset_id, body.model_id, body.task, body.params, body.name, body.webhook_url)
    await runs.submit(run["id"], body.priority)
    return run


def _by_item(scores: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """Scores grouped by the item they score."""
    out: Dict[str, List[Dict[str, Any]]] = {}
    for s in scores:
        out.setdefault(s["item_id"], []).append(s)
    return out


@router.get("/runs/{run_id}", **responses.answering(responses.RunDetail))
async def get_run(store: deps.Evals, runs: deps.Runs, run_id: str):
    """The run, its items (with each example's input and reference), their scores, and a summary per metric."""
    run = await store.run(run_id)
    items = await store.items(run_id)
    scores = await store.scores(run_id)
    by_item = _by_item(scores)
    return {
        **run,
        "activity": (await runs.activity([run])).get(run_id),
        "items": [{**i, "scores": by_item.get(i["id"], [])} for i in items],
        "summary": summarize_scores(scores),
    }


@router.delete("/runs/{run_id}", **responses.answering(responses.Deleted))
async def delete_run(store: deps.Evals, runs: deps.Runs, run_id: str):
    await runs.cancel(run_id)
    await store.delete_run(run_id)
    return {"deleted": run_id}


@router.post("/runs/{run_id}/cancel", **responses.answering(responses.Run))
async def cancel_run(store: deps.Evals, runs: deps.Runs, run_id: str):
    await runs.cancel(run_id)
    return await store.run(run_id)


@router.post("/runs/{run_id}/resume", **responses.answering(responses.Run))
async def resume_run(store: deps.Evals, runs: deps.Runs, run_id: str):
    """Queue an interrupted, failed or cancelled run again, at its priority; the examples it answered are skipped."""
    run = await store.run(run_id)
    if run["status"] not in ("interrupted", "failed", "cancelled"):
        raise EvalError(f"Only an interrupted, failed or cancelled run resumes; this one is {run['status']}")
    deps.check_model_allowed(run["model_id"])
    await store.update_run(run_id, status="queued", error=None, finished=None)
    await runs.submit(run_id, await runs.priority(run_id))
    return await store.run(run_id)


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
    base_url = Config.JUDGE_BASE_URL if body.base_url is None else body.base_url
    # A stored key goes only where it belongs: JUDGE_API_KEY to JUDGE_BASE_URL, this service's own key to its own /v1.
    if body.api_key is not None:
        api_key = body.api_key or None
    elif not base_url:
        api_key = Config.ADMIN_API_KEY or Config.API_KEY
    else:
        api_key = Config.JUDGE_API_KEY if base_url == Config.JUDGE_BASE_URL else None
    if not base_url:
        deps.check_model_allowed(model)
    run = await store.run(run_id)
    if run["status"] != "done":
        raise EvalError("Only a finished run can be judged")
    if body.against:
        other = await store.run(body.against)
        if other["dataset_id"] != run["dataset_id"]:
            raise EvalError("The runs must be of the same dataset")
        if other["status"] != "done":
            raise EvalError("The other run has not finished")
    await runs.judge(run_id, Judge(model=model, base_url=base_url or "", api_key=api_key, rubric=body.rubric), body.against,
                     body.concurrency, body.priority)
    return {"started": run_id, "against": body.against, "judge": model}


@router.get("/runs/compare/{run_a}/{run_b}", **responses.answering(responses.Comparison))
async def compare_runs(store: deps.Evals, run_a: str, run_b: str):
    """The two runs' outputs and scores side by side, example by example."""
    a, b = await store.run(run_a), await store.run(run_b)
    if a["dataset_id"] != b["dataset_id"]:
        raise EvalError("The runs must be of the same dataset")
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
    return {"a": {**a, "summary": summarize_scores(scores_a)}, "b": {**b, "summary": summarize_scores(scores_b)}, "rows": rows}
